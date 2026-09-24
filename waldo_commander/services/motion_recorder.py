"""Motion recorder for capturing robot actions as code during teaching."""

import asyncio
import contextlib
import logging
import math
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

from nicegui import app as ng_app
from nicegui import run

import waldoctl
from waldoctl.recordings import Demonstration, RecordedSample
from waldoctl.shapes import param_names

from waldo_commander.demonstrations import (
    STILL_DEG,
    Conversion,
    _sample,
    recordings_dir,
    span_to_lines,
)
from waldo_commander.services.programs import (
    active_cursor_line,
    advance_active_cursor,
    insert_below_line,
    is_any_program_recording,
    is_any_program_running,
    replace_lines,
)
from waldo_commander.state import (
    ui_state,
)
from waldo_commander.common.logging_config import TRACE_ENABLED
from waldo_commander.services.command_discovery import discover_robot_commands

logger = logging.getLogger(__name__)

_SHAPE_DEFAULT_POSE = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

_SELECT_TOOL_RE = re.compile(r"^\s*rbt\.\s*select_tool\s*\(")

# Line-anchor id for the recording insertion cursor: the browser remaps it
# across user edits so recorded snippets follow the code, not a line number.
_RECORD_ANCHOR_ID = "__recording_insert__"
# The lines a re-recording replaces, tracked until its first write.
_RETAKE_FIRST_ID = "__retake_first__"
_RETAKE_LAST_ID = "__retake_last__"

#: Standing still this long ends a captured span.
CAPTURE_STILL_S = 0.5
CAPTURE_GAP_S = 0.2
#: How long the observer waits for the controller before dropping its span.
OBSERVER_STALE_S = 2.0


def _shape_to_code(s) -> str:
    """One waldoctl Shape as a constructor call, omitting default fields."""
    parts = [f"name={s.name!r}"]
    for p in param_names(type(s)):
        parts.append(f"{p}={getattr(s, p)!r}")
    if tuple(s.pose) != _SHAPE_DEFAULT_POSE:
        parts.append(f"pose={tuple(s.pose)!r}")
    if not s.collision:
        parts.append("collision=False")
    if s.margin is not None:
        parts.append(f"margin={s.margin!r}")
    if s.physics is not None:
        parts.append(f"physics={s.physics!r}")
    if s.attachment is not None:
        parts.append(f"attachment={s.attachment!r}")
    return f"{type(s).__name__}({', '.join(parts)})"


def shapes_to_code(shapes) -> str:
    """A runnable ``rbt.set_shapes([...])`` block for the given world —
    the environment's durable form is program code, not GUI state."""
    if not shapes:
        return "rbt.set_shapes([])"
    body = "\n".join(f"    {_shape_to_code(s)}," for s in shapes)
    return f"rbt.set_shapes([\n{body}\n])"


JOG_BLEND_R_MAX = 100.0


def jog_blend_r() -> float:
    """Default blend radius for generated moves (mm); 0 = exact stop."""
    # Storage is user-editable JSON — a bad value must not break code generation.
    try:
        r = float(ng_app.storage.general.get("jog_blend_r", 0.0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(r):
        return 0.0
    return min(max(r, 0.0), JOG_BLEND_R_MAX)


def blend_r_arg() -> str:
    """``, r=<v>`` code fragment when the blend-radius setting is positive."""
    r = jog_blend_r()
    return f", r={r:g}" if r > 0 else ""


def move_snippet(
    method: str,
    values: Sequence[float],
    *,
    speed: float,
    accel: float,
    precision: int = 3,
    wait: bool = True,
    comment: str = "",
) -> str:
    """One ``rbt.move_*`` code line honoring the blend-radius setting.

    Blended moves always queue (``wait=False``) — the controller can only
    blend commands it sees together, and a per-move wait stale-flushes the
    lookahead buffer into unblended singles."""
    vals = ", ".join(f"{v:.{precision}f}" for v in values)
    r = blend_r_arg()
    wait_str = ", wait=False" if (r or not wait) else ""
    tail = f"  # {comment}" if comment else ""
    return f"rbt.{method}([{vals}], speed={speed}, accel={accel}{r}{wait_str}){tail}"


def _imported_waldoctl_names(text: str) -> set[str]:
    """Names bound by plain ``from waldoctl import X`` statements in *text*.

    Parsed with ``ast`` — a substring scan would count comments, attribute
    access (``waldoctl.Box``), and aliased imports (which don't bind the bare
    name). An unparseable program yields the empty set: prepending an import
    that turns out redundant is harmless, omitting a needed one is a NameError.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "waldoctl":
            names.update(a.name for a in node.names if a.asname is None)
    return names


@dataclass
class ActiveJog:
    """Tracks an in-progress jog action."""

    start_time: float
    move_type: str  # "joint" or "cartesian"
    axis_info: str  # e.g., "J1+", "X+", "RZ-"


BlockKind = Literal["action", "capture"]
CaptureMode = Literal["moves", "raw"]


@dataclass
class StagedBlock:
    """Lines a recording session wrote, tracked by a pair of line anchors."""

    id: str
    kind: BlockKind
    first_line: int
    last_line: int
    # A captured span: what was observed, what it was written as, and how.
    recording: Demonstration | None = None
    conversion: Conversion | None = None
    guided: bool = False
    mode: CaptureMode = "moves"
    busy: bool = False

    @property
    def first_id(self) -> str:
        return f"__staged_{self.id}_first__"

    @property
    def last_id(self) -> str:
        return f"__staged_{self.id}_last__"

    def summary(self) -> str:
        """What a captured block is, as its badge in the editor says it."""
        source = "hand-guided" if self.guided else "captured"
        if self.mode == "raw" or self.conversion is None:
            return f"{source} · raw"
        moves = sum(
            len(s.lines)
            for s in self.conversion.spans
            if s.kind in ("move_l", "move_j")
        )
        text = f"{source} · {moves} move{'s' if moves != 1 else ''}"
        if replayed := len(self.conversion.replayed):
            text += f", {replayed} replayed"
        return text


@dataclass
class RecordingSession:
    """What recording wrote into one program, staged until kept or undone.

    Recording can stop while a session goes on, so the operator can play the
    program back before deciding. A re-recording replaces the lines that
    were selected when it started, and Undo puts them back.
    """

    tab_id: str
    textarea: Any
    blocks: list[StagedBlock] = field(default_factory=list)
    retake: tuple[int, int] | None = None
    retake_text: str = ""
    restores: str | None = None
    serial: int = 0


class MotionRecorder:
    """Records robot actions as code snippets.

    Visualization is delegated to the dry-run simulation - this recorder
    only generates code. When code is inserted, the editor's debounced
    simulation will update the 3D visualization automatically.
    """

    def __init__(self):
        self._active_jog: ActiveJog | None = None
        # Actions queued while a jog is in progress (arm still moving).
        # Each entry: (action_type, params, timestamp_of_click)
        self._pending_actions: list[tuple[str, dict, float]] = []
        # Wall-clock time of the last recorded action (for inserting gaps)
        self._last_action_wall_time: float = 0.0
        # True once a session move was generated under a positive blend
        # radius: the session must then end in a wait_motion() barrier.
        self._blend_terminator_pending: bool = False
        # Insertion cursor for the active recording session: 1-indexed line new
        # snippets go below (advances past each insert); 0 = append at EOF,
        # None = no session (inserts follow the user's live cursor line).
        self._insert_line: int | None = None
        # Motion WC commanded through a path with no hook of its own (a
        # program run, the calibration auto-run) is declared owned so the
        # observer does not capture it a second time.
        self._owned = 0
        self._observer: asyncio.Task | None = None
        self._observer_generation = 0
        self._session: RecordingSession | None = None
        self._session_listeners: list[Callable[[], None]] = []
        # What the recorder last declared as line anchors, whether the
        # browser has reported exactly that back yet, and whether anyone
        # else has changed the text since.
        self._declared: dict[str, int] = {}
        self._confirmed = False
        self._redeclared = 0
        self._writing = False
        self._edited_since_declared = False
        self._watched: set[int] = set()
        # Where a span still open when recording stopped goes.
        self._flush_after: int | None = None
        # The page the recorder writes into from its own tasks.
        self.ui_client = None

    def reset_for_test(self) -> None:
        """Drop any session and in-flight state between tests. The observer
        generation keeps counting, so an observer from before cannot match."""
        generation = self._observer_generation + 1
        type(self).__init__(self)
        self._observer_generation = generation

    def _get_wrf_pose(self) -> list[float]:
        """Get current TCP pose in World Reference Frame (always WRF).

        Returns [x, y, z, rx, ry, rz] in mm/deg.
        """
        return [
            waldoctl.commander.status.pose.x,
            waldoctl.commander.status.pose.y,
            waldoctl.commander.status.pose.z,
            waldoctl.commander.status.pose.rx,
            waldoctl.commander.status.pose.ry,
            waldoctl.commander.status.pose.rz,
        ]

    def _get_current_angles(self) -> list[float]:
        """Get current joint angles as list."""
        n = ui_state.active_robot.joints.count
        return (
            list(waldoctl.commander.status.joints.angles.deg[:n])
            if len(waldoctl.commander.status.joints.angles) >= n
            else [0.0] * n
        )

    @staticmethod
    def _matches_sim_end(current_angles_deg: list[float], tol_deg: float = 0.5) -> bool:
        """Check if current joint angles match the simulation's final position."""
        tab = waldoctl.commander.programs.active
        if tab is None or tab.dry_run.final_joints_rad is None:
            return False
        final_deg = np.degrees(tab.dry_run.final_joints_rad)
        return bool(np.allclose(current_angles_deg, final_deg, atol=tol_deg))

    @staticmethod
    def _get_motion_cmd_names() -> frozenset[str]:
        """Get motion command names from the command palette discovery."""
        commands = discover_robot_commands()
        return frozenset(
            name
            for name, info in commands.items()
            if info["category"] in ("Motion", "Jog", "Streaming")
        )

    def _ensure_select_tool(self, tool_key: str, variant_key: str = "") -> int | None:
        """Ensure rbt.select_tool() is in the script before the first move command.

        If an existing select_tool line is found, update it. Otherwise insert one
        before the first motion command (home, move_j, move_l, etc.).

        Returns the 1-indexed line a new line was inserted at, or ``None``
        when a line was updated in place or appended via ``_insert_snippet``
        (which advances the session cursor itself).
        """
        textarea = ui_state.active_textarea
        if not textarea:
            return None
        val: str = str(textarea.value or "")
        lines: list[str] = val.split("\n")

        if variant_key:
            set_tool_line = (
                f'rbt.select_tool("{tool_key}", variant_key="{variant_key}")'
            )
        else:
            set_tool_line = f'rbt.select_tool("{tool_key}")'

        for i, line in enumerate(lines):
            if _SELECT_TOOL_RE.match(line):
                lines[i] = set_tool_line
                self._write(textarea, "\n".join(lines))
                logger.info("Updated existing select_tool to %s", tool_key)
                return None

        # No existing select_tool — insert before first motion command
        motion_names = self._get_motion_cmd_names()
        motion_re = re.compile(
            r"^\s*rbt\.(" + "|".join(re.escape(n) for n in motion_names) + r")\s*\("
        )
        for i, line in enumerate(lines):
            if motion_re.match(line):
                lines.insert(i, set_tool_line)
                self._write(textarea, "\n".join(lines))
                if self._session is not None:
                    self._shift(i, 1)
                    self._stage(i + 1, 1, "action")
                logger.info(
                    "Inserted select_tool before first motion at line %d", i + 1
                )
                return i + 1

        # No motion commands found — just append
        self._insert_snippet(set_tool_line)
        return None

    # ------------------------------------------------------ staged session

    @property
    def session(self) -> RecordingSession | None:
        return self._session

    def add_session_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._session_listeners:
            self._session_listeners.append(callback)

    def remove_session_listener(self, callback: Callable[[], None]) -> None:
        if callback in self._session_listeners:
            self._session_listeners.remove(callback)

    def _notify_session(self) -> None:
        for callback in list(self._session_listeners):
            try:
                callback()
            except Exception:
                logger.exception("Recording session listener failed")

    def _mirror(self, textarea) -> dict[str, int]:
        """Anchor lines as the browser last reported them, once it has
        reported back what the recorder last declared. Until then the tracked
        lines are newer: they include the recorder's own last write."""
        if textarea is None or not self._confirmed:
            return {}
        return dict(textarea.line_anchors)

    def _write(self, textarea, text: str) -> None:
        """Set the editor text. Every anchor the browser reported is from
        before this change, so none is current until it reports again."""
        self._writing = True
        try:
            textarea.value = text
        finally:
            self._writing = False
        self._confirmed = False

    def _watch(self, textarea) -> None:
        if textarea is None or id(textarea) in self._watched:
            return
        self._watched.add(id(textarea))
        textarea.on_anchor_change(self._on_anchor_report)
        textarea.on_value_change(self._on_text_change)

    def _on_text_change(self, _event) -> None:
        if not self._writing:
            self._edited_since_declared = True

    def _on_anchor_report(self, event) -> None:
        """The browser reports anchor lines after every change to the text.

        A report of what the recorder declared confirms it. Until someone else
        changes the text, a report that differs is of a misplaced declaration:
        sent before the write it came with reached the browser, or placed on
        the text from before that write and then moved by it. The recorder's
        own lines stand, and the declaration goes out again.
        """
        reported = {k: v for k, v in event.anchors.items() if k.startswith("__")}
        if all(reported.get(k) == v for k, v in self._declared.items()):
            self._confirmed = True
            return
        if self._confirmed and self._edited_since_declared:
            return
        self._confirmed = False
        if self._redeclared < 2:
            self._redeclared += 1
            self._push_anchors(redeclare=True)

    def _sync_from_mirror(self) -> None:
        """Take where the operator's edits have moved the tracked lines."""
        session = self._session
        textarea = session.textarea if session is not None else ui_state.active_textarea
        mirror = self._mirror(textarea)
        if not mirror:
            return
        if self._insert_line:
            self._insert_line = mirror.get(_RECORD_ANCHOR_ID, self._insert_line)
        if session is None:
            return
        for block in session.blocks:
            block.first_line = mirror.get(block.first_id, block.first_line)
            block.last_line = mirror.get(block.last_id, block.last_line)
        if session.retake is not None:
            first, last = session.retake
            session.retake = (
                mirror.get(_RETAKE_FIRST_ID, first),
                mirror.get(_RETAKE_LAST_ID, last),
            )

    def _shift(self, after_line: int, delta: int, *, exclude=None) -> None:
        """Move every tracked line below *after_line* by *delta*."""
        session = self._session
        if session is None or not delta:
            return
        for block in session.blocks:
            if block is exclude:
                continue
            if block.first_line > after_line:
                block.first_line += delta
            if block.last_line > after_line:
                block.last_line += delta
        if session.retake is not None:
            first, last = session.retake
            session.retake = (
                first + delta if first > after_line else first,
                last + delta if last > after_line else last,
            )

    def _stage(
        self, first: int, count: int, kind: BlockKind, *, merge: bool = True
    ) -> StagedBlock:
        session = self._session
        assert session is not None
        last = first + count - 1
        if merge and kind == "action" and session.blocks:
            previous = session.blocks[-1]
            if previous.kind == "action" and previous.last_line + 1 == first:
                previous.last_line = last
                return previous
        session.serial += 1
        block = StagedBlock(f"s{session.serial}", kind, first, last)
        session.blocks.append(block)
        return block

    def line_anchors(self, tab_id: str) -> dict[str, int]:
        """The anchors the recorder tracks in *tab_id*'s editor: the
        recording cursor, each staged block's first and last line, and a
        selection waiting to be re-recorded."""
        session = self._session
        if session is not None and session.tab_id != tab_id:
            return {}
        self._sync_from_mirror()
        anchors: dict[str, int] = {}
        if self._insert_line and is_any_program_recording():
            anchors[_RECORD_ANCHOR_ID] = self._insert_line
        if session is not None:
            for block in session.blocks:
                anchors[block.first_id] = block.first_line
                anchors[block.last_id] = block.last_line
            if session.retake is not None:
                anchors[_RETAKE_FIRST_ID], anchors[_RETAKE_LAST_ID] = session.retake
        self._declared = dict(anchors)
        self._confirmed = False
        self._edited_since_declared = False
        return anchors

    def _push_anchors(
        self, session: RecordingSession | None = None, *, redeclare: bool = False
    ) -> None:
        session = session or self._session
        from waldo_commander.components.editor_decorations import decorations

        if not redeclare:
            self._redeclared = 0
        if session is not None:
            self._watch(session.textarea)
            decorations.push_line_anchors(session.tab_id, textarea=session.textarea)
            return
        active = waldoctl.commander.programs.active
        if active is not None:
            self._watch(ui_state.active_textarea)
            decorations.push_line_anchors(active.id, textarea=ui_state.active_textarea)

    def keep(self) -> None:
        """Keep what the session wrote; stops recording if it is on."""
        if is_any_program_recording():
            self._stop_recording()
        session, self._session = self._session, None
        if session is not None:
            self._push_anchors(session)
        self._notify_session()

    def undo(self) -> None:
        """Take out every line the session wrote and put back the lines it
        re-recorded; stops recording if it is on."""
        if is_any_program_recording():
            self._stop_recording()
        session = self._session
        if session is None:
            return
        self._sync_from_mirror()
        textarea = session.textarea
        lines = str(textarea.value or "").split("\n")
        # Bottom up, so the lines above each block keep their numbers; a
        # block the operator edited into overlapping another is taken once.
        floor = len(lines) + 1
        for block in sorted(session.blocks, key=lambda b: b.first_line, reverse=True):
            first = max(1, block.first_line)
            last = min(block.last_line, floor - 1, len(lines))
            if first > last:
                continue
            lines[first - 1 : last] = (
                session.retake_text.split("\n") if block.id == session.restores else []
            )
            floor = first
        self._session = None
        self._write(textarea, "\n".join(lines))
        self._push_anchors(session)
        self._notify_session()

    def forget_session(self) -> None:
        """Drop the session without writing: its program went away."""
        if self._session is not None:
            self._session = None
            self._notify_session()

    async def set_capture_mode(self, block_id: str, mode: CaptureMode) -> None:
        """Rewrite a captured block as planned moves or as the recording."""
        session = self._session
        if session is None:
            return
        block = next((b for b in session.blocks if b.id == block_id), None)
        if (
            block is None
            or block.busy
            or block.mode == mode
            or block.recording is None
            or block.conversion is None
        ):
            return
        block.busy = True
        self._notify_session()
        program = waldoctl.commander.programs.get(session.tab_id)
        name = Path(program.filename).stem if program is not None else "program"
        try:
            conversion = await run.io_bound(
                span_to_lines,
                block.recording,
                ui_state.active_robot,
                program=name or "program",
                directory=recordings_dir(),
                as_recorded=mode == "raw",
                recording_path=block.conversion.recording_path,
            )
        except (ValueError, OSError) as error:
            logger.warning("Captured motion could not be rewritten: %s", error)
            conversion = None
        finally:
            block.busy = False
        if (
            conversion is None
            or self._session is not session
            or block not in session.blocks
        ):
            self._notify_session()
            return
        if self.ui_client is not None:
            with self.ui_client:
                self._replace_block(block, conversion.source)
        else:
            self._replace_block(block, conversion.source)
        block.conversion, block.mode = conversion, mode
        self._notify_session()

    def _replace_block(self, block: StagedBlock, snippet: str) -> None:
        """Rewrite a staged block's lines as *snippet*."""
        session = self._session
        assert session is not None
        textarea = session.textarea
        self._sync_from_mirror()
        first, last = block.first_line, block.last_line
        new_value, first, count = replace_lines(
            str(textarea.value or ""), first, last, snippet
        )
        self._write(textarea, new_value)
        delta = count - (last - first + 1)
        self._shift(last, delta, exclude=block)
        if self._insert_line and self._insert_line >= last:
            self._insert_line += delta
        if count:
            block.first_line, block.last_line = first, first + count - 1
        else:
            session.blocks.remove(block)
        self._push_anchors(session)
        if count:
            from waldo_commander.components.editor_decorations import decorations

            decorations.flash_editor_lines(list(range(first, first + count)))

    def _clamp_below_select_tool(self, text: str) -> None:
        """Recorded motions must play back after the tool selection, so the
        session cursor never stays above an existing select_tool line."""
        if not self._insert_line:
            return
        for i, line in enumerate(text.split("\n")):
            if _SELECT_TOOL_RE.match(line):
                self._insert_line = max(self._insert_line, i + 1)
                return

    def toggle_recording(self, *, replace: tuple[int, int] | None = None) -> None:
        """Toggle recording on or off. Started with *replace*, the lines in
        that range are what the session re-records."""
        if is_any_program_recording():
            self._stop_recording()
        else:
            self._start_recording(replace)

    def _start_recording(self, replace: tuple[int, int] | None = None) -> None:
        """Start a new recording session on the active program."""
        active = waldoctl.commander.programs.active
        if active is None:
            logger.warning("Cannot start recording: no active program")
            return
        if self._session is not None:
            # A new take keeps the last one.
            self.keep()
        textarea = ui_state.active_textarea
        session = RecordingSession(tab_id=active.id, textarea=textarea)
        if replace is not None and textarea is not None:
            lines = str(textarea.value or "").split("\n")
            first, last = replace
            if 1 <= first <= last <= len(lines):
                session.retake = (first, last)
                session.retake_text = "\n".join(lines[first - 1 : last])
        self._session = session
        active.recording.is_recording = True
        self._active_jog = None
        self._last_action_wall_time = 0.0
        self._blend_terminator_pending = False
        self._insert_line = (
            session.retake[0] - 1 if session.retake else active_cursor_line()
        )

        if (
            len(waldoctl.commander.status.joints.angles)
            >= ui_state.active_robot.joints.count
        ):
            logger.info(
                "Recording started - initial joints: %s deg",
                [f"{a:.1f}" for a in waldoctl.commander.status.joints.angles.deg],
            )
        logger.info(
            "Recording started - initial pose: [%.1f, %.1f, %.1f, %.1f, %.1f, %.1f] (mm/deg)",
            waldoctl.commander.status.pose.x,
            waldoctl.commander.status.pose.y,
            waldoctl.commander.status.pose.z,
            waldoctl.commander.status.pose.rx,
            waldoctl.commander.status.pose.ry,
            waldoctl.commander.status.pose.rz,
        )

        # Ensure select_tool is before the first move command in the script
        tool_key = waldoctl.commander.status.tool.key
        if tool_key and tool_key != "NONE":
            inserted = self._ensure_select_tool(
                tool_key, variant_key=waldoctl.commander.status.tool.variant_key
            )
            if (
                inserted is not None
                and self._insert_line
                and inserted <= self._insert_line
            ):
                self._insert_line += 1
            textarea = ui_state.active_textarea
            if textarea and session.retake is None:
                self._clamp_below_select_tool(str(textarea.value or ""))

        # Insert anchor move_j to establish recording start position — but only
        # if the robot has moved away from where the script's simulation ends.
        # This avoids a redundant zero-distance segment (e.g. script ends with
        # home() and robot is still at home when recording starts).
        if (
            len(waldoctl.commander.status.joints.angles)
            >= ui_state.active_robot.joints.count
        ):
            angles = self._get_current_angles()
            # A re-recording starts where the arm is, not where the program
            # would be after the lines it replaces.
            if session.retake is not None or not self._matches_sim_end(angles):
                spd = waldoctl.commander.settings.jog.speed / 100.0
                acc = waldoctl.commander.settings.jog.accel / 100.0
                anchor_snippet = move_snippet(
                    "move_j",
                    angles,
                    speed=spd,
                    accel=acc,
                    precision=2,
                    comment="Recording start position",
                )
                self._insert_snippet(anchor_snippet)
                if jog_blend_r() > 0:
                    self._blend_terminator_pending = True
                logger.info(
                    "Inserted recording start anchor at joints: %s",
                    [f"{a:.1f}" for a in angles],
                )
            else:
                logger.info("Skipped anchor — robot matches script end position")

        # One declaration at the settled cursor: _start_recording is
        # synchronous, so no user edit can interleave before this point.
        self._push_anchors()
        self._start_observer()
        self._notify_session()

    def _stop_recording(self) -> None:
        """Stop recording session."""
        # If there's an active jog, end it first
        if self._active_jog:
            self.on_jog_end()
        self._flush_after = self._insert_line

        if self._blend_terminator_pending:
            # Blended moves queue (wait=False): without a final barrier the
            # program exits while the arm is still executing them.
            self._insert_snippet("rbt.wait_motion()")
            self._blend_terminator_pending = False

        # Clear is_recording on every program — the invariant says only one
        # could have been True, but the sweep makes the stop idempotent.
        for p in waldoctl.commander.programs.items:
            p.recording.is_recording = False
        self._insert_line = None
        # The cursor anchor goes; the staged lines stay until kept or undone.
        self._push_anchors()
        logger.info("Recording stopped")
        self._notify_session()

    def record_completed_skill(self, source: str, *, started_at: float) -> None:
        """Keep a successful skill once, without adding its duration as idle time."""
        if not is_any_program_recording():
            return
        delay = started_at - self._last_action_wall_time
        if self._last_action_wall_time > 0 and delay > 0.05:
            self._record_action_impl("delay", seconds=delay)
        self._record_action_impl("skill", source=source)

    def record_action(self, action_type: str, **params) -> None:
        """Record any robot action when recording is active.

        Args:
            action_type: One of "move_j", "move_l", "home",
                        "gripper", "io", "delay", "set_shapes"
            **params: Action-specific parameters
        """
        if not is_any_program_recording():
            return

        # If a jog is in progress (arm still moving to target), queue
        # non-motion actions so they appear AFTER the pending move_j/move_l.
        if self._active_jog and action_type not in ("move_j", "move_l"):
            self._pending_actions.append((action_type, params, time.time()))
            return

        # Insert delay if time has passed since last recorded action
        # (covers remaining move time after non-blocking moves + idle time)
        if self._last_action_wall_time > 0 and action_type not in ("move_j", "move_l"):
            delay = time.time() - self._last_action_wall_time
            if delay > 0.05:
                self._record_action_impl("delay", seconds=delay)

        self._record_action_impl(action_type, **params)

    def _record_action_impl(self, action_type: str, **params) -> None:
        """Core recording logic (no is_recording guard)."""
        snippet = self._generate_code(action_type, params)
        self._insert_snippet(snippet)
        self._last_action_wall_time = time.time()
        if action_type in ("move_j", "move_l") and jog_blend_r() > 0:
            self._blend_terminator_pending = True

        if TRACE_ENABLED:
            logger.log(
                5, "RECORDER: Recorded action %s with params %s", action_type, params
            )  # TRACE level
        logger.debug("Recorded action: %s", action_type)

    def _generate_code(self, action_type: str, params: dict) -> str:
        """Generate Python code snippet for an action.

        Args:
            action_type: Type of action
            params: Action parameters

        Returns:
            Python code snippet string
        """
        if action_type == "skill":
            return params["source"]

        if action_type == "move_j":
            spd = waldoctl.commander.settings.jog.speed / 100.0
            acc = waldoctl.commander.settings.jog.accel / 100.0
            return move_snippet(
                "move_j",
                params["angles"],
                speed=spd,
                accel=acc,
                precision=2,
                wait=params.get("wait", True),
            )

        elif action_type == "move_l":
            spd = waldoctl.commander.settings.jog.speed / 100.0
            acc = waldoctl.commander.settings.jog.accel / 100.0
            return move_snippet(
                "move_l",
                params["pose"],
                speed=spd,
                accel=acc,
                wait=params.get("wait", True),
            )

        elif action_type == "home":
            return (
                "rbt.home(calibrate=True)" if params.get("calibrate") else "rbt.home()"
            )

        elif action_type == "gripper":
            if params.get("calibrate"):
                return "rbt.tool.calibrate()"
            pos = params["position"]
            kwargs = []
            spd = params.get("speed")
            cur = params.get("current")
            if spd is not None:
                kwargs.append(f"speed={spd}")
            if cur is not None:
                kwargs.append(f"current={cur}")
            kwargs_str = ", ".join(kwargs)
            if kwargs_str:
                return f"rbt.tool.set_position({pos}, {kwargs_str})"
            return f"rbt.tool.set_position({pos})"

        elif action_type == "io":
            port = params["port"]
            state = params["state"]
            return f"rbt.write_io({port}, {state})"

        elif action_type == "delay":
            seconds = params["seconds"]
            return f"time.sleep({seconds:.2f})"

        elif action_type == "set_shapes":
            shapes = params["shapes"]
            snippet = shapes_to_code(shapes)
            # Prepend the constructor imports the program doesn't have yet.
            text = (
                (ui_state.active_textarea.value or "")
                if ui_state.active_textarea
                else ""
            )
            imported = _imported_waldoctl_names(text)
            names = {type(s).__name__ for s in shapes}
            if any(s.physics is not None for s in shapes):
                names.add("Physical")  # _shape_to_code emits it by repr
            if any(s.attachment is not None for s in shapes):
                names.add("Attachment")
            missing = sorted(names - imported)
            if missing:
                snippet = f"from waldoctl import {', '.join(missing)}\n{snippet}"
            return snippet

        else:
            return f"# Unknown action: {action_type}"

    def on_jog_start(self, move_type: str, axis_info: str) -> None:
        """Called when a jog action starts.

        Args:
            move_type: "joint" or "cartesian"
            axis_info: Axis identifier like "J1+", "J3-", "X+", "RZ-"
        """
        if not is_any_program_recording():
            return

        # If there's already an active jog, end it first
        if self._active_jog:
            self.on_jog_end()

        self._active_jog = ActiveJog(
            start_time=time.time(), move_type=move_type, axis_info=axis_info
        )
        logger.debug("Jog started: %s %s", move_type, axis_info)

    def on_jog_end(self) -> None:
        """Called when a jog action ends. Records the move as code."""
        if not is_any_program_recording() or not self._active_jog:
            return

        end_time = time.time()
        duration = end_time - self._active_jog.start_time

        # Only record if there was actual movement (> 0.1s)
        if duration > 0.1:
            # Use wait=False when actions were queued mid-motion so the
            # tool fires while the arm is still moving on playback.
            wait = not bool(self._pending_actions)
            if self._active_jog.move_type == "joint":
                self.record_action(
                    "move_j",
                    angles=self._get_current_angles(),
                    duration=duration,
                    wait=wait,
                )
            else:
                self.record_action(
                    "move_l", pose=self._get_wrf_pose(), duration=duration, wait=wait
                )

            logger.debug(
                "Jog ended: %s - recorded move (%.2fs)",
                self._active_jog.axis_info,
                duration,
            )
        else:
            logger.debug(
                "Jog ended: %s - too short to record (%.2fs)",
                self._active_jog.axis_info,
                duration,
            )

        self._flush_pending_actions(self._active_jog.start_time)
        self._active_jog = None

    def _flush_pending_actions(self, jog_start_time: float) -> None:
        """Flush actions queued during a jog, inserting time.sleep delays."""
        if not self._pending_actions:
            return

        last_t = jog_start_time
        for action_type, params, queued_at in self._pending_actions:
            delay = queued_at - last_t
            if delay > 0.05:
                self._record_action_impl("delay", seconds=delay)
            self._record_action_impl(action_type, **params)
            last_t = queued_at

        # Track wall time of last flushed action for gap detection
        self._last_action_wall_time = self._pending_actions[-1][2]
        self._pending_actions.clear()

    def current_pose_snippet(self, move_type: str = "cartesian") -> str:
        """Code line moving to the robot's current position.

        Args:
            move_type: "cartesian" or "joints"
        """
        if move_type == "joints":
            return self._generate_code(
                "move_j", {"angles": self._get_current_angles(), "duration": 1.0}
            )
        return self._generate_code(
            "move_l", {"pose": self._get_wrf_pose(), "duration": 1.0}
        )

    def capture_current_pose(self, move_type: str = "cartesian") -> None:
        """Capture current robot pose and insert as move command.

        Args:
            move_type: "cartesian" or "joints"
        """
        self._insert_snippet(self.current_pose_snippet(move_type))
        self._last_action_wall_time = time.time()

    def stamp_action_clock(self) -> None:
        """Count now as the end of the last recorded action, so the next one
        does not wait out time the program already spends on its own."""
        self._last_action_wall_time = time.time()

    def insert_skill_call(self, source: str) -> None:
        """Insert an explicitly requested Python call at the editor cursor.

        The insertion is an action in the recording like a captured pose, so it
        stamps the action clock: otherwise the next recorded jog is delayed by
        the time the operator spent composing the call, and the program waits
        that long every time it runs.
        """
        self._insert_snippet(source)
        self._last_action_wall_time = time.time()

    def _insert_snippet(
        self,
        snippet: str,
        *,
        after: int | None = None,
        kind: BlockKind = "action",
    ) -> tuple[int, int]:
        """Insert code below the recording session's insertion cursor (or the
        user's cursor line outside a session, or *after*) and flash the
        inserted lines. What recording writes into the session's editor is
        staged (a span that closes after recording stopped too), and the first
        write of a re-recording replaces the selected lines instead. Returns
        the first line written and how many."""
        textarea = ui_state.active_textarea
        if not textarea:
            logger.error("Editor textarea not ready - open Program tab first")
            return 0, 0

        # A session can end without _stop_recording (e.g. the recording
        # program was closed); drop the stale session cursor then.
        if self._insert_line is not None and not is_any_program_recording():
            self._insert_line = None

        session = self._session
        staging = (
            session is not None
            and session.textarea is textarea
            and (is_any_program_recording() or kind == "capture")
        )
        if staging or self._insert_line:
            self._sync_from_mirror()
        val = str(textarea.value or "")
        if staging and session is not None and session.retake is not None:
            first, last = session.retake
            new_value, first_line, count = replace_lines(val, first, last, snippet)
            self._write(textarea, new_value)
            session.retake = None
            self._shift(last, count - (last - first + 1))
            session.restores = self._stage(first_line, count, kind, merge=False).id
        else:
            if after is None:
                after = (
                    self._insert_line
                    if self._insert_line is not None
                    else active_cursor_line()
                )
            new_value, first_line, count = insert_below_line(val, snippet, after)
            # Assigning value triggers the editor's on_change -> debounced simulation.
            self._write(textarea, new_value)
            if staging:
                self._shift(first_line - 1, count)
                self._stage(first_line, count, kind)

        last_line = first_line + count - 1
        if self._insert_line is None:
            advance_active_cursor(last_line)
        elif self._insert_line or staging:
            # The session cursor advances past each insert so recorded steps
            # stay chronological while the user's cursor stays put.
            self._insert_line = last_line
        if staging or self._insert_line:
            self._push_anchors()

        # Local import: motion_recorder is in services/ and decorations
        # is in components/, so a top-level import would invert the
        # layered dependency direction. Keep it lazy.
        from waldo_commander.components.editor_decorations import decorations

        decorations.flash_editor_lines(list(range(first_line, last_line + 1)))
        if staging:
            self._notify_session()
        return first_line, count

    # ------------------------------------------------------ captured motion

    @contextlib.contextmanager
    def owned(self) -> Iterator[None]:
        """A window of motion WC commanded, which the observer leaves alone."""
        self._owned += 1
        try:
            yield
        finally:
            self._owned -= 1

    @property
    def owns_motion(self) -> bool:
        return (
            self._owned > 0 or self._active_jog is not None or is_any_program_running()
        )

    def _start_observer(self) -> None:
        self._observer_generation += 1
        try:
            asyncio.get_running_loop()
            client = waldoctl.commander.client
        except RuntimeError:
            return
        if client is None or client.robot is None:
            return
        self._observer = asyncio.create_task(
            self._observe(client, self._observer_generation), name="recording-observer"
        )

    async def _observe(self, client, generation: int) -> None:
        """Motion nobody in WC commanded, while recording, becomes program lines.

        The controller's status stream is sampled the way a demonstration is
        recorded. Joints moving while no owned window is open, or the arm in
        freedrive, open a span; standing still for ``CAPTURE_STILL_S`` or an
        owned window opening closes it, and the span is converted and written
        below the recording cursor.
        """
        stream = client.stream_status()
        span: list[RecordedSample] = []
        guided = False
        last: RecordedSample | None = None
        still_since: int | None = None
        # An owned window's motion can outlast the window by a sample or two
        # as the arm settles; nothing opens a span until this has passed.
        grace_until = 0
        session = 0
        simulator = False
        try:
            rate = await client.status_rate()
            tcp = await client.tcp_transform()
            if rate is None or tcp is None:
                logger.warning("Recording observer: the controller gave no rate or TCP")
                return
            meta = (client.robot.backend_package, rate.hz, tuple(tcp))
            while True:
                try:
                    async with asyncio.timeout(OBSERVER_STALE_S):
                        status = await anext(stream)
                except TimeoutError:
                    span, last, still_since = [], None, None
                    if not self._observer_live(generation):
                        return
                    continue
                except (StopAsyncIteration, OSError, RuntimeError):
                    return
                if not self._observer_live(generation):
                    if span:
                        await self._close_span(
                            span, guided, session, simulator, meta, self._flush_after
                        )
                    return
                if (
                    not status.session_id
                    or not status.mono_time_ns
                    or not status.enabled
                    or not status.homed
                ):
                    span, last, still_since = [], None, None
                    continue
                if status.session_id != session or status.simulator_active != simulator:
                    session, simulator = status.session_id, status.simulator_active
                    span, last, still_since = [], None, None
                try:
                    sample = _sample(status)
                except ValueError:
                    continue
                if last is not None and (
                    sample.seq <= last.seq or sample.observed_ns <= last.observed_ns
                ):
                    continue
                moving = last is not None and _moved(last, sample)
                freedrive = bool(status.freedrive)
                if self.owns_motion:
                    if span:
                        await self._close_span(span, guided, session, simulator, meta)
                        span = []
                    still_since, guided = None, False
                    grace_until = sample.received_ns + int(CAPTURE_STILL_S * 1e9)
                elif span:
                    span.append(sample)
                    if moving or freedrive:
                        still_since = None
                        guided = guided or freedrive
                    elif still_since is None:
                        still_since = sample.received_ns
                    elif sample.received_ns - still_since >= CAPTURE_STILL_S * 1e9:
                        await self._close_span(span, guided, session, simulator, meta)
                        span, still_since, guided = [], None, False
                elif (
                    last is not None
                    and sample.received_ns >= grace_until
                    and (moving or freedrive)
                ):
                    span = [last, sample]
                    guided = freedrive
                    still_since = None
                last = sample
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    def _observer_live(self, generation: int) -> bool:
        return generation == self._observer_generation and is_any_program_recording()

    async def _close_span(
        self,
        samples: list[RecordedSample],
        guided: bool,
        session: int,
        simulator: bool,
        meta: tuple[str, float, tuple[float, ...]],
        after: int | None = None,
    ) -> None:
        # The arm coming to rest is not part of the motion: keep one still
        # sample after the last move so the span ends where it stopped.
        last_moving = 0
        for index in range(1, len(samples)):
            if _moved(samples[index - 1], samples[index]):
                last_moving = index
        samples = samples[: last_moving + 2]
        if len(samples) < 2:
            return
        backend, rate_hz, tcp = meta
        try:
            recording = Demonstration(
                backend=backend,
                session_id=session,
                simulator=simulator,
                tcp_transform=tcp,
                requested_rate_hz=rate_hz,
                gap_threshold_s=CAPTURE_GAP_S,
                ended="stopped",
                samples=tuple(samples),
            )
        except ValueError as error:
            logger.warning("Captured motion was not a recording: %s", error)
            return
        program = await self._recording_program()
        if program is None:
            return
        name = Path(program.filename).stem or "program"
        try:
            conversion = await run.io_bound(
                span_to_lines,
                recording,
                ui_state.active_robot,
                program=name,
                directory=recordings_dir(),
            )
        except (ValueError, OSError) as error:
            logger.warning("Captured motion could not be written as code: %s", error)
            return
        if conversion is None:
            return
        if self.ui_client is not None:
            with self.ui_client:
                self._show_capture(recording, conversion, guided, after)
        else:
            self._show_capture(recording, conversion, guided, after)

    async def _recording_program(self):
        """The program being recorded, once it is the active one again: Run
        selection switches tabs while it runs, and the lines belong to the
        recording, not to the selection's program."""
        programs = waldoctl.commander.programs
        recording = next((p for p in programs.items if p.recording.is_recording), None)
        if recording is None:
            return programs.active
        while programs.active is not recording:
            if not recording.recording.is_recording:
                return None
            await asyncio.sleep(0.1)
        return recording

    def _show_capture(
        self,
        recording: Demonstration,
        conversion: Conversion,
        guided: bool,
        after: int | None,
    ) -> None:
        if self._session is None:
            # A span that closes after the last take was kept is a new take.
            program = waldoctl.commander.programs.active
            if program is None:
                return
            self._session = RecordingSession(
                tab_id=program.id, textarea=ui_state.active_textarea
            )
        first, count = self._insert_snippet(
            conversion.source, after=after, kind="capture"
        )
        if not count:
            return
        self._last_action_wall_time = time.time()
        block = next(
            b
            for b in self._session.blocks
            if b.first_line == first and b.kind == "capture"
        )
        block.recording, block.conversion, block.guided = recording, conversion, guided
        self._notify_session()


def _moved(before: RecordedSample, after: RecordedSample) -> bool:
    return (
        max(abs(a - b) for a, b in zip(after.joints_deg, before.joints_deg)) > STILL_DEG
    )


# Singleton
motion_recorder = MotionRecorder()
