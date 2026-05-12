"""
TCP Controls Mixin for UrdfScene.

Provides TCP TransformControls functionality:
- Enable/disable TransformControls on TCP ball
- Handle transform events for direct Cartesian moves (jogging) or IK (editing)
- Mode switching (translate/rotate)

The TCP ball behavior depends on RobotAppearanceMode:
- LIVE/SIMULATOR: Streams Cartesian jog moves to backend
- EDITING: Solves IK for target positioning
"""

import asyncio
import logging
import math
from typing import Any, Callable

import numpy as np
from nicegui import ui
from pinokin import arrays_equal_n
from nicegui.helpers import is_user_simulation

from waldo_commander.common.theme import SceneColors
from waldo_commander.state import robot_state
from .ik_solver import EditingIKSolver

from .config import RobotAppearanceMode

logger = logging.getLogger(__name__)


class TCPControlsMixin:
    """Mixin providing TCP TransformControls functionality for UrdfScene."""

    # These attributes are defined in the main UrdfScene class
    scene: Any
    _appearance_mode: RobotAppearanceMode
    _editing_angles: list[float]
    joint_groups: dict
    joint_trafos: dict
    joint_names: list[str]

    # Methods from other mixins (for type checking)
    def _sync_robot_state_from_editing(self) -> None: ...

    def _update_edit_bar_values(self, editing_type: str) -> None: ...

    _current_editing_type: str | None

    @property
    def tcp_transform_mode(self) -> str:
        """Current TCP transform mode ('translate' or 'rotate')."""
        return self._tcp_transform_mode

    def _init_tcp_controls_state(self) -> None:
        """Initialize TCP controls state variables."""
        # TCP TransformControls state
        self._tcp_transform_enabled: bool = False
        self._tcp_enable_in_progress: bool = (
            False  # Guard against concurrent enablement
        )
        self._tcp_transform_mode: str = "translate"  # "translate" or "rotate"

        self._tcp_cartesian_move_callback: Callable[[list[float]], None] | None = None
        self._tcp_cartesian_move_start_callback: Callable[[], None] | None = None
        self._tcp_cartesian_move_end_callback: Callable[[], None] | None = None
        self._tcp_drag_start_rot_deg: tuple[float, float, float] | None = None
        self._tcp_ball: Any | None = None
        self._tcp_ball_dragging: bool = False
        self._ik_solver: EditingIKSolver | None = None
        self._editing_rotation: list[float] = [0.0, 0.0, 0.0]
        self._editing_rotation_set: bool = False  # Track if rotation was explicitly set

        # Pre-allocated buffers to avoid per-call allocations
        self._target_pos_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
        self._target_orientation_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
        self._pose_mm_buffer: list[float] = [0.0] * 6

        # FK dirty checking cache - skip FK when angles unchanged
        self._last_fk_angles_tuple: tuple[float, ...] | None = None
        self._last_fk_angles_raw: np.ndarray | None = (
            None  # For fast LIVE mode comparison
        )
        self._last_fk_pose: tuple[float, ...] | None = None  # cached FK result

    def _ensure_ik_solver(self) -> EditingIKSolver | None:
        """Lazy-initialize the FK/IK solver, returning it or None on failure."""
        if self._ik_solver is None:
            try:
                self._ik_solver = EditingIKSolver.from_urdf_scene(self)
            except Exception as e:
                logger.warning("FK/IK solver init failed: %s", e)
        return self._ik_solver

    def invalidate_fk_cache(self) -> None:
        """Force TCP ball FK recomputation on next update cycle."""
        self._last_fk_angles_tuple = None
        self._last_fk_angles_raw = None

    def on_tcp_cartesian_move(self, callback: Callable[[list[float]], None]) -> None:
        """Register callback to receive absolute TCP position for Cartesian moves.

        The callback receives pose as [x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg].
        This is used for drag-to-move functionality where the robot should move
        directly to the dragged position.

        Args:
            callback: Function to call with target pose in mm/degrees
        """
        self._tcp_cartesian_move_callback = callback

    def on_tcp_cartesian_move_start(self, callback: Callable[[], None]) -> None:
        """Register callback to be called when a TCP TransformControls drag starts."""
        self._tcp_cartesian_move_start_callback = callback

    def on_tcp_cartesian_move_end(self, callback: Callable[[], None]) -> None:
        """Register callback to be called when a TCP TransformControls drag ends."""
        self._tcp_cartesian_move_end_callback = callback

    def set_gizmo_visible(self, visible: bool) -> None:
        """Show or hide the TCP gizmo (TransformControls).

        Args:
            visible: True to show, False to hide
        """
        if visible:
            # Ensure jog ball exists and is visible
            self._ensure_tcp_ball()
            if self._tcp_ball:
                self._tcp_ball.visible(True)
            # Enable TransformControls if not already enabled
            if not self._tcp_transform_enabled:
                self.enable_tcp_transform_controls(self._tcp_transform_mode)
        else:
            # Disable TransformControls and hide jog ball
            if self._tcp_transform_enabled:
                self.disable_tcp_transform_controls()
            if self._tcp_ball:
                self._tcp_ball.visible(False)

    def set_gizmo_display_mode(self, mode: str) -> None:
        """Toggle gizmo display between translation and rotation modes.

        Args:
            mode: Either "TRANSLATE" for translation or "ROTATE" for rotation
        """
        mode = (mode or "").upper()
        if mode not in ("TRANSLATE", "ROTATE"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'TRANSLATE' or 'ROTATE'.")

        self._tcp_transform_mode = "translate" if mode == "TRANSLATE" else "rotate"

        # Update TCP TransformControls mode if enabled
        if self._tcp_transform_enabled:
            self.set_tcp_transform_mode(self._tcp_transform_mode)

    def enable_tcp_transform_controls(self, mode: str = "translate") -> None:
        """Enable TransformControls on the TCP anchor for Cartesian jogging.

        Args:
            mode: "translate" or "rotate"
        """
        # Guard against concurrent enablement attempts
        if self._tcp_transform_enabled or self._tcp_enable_in_progress:
            return

        if not self.scene:
            logger.warning("Cannot enable TCP transform controls: scene not available")
            return
        # Ensure jog ball exists
        self._ensure_tcp_ball()
        if not self._tcp_ball:
            logger.warning(
                "Cannot enable TCP transform controls: jog ball not available"
            )
            return

        # Mark enablement in progress to prevent concurrent attempts
        self._tcp_enable_in_progress = True

        # Store mode
        self._tcp_transform_mode = mode.lower()

        # Sync jog ball to current TCP via FK before enabling (like joint edit)
        self._update_jog_ball_from_robot_state()

        # In NiceGUI user simulation mode, no browser is running; skip JS enablement
        if is_user_simulation():
            try:
                if self._tcp_ball:
                    self._tcp_ball.visible(True)
            except Exception as e:
                logger.debug(
                    "User simulation: jog ball visibility update failed: %s", e
                )
            logger.debug(
                "User simulation detected; skipping TCP TransformControls enablement"
            )
            self._tcp_enable_in_progress = False
            return

        # Enable TransformControls with a Python-side retry until the JS object exists.
        # The per-object wrapper dispatches `scene.run_method('enable_transform_controls', id, ...)`
        # once; if the JS side hasn't received the init_objects payload yet, the call silently no-ops.
        # `has_transform_controls` on the scene is the only way to confirm attach succeeded.
        # Total budget bounded by a wall-clock deadline: the first probe absorbs page-load latency
        # (up to the full deadline); subsequent probes clamp to 1s so a stuck JS side can't tie up
        # the enablement guard for the full 5s × N times.
        async def _enable_with_retry():
            ball = self._tcp_ball
            if ball is None:
                return
            try:
                tcp_object_id = str(ball.id)
                loop = asyncio.get_event_loop()
                deadline = loop.time() + 5.0
                first = True
                while loop.time() < deadline:
                    ball.enable_transform_controls(
                        mode=self._tcp_transform_mode,
                        size=0.8,
                        space="local",
                    )
                    await asyncio.sleep(0.05)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    probe_timeout = remaining if first else min(remaining, 1.0)
                    first = False
                    ok = await self.scene.run_method(
                        "has_transform_controls", tcp_object_id, timeout=probe_timeout
                    )
                    if ok:
                        ball.visible(True)
                        self._tcp_transform_enabled = True
                        logger.debug("Enabled TCP TransformControls in %s mode", mode)
                        return
                logger.warning("Failed to enable TCP TransformControls within 5s")
            except (TimeoutError, asyncio.CancelledError):
                logger.debug("TCP TransformControls enablement cancelled (shutdown)")
            finally:
                self._tcp_enable_in_progress = False

        with self.scene:
            ui.timer(0.0, _enable_with_retry, once=True)

    def disable_tcp_transform_controls(self) -> None:
        """Disable TransformControls on the TCP anchor."""
        if not self.scene or not self._tcp_ball:
            return

        self._tcp_ball.disable_transform_controls()

        # Hide jog ball when controls are disabled
        self._tcp_ball.visible(False)

        self._tcp_transform_enabled = False
        self._tcp_enable_in_progress = False  # Reset guard on disable
        logger.debug("Disabled TCP TransformControls")

    def set_tcp_transform_mode(self, mode: str) -> None:
        """Change the TCP TransformControls mode.

        Args:
            mode: "translate" or "rotate"
        """
        if not self._tcp_transform_enabled:
            return

        if not self.scene or not self._tcp_ball:
            return

        self._tcp_transform_mode = mode.lower()

        # Sync ball position/rotation from FK before switching modes
        # This ensures rotate mode starts from the correct orientation
        self._update_tcp_ball_position()

        self._tcp_ball.set_transform_mode(self._tcp_transform_mode)

        logger.debug("Changed TCP TransformControls mode to %s", mode)

    def _ensure_tcp_ball(self) -> None:
        """Create the unified TCP ball if missing.

        The TCP ball is used for both jogging (LIVE/SIMULATOR) and IK target editing (EDITING).
        """
        if not self.scene:
            return
        if self._tcp_ball:
            return
        with self.scene:
            ball = ui.scene.sphere(
                radius=0.008,
                width_segments=16,
                height_segments=16,
                wireframe=False,
            ).with_name("tcp:ball")
            ball.material(SceneColors.EDIT_GRAY_HEX, 0.9)
            self._tcp_ball = ball
        # Initial position/orientation based on mode
        self._update_tcp_ball_position()

    def _snap_tcp_to_fk(self) -> None:
        """Snap TCP ball back to FK position from current editing angles.

        Called on transform_end in editing mode to correct the ball
        position when IK failed and the ball was dragged to an
        unreachable position.
        """
        self.invalidate_fk_cache()
        self._update_tcp_ball_position()

    def _update_tcp_ball_position(self) -> None:
        """Update TCP ball position from FK, with drift correction.

        Recomputes FK only when joint angles change.  If angles are
        unchanged but the ball was moved (e.g. by a drag), the cached
        pose is re-applied without recomputing FK.
        """
        if self._tcp_ball_dragging or not self._tcp_ball:
            return

        # Determine whether angles changed since last FK
        n = len(self.joint_names)
        angles_changed = False
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            angles_rad: list[float] | np.ndarray = self._editing_angles
            key = tuple(angles_rad[:n])
            if key != self._last_fk_angles_tuple:
                self._last_fk_angles_tuple = key
                angles_changed = True
        else:
            angles_deg = robot_state.angles.deg
            if self._last_fk_angles_raw is None or not arrays_equal_n(
                angles_deg[:n], self._last_fk_angles_raw
            ):
                self._last_fk_angles_raw = angles_deg[:n].copy()
                angles_rad = robot_state.angles.rad
                angles_changed = True

        if angles_changed:
            if not self._ensure_ik_solver() or self._ik_solver is None:
                return
            try:
                ee = self._ik_solver.forward_kinematics(angles_rad)
                self._last_fk_pose = tuple(float(v) for v in ee[:6])
            except Exception as e:
                logger.debug("FK failed: %s", e)
                return

        # Apply pose if ball drifted (or FK just computed a new one)
        p = self._last_fk_pose
        if p and (
            self._tcp_ball.x != p[0]
            or self._tcp_ball.y != p[1]
            or self._tcp_ball.z != p[2]
        ):
            self._tcp_ball.move(p[0], p[1], p[2])
            self._tcp_ball.rotate(p[3], p[4], p[5], order="XYZ")

    def _update_jog_ball_from_robot_state(self) -> None:
        """Position the TCP ball using live robot state (LIVE/SIMULATOR modes)."""
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            return  # Don't update from robot state while editing
        self._update_tcp_ball_position()

    def _handle_tcp_transform_for_jog(self, e) -> None:
        """Handle TCP transform events - behavior depends on appearance mode.

        Called from _handle_transform_continuous when TCP ball is being transformed.
        Drag-start side effects (capturing rotation, notifying consumers) live in
        UrdfScene._handle_transform_start since on_transform_start now fires before
        the first on_transform event.
        - LIVE/SIMULATOR: Sends direct Cartesian move commands via callback
        - EDITING: Solves IK to update editing angles
        """
        if not self._tcp_transform_enabled:
            return

        object_name = getattr(e, "object_name", "") or ""
        if object_name not in ("tcp:ball", "tcp:jog_ball", "tcp:offset"):
            return

        if self._appearance_mode == RobotAppearanceMode.EDITING:
            self._handle_tcp_transform_for_ik(e)
        else:
            self._handle_tcp_transform_for_cartesian(e)

    def _handle_tcp_transform_for_cartesian(self, e) -> None:
        """Handle TCP ball drag in LIVE/SIMULATOR mode - stream Cartesian moves.

        Uses absolute world coordinates from the gizmo.
        """
        if self._tcp_transform_mode == "translate":
            # Get world coordinates (prefer wx, wy, wz for absolute position)
            new_x = getattr(e, "wx", None)
            new_y = getattr(e, "wy", None)
            new_z = getattr(e, "wz", None)

            if new_x is None or new_y is None or new_z is None:
                # Fallback to local coordinates
                new_x = e.x if e.x is not None else 0.0
                new_y = e.y if e.y is not None else 0.0
                new_z = e.z if e.z is not None else 0.0

            # Use absolute world coordinates
            if self._tcp_cartesian_move_callback:
                # Reuse pre-allocated buffer
                buf = self._pose_mm_buffer
                buf[0] = float(new_x) * 1000.0  # x: m -> mm
                buf[1] = float(new_y) * 1000.0  # y: m -> mm
                buf[2] = float(new_z) * 1000.0  # z: m -> mm
                # Use rotation from drag START to avoid rotation during translation
                if self._tcp_drag_start_rot_deg is not None:
                    buf[3] = self._tcp_drag_start_rot_deg[0]
                    buf[4] = self._tcp_drag_start_rot_deg[1]
                    buf[5] = self._tcp_drag_start_rot_deg[2]
                else:
                    buf[3] = robot_state.rx
                    buf[4] = robot_state.ry
                    buf[5] = robot_state.rz
                self._tcp_cartesian_move_callback(buf)

        else:  # rotate mode
            # Use absolute rotation values
            if self._tcp_cartesian_move_callback:
                # Get rotation values (radians from event)
                rx = e.rx if e.rx is not None else 0.0
                ry = e.ry if e.ry is not None else 0.0
                rz = e.rz if e.rz is not None else 0.0

                # Reuse pre-allocated buffer
                buf = self._pose_mm_buffer
                buf[0] = robot_state.x  # Keep current position (already in mm)
                buf[1] = robot_state.y
                buf[2] = robot_state.z
                buf[3] = math.degrees(rx)  # rx: rad -> deg
                buf[4] = math.degrees(ry)  # ry: rad -> deg
                buf[5] = math.degrees(rz)  # rz: rad -> deg
                self._tcp_cartesian_move_callback(buf)

    def _handle_tcp_transform_for_ik(self, e) -> None:
        """Handle TCP ball drag in EDITING mode - solve IK for position and/or orientation."""
        # Ensure FK/IK solver is initialized
        if not self._ensure_ik_solver():
            return
        assert self._ik_solver is not None

        target_orientation = None
        target_pos = self._target_pos_buffer

        if self._tcp_transform_mode == "rotate":
            # Rotation mode: get orientation from event, keep current position
            rx = e.rx if e.rx is not None else 0.0
            ry = e.ry if e.ry is not None else 0.0
            rz = e.rz if e.rz is not None else 0.0

            # Fill pre-allocated orientation buffer
            orient_buf = self._target_orientation_buffer
            orient_buf[0] = float(rx)
            orient_buf[1] = float(ry)
            orient_buf[2] = float(rz)
            target_orientation = orient_buf

            # Store edited rotation for reference
            self._editing_rotation[0] = float(rx)
            self._editing_rotation[1] = float(ry)
            self._editing_rotation[2] = float(rz)
            self._editing_rotation_set = True

            # Get current position from FK (maintain position while rotating)
            fk_result = self._ik_solver.forward_kinematics(self._editing_angles)
            target_pos[0] = fk_result[0]
            target_pos[1] = fk_result[1]
            target_pos[2] = fk_result[2]
        else:
            # Translate mode: get position from event
            new_x = getattr(e, "wx", None)
            new_y = getattr(e, "wy", None)
            new_z = getattr(e, "wz", None)

            if new_x is None or new_y is None or new_z is None:
                # Fallback to local coordinates
                new_x = e.x if e.x is not None else 0.0
                new_y = e.y if e.y is not None else 0.0
                new_z = e.z if e.z is not None else 0.0

            target_pos[0] = float(new_x)
            target_pos[1] = float(new_y)
            target_pos[2] = float(new_z)

            # If we have a stored rotation from previous rotate mode, use it
            if self._editing_rotation_set:
                orient_buf = self._target_orientation_buffer
                orient_buf[0] = self._editing_rotation[0]
                orient_buf[1] = self._editing_rotation[1]
                orient_buf[2] = self._editing_rotation[2]
                target_orientation = orient_buf

        # Solve IK with throttling (~30Hz)
        result = self._ik_solver.solve(
            target_pos=target_pos,
            current_angles=self._editing_angles,
            throttle=True,
            target_orientation=target_orientation,
        )

        # If throttled (returns None), skip this frame
        if result is None:
            return

        if result.success:
            # Update editing angles without repositioning TCP ball (user is dragging it)
            n = len(self.joint_names)
            self._editing_angles[:n] = result.angles[:n]
            # Apply to robot joints
            for joint_name, q in zip(self.joint_names, self._editing_angles):
                if joint_name in self.joint_groups and joint_name in self.joint_trafos:
                    t, r = self.joint_trafos[joint_name](q)
                    self.joint_groups[joint_name].move(*t).rotate(*r)
            # Sync robot_state so readouts and control panel update
            self._sync_robot_state_from_editing()
            # Update edit bar delta values
            if self._current_editing_type:
                self._update_edit_bar_values(self._current_editing_type)
