"""Motion recorder for capturing robot actions as code during teaching."""

import logging
import re
import time
from dataclasses import dataclass

import numpy as np

from waldo_commander.state import (
    editor_tabs_state,
    recording_state,
    robot_state,
    ui_state,
)
from waldo_commander.common.logging_config import TRACE_ENABLED

logger = logging.getLogger(__name__)


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
            robot_state.x,
            robot_state.y,
            robot_state.z,
            robot_state.rx,
            robot_state.ry,
            robot_state.rz,
        ]

    def _get_current_angles(self) -> list[float]:
        """Get current joint angles as list."""
        n = ui_state.active_robot.joints.count
        return (
            list(robot_state.angles.deg[:n])
            if len(robot_state.angles) >= n
            else [0.0] * n
        )

    @staticmethod
    def _matches_sim_end(current_angles_deg: list[float], tol_deg: float = 0.5) -> bool:
        """Check if current joint angles match the simulation's final position."""
        tab = editor_tabs_state.get_active_tab()
        if tab is None or tab.final_joints_rad is None:
            return False
        final_deg = np.degrees(tab.final_joints_rad)
        return bool(np.allclose(current_angles_deg, final_deg, atol=tol_deg))

    @staticmethod
    def _get_motion_cmd_names() -> frozenset[str]:
        """Get motion command names from the command palette discovery."""
        from waldo_commander.components.editor import discover_robot_commands

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
        textarea = ui_state.editor_panel.program_textarea
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

        # Check for existing select_tool line
        for i, line in enumerate(lines):
            if set_tool_re.match(line):
                # Update existing select_tool with current tool
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
        if recording_state.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        """Start a new recording session."""
        recording_state.is_recording = True
        self._active_jog = None
        self._last_action_wall_time = 0.0

        # Log the initial position for reference
        if len(robot_state.angles) >= ui_state.active_robot.joints.count:
            logger.info(
                "Recording started - initial joints: %s deg",
                [f"{a:.1f}" for a in robot_state.angles.deg],
            )
        logger.info(
            "Recording started - initial pose: [%.1f, %.1f, %.1f, %.1f, %.1f, %.1f] (mm/deg)",
            robot_state.x,
            robot_state.y,
            robot_state.z,
            robot_state.rx,
            robot_state.ry,
            robot_state.rz,
        )

        # Ensure select_tool is before the first move command in the script
        tool_key = robot_state.tool_key
        if tool_key and tool_key != "NONE":
            self._ensure_select_tool(tool_key, variant_key=robot_state.tool_variant_key)

        # Insert anchor move_j to establish recording start position — but only
        # if the robot has moved away from where the script's simulation ends.
        # This avoids a redundant zero-distance segment (e.g. script ends with
        # home() and robot is still at home when recording starts).
        if len(robot_state.angles) >= ui_state.active_robot.joints.count:
            angles = self._get_current_angles()
            if not self._matches_sim_end(angles):
                args = ", ".join(f"{a:.2f}" for a in angles)
                spd = ui_state.jog_speed / 100.0
                acc = ui_state.jog_accel / 100.0
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

        recording_state.is_recording = False
        logger.info("Recording stopped")

    def record_action(self, action_type: str, **params) -> None:
        """Record any robot action when recording is active.

        Args:
            action_type: One of "move_j", "move_l", "home",
                        "gripper", "io", "delay"
            **params: Action-specific parameters
        """
        if not recording_state.is_recording:
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
            spd = ui_state.jog_speed / 100.0
            acc = ui_state.jog_accel / 100.0
            args = ", ".join(f"{a:.2f}" for a in angles)
            wait_str = ", wait=False" if not params.get("wait", True) else ""
            return f"rbt.move_j([{args}], speed={spd}, accel={acc}{wait_str})"

        elif action_type == "move_l":
            pose = params["pose"]
            spd = ui_state.jog_speed / 100.0
            acc = ui_state.jog_accel / 100.0
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

        else:
            return f"# Unknown action: {action_type}"

    def on_jog_start(self, move_type: str, axis_info: str) -> None:
        """Called when a jog action starts.

        Args:
            move_type: "joint" or "cartesian"
            axis_info: Axis identifier like "J1+", "J3-", "X+", "RZ-"
        """
        if not recording_state.is_recording:
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
        if not recording_state.is_recording or not self._active_jog:
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

    def capture_current_pose(self, move_type: str = "cartesian") -> None:
        """Capture current robot pose and insert as move command.

        Args:
            move_type: "cartesian" or "joints"
        """
        if move_type == "joints":
            self._record_action_impl(
                "move_j", angles=self._get_current_angles(), duration=1.0
            )
        else:
            self._record_action_impl("move_l", pose=self._get_wrf_pose(), duration=1.0)

    def _insert_snippet(self, snippet: str) -> None:
        """Insert code snippet into the editor and flash the new line."""

        if ui_state.editor_panel.program_textarea:
            textarea = ui_state.editor_panel.program_textarea
            val = textarea.value or ""

            # Count lines before insertion for flash highlighting
            lines_before = len(val.splitlines())

            if val and not val.endswith("\n"):
                val += "\n"
            new_value = val + snippet + "\n"
            # Direct assignment - NiceGUI's binding handles the update
            # This will trigger the editor's on_change -> debounced simulation
            textarea.value = new_value

            # Flash the newly added line
            new_line_number = lines_before + 1
            ui_state.editor_panel.flash_editor_lines([new_line_number])
        else:
            logger.error("Editor textarea not ready - open Program tab first")


# Singleton
motion_recorder = MotionRecorder()
