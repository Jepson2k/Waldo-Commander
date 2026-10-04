"""
TCP Controls Mixin for UrdfScene.

Provides TCP TransformControls functionality:
- Show/remove the TCP ball with its TransformControls (the gizmo)
- Handle transform events for direct Cartesian moves (jogging) or IK (editing)
- Mode switching (translate/rotate)

The ball sits at the origin of ``tcp:ball_frame``, a group placed on the TCP
pose. TransformControls snaps to a lattice in the ball's parent frame, so with
the frame on the TCP the snapped positions are whole steps from where the drag
began, along the tool's axes; the ball's pose in the frame is the drag's delta.

The TCP ball behavior depends on RobotAppearanceMode:
- LIVE/SIMULATOR: Streams Cartesian jog moves to backend
- EDITING: Solves IK for target positioning
"""

import asyncio
import logging
import math
from typing import Any, Callable

import numpy as np
import waldoctl
from nicegui import ui
from nicegui.helpers import is_user_simulation
from pinokin import arrays_equal_n, so3_from_rpy, so3_rpy

from waldo_commander.common.theme import SceneColors, linear_rgb

from .config import RobotAppearanceMode
from .ik_solver import EditingIKSolver
from .jog_handles_mixin import GIZMO, HANDLE_LABEL_STYLE, signed
from .scene_fx import SceneFx
from .snap import SceneSnap

logger = logging.getLogger(__name__)

_IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
_GIZMO_TICKS_EACH_SIDE = 10
_GIZMO_TICK_RING_M = 0.06
_GIZMO_LABEL_LIFT_M = 0.03
# A release nearer its frame than this needs no spring back.
_SPRING_MIN_M = 0.001


def _local_angle(R: np.ndarray, axis: str) -> float:
    """Signed angle of a rotation about one of its own axes, from its matrix."""
    if axis == "X":
        return math.atan2(R[2, 1], R[1, 1])
    if axis == "Y":
        return math.atan2(R[0, 2], R[2, 2])
    return math.atan2(R[1, 0], R[0, 0])


class TCPControlsMixin:
    """Mixin providing TCP TransformControls functionality for UrdfScene."""

    # Defined in the main UrdfScene class.
    scene: Any
    _appearance_mode: RobotAppearanceMode
    _editing_angles: list[float]
    joint_groups: dict
    joint_trafos: dict
    joint_names: list[str]

    # Provided by other mixins; declared here for type checking.
    def _sync_robot_state_from_editing(self) -> None: ...

    def _update_edit_bar_values(self, editing_type: str) -> None: ...

    def _update_collision_highlight(self) -> None: ...

    _current_editing_type: str | None
    # Defined later in the MRO (JogHandlesMixin), so annotations only: a stub
    # here would shadow the real method.
    snap: SceneSnap
    _watch_hover: Callable[[Any, str], None]
    _apply_joint_angles: Callable[[list[float]], None]

    @property
    def tcp_transform_mode(self) -> str:
        """Current TCP transform mode ('translate' or 'rotate')."""
        return self._tcp_transform_mode

    def _init_tcp_controls_state(self) -> None:
        """Initialize TCP controls state variables."""
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
        self._tcp_ball_frame: Any | None = None
        self._tcp_ball_dragging: bool = False
        # The FK pose the frame was last placed at (identity-compared).
        self._tcp_placed_pose: tuple[float, ...] | None = None
        self._tcp_frame_p: np.ndarray = np.zeros(3, dtype=np.float64)
        self._tcp_frame_R: np.ndarray = np.eye(3, dtype=np.float64)
        self._tcp_local_p: np.ndarray = np.zeros(3, dtype=np.float64)
        self._tcp_local_R: np.ndarray = np.eye(3, dtype=np.float64)
        self._tcp_world_p: np.ndarray = np.zeros(3, dtype=np.float64)
        self._tcp_world_R: np.ndarray = np.eye(3, dtype=np.float64)
        self._tcp_world_rpy: np.ndarray = np.zeros(3, dtype=np.float64)
        # Drag marks: ticks along the dragged axis and the delta label.
        self._tcp_drag_mode: str = "translate"
        self._tcp_drag_axis: str | None = None
        self._tcp_ticks: Any | None = None
        self._tcp_label: Any | None = None
        # What the label shows, in tenths of its unit plus the step, so an
        # unchanged reading is not reformatted.
        self._tcp_label_key: list[float] = [math.nan] * 4
        # Whether the drag's latest IK solve failed, leaving the ball where
        # the arm cannot follow.
        self._tcp_ik_missed: bool = False
        # World point where the latest drag let go of the ball.
        self._tcp_release_p: np.ndarray | None = None
        self._ik_solver: EditingIKSolver | None = None
        self._editing_rotation: list[float] = [0.0, 0.0, 0.0]
        self._editing_rotation_set: bool = False

        # Pre-allocated buffers to avoid per-call allocations.
        self._target_pos_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
        self._target_orientation_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
        self._pose_mm_buffer: list[float] = [0.0] * 6

        # FK dirty-checking cache: skip FK when angles are unchanged.
        self._last_fk_angles_tuple: tuple[float, ...] | None = None
        self._last_fk_angles_raw: np.ndarray | None = (
            None  # For fast LIVE mode comparison
        )
        self._last_fk_pose: tuple[float, ...] | None = None

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
        """Show the TCP gizmo (the ball and its TransformControls), or remove it.

        Removed rather than hidden: the scene's raycaster hits invisible objects.
        """
        if visible:
            self._ensure_tcp_ball()
            self.enable_tcp_transform_controls(self._tcp_transform_mode)
        else:
            self.disable_tcp_transform_controls()

    def set_gizmo_display_mode(self, mode: str) -> None:
        """Toggle gizmo display between translation and rotation modes.

        Args:
            mode: Either "TRANSLATE" for translation or "ROTATE" for rotation
        """
        mode = (mode or "").upper()
        if mode not in ("TRANSLATE", "ROTATE"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'TRANSLATE' or 'ROTATE'.")

        self._tcp_transform_mode = "translate" if mode == "TRANSLATE" else "rotate"

        if self._tcp_transform_enabled:
            self.set_tcp_transform_mode(self._tcp_transform_mode)

    def enable_tcp_transform_controls(self, mode: str = "translate") -> None:
        """Enable TransformControls on the TCP anchor for Cartesian jogging.

        Args:
            mode: "translate" or "rotate"
        """
        if self._tcp_transform_enabled or self._tcp_enable_in_progress:
            return

        if not self.scene:
            logger.warning("Cannot enable TCP transform controls: scene not available")
            return
        self._ensure_tcp_ball()
        if not self._tcp_ball:
            logger.warning(
                "Cannot enable TCP transform controls: jog ball not available"
            )
            return

        self._tcp_enable_in_progress = True
        self._tcp_transform_mode = mode.lower()

        # Sync jog ball to current TCP via FK before enabling, like joint edit.
        self._update_jog_ball_from_robot_state()

        # No browser answers the attach probe under user simulation, so the
        # request is taken as done.
        if is_user_simulation():
            self._tcp_ball.enable_transform_controls(
                mode=self._tcp_transform_mode,
                size=0.8,
                space="local",
                rotation_snap=math.radians(self.snap.joint_deg),
                translation_snap=self.snap.cart_mm / 1000.0,
            )
            self._tcp_transform_enabled = True
            self._tcp_enable_in_progress = False
            return

        # Retry enablement until the JS object exists. The per-object wrapper dispatches
        # `scene.run_method('enable_transform_controls', id, ...)` once; if the JS side hasn't
        # received the init_objects payload yet, the call silently no-ops, and
        # `has_transform_controls` on the scene is the only way to confirm attach succeeded.
        # A wall-clock deadline bounds the budget: the first probe absorbs page-load latency (up
        # to the full deadline); later probes clamp to 1s so a stuck JS side can't tie up the
        # enablement guard for the full 5s x N times.
        async def _enable_with_retry():
            ball = None
            try:
                loop = asyncio.get_event_loop()
                deadline = loop.time() + 5.0
                first = True
                while loop.time() < deadline:
                    # Read every pass: a hover can replace the ball meanwhile.
                    ball = self._tcp_ball
                    if ball is None:
                        return
                    ball.enable_transform_controls(
                        mode=self._tcp_transform_mode,
                        size=0.8,
                        space="local",
                        rotation_snap=math.radians(self.snap.joint_deg),
                        translation_snap=self.snap.cart_mm / 1000.0,
                    )
                    await asyncio.sleep(0.05)
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    probe_timeout = remaining if first else min(remaining, 1.0)
                    first = False
                    ok = await self.scene.run_method(
                        "has_transform_controls", str(ball.id), timeout=probe_timeout
                    )
                    if ok and ball is self._tcp_ball:
                        self._tcp_transform_enabled = True
                        logger.debug("Enabled TCP TransformControls in %s mode", mode)
                        return
                logger.warning("Failed to enable TCP TransformControls within 5s")
            except (TimeoutError, asyncio.CancelledError):
                logger.debug("TCP TransformControls enablement cancelled (shutdown)")
            finally:
                self._tcp_enable_in_progress = False
                if (
                    self._tcp_ball is not None
                    and self._tcp_ball is not ball
                    and not self._tcp_transform_enabled
                ):
                    self.enable_tcp_transform_controls(self._tcp_transform_mode)

        with self.scene:
            ui.timer(0.0, _enable_with_retry, once=True)

    def disable_tcp_transform_controls(self) -> None:
        """Remove the TCP ball; its TransformControls go with it."""
        frame = self._tcp_ball_frame
        if frame is None:
            return
        if self._tcp_ball_dragging:
            self._abandon_tcp_drag()
        self._tcp_ball = None
        self._tcp_ball_frame = None
        self._tcp_ticks = None
        self._tcp_label = None
        self._tcp_placed_pose = None
        self._tcp_transform_enabled = False
        frame.delete()
        logger.debug("Removed the TCP gizmo")

    def _abandon_tcp_drag(self) -> None:
        """End a drag whose gizmo is going away, as its release would have."""
        self._tcp_ball_dragging = False
        self._tcp_drag_start_rot_deg = None
        if self.scene:
            self.scene.set_orbit_enabled(True)
        if self._appearance_mode != RobotAppearanceMode.EDITING:
            cb = self._tcp_cartesian_move_end_callback
            if cb is not None:
                cb()

    def _apply_gizmo_snap(self) -> None:
        """Push the scene's snap step to the gizmo, and redraw a drag's ticks."""
        ball = self._tcp_ball
        if ball is None or not self._tcp_transform_enabled:
            return
        ball.set_transform_translation_snap(self.snap.cart_mm / 1000.0)
        ball.set_transform_rotation_snap(math.radians(self.snap.joint_deg))
        if self._tcp_ball_dragging:
            self._draw_gizmo_ticks()
            self._update_gizmo_label()

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

        # Sync ball pose from FK first so rotate mode starts from the correct orientation.
        self._update_tcp_ball_position()

        self._tcp_ball.set_transform_mode(self._tcp_transform_mode)

        logger.debug("Changed TCP TransformControls mode to %s", mode)

    def _ensure_tcp_ball(self) -> None:
        """Create the TCP ball in its frame if missing.

        The TCP ball is used for both jogging (LIVE/SIMULATOR) and IK target editing (EDITING).
        """
        if not self.scene:
            return
        if self._tcp_ball:
            return
        with self.scene:
            frame = ui.scene.group().with_name("tcp:ball_frame")
            with frame:
                ball = ui.scene.sphere(
                    radius=0.008,
                    width_segments=16,
                    height_segments=16,
                    wireframe=False,
                ).with_name("tcp:ball")
                ball.material(SceneColors.EDIT_GRAY_HEX, 0.9)
        # A pointer on the TransformControls handles counts as on the ball
        # only while the ball has pointer handlers.
        self._watch_hover(ball, GIZMO)
        self._tcp_ball_frame = frame
        self._tcp_ball = ball
        self._tcp_placed_pose = None
        self._update_tcp_ball_position()

    def _snap_tcp_to_fk(self) -> None:
        """Snap TCP ball back to FK position from current editing angles.

        Called on transform_end in editing mode to correct the ball
        position when IK failed and the ball was dragged to an
        unreachable position.
        """
        self.invalidate_fk_cache()
        self._update_tcp_ball_position()

    def refresh_tcp_ball(self) -> None:
        """Public wrapper around ``_update_tcp_ball_position`` for external
        callers that need to re-position the TCP ball after a tool swap."""
        self._update_tcp_ball_position()

    def _update_tcp_ball_position(self) -> None:
        """Place the ball's frame on the TCP pose from FK.

        Recomputes FK only when joint angles change, and moves the frame only
        when FK produced a pose it has not been placed at. The frame holds
        still during a drag, since the drag's delta is measured from it.
        """
        if self._tcp_ball_dragging or self._tcp_ball_frame is None:
            return

        n = len(self.joint_names)
        angles_changed = False
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            angles_rad: list[float] | np.ndarray = self._editing_angles
            key = tuple(angles_rad[:n])
            if key != self._last_fk_angles_tuple:
                self._last_fk_angles_tuple = key
                angles_changed = True
        else:
            angles_deg = waldoctl.commander.status.joints.angles.deg
            if self._last_fk_angles_raw is None or not arrays_equal_n(
                angles_deg[:n], self._last_fk_angles_raw
            ):
                self._last_fk_angles_raw = angles_deg[:n].copy()
                angles_rad = waldoctl.commander.status.joints.angles.rad
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

        p = self._last_fk_pose
        if p is None or p is self._tcp_placed_pose:
            return
        self._tcp_placed_pose = p
        self._tcp_frame_p[0] = p[0]
        self._tcp_frame_p[1] = p[1]
        self._tcp_frame_p[2] = p[2]
        so3_from_rpy(p[3], p[4], p[5], self._tcp_frame_R)
        self._tcp_ball_frame.move(p[0], p[1], p[2])
        self._tcp_ball_frame.rotate_R(self._tcp_frame_R.tolist())

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
        if (
            not self._tcp_transform_enabled
            or self._tcp_ball is None
            or e.object_id != self._tcp_ball.id
        ):
            return

        object_name = getattr(e, "object_name", "") or ""
        if object_name != "tcp:ball":
            return

        self._compose_tcp_pose(e)
        self._update_gizmo_label()
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            self._handle_tcp_transform_for_ik(e)
        else:
            self._handle_tcp_transform_for_cartesian(e)

    def _compose_tcp_pose(self, e) -> None:
        """World pose of the ball from its pose in the TCP frame.

        Position into ``_tcp_world_p`` (m); orientation ``R_frame · R_local``
        into ``_tcp_world_R`` and, as intrinsic XYZ, ``_tcp_world_rpy`` (rad).
        """
        lp = self._tcp_local_p
        lp[0] = e.x
        lp[1] = e.y
        lp[2] = e.z
        np.dot(self._tcp_frame_R, lp, out=self._tcp_world_p)
        self._tcp_world_p += self._tcp_frame_p
        so3_from_rpy(float(e.rx), float(e.ry), float(e.rz), self._tcp_local_R)
        np.matmul(self._tcp_frame_R, self._tcp_local_R, out=self._tcp_world_R)
        so3_rpy(self._tcp_world_R, self._tcp_world_rpy)

    def _handle_tcp_transform_for_cartesian(self, e) -> None:
        """Handle TCP ball drag in LIVE/SIMULATOR mode - stream Cartesian moves."""
        if self._tcp_transform_mode == "translate":
            if self._tcp_cartesian_move_callback:
                buf = self._pose_mm_buffer
                buf[0] = self._tcp_world_p[0] * 1000.0  # m -> mm
                buf[1] = self._tcp_world_p[1] * 1000.0
                buf[2] = self._tcp_world_p[2] * 1000.0
                # Hold rotation from drag start so translation doesn't also rotate.
                if self._tcp_drag_start_rot_deg is not None:
                    buf[3] = self._tcp_drag_start_rot_deg[0]
                    buf[4] = self._tcp_drag_start_rot_deg[1]
                    buf[5] = self._tcp_drag_start_rot_deg[2]
                else:
                    buf[3] = waldoctl.commander.status.pose.rx
                    buf[4] = waldoctl.commander.status.pose.ry
                    buf[5] = waldoctl.commander.status.pose.rz
                self._tcp_cartesian_move_callback(buf)

        else:  # rotate mode
            if self._tcp_cartesian_move_callback:
                buf = self._pose_mm_buffer
                buf[0] = (
                    waldoctl.commander.status.pose.x
                )  # keep position, already in mm
                buf[1] = waldoctl.commander.status.pose.y
                buf[2] = waldoctl.commander.status.pose.z
                buf[3] = math.degrees(self._tcp_world_rpy[0])
                buf[4] = math.degrees(self._tcp_world_rpy[1])
                buf[5] = math.degrees(self._tcp_world_rpy[2])
                self._tcp_cartesian_move_callback(buf)

    def _handle_tcp_transform_for_ik(self, e) -> None:
        """Handle TCP ball drag in EDITING mode - solve IK for position and/or orientation."""
        if not self._ensure_ik_solver():
            return
        assert self._ik_solver is not None

        target_orientation = None
        target_pos = self._target_pos_buffer

        if self._tcp_transform_mode == "rotate":
            rpy = self._tcp_world_rpy
            orient_buf = self._target_orientation_buffer
            orient_buf[0] = rpy[0]
            orient_buf[1] = rpy[1]
            orient_buf[2] = rpy[2]
            target_orientation = orient_buf

            self._editing_rotation[0] = float(rpy[0])
            self._editing_rotation[1] = float(rpy[1])
            self._editing_rotation[2] = float(rpy[2])
            self._editing_rotation_set = True

            # Hold the current FK position so rotating doesn't translate the TCP.
            fk_result = self._ik_solver.forward_kinematics(self._editing_angles)
            target_pos[0] = fk_result[0]
            target_pos[1] = fk_result[1]
            target_pos[2] = fk_result[2]
        else:
            target_pos[0] = self._tcp_world_p[0]
            target_pos[1] = self._tcp_world_p[1]
            target_pos[2] = self._tcp_world_p[2]

            # Carry forward any orientation set in a prior rotate-mode drag.
            if self._editing_rotation_set:
                orient_buf = self._target_orientation_buffer
                orient_buf[0] = self._editing_rotation[0]
                orient_buf[1] = self._editing_rotation[1]
                orient_buf[2] = self._editing_rotation[2]
                target_orientation = orient_buf

        # Throttled to ~30Hz; returns None when this frame is skipped.
        result = self._ik_solver.solve(
            target_pos=target_pos,
            current_angles=self._editing_angles,
            throttle=True,
            target_orientation=target_orientation,
        )

        if result is None:
            return

        self._tcp_ik_missed = not result.success
        if result.success:
            # Update angles without repositioning the TCP ball; the user is dragging it.
            n = len(self.joint_names)
            self._editing_angles[:n] = result.angles[:n]
            self._apply_joint_angles(self._editing_angles)
            self._sync_robot_state_from_editing()
            self._update_collision_highlight()
            if self._current_editing_type:
                self._update_edit_bar_values(self._current_editing_type)

    # ---- Drag marks ----

    def _begin_gizmo_marks(self, e) -> None:
        """Ticks along the dragged axis and a delta label, from grab to release."""
        self._tcp_drag_mode = e.mode
        self._tcp_drag_axis = e.axis
        self._tcp_local_p[:] = 0.0
        self._tcp_local_R[:] = np.eye(3)
        self._tcp_label_key[:] = [math.nan] * 4
        self._tcp_ik_missed = False
        ball = self._tcp_ball
        if ball is None or not self.scene:
            return
        self._draw_gizmo_ticks()
        with self.scene, ball:
            self._tcp_label = (
                ui.scene.text("", HANDLE_LABEL_STYLE)
                .with_name("tcp:label")
                .move(0.0, 0.0, _GIZMO_LABEL_LIFT_M)
            )
        self._update_gizmo_label()

    def _end_gizmo_marks(self, e) -> None:
        """Drop the marks and put the ball back on its frame's origin."""
        ball = self._tcp_ball
        if ball is None or e.object_id != ball.id:
            return
        for mark in (self._tcp_ticks, self._tcp_label):
            if mark is not None:
                mark.delete()
        self._tcp_ticks = None
        self._tcp_label = None
        self._tcp_drag_axis = None
        ball.move(0.0, 0.0, 0.0)
        ball.rotate_R(_IDENTITY)
        self._tcp_release_p = self._tcp_frame_p + self._tcp_frame_R @ np.array(
            (e.x, e.y, e.z), dtype=np.float64
        )

    def _spring_tcp_ball(self) -> None:
        """Spring the ball from where the drag let go of it onto its frame,
        which may have moved on release, tinted when the drag asked for a
        pose the arm could not reach."""
        release = self._tcp_release_p
        ball = self._tcp_ball
        self._tcp_release_p = None
        if release is None or ball is None or not self.scene:
            return
        self._update_tcp_ball_position()
        local = self._tcp_frame_R.T @ (release - self._tcp_frame_p)
        if float(np.linalg.norm(local)) <= _SPRING_MIN_M:
            return
        SceneFx.spring_back(
            self.scene,
            ball,
            (float(local[0]), float(local[1]), float(local[2])),
            SceneColors.COLLISION_HEX if self._tcp_ik_missed else None,
        )

    def _draw_gizmo_ticks(self) -> None:
        """Dots at whole steps along the dragged axis (or around it, rotating), in the TCP frame."""
        frame = self._tcp_ball_frame
        if self._tcp_ticks is not None:
            self._tcp_ticks.delete()
            self._tcp_ticks = None
        axis = self._tcp_drag_axis or ""
        n = _GIZMO_TICKS_EACH_SIDE
        points: list[list[float]] = []
        colors: list[list[float]] = []
        size = 0.003
        if self._tcp_drag_mode == "translate":
            step = self.snap.cart_mm / 1000.0
            size = max(0.0008, min(0.003, 0.4 * step))
            for letter in axis:
                i = "XYZ".find(letter)
                if i < 0:
                    continue
                color = linear_rgb(f"axis-{letter.lower()}")
                for k in range(-n, n + 1):
                    point = [0.0, 0.0, 0.0]
                    point[i] = k * step
                    points.append(point)
                    colors.append(color)
        elif self._tcp_drag_mode == "rotate" and axis in ("X", "Y", "Z"):
            step = math.radians(self.snap.joint_deg)
            r = _GIZMO_TICK_RING_M
            size = max(0.0008, min(0.003, 0.4 * r * step))
            i = "XYZ".index(axis)
            a, b = (i + 1) % 3, (i + 2) % 3
            color = linear_rgb(f"axis-{axis.lower()}")
            for k in range(-n, n + 1):
                point = [0.0, 0.0, 0.0]
                point[a] = r * math.cos(k * step)
                point[b] = r * math.sin(k * step)
                points.append(point)
                colors.append(color)
        if frame is None or not points or not self.scene:
            return
        with self.scene, frame:
            self._tcp_ticks = ui.scene.point_cloud(
                points, colors, point_size=size
            ).with_name("tcp:ticks")
        self._tcp_label_key[3] = math.nan

    def _update_gizmo_label(self) -> None:
        """Show the drag's delta in the tool frame and the step, when the shown reading changes."""
        label = self._tcp_label
        if label is None:
            return
        axis = self._tcp_drag_axis or ""
        key = self._tcp_label_key
        if self._tcp_drag_mode == "translate":
            step = self.snap.cart_mm
            k0 = round(self._tcp_local_p[0] * 1e4)
            k1 = round(self._tcp_local_p[1] * 1e4)
            k2 = round(self._tcp_local_p[2] * 1e4)
        else:
            step = self.snap.joint_deg
            if axis in ("X", "Y", "Z"):
                angle = _local_angle(self._tcp_local_R, axis)
            else:
                trace = float(np.trace(self._tcp_local_R))
                angle = math.acos(min(1.0, max(-1.0, (trace - 1.0) / 2.0)))
            k0, k1, k2 = round(math.degrees(angle) * 10), 0, 0
        if key[0] == k0 and key[1] == k1 and key[2] == k2 and key[3] == step:
            return
        key[0], key[1], key[2], key[3] = k0, k1, k2, step
        if self._tcp_drag_mode == "translate":
            letters = "".join(c for c in axis if c in "XYZ")
            deltas = " ".join(
                signed(self._tcp_local_p["XYZ".index(c)] * 1000.0) for c in letters
            )
            text = f"Tool {letters}  Δ{deltas} mm  step {step:g} mm"
        elif axis in ("X", "Y", "Z"):
            text = f"Tool R{axis}  Δ{signed(k0 / 10.0)}°  step {step:g}°"
        else:
            text = f"Tool  Δ{k0 / 10.0:.1f}°  step {step:g}°"
        label.set_text(text)
