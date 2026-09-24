"""Motion recorder for capturing robot actions as code during teaching."""

import asyncio
import contextlib
import logging
import math
import re
import time
from collections.abc import Iterator, Sequence
from typing import Any
from dataclasses import dataclass
from pathlib import Path

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
# The lines the last captured span was written as, so a trim or an undo can
# find them after the operator has edited around them.
_CAPTURE_FIRST_ID = "__capture_first__"
_CAPTURE_LAST_ID = "__capture_last__"

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


@dataclass
class Capture:
    """Motion nobody in WC commanded, and the lines it was written as."""

    recording: Demonstration
    conversion: Conversion
    first_line: int
    last_line: int
    guided: bool
    # The editor the lines went into: a trim or an undo must find them there
    # even after the operator has switched tabs.
    textarea: Any = None


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
        self._capture: Capture | None = None
        # Where a span still open when recording stopped goes.
        self._flush_after: int | None = None
        # The page the recorder writes into from its own tasks.
        self.ui_client = None

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
                textarea.value = "\n".join(lines)
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
                textarea.value = "\n".join(lines)
                logger.info(
                    "Inserted select_tool before first motion at line %d", i + 1
                )
                return i + 1

        # No motion commands found — just append
        self._insert_snippet(set_tool_line)
        return None

    def _declare_insert_anchor(self, textarea) -> None:
        if self._insert_line:
            textarea.line_anchors = {
                **textarea.line_anchors,
                _RECORD_ANCHOR_ID: self._insert_line,
            }

    def _retract_insert_anchor(self, textarea) -> None:
        declared = textarea.line_anchors
        if declared.pop(_RECORD_ANCHOR_ID, None) is not None:
            textarea.line_anchors = declared

    def insertion_anchor(self) -> dict[str, int]:
        """Declared position for the recording insertion cursor: the live
        mirror when the browser has echoed one, else the tracked line.
        Empty outside a session or in append mode."""
        if not self._insert_line or not is_any_program_recording():
            return {}
        textarea = ui_state.active_textarea
        line = self._insert_line
        if textarea is not None:
            line = textarea.line_anchors.get(_RECORD_ANCHOR_ID, line)
        return {_RECORD_ANCHOR_ID: line}

    def _clamp_below_select_tool(self, text: str) -> None:
        """Recorded motions must play back after the tool selection, so the
        session cursor never stays above an existing select_tool line."""
        if not self._insert_line:
            return
        for i, line in enumerate(text.split("\n")):
            if _SELECT_TOOL_RE.match(line):
                self._insert_line = max(self._insert_line, i + 1)
                return

    def toggle_recording(self) -> None:
        """Toggle recording state on/off."""
        if is_any_program_recording():
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        """Start a new recording session on the active program."""
        active = waldoctl.commander.programs.active
        if active is None:
            logger.warning("Cannot start recording: no active program")
            return
        active.recording.is_recording = True
        self._active_jog = None
        self._last_action_wall_time = 0.0
        self._blend_terminator_pending = False
        self._insert_line = active_cursor_line()

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
            if textarea:
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
            if not self._matches_sim_end(angles):
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
        textarea = ui_state.active_textarea
        if textarea is not None:
            self._declare_insert_anchor(textarea)
        self._start_observer()

    def _stop_recording(self) -> None:
        """Stop recording session."""
        # If there's an active jog, end it first
        if self._active_jog:
            self.on_jog_end()
        self._keep_capture()
        self._flush_after = self._insert_line

        if self._blend_terminator_pending:
            # Blended moves queue (wait=False): without a final barrier the
            # program exits while the arm is still executing them.
            self._insert_snippet("rbt.wait_motion()")
            self._blend_terminator_pending = False

        textarea = ui_state.active_textarea
        if textarea is not None:
            self._retract_insert_anchor(textarea)

        # Clear is_recording on every program — the invariant says only one
        # could have been True, but the sweep makes the stop idempotent.
        for p in waldoctl.commander.programs.items:
            p.recording.is_recording = False
        self._insert_line = None
        logger.info("Recording stopped")

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
        self, snippet: str, *, after: int | None = None
    ) -> tuple[int, int]:
        """Insert code below the recording session's insertion cursor (or the
        user's cursor line outside a session, or *after*) and flash the
        inserted lines. Returns the first line written and how many."""
        textarea = ui_state.active_textarea
        if not textarea:
            logger.error("Editor textarea not ready - open Program tab first")
            return 0, 0
        # A recorded action after a captured span settles the span.
        self._keep_capture()

        # A session can end without _stop_recording (e.g. the recording
        # program was closed); drop the stale session cursor then.
        if self._insert_line is not None and not is_any_program_recording():
            self._insert_line = None

        val = str(textarea.value or "")
        if after is None:
            after = (
                self._insert_line
                if self._insert_line is not None
                else active_cursor_line()
            )
            if self._insert_line:
                # The browser remaps the anchor across user edits; the tracked
                # int is the fallback until an echo arrives (or when a deletion
                # swallowed the anchor line).
                after = textarea.line_anchors.get(_RECORD_ANCHOR_ID, self._insert_line)
        new_value, first_line, count = insert_below_line(val, snippet, after)
        # Assigning value triggers the editor's on_change -> debounced simulation.
        textarea.value = new_value

        last_line = first_line + count - 1
        if self._insert_line is None:
            advance_active_cursor(last_line)
        elif self._insert_line:
            # The session cursor advances past each insert so recorded steps
            # stay chronological while the user's cursor stays put.
            self._insert_line = last_line
            self._declare_insert_anchor(textarea)

        # Local import: motion_recorder is in services/ and decorations
        # is in components/, so a top-level import would invert the
        # layered dependency direction. Keep it lazy.
        from waldo_commander.components.editor_decorations import decorations

        decorations.flash_editor_lines(list(range(first_line, last_line + 1)))
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

    @property
    def capture(self) -> Capture | None:
        return self._capture

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
        """The program being recorded, once it is the active one again: a
        skill's Run once switches tabs while it runs, and the lines belong
        to the recording, not to the skill's program."""
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
        first, count = self._insert_snippet(conversion.source, after=after)
        if not count:
            return
        self._last_action_wall_time = time.time()
        self._capture = Capture(
            recording,
            conversion,
            first,
            first + count - 1,
            guided,
            textarea=ui_state.active_textarea,
        )
        self._declare_capture_anchors()
        from waldo_commander.components.capture_review import capture_review

        capture_review.show(
            recording,
            conversion,
            guided=guided,
            on_trim=self._trim_capture,
            on_undo=self._undo_capture,
            on_keep=self._keep_capture,
        )

    def _declare_capture_anchors(self) -> None:
        capture = self._capture
        textarea = capture.textarea if capture is not None else None
        if textarea is None or capture is None:
            return
        textarea.line_anchors = {
            **textarea.line_anchors,
            _CAPTURE_FIRST_ID: capture.first_line,
            _CAPTURE_LAST_ID: capture.last_line,
        }

    def _capture_lines(self) -> tuple[int, int]:
        capture = self._capture
        assert capture is not None
        textarea = capture.textarea
        anchors = textarea.line_anchors if textarea is not None else {}
        return (
            anchors.get(_CAPTURE_FIRST_ID, capture.first_line),
            anchors.get(_CAPTURE_LAST_ID, capture.last_line),
        )

    async def _trim_capture(self, start_s: float, end_s: float) -> None:
        capture = self._capture
        if capture is None:
            return
        from waldo_commander.components.capture_review import capture_review

        selection = capture_review.selection()
        recording = (
            capture.recording
            if selection is None
            else capture.recording.select(*selection)
        )
        if len(recording.samples) < 2:
            return
        program = waldoctl.commander.programs.active
        name = Path(program.filename).stem if program is not None else "program"
        try:
            conversion = await run.io_bound(
                span_to_lines,
                recording,
                ui_state.active_robot,
                program=name or "program",
                directory=recordings_dir(),
            )
        except (ValueError, OSError) as error:
            logger.warning("Trimmed motion could not be written as code: %s", error)
            return
        if conversion is None or self._capture is not capture:
            return
        if self.ui_client is not None:
            with self.ui_client:
                self._replace_capture(conversion.source)
        else:
            self._replace_capture(conversion.source)
        capture.conversion = conversion
        capture_review.retrimmed(recording, conversion)

    def _replace_capture(self, snippet: str) -> None:
        """Rewrite the captured lines as *snippet*, or take them out."""
        capture = self._capture
        textarea = capture.textarea if capture is not None else None
        if capture is None or textarea is None:
            return
        first, last = self._capture_lines()
        new_value, first, count = replace_lines(
            str(textarea.value or ""), first, last, snippet
        )
        textarea.value = new_value
        if self._insert_line:
            # The session cursor stays on the last captured line, or moves
            # back above where the lines were.
            self._insert_line = first + count - 1 if count else max(first - 1, 1)
            self._declare_insert_anchor(textarea)
        if count:
            capture.first_line, capture.last_line = first, first + count - 1
            self._declare_capture_anchors()
            from waldo_commander.components.editor_decorations import decorations

            decorations.flash_editor_lines(list(range(first, first + count)))
        else:
            self._forget_capture_anchors(textarea)
            self._capture = None

    def _forget_capture_anchors(self, textarea) -> None:
        anchors = dict(textarea.line_anchors)
        if any(
            anchors.pop(k, None) is not None
            for k in (_CAPTURE_FIRST_ID, _CAPTURE_LAST_ID)
        ):
            textarea.line_anchors = anchors

    def _undo_capture(self) -> None:
        if self._capture is None:
            return
        self._replace_capture("")
        self._capture = None
        from waldo_commander.components.capture_review import capture_review

        capture_review.hide()

    def _keep_capture(self) -> None:
        if self._capture is None:
            return
        textarea = self._capture.textarea
        if textarea is not None:
            self._forget_capture_anchors(textarea)
        self._capture = None
        from waldo_commander.components.capture_review import capture_review

        capture_review.hide()


def _moved(before: RecordedSample, after: RecordedSample) -> bool:
    return (
        max(abs(a - b) for a, b in zip(after.joints_deg, before.joints_deg)) > STILL_DEG
    )


# Singleton
motion_recorder = MotionRecorder()
