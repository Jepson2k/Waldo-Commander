"""
Path preview client for offline simulation and visualization.

Wraps a backend's DryRunRobotClient with what the backend cannot know
about the program: the editor line each command came from, the duration
it asked for, the checkpoint label it carries, and whether its target was
written as a literal the host may edit in place. The motion itself lives
in the backend's records — ``plan()`` for what the program commands,
``simulate()`` for what the arm would do — one block per command, and
this client's notes line up with those blocks by index.
"""

import asyncio
import inspect
import linecache
import logging
import math
import re
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any, TypeVar, cast

import numpy as np
from waldoctl import CommandNote, TickIndex
from waldoctl.client import RobotClient
from waldoctl.commands import CommandKind, command_table
from waldoctl.skills import UnresolvedPreview

from waldo_commander.services.preview_segments import targets_from_record
from waldo_commander.state import ShapeChange, ToolAction, ToolSelection

logger = logging.getLogger(__name__)

R = TypeVar("R")

_LITERAL_LIST_RE = re.compile(
    r"(?:move_j|move_l|move_c|move_s|move_p)\s*\(\s*(?:\w+\s*=\s*)?\["
    r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
    r"(?:\s*,\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)*\s*\]"
)
_DURATION_RE = re.compile(r"duration\s*=\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")

_COMMANDS = command_table()

# Methods that produce trajectory segments for visualization, by path shape.
MOTION_METHODS: dict[str, str] = {
    name: spec.move_type or ""
    for name, spec in _COMMANDS.items()
    if spec.kind is CommandKind.MOTION
}

# There is no observation behind these in a planning preview; the PAROL6
# extras are not on the ABC but answer the same live-only questions.
_UNRESOLVED = frozenset(
    name for name, spec in _COMMANDS.items() if spec.kind is CommandKind.OBSERVATION
) | {"command_verdict", "is_estop_pressed"}

#: Tool calls that move the jaws. Anything else on a tool is a read, and a
#: read answers with what it read, not with a queue index.
_TOOL_ACTIONS = frozenset({"set_position", "open", "close", "calibrate", "grip"})

#: Reads a program makes on its client that are not commands: nothing to
#: note, and no blend hold to close first.
_PASSTHROUGH = frozenset({"robot", "tool", "program_length", "active_tool_key"})


class _ToolCollectionProxy:
    """Wraps DryRunRobotClient.tool with visualization metadata.

    Intercepts tool action method calls, delegates to the dry-run tool
    (which dispatches through the planner), notes the action's command
    and augments it with tool visualization metadata for 3D rendering.
    """

    def __init__(self, preview_client: "PathPreviewClient"):
        self._preview = preview_client

    def __getattr__(self, name: str) -> Any:
        dry_run_tool = self._preview._client.tool
        attr = getattr(dry_run_tool, name)
        if not callable(attr):
            return attr

        if name not in _TOOL_ACTIONS:
            # A read (`tool.status()`, `tool.is_open(...)`) answers with its
            # value; minting an index for it would hand a program a number
            # where the live client hands it a reading.
            return attr

        def interceptor(*args: Any, **kwargs: Any) -> Any:
            index = attr(*args, **kwargs)
            self._preview._record_tool_action(name, args, kwargs, index)
            return index

        return interceptor


class PathPreviewClient:
    """Wraps DryRunRobotClient with the program's own knowledge.

    Delegates every command to the backend's DryRunClient (which runs it
    through the real planning pipeline) and answers exactly what it
    answered: a queue index for queued work, a code for the rest. After
    each call it notes the command's line and what the program asked for,
    so the records the client hands back can be labelled by line.

    Methods are resolved via __getattr__:
    - Motion methods: dispatch through _client + note the command
    - All other methods: delegate to _client (which raises AttributeError
      for unknown names, catching typos in user scripts)
    """

    def __init__(
        self,
        dry_run_client_cls: Callable[..., Any],
        target_collector: list[dict] | None = None,
        tool_action_collector: list[ToolAction] | None = None,
        tool_selection_collector: list | None = None,
        shape_change_collector: list | None = None,
        initial_joints: list[float] | np.ndarray | None = None,
        initial_homed: bool = True,
        tool_meta_registry: dict[str, dict] | None = None,
        robot: Any = None,
    ):
        self.target_collector: list[dict] = (
            [] if target_collector is None else target_collector
        )
        self.tool_action_collector: list[ToolAction] = (
            [] if tool_action_collector is None else tool_action_collector
        )
        self.tool_selection_collector: list = (
            [] if tool_selection_collector is None else tool_selection_collector
        )
        self.shape_change_collector: list = (
            [] if shape_change_collector is None else shape_change_collector
        )
        self._tool_meta_registry: dict[str, dict] = tool_meta_registry or {}
        self._tool_metadata: dict | None = None
        self.accumulated_errors: list[str] = []
        self._skill_line: int = 0

        init_deg: list[float] | None = None
        if initial_joints is not None:
            init_deg = np.degrees(np.asarray(initial_joints, dtype=np.float64)).tolist()

        self._client = dry_run_client_cls(
            initial_joints_deg=init_deg, initial_homed=initial_homed
        )
        if robot is not None:
            # The worker already holds the backend it planned with; a bare
            # client would otherwise build its own on first read.
            self._client.robot = robot
        self._tool_proxy = _ToolCollectionProxy(self)
        self._pending_sleep: float = 0.0
        self._last_move_non_blocking: bool = False
        self._current_tool_position: float = 0.0  # 0=open, 1=closed
        self._first_motion_seen: bool = False
        # A corner move waits in the backend's blend hold for the move
        # behind it; reading a record closes the hold, so the program's
        # clock is not refreshed while one is open.
        self._holding: bool = False
        self._clock_s: float = 0.0
        # One note per command the backend has recorded, in its order. The
        # backend keeps the commands so they can be planned and simulated
        # but cannot know where they came from — the program is ours.
        self.notes: list[CommandNote] = []
        self._last_attributed_line = 0

        logger.debug("PathPreviewClient initialized")

    @property
    def robot(self) -> Any:
        """The backend the preview stands in for; what a skill checks its
        requirements against."""
        return self._client.robot

    @property
    def sim_time_s(self) -> float:
        """The program's own clock, in simulated seconds.

        A script that polls ``time.monotonic()`` in a loop needs this to
        advance or it never leaves the loop; the real clock cannot help,
        because a preview runs a minute of robot time in a fraction of a
        second. It is the commanded record's length: every move, delay
        and jaw travel is time on it.
        """
        if not self._holding:
            self._clock_s = self._client.plan().duration_s
        return self._clock_s

    @property
    def skill_capabilities(self) -> frozenset[str]:
        return getattr(
            self._client,
            "skill_capabilities",
            frozenset({"motion.joint", "motion.linear"}),
        )

    def run_skill(self, invoke: Callable[[RobotClient], Coroutine[Any, Any, R]]) -> R:
        """Use this collector's async view, without opening a backend client."""
        line = self._skill_line
        self._skill_line = self._get_caller_line_number()
        try:
            return asyncio.run(
                invoke(cast(RobotClient, AsyncPathPreviewClient.from_sync(self)))
            )
        finally:
            self._skill_line = line

    def wait_command(
        self, command_index: int, timeout: float = 10.0, **kwargs: Any
    ) -> bool:
        """The live signature, positional *timeout* included: a program written
        as ``rbt.wait_command(index, 30.0)`` runs in preview as it does live."""
        self._holding = False
        return bool(self._client.wait_command(command_index, timeout))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.flush()

    def close(self):
        """Close the blend hold, note whatever the backend recorded after
        the last noted call, and place a target at the end of every move
        the program wrote with literal coordinates."""
        self.flush()
        self._attribute_commands(self._last_attributed_line)
        known = {t["id"] for t in self.target_collector}
        for target in targets_from_record(self._client.plan(), self.notes):
            if target.id not in known:
                self.target_collector.append(
                    {
                        "id": target.id,
                        "line_number": target.line_number,
                        "pose": target.pose,
                        "move_type": target.move_type,
                        "scene_object_id": target.scene_object_id,
                    }
                )

    def flush(self) -> None:
        """Plan whatever the backend's blend hold still holds."""
        self._client.flush()
        self._holding = False

    # The name the rest of the preview machinery calls it by.
    _flush_blend = flush

    def plan(self, max_seconds: float | None = None) -> TickIndex:
        """The commanded record, its blocks labelled with the lines that
        produced them."""
        self._holding = False
        return self._label(self._client.plan(max_seconds))

    def simulate(self, max_seconds: float | None = None) -> TickIndex:
        """The predicted record, labelled the same way."""
        self._holding = False
        return self._label(self._client.simulate(max_seconds))

    def _label(self, record: TickIndex) -> TickIndex:
        """Give each block the editor line that produced it."""
        notes = self.notes
        record.blocks = tuple(
            replace(
                b,
                line_number=notes[b.command].line_number
                if b.command < len(notes)
                else None,
            )
            for b in record.blocks
        )
        return record

    def record_sleep(self, seconds: float) -> None:
        """A script's ``time.sleep`` as the program means it.

        The arm holds where the last move left it for that long — time on
        the timeline, and in a simulation the moment whatever it carries
        settles or does not — so it is queued as a delay the live program
        never sends, and noted as a sleep so the live run's command count
        is not thrown off by it.
        """
        if seconds <= 0:
            return
        if self._last_move_non_blocking:
            self._pending_sleep += seconds
        self._client.delay(seconds)
        self._attribute_commands(self._get_caller_line_number(), method="sleep")

    def delay(self, seconds: float) -> int:
        """A queued delay: the arm holds for *seconds*."""
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Delay must be positive and finite")
        index = self._client.delay(seconds)
        self._attribute_commands(self._get_caller_line_number(), method="delay")
        return index

    def _attribute_commands(
        self, line_number: int, method: str = "", **fields: Any
    ) -> None:
        """Note every command recorded since the last call as issued by
        *method* on *line_number*; *fields* describe the last of them.

        Called from each site that already knows a line. A command the
        backend queued on the program's behalf is picked up by the next
        site, or by :meth:`close`, so the notes always end the same length
        as the backend's program.
        """
        self._last_attributed_line = line_number
        recorded = self._client.program_length
        missing = recorded - len(self.notes)
        for k in range(missing):
            extra = fields if k == missing - 1 else {}
            self.notes.append(
                CommandNote(
                    line_number=line_number,
                    method=method,
                    travel=not self._first_motion_seen,
                    **extra,
                )
            )

    @property
    def tool(self) -> _ToolCollectionProxy:
        """Return a proxy that delegates to DryRunRobotClient.tool and notes
        the actions it takes."""
        return self._tool_proxy

    def _record_tool_action(
        self, method_name: str, args: tuple, kwargs: dict, index: Any
    ) -> None:
        """Note a tool action's command and keep what the scene needs to
        draw it; where the TCP stood and how long the arm held are read
        off the record later."""
        line_no = self._get_caller_line_number()
        self._attribute_commands(line_no, method="tool_action")
        self._holding = False
        if self._tool_metadata is None:
            return

        if method_name == "set_position" and args:
            target_pos = (float(args[0]),)
        elif method_name == "open":
            target_pos = (0.0,)
        elif method_name == "close":
            target_pos = (1.0,)
        else:
            return

        start_pos = (self._current_tool_position,)
        self._current_tool_position = target_pos[0]
        command = (
            index
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0
            else self._client.program_length - 1
        )
        self.tool_action_collector.append(
            ToolAction(
                tcp_pose=None,
                motions=self._tool_metadata["motions"],
                target_positions=target_pos,
                start_positions=start_pos,
                activation_type=self._tool_metadata["activation_type"],
                line_number=line_no,
                method=method_name,
                estimated_duration=0.0,
                # A sleep after a non-blocking move puts the action mid-motion,
                # offset from the start of the move it rides.
                sleep_offset=self._pending_sleep,
                segment_index=-1,
                tcp_path=None,
                command=command,
            )
        )

    # ---- Source introspection ----

    def _get_caller_line_number(self) -> int:
        if self._skill_line:
            return self._skill_line
        try:
            frame = inspect.currentframe()
            while frame:
                if frame.f_code.co_filename == "simulation_script.py":
                    return frame.f_lineno
                frame = frame.f_back
        except (AttributeError, RuntimeError):
            pass
        return 0

    def _get_source_line(self, line_no: int) -> str:
        try:
            line = linecache.getline("simulation_script.py", line_no)
            if line:
                return line.strip()
        except (OSError, ValueError, IndexError):
            pass
        return ""

    def _has_literal_list_args(self, line: str) -> bool:
        return bool(_LITERAL_LIST_RE.search(line))

    @staticmethod
    def _extract_requested_duration(line: str) -> float | None:
        """Extract duration=<value> from a source line. Returns None if not found or <= 0."""
        match = _DURATION_RE.search(line)
        if match:
            val = float(match.group(1))
            return val if val > 0 else None
        return None

    def _collect_failed_target(
        self,
        line_no: int,
        move_type: str,
        args: tuple,
        kwargs: dict,
    ) -> None:
        """Create a target marker for a move that failed (out of range, IK failure).

        Extracts the intended pose from the move arguments so the user can
        at least see where the unreachable target is. Targets from non-literal
        source (e.g. computed expressions) are still rendered red but won't
        back-edit on drag — drag interaction is a separate concern.
        """
        # Runtime values, regardless of whether the source line is a literal
        # or a computed expression.
        pose: list[float] | None = None
        pose_kwarg = kwargs.get("pose")
        if pose_kwarg is not None:
            pose = [float(v) for v in pose_kwarg[:6]]
        elif move_type in ("cartesian", "smooth_arc", "smooth_spline") and args:
            # move_l/move_c: the first arg is [x,y,z,rx,ry,rz] in mm/deg;
            # move_p/move_s pass a list of such waypoints, and the last one
            # is the pose the move was trying to reach.
            first = args[0]
            if len(first) and isinstance(first[0], (list, tuple, np.ndarray)):
                first = first[-1]
            pose = [float(v) for v in first[:6]]
        # move_j with joint angles: skip — we'd need FK to get TCP pose

        if pose is None or len(pose) < 3:
            return

        # Convert mm/deg to m/rad for consistency with valid targets
        pose_m = [
            pose[0] / 1000.0,
            pose[1] / 1000.0,
            pose[2] / 1000.0,
            *(math.radians(v) for v in pose[3:]),
        ]

        target_id = f"auto_{line_no}"
        self.target_collector.append(
            {
                "id": target_id,
                "line_number": line_no,
                "pose": pose_m,
                "move_type": move_type,
                "scene_object_id": "",
                "is_valid": False,
            }
        )
        logger.debug("Created failed-move target %s at line %d", target_id, line_no)

    # ---- Explicit: home and checkpoint ----

    def home(self, **kw: Any) -> int:
        self._first_motion_seen = True
        line_no = self._get_caller_line_number()
        try:
            index = self._client.home(**kw)
        except Exception as e:
            logger.warning("home failed: %s", e)
            self.accumulated_errors.append(f"Line {line_no}: {e}")
            self._attribute_commands(line_no, method="home", checkpoint="home")
            return -1
        self._holding = False
        self._attribute_commands(line_no, method="home", checkpoint="home")
        return index

    def checkpoint(self, label: str) -> int:
        """Record a checkpoint marker in the timeline: a command that owns
        no rows and carries its label."""
        index = self._client.checkpoint(label)
        self._holding = False
        self._attribute_commands(
            self._get_caller_line_number(), method="checkpoint", checkpoint=label
        )
        return index

    # ---- Dynamic dispatch ----

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        if name in _PASSTHROUGH:
            return getattr(self._client, name)

        # Motion methods: dispatch through _client + note the command
        move_type = MOTION_METHODS.get(name)
        if move_type is not None:
            # A backend without this optional motion raises here, as the live
            # client would, rather than previewing a refusal.
            method = getattr(self._client, name)

            def motion_method(*args: Any, **kwargs: Any) -> int:
                self._first_motion_seen = True
                self._pending_sleep = 0.0
                self._last_move_non_blocking = not kwargs.get("wait", True)
                line_no = self._get_caller_line_number()
                source_line = self._get_source_line(line_no)
                fields = {
                    "requested_duration": self._extract_requested_duration(source_line),
                    "literal": self._has_literal_list_args(source_line),
                }
                try:
                    result = method(*args, **kwargs)
                except Exception as e:
                    self.accumulated_errors.append(f"Line {line_no}: {e}")
                    logger.warning("%s failed: %s", name, e)
                    # Still create a target for the failed move so the user
                    # can see and drag it to a valid position
                    self._collect_failed_target(line_no, move_type, args, kwargs)
                    self._attribute_commands(line_no, method=name, **fields)
                    return -1
                # A corner move waits for its successor; a stopping move,
                # and every stream, closes whatever waited.
                self._holding = float(kwargs.get("r", 0) or 0) > 0
                self._attribute_commands(line_no, method=name, **fields)
                return result

            return motion_method

        # Intercept select_tool to update tool metadata from registry
        if name == "select_tool":
            client_method = getattr(self._client, name)

            def set_tool_wrapper(*args: Any, **kw: Any) -> Any:
                line_no = self._get_caller_line_number()
                result = client_method(*args, **kw)
                self._holding = False
                self._attribute_commands(line_no, method="select_tool")
                if isinstance(result, int) and result < 0:
                    return result
                self._current_tool_position = 0.0  # New tool starts open
                if args:
                    key = str(args[0]).strip().upper()
                    variant_key = kw.get(
                        "variant_key", args[1] if len(args) > 1 else ""
                    )
                    entry = self._tool_meta_registry.get(key)
                    if entry:
                        # Prefer variant-specific motions if available
                        variants = entry.get("variants", {})
                        if variant_key and variant_key in variants:
                            self._tool_metadata = {
                                "motions": variants[variant_key]["motions"],
                                "activation_type": entry["activation_type"],
                            }
                        elif entry.get("motions"):
                            self._tool_metadata = entry
                        else:
                            self._tool_metadata = None
                    else:
                        self._tool_metadata = None
                    self.tool_selection_collector.append(
                        ToolSelection(
                            tool_key=key,
                            variant_key=str(variant_key),
                            segment_index=-1,
                            line_number=line_no,
                            command=self._client.program_length - 1,
                        )
                    )
                return result

            return set_tool_wrapper

        # Intercept set_shapes — record the boundary so collision marking can
        # replay the world that was active at each command (like tool
        # selections).
        if name == "set_shapes":
            underlying = getattr(self._client, name)

            def set_shapes_wrapper(shapes: list, *args: Any, **kwargs: Any) -> Any:
                line_no = self._get_caller_line_number()
                result = underlying(shapes, *args, **kwargs)
                self._holding = False
                self._attribute_commands(line_no, method="set_shapes")
                self.shape_change_collector.append(
                    ShapeChange(
                        shapes=tuple(shapes),
                        segment_index=-1,
                        line_number=line_no,
                        command=self._client.program_length - 1,
                    )
                )
                return result

            return set_shapes_wrapper

        # There is no observation behind a status predicate in a planning
        # preview. Inventing a successful handshake would select the wrong
        # branch of an ordinary Python program or skill.
        if name in _UNRESOLVED:

            def unresolved(*args: Any, **kwargs: Any) -> Any:
                raise UnresolvedPreview(f"{name} needs an explicit observation fixture")

            return unresolved

        # Everything else delegates to the backend, which raises
        # AttributeError for unknown names, catching typos. A command is
        # noted; a read answers with what it read.
        underlying = getattr(self._client, name)
        spec = _COMMANDS.get(name)
        if spec is None or not callable(underlying):
            return underlying
        if spec.kind in (CommandKind.QUERY, CommandKind.SYNC):
            return underlying

        def command(*args: Any, **kwargs: Any) -> Any:
            line_no = self._get_caller_line_number()
            result = underlying(*args, **kwargs)
            self._holding = False
            self._attribute_commands(line_no, method=name)
            return result

        return command


class AsyncPathPreviewClient:
    """Async wrapper around PathPreviewClient."""

    @classmethod
    def from_sync(cls, client: PathPreviewClient) -> "AsyncPathPreviewClient":
        view = cls.__new__(cls)
        view._sync_client = client
        return view

    @property
    def tool(self) -> "_AsyncPreviewTool":
        return _AsyncPreviewTool(self._sync_client.tool)

    @property
    def robot(self) -> Any:
        return self._sync_client.robot

    def __init__(
        self,
        dry_run_client_cls: Callable[..., Any],
        target_collector: list[dict] | None = None,
        tool_action_collector: list[ToolAction] | None = None,
        tool_selection_collector: list | None = None,
        shape_change_collector: list | None = None,
        initial_joints: list[float] | np.ndarray | None = None,
        initial_homed: bool = True,
        tool_meta_registry: dict[str, dict] | None = None,
        robot: Any = None,
    ):
        self._sync_client = PathPreviewClient(
            dry_run_client_cls=dry_run_client_cls,
            target_collector=target_collector,
            tool_action_collector=tool_action_collector,
            tool_selection_collector=tool_selection_collector,
            shape_change_collector=shape_change_collector,
            initial_joints=initial_joints,
            initial_homed=initial_homed,
            tool_meta_registry=tool_meta_registry,
            robot=robot,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._sync_client.flush()

    async def close(self):
        self._sync_client.flush()

    @property
    def notes(self) -> list[CommandNote]:
        return self._sync_client.notes

    @property
    def target_collector(self) -> list[dict]:
        return self._sync_client.target_collector

    @property
    def tool_action_collector(self) -> list[ToolAction]:
        return self._sync_client.tool_action_collector

    @property
    def tool_selection_collector(self) -> list:
        return self._sync_client.tool_selection_collector

    def __getattr__(self, name: str) -> Any:
        # `run_skill` is the sync client's own entry point; exposing it here as
        # a coroutine would let a sync skill call on an async client preview as
        # a no-op where the real client raises AttributeError.
        if name == "run_skill":
            raise AttributeError(name)
        attr = getattr(self._sync_client, name)
        if callable(attr) and name != "close":

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                return attr(*args, **kwargs)

            return wrapper
        return attr


class _AsyncPreviewTool:
    def __init__(self, tool: _ToolCollectionProxy) -> None:
        self._tool = tool

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._tool, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            return attr(*args, **kwargs)

        return call
