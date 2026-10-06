"""The TCP gizmo: where it sits, and what its drags do.

The browser draws the gizmo on a frame the app places on the TCP from its
forward kinematics, each placement numbered, and drags the ball within that
frame. A drag reports the ball's pose in the frame, so the delta is in the
tool's axes.

The app admits a drag only if the frame the browser showed is where the TCP
is now; the drag's poses are then taken from the app's own frame. While live
or simulating, a drag streams Cartesian jogs; while editing a target, it
solves IK, at most 30 times a second, always for the last pose.
"""

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import waldoctl
from pinokin import arrays_equal_n, so3_from_rpy, so3_rpy

from waldo_commander.scene3d.interaction import Gesture, finite
from waldo_commander.state import robot_state

from .config import RobotAppearanceMode
from .ik_solver import EditingIKSolver

logger = logging.getLogger(__name__)

#: How far the frame a browser showed may be from the TCP for its drag to be taken.
_SHOWN_FRAME_MM = 2.0
_SHOWN_FRAME_DEG = 1.0
_IK_INTERVAL_S = 1 / 30


class TCPControlsMixin:
    """Mixin placing the TCP gizmo and handling its drags for UrdfScene."""

    # Defined in the main UrdfScene class.
    scene: Any
    _appearance_mode: RobotAppearanceMode
    _editing_angles: list[float]
    joint_names: list[str]
    _handles_available: bool
    _hover_suspended: bool

    def _sync_robot_state_from_editing(self) -> None: ...

    def _update_edit_bar_values(self, editing_type: str) -> None: ...

    def _update_collision_highlight(self) -> None: ...

    _current_editing_type: str | None
    _gizmo_mode: str
    # Defined later in the MRO (JogHandlesMixin), so annotations only: a stub
    # here would shadow the real method.
    _apply_joint_angles: Callable[[list[float]], None]
    _rules_changed: Callable[[], None]

    def _init_tcp_controls_state(self) -> None:
        self._tcp_cartesian_move_callback: Callable[[list[float]], None] | None = None
        self._tcp_cartesian_move_start_callback: Callable[[], int | None] | None = None
        self._tcp_cartesian_move_end_callback: Callable[[int], None] | None = None
        self._tcp_cartesian_move_cancel_callback: Callable[[int], None] | None = None
        self._tcp_drag: int | None = None
        self._tcp_drag_mode = "translate"
        self._tcp_drag_start_rot_deg: tuple[float, float, float] | None = None
        # The FK pose the frame was last placed at (identity-compared).
        self._tcp_placed_pose: tuple[float, ...] | None = None
        self._tcp_frame_p: np.ndarray = np.zeros(3, dtype=np.float64)
        self._tcp_frame_R: np.ndarray = np.eye(3, dtype=np.float64)
        self._tcp_local_p: np.ndarray = np.zeros(3, dtype=np.float64)
        self._tcp_local_R: np.ndarray = np.eye(3, dtype=np.float64)
        self._tcp_world_p: np.ndarray = np.zeros(3, dtype=np.float64)
        self._tcp_world_R: np.ndarray = np.eye(3, dtype=np.float64)
        self._tcp_world_rpy: np.ndarray = np.zeros(3, dtype=np.float64)
        self._placed_R: np.ndarray = np.eye(3, dtype=np.float64)
        # Whether the drag's latest IK solve failed, leaving the ball where
        # the arm cannot follow.
        self._tcp_ik_missed: bool = False
        self._ik_solver: EditingIKSolver | None = None
        self._ik_handle: Any = None
        self._ik_last = 0.0
        self._editing_rotation: list[float] = [0.0, 0.0, 0.0]
        self._editing_rotation_set: bool = False

        # Pre-allocated buffers to avoid per-call allocations.
        self._target_pos_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
        self._target_orientation_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
        self._pose_mm_buffer: list[float] = [0.0] * 6

        # FK dirty-checking cache: skip FK when angles are unchanged.
        self._last_fk_angles_tuple: tuple[float, ...] | None = None
        self._last_fk_angles_raw: np.ndarray | None = None
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
        """Force the gizmo's FK to be recomputed on the next update."""
        self._last_fk_angles_tuple = None
        self._last_fk_angles_raw = None

    def on_tcp_cartesian_move(self, callback: Callable[[list[float]], None]) -> None:
        """Register the sink for a gizmo drag's TCP targets, as
        ``[x_mm, y_mm, z_mm, rx_deg, ry_deg, rz_deg]``."""
        self._tcp_cartesian_move_callback = callback

    def on_tcp_cartesian_move_start(self, callback: Callable[[], int | None]) -> None:
        """Register the callback that starts a gizmo drag: it returns the
        drag's token, or None when the drag may not move the robot."""
        self._tcp_cartesian_move_start_callback = callback

    def on_tcp_cartesian_move_end(self, callback: Callable[[int], None]) -> None:
        """Register the callback for a gizmo drag released, given its token."""
        self._tcp_cartesian_move_end_callback = callback

    def on_tcp_cartesian_move_cancel(self, callback: Callable[[int], None]) -> None:
        """Register the callback for a gizmo drag that ended other than by its release."""
        self._tcp_cartesian_move_cancel_callback = callback

    def set_gizmo_display_mode(self, mode: str) -> None:
        """Toggle the gizmo between translation ("TRANSLATE") and rotation ("ROTATE")."""
        mode = (mode or "").upper()
        if mode not in ("TRANSLATE", "ROTATE"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'TRANSLATE' or 'ROTATE'.")
        self._gizmo_mode = "translate" if mode == "TRANSLATE" else "rotate"
        self._rules_changed()

    def _gizmo_shown(self) -> bool:
        """Whether the gizmo can be on screen now, so its frame needs placing."""
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            return True
        return (
            self._handles_available
            and not self._hover_suspended
            and bool(waldoctl.commander.settings.view.gizmo_visible)
        )

    def refresh_tcp_pose(self) -> None:
        """Place the gizmo's frame again, e.g. after the tool changed."""
        self.invalidate_fk_cache()
        self._push_tcp_pose(force=True)

    def _push_tcp_pose(self, force: bool = False) -> None:
        """Place the gizmo's frame on the TCP from FK.

        Recomputes FK only when joint angles change, and sends a placement
        only when FK produced a pose the frame has not been placed at.
        """
        if self.scene is None or not (force or self._gizmo_shown()):
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
        if p is None or (p is self._tcp_placed_pose and not force):
            return
        self._tcp_placed_pose = p
        so3_from_rpy(p[3], p[4], p[5], self._placed_R)
        self.scene.set_tcp(p[:3], self._placed_R.tolist())

    # ---- Drags ----

    def _tcp_admit(self, gesture: Gesture, args: dict[str, Any]) -> bool:
        mode = args.get("mode")
        if mode not in ("translate", "rotate") or not self._gizmo_shown():
            return False
        # The frame the drag's delta is measured from must be the one the
        # browser showed: a display behind the arm would aim it backwards.
        shown = self.scene.tcp_at(args.get("rev"))
        placed = self._tcp_placed_pose
        if shown is None or placed is None or not self._frame_matches(shown, placed):
            self._push_tcp_pose(force=True)
            return False
        self._tcp_frame_p[:] = placed[:3]
        self._tcp_frame_R[:] = self._placed_R
        self._tcp_drag_mode = mode
        self._tcp_ik_missed = False
        self.scene.set_gizmo_miss(False)
        if self._appearance_mode != RobotAppearanceMode.EDITING:
            start = self._tcp_cartesian_move_start_callback
            token = start() if start is not None else None
            if token is None:
                return False
            gesture.data["token"] = token
            self._tcp_drag_start_rot_deg = tuple(robot_state.orientation.deg)
        self._tcp_drag = gesture.drag
        return True

    @staticmethod
    def _frame_matches(
        shown: tuple[list[float], list[list[float]]], placed: tuple[float, ...]
    ) -> bool:
        p, R = shown
        if np.linalg.norm(np.subtract(p, placed[:3])) * 1000 > _SHOWN_FRAME_MM:
            return False
        now = np.empty((3, 3))
        so3_from_rpy(placed[3], placed[4], placed[5], now)
        c = (float(np.trace(np.asarray(R).T @ now)) - 1.0) / 2.0
        return math.degrees(math.acos(min(1.0, max(-1.0, c)))) <= _SHOWN_FRAME_DEG

    def _tcp_move(self, gesture: Gesture, args: dict[str, Any]) -> None:
        pose = finite(args, "x", "y", "z", "rx", "ry", "rz")
        if pose is None or gesture.drag != self._tcp_drag:
            return
        self._compose_tcp_pose(pose)
        if "token" in gesture.data:
            self._handle_tcp_transform_for_cartesian()
        elif self._appearance_mode == RobotAppearanceMode.EDITING:
            self._queue_ik()

    def _tcp_finish(
        self, gesture: Gesture, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        if gesture.drag != self._tcp_drag:
            return
        self._tcp_drag = None
        self._tcp_drag_start_rot_deg = None
        if self._ik_handle is not None:
            self._ik_handle.cancel()
            self._ik_handle = None
        pose = (
            None
            if aborted or args is None
            else finite(args, "x", "y", "z", "rx", "ry", "rz")
        )
        # A drag is a target edit or a jog for its whole life, whatever the
        # mode has become since.
        token = gesture.data.get("token")
        if token is None:
            if (
                pose is not None
                and self._appearance_mode == RobotAppearanceMode.EDITING
            ):
                self._compose_tcp_pose(pose)
                self._handle_tcp_transform_for_ik()
            self.scene.set_gizmo_miss(self._tcp_ik_missed)
            self._push_tcp_pose(force=True)
            return
        if pose is None:
            cancel = self._tcp_cartesian_move_cancel_callback
            if cancel is not None:
                cancel(token)
            return
        self._compose_tcp_pose(pose)
        self._handle_tcp_transform_for_cartesian()
        end = self._tcp_cartesian_move_end_callback
        if end is not None:
            end(token)

    def _queue_ik(self) -> None:
        """Solve for the latest pose at most 30 times a second."""
        if self._ik_handle is not None:
            return
        loop = self.scene._loop
        delay = max(0.0, self._ik_last + _IK_INTERVAL_S - time.monotonic())
        self._ik_handle = loop.call_later(delay, self._run_queued_ik)

    def _run_queued_ik(self) -> None:
        self._ik_handle = None
        if self._tcp_drag is not None:
            self._handle_tcp_transform_for_ik()

    def _compose_tcp_pose(self, pose: list[float]) -> None:
        """World pose of the ball from its pose in the TCP frame.

        Position into ``_tcp_world_p`` (m); orientation ``R_frame · R_local``
        into ``_tcp_world_R`` and, as intrinsic XYZ, ``_tcp_world_rpy`` (rad).
        """
        lp = self._tcp_local_p
        lp[0], lp[1], lp[2] = pose[0], pose[1], pose[2]
        np.dot(self._tcp_frame_R, lp, out=self._tcp_world_p)
        self._tcp_world_p += self._tcp_frame_p
        so3_from_rpy(pose[3], pose[4], pose[5], self._tcp_local_R)
        np.matmul(self._tcp_frame_R, self._tcp_local_R, out=self._tcp_world_R)
        so3_rpy(self._tcp_world_R, self._tcp_world_rpy)

    def _handle_tcp_transform_for_cartesian(self) -> None:
        """A gizmo drag while live or simulating: stream the TCP target."""
        callback = self._tcp_cartesian_move_callback
        if callback is None:
            return
        buf = self._pose_mm_buffer
        if self._tcp_drag_mode == "translate":
            buf[0] = self._tcp_world_p[0] * 1000.0
            buf[1] = self._tcp_world_p[1] * 1000.0
            buf[2] = self._tcp_world_p[2] * 1000.0
            # Hold rotation from drag start so translation doesn't also rotate.
            if self._tcp_drag_start_rot_deg is not None:
                buf[3], buf[4], buf[5] = self._tcp_drag_start_rot_deg
            else:
                buf[3] = waldoctl.commander.status.pose.rx
                buf[4] = waldoctl.commander.status.pose.ry
                buf[5] = waldoctl.commander.status.pose.rz
        else:
            buf[0] = waldoctl.commander.status.pose.x
            buf[1] = waldoctl.commander.status.pose.y
            buf[2] = waldoctl.commander.status.pose.z
            buf[3] = math.degrees(self._tcp_world_rpy[0])
            buf[4] = math.degrees(self._tcp_world_rpy[1])
            buf[5] = math.degrees(self._tcp_world_rpy[2])
        callback(buf)

    def _handle_tcp_transform_for_ik(self) -> None:
        """A gizmo drag while editing a target: solve IK for its pose."""
        if not self._ensure_ik_solver():
            return
        assert self._ik_solver is not None
        self._ik_last = time.monotonic()

        target_orientation = None
        target_pos = self._target_pos_buffer

        if self._tcp_drag_mode == "rotate":
            rpy = self._tcp_world_rpy
            orient_buf = self._target_orientation_buffer
            orient_buf[:] = rpy
            target_orientation = orient_buf
            self._editing_rotation[0] = float(rpy[0])
            self._editing_rotation[1] = float(rpy[1])
            self._editing_rotation[2] = float(rpy[2])
            self._editing_rotation_set = True
            # Hold the current FK position so rotating doesn't translate the TCP.
            fk_result = self._ik_solver.forward_kinematics(self._editing_angles)
            target_pos[:] = fk_result[:3]
        else:
            target_pos[:] = self._tcp_world_p
            # Carry forward any orientation set in a prior rotate-mode drag.
            if self._editing_rotation_set:
                orient_buf = self._target_orientation_buffer
                orient_buf[:] = self._editing_rotation
                target_orientation = orient_buf

        result = self._ik_solver.solve(
            target_pos=target_pos,
            current_angles=self._editing_angles,
            target_orientation=target_orientation,
        )
        self._tcp_ik_missed = not result.success
        if result.success:
            n = len(self.joint_names)
            self._editing_angles[:n] = result.angles[:n]
            self._apply_joint_angles(self._editing_angles)
            self._sync_robot_state_from_editing()
            self._update_collision_highlight()
            if self._current_editing_type:
                self._update_edit_bar_values(self._current_editing_type)
