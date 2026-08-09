"""Motion recorder for capturing robot actions as code during teaching."""

import logging
import re
import time
from dataclasses import dataclass, fields as _dc_fields

import numpy as np

import waldoctl

from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import (
    ui_state,
)
from waldo_commander.common.logging_config import TRACE_ENABLED
from waldo_commander.services.command_discovery import discover_robot_commands

logger = logging.getLogger(__name__)

# ShapeBase's non-geometry fields — everything else on a Shape is a dimension
# constructor kwarg.
_SHAPE_COMMON_FIELDS = ("name", "pose", "collision", "margin")
_SHAPE_DEFAULT_POSE = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _shape_to_code(s) -> str:
    """One waldoctl Shape as a constructor call, omitting default fields."""
    parts = [f"name={s.name!r}"]
    for f in _dc_fields(s):
        if f.name in _SHAPE_COMMON_FIELDS:
            continue
        parts.append(f"{f.name}={getattr(s, f.name)!r}")
    if tuple(s.pose) != _SHAPE_DEFAULT_POSE:
        parts.append(f"pose={tuple(s.pose)!r}")
    if not s.collision:
        parts.append("collision=False")
    if s.margin is not None:
        parts.append(f"margin={s.margin!r}")
    return f"{type(s).__name__}({', '.join(parts)})"


def shapes_to_code(shapes) -> str:
    """A runnable ``rbt.set_shapes([...])`` block for the given world —
    the environment's durable form is program code, not GUI state."""
    if not shapes:
        return "rbt.set_shapes([])"
    body = "\n".join(f"    {_shape_to_code(s)}," for s in shapes)
    return f"rbt.set_shapes([\n{body}\n])"


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

    def _ensure_select_tool(self, tool_key: str, variant_key: str = "") -> None:
        """Ensure rbt.select_tool() is in the script before the first move command.

        If an existing select_tool line is found, update it. Otherwise insert one
        before the first motion command (home, move_j, move_l, etc.).
        """
        textarea = ui_state.active_textarea
        if not textarea:
            return
        val: str = str(textarea.value or "")
        lines: list[str] = val.split("\n")

        if variant_key:
            set_tool_line = (
                f'rbt.select_tool("{tool_key}", variant_key="{variant_key}")'
            )
        else:
            set_tool_line = f'rbt.select_tool("{tool_key}")'
        set_tool_re = re.compile(r"^\s*rbt\.\s*select_tool\s*\(")

        for i, line in enumerate(lines):
            if set_tool_re.match(line):
                lines[i] = set_tool_line
                textarea.value = "\n".join(lines)
                logger.info("Updated existing select_tool to %s", tool_key)
                return

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
                return

        # No motion commands found — just append
        self._insert_snippet(set_tool_line)

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
            self._ensure_select_tool(
                tool_key, variant_key=waldoctl.commander.status.tool.variant_key
            )

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
                args = ", ".join(f"{a:.2f}" for a in angles)
                spd = waldoctl.commander.settings.jog.speed / 100.0
                acc = waldoctl.commander.settings.jog.accel / 100.0
                anchor_snippet = f"rbt.move_j([{args}], speed={spd}, accel={acc})  # Recording start position"
                self._insert_snippet(anchor_snippet)
                logger.info(
                    "Inserted recording start anchor at joints: %s",
                    [f"{a:.1f}" for a in angles],
                )
            else:
                logger.info("Skipped anchor — robot matches script end position")

    def _stop_recording(self) -> None:
        """Stop recording session."""
        # If there's an active jog, end it first
        if self._active_jog:
            self.on_jog_end()

        # Clear is_recording on every program — the invariant says only one
        # could have been True, but the sweep makes the stop idempotent.
        for p in waldoctl.commander.programs.items:
            p.recording.is_recording = False
        logger.info("Recording stopped")

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
        if action_type == "move_j":
            angles = params["angles"]
            spd = waldoctl.commander.settings.jog.speed / 100.0
            acc = waldoctl.commander.settings.jog.accel / 100.0
            args = ", ".join(f"{a:.2f}" for a in angles)
            wait_str = ", wait=False" if not params.get("wait", True) else ""
            return f"rbt.move_j([{args}], speed={spd}, accel={acc}{wait_str})"

        elif action_type == "move_l":
            pose = params["pose"]
            spd = waldoctl.commander.settings.jog.speed / 100.0
            acc = waldoctl.commander.settings.jog.accel / 100.0
            args = ", ".join(f"{p:.3f}" for p in pose)
            wait_str = ", wait=False" if not params.get("wait", True) else ""
            return f"rbt.move_l([{args}], speed={spd}, accel={acc}{wait_str})"

        elif action_type == "home":
            return "rbt.home()"

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
            missing = sorted(
                {type(s).__name__ for s in shapes if type(s).__name__ not in imported}
            )
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

    def _insert_snippet(self, snippet: str) -> None:
        """Insert code snippet into the editor and flash the new line."""

        if ui_state.active_textarea:
            textarea = ui_state.active_textarea
            val = textarea.value or ""

            # Count lines before insertion for flash highlighting
            lines_before = len(val.splitlines())

            if val and not val.endswith("\n"):
                val += "\n"
            new_value = val + snippet + "\n"
            # Assigning value triggers the editor's on_change -> debounced simulation.
            textarea.value = new_value

            # Flash the newly added line.
            new_line_number = lines_before + 1
            # Local import: motion_recorder is in services/ and decorations
            # is in components/, so a top-level import would invert the
            # layered dependency direction. Keep it lazy.
            from waldo_commander.components.editor_decorations import decorations

            decorations.flash_editor_lines([new_line_number])
        else:
            logger.error("Editor textarea not ready - open Program tab first")


# Singleton
motion_recorder = MotionRecorder()
