"""The TCP gizmo: where it sits, and what its drags do.

The browser draws the gizmo on a frame the app places on the TCP from its
forward kinematics, each placement numbered. A drag reports translation and
orientation in the selected alignment basis, frozen at that starting pivot.

The app admits a drag only if the frame the browser showed is where the TCP
is now; the drag's poses are then taken from the app's own frame. While live
or simulating, a drag streams Cartesian jogs. While editing a target the
frame sits on the target, a drag moves the target, and IK, at most 30 times a
second for the last pose, only says whether the arm can reach it.
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
from waldo_commander.scene3d.protocol import quaternion
from waldo_commander.services.python_source import program_setup
from waldo_commander.services.scene_source import target_binding
from waldo_commander.setup import SetupStore
from waldo_commander.state import robot_state

from .config import RobotAppearanceMode
from .ik_solver import EditingIKSolver

logger = logging.getLogger(__name__)

#: How far the frame a browser showed may be from the TCP for its drag to be taken.
_SHOWN_FRAME_MM = 2.0
_SHOWN_FRAME_DEG = 1.0
_IK_INTERVAL_S = 1 / 30
# asyncio runs a timer up to one clock tick early (15.6 ms on Windows).
_CLOCK_TICK_S = time.get_clock_info("monotonic").resolution


class TCPControlsMixin:
    """Mixin placing the TCP gizmo and handling its drags for UrdfScene."""

    # Defined in the main UrdfScene class.
    scene: Any
    _appearance_mode: RobotAppearanceMode
    _editing_angles: list[float]
    joint_names: list[str]
    _handles_available: bool
    _hover_suspended: bool
    _teaching: Any
    _edit_active: bool
    _edit_target_first: bool
    _edit_marker: Any
    _edit_line: int | None
    _editing_unified_target: bool
    cancel_target_edit: Callable[[], None]

    def _refresh_editing_pose(self) -> tuple[float, ...] | None: ...

    def _write_target_edit(self) -> None: ...

    # Defined later in the MRO (JogHandlesMixin), so annotations only: a stub
    # here would shadow the real method.
    _push_interaction: Callable[[], None]
    _rules_changed: Callable[[], None]
    _admitted: Callable[[], None]

    def _init_tcp_controls_state(self) -> None:
        self._gizmo_choices = {"live": "tool"}
        self._gizmo_frames: dict[str, dict[str, Any]] = {}
        self._gizmo_setup_key: tuple | None = None
        self._gizmo_storage_key = "world"
        self._keep_tool_vertical = False
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
        # What the browser last heard of a miss during this drag.
        self._gizmo_miss_sent: bool | None = None
        self._requested_edit_pose: tuple[float, ...] | None = None
        self._ik_solver: EditingIKSolver | None = None
        self._ik_handle: Any = None
        self._ik_last = 0.0

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

    def _gizmo_shown(self) -> bool:
        """Whether the gizmo can be on screen now, so its frame needs placing."""
        if self._appearance_mode == RobotAppearanceMode.EDITING or (
            self._edit_active and self._edit_target_first
        ):
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
        """Place the gizmo's frame on the TCP from FK, or on the target a
        target edit moves.

        Recomputes FK only when joint angles change, and sends a placement
        only when FK produced a pose the frame has not been placed at.
        """
        if self.scene is None or not (force or self._gizmo_shown()):
            return
        if self._edit_active and self._edit_target_first:
            # The browser moves what it drags; placing it meanwhile would make
            # the dragged pose the one an aborted drag goes back to.
            if self._tcp_drag is not None and not force:
                return
            target = self._requested_edit_pose
            if target is not None and (force or target is not self._tcp_placed_pose):
                self._place_frame(target)
                if self._edit_marker is not None:
                    self._edit_marker.move(target[0], target[1], target[2])
            return
        n = len(self.joint_names)
        angles_changed = False
        editing = self._appearance_mode == RobotAppearanceMode.EDITING
        key: tuple[float, ...] | None = None
        if editing:
            angles_rad: list[float] | np.ndarray = self._editing_angles
            key = tuple(angles_rad[:n])
            angles_changed = key != self._last_fk_angles_tuple
        else:
            angles_deg = waldoctl.commander.status.joints.angles.deg
            if self._last_fk_angles_raw is None or not arrays_equal_n(
                angles_deg[:n], self._last_fk_angles_raw
            ):
                angles_rad = waldoctl.commander.status.joints.angles.rad
                angles_changed = True
        if angles_changed:
            if not self._ensure_ik_solver() or self._ik_solver is None:
                return
            try:
                ee = self._ik_solver.forward_kinematics(angles_rad)
            except Exception as e:
                logger.debug("FK failed: %s", e)
                return
            self._last_fk_pose = tuple(float(v) for v in ee[:6])
            # One cached pose serves both sources: the other's key is stale.
            if editing:
                self._last_fk_angles_tuple = key
                self._last_fk_angles_raw = None
            else:
                self._last_fk_angles_raw = angles_deg[:n].copy()
                self._last_fk_angles_tuple = None
        p = (
            self._requested_edit_pose
            if self._appearance_mode == RobotAppearanceMode.EDITING
            and self._requested_edit_pose is not None
            else self._last_fk_pose
        )
        if p is None or (p is self._tcp_placed_pose and not force):
            return
        self._place_frame(p)

    def _place_frame(self, p: tuple[float, ...]) -> None:
        self._tcp_placed_pose = p
        so3_from_rpy(p[3], p[4], p[5], self._placed_R)
        self.scene.set_tcp(p[:3], self._placed_R.tolist())

    def _gizmo_alignment_state(self, *, refresh: bool = False) -> dict[str, Any]:
        from waldo_commander.state import ui_state

        target = self._edit_active
        source = (
            str(ui_state.active_textarea.value or "")
            if ui_state.active_textarea
            else ""
        )
        line = self._edit_line if target else None
        key = (source, line)
        if refresh or key != self._gizmo_setup_key:
            self._gizmo_setup_key = key
            self._gizmo_frames = {}
            self._gizmo_storage_key = "world"
            if target and line is not None:
                try:
                    storage = target_binding(
                        source, line, len(self.joint_names)
                    ).storage
                    if storage is not None:
                        self._gizmo_storage_key = f"frame:{storage.name}"
                except ValueError:
                    pass
            binding = program_setup(source, line)
            if binding is not None:
                try:
                    setup = SetupStore(binding.directory).read_literal(binding.name)
                    for name in setup.frames:
                        matrix = setup.frame_matrix(name)
                        self._gizmo_frames[f"frame:{name}"] = {
                            "name": name,
                            "quaternion": quaternion(matrix[:3, :3]),
                            "origin": (matrix[:3, 3] / 1000).tolist(),
                        }
                except (OSError, ValueError) as error:
                    logger.debug("No gizmo frames from %s: %s", binding.name, error)
        kind = "target" if target else "live"
        default = self._gizmo_storage_key if target else "tool"
        choice = self._gizmo_choices.get(kind, default)
        if choice not in ("world", "tool") and choice not in self._gizmo_frames:
            choice = "world" if target else "tool"
        frame = self._gizmo_frames.get(choice)
        return {
            "key": choice,
            "name": frame["name"]
            if frame
            else "World"
            if choice == "world"
            else "Tool",
            "quaternion": frame["quaternion"] if frame else None,
            "origin": frame["origin"] if frame else None,
            "vertical": bool(self._editing_unified_target and self._keep_tool_vertical),
        }

    def _gizmo_setup_saved(self, directory, name) -> None:
        if self._gizmo_setup_key is None:
            return
        source, line = self._gizmo_setup_key
        binding = program_setup(source, line)
        if (
            binding is None
            or binding.name != name
            or SetupStore(binding.directory).directory != directory
        ):
            return
        self.scene.gestures.bump()
        if self._editing_unified_target and line is not None:
            try:
                stored = target_binding(source, line, len(self.joint_names)).storage
            except ValueError:
                self.cancel_target_edit()
            else:
                if stored is not None:
                    self.cancel_target_edit()
        self._gizmo_setup_key = None
        self._push_interaction()

    def _gizmo_basis(self, mode: str) -> np.ndarray:
        from scipy.spatial.transform import Rotation

        state = self.scene.interaction["alignment"]
        if state["vertical"] and mode == "rotate":
            return np.eye(3)
        if state["key"] == "tool":
            return self._placed_R.copy()
        if state["quaternion"] is not None:
            return Rotation.from_quat(state["quaternion"]).as_matrix()
        return np.eye(3)

    @staticmethod
    def _vertical_rotation(rotation: np.ndarray) -> np.ndarray:
        from .shape_drawing_mixin import surface_frame

        return surface_frame(np.array([0.0, 0.0, 1.0]), rotation[:, 0]) @ np.diag(
            [1, -1, -1]
        )

    # ---- Drags ----

    def _tcp_admit(self, gesture: Gesture, args: dict[str, Any]) -> bool:
        if self._teaching is not None:
            return False
        if self._editing_unified_target and self._edit_line is not None:
            from waldo_commander.state import ui_state

            try:
                target_binding(
                    str(ui_state.active_textarea.value or ""),
                    self._edit_line,
                    len(self.joint_names),
                )
            except ValueError:
                self.cancel_target_edit()
                self._push_interaction()
                return False
        mode = args.get("mode")
        if mode not in ("translate", "rotate") or not self._gizmo_shown():
            return False
        # The frame the drag's delta is measured from must be the one the
        # browser showed, and that must be where the TCP is now: a display
        # behind the arm would aim it backwards.
        self._push_tcp_pose()
        shown = self.scene.tcp_at(args.get("rev"))
        placed = self._tcp_placed_pose
        if shown is None or placed is None or not self._frame_matches(shown, placed):
            self._push_tcp_pose(force=True)
            return False
        self._tcp_frame_p[:] = placed[:3]
        if (
            self._keep_tool_vertical
            and self._editing_unified_target
            and mode == "rotate"
            and args.get("axis") != "Z"
        ):
            return False
        self._tcp_frame_R[:] = self._gizmo_basis(mode)
        self._tcp_drag_mode = mode
        self._gizmo_miss_sent = None
        if not self._edit_active:
            self._tcp_ik_missed = False
            self.scene.set_gizmo_miss(False)
            start = self._tcp_cartesian_move_start_callback
            token = start() if start is not None else None
            if token is None:
                return False
            gesture.data["token"] = token
            self._tcp_drag_start_rot_deg = tuple(robot_state.orientation.deg)
            self._admitted()
        else:
            gesture.data["target"] = (
                self._editing_angles.copy(),
                self._requested_edit_pose,
                self._tcp_ik_missed,
            )
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
        elif "target" in gesture.data:
            self._queue_ik()

    def _tcp_finish(
        self, gesture: Gesture, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        if gesture.drag != self._tcp_drag:
            return
        self._tcp_drag = None
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
            # A target edit: an edit closed since leaves the new one alone.
            if not self._edit_active:
                return
            if pose is None:
                angles, requested, missed = gesture.data["target"]
                self._editing_angles[:] = angles
                self._requested_edit_pose = requested
                self._tcp_ik_missed = missed
                self._refresh_editing_pose()
            else:
                self._compose_tcp_pose(pose)
                self._handle_tcp_transform_for_ik()
            self.scene.set_gizmo_miss(self._tcp_ik_missed)
            self._gizmo_miss_sent = self._tcp_ik_missed
            self._push_tcp_pose(force=True)
            if pose is not None:
                # Each release is kept, one undoable edit at a time.
                self._write_target_edit()
            return
        if pose is None:
            self._tcp_drag_start_rot_deg = None
            cancel = self._tcp_cartesian_move_cancel_callback
            if cancel is not None:
                cancel(token)
            return
        # The release holds the orientation every sample of the drag held.
        self._compose_tcp_pose(pose)
        self._handle_tcp_transform_for_cartesian()
        self._tcp_drag_start_rot_deg = None
        end = self._tcp_cartesian_move_end_callback
        if end is not None:
            end(token)

    def _queue_ik(self) -> None:
        """Solve for the latest pose at most 30 times a second."""
        if self._ik_handle is not None:
            return
        loop = self.scene._loop
        delay = max(
            0.0, self._ik_last + _IK_INTERVAL_S + _CLOCK_TICK_S - time.monotonic()
        )
        self._ik_handle = loop.call_later(delay, self._run_queued_ik)

    def _run_queued_ik(self) -> None:
        self._ik_handle = None
        if self._tcp_drag is not None:
            self._handle_tcp_transform_for_ik()

    def _compose_tcp_pose(self, pose: list[float]) -> None:
        """World pose from the same frozen alignment basis used by the handles.

        Position into ``_tcp_world_p`` (m); orientation ``R_frame · R_local``
        into ``_tcp_world_R`` and, as intrinsic XYZ, ``_tcp_world_rpy`` (rad).
        """
        lp = self._tcp_local_p
        lp[0], lp[1], lp[2] = pose[0], pose[1], pose[2]
        np.dot(self._tcp_frame_R, lp, out=self._tcp_world_p)
        self._tcp_world_p += self._tcp_frame_p
        so3_from_rpy(pose[3], pose[4], pose[5], self._tcp_local_R)
        np.matmul(self._tcp_frame_R, self._tcp_local_R, out=self._tcp_world_R)
        if self._keep_tool_vertical and self._editing_unified_target:
            self._tcp_world_R[:] = self._vertical_rotation(self._tcp_world_R)
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
        """A gizmo drag while editing a target: move the target there."""
        if not self._ensure_ik_solver():
            return
        self._ik_last = time.monotonic()
        self._requested_edit_pose = tuple(self._tcp_world_p) + tuple(
            self._tcp_world_rpy
        )
        self._solve_target_reach()
        self._refresh_editing_pose()

    def _solve_target_reach(self) -> None:
        """Whether the arm can reach the requested target, and the joints it
        would reach it with, for a joint target's literals. The arm itself
        stays where it is."""
        pose = self._requested_edit_pose
        if pose is None or self._ik_solver is None:
            return
        self._target_pos_buffer[:] = pose[:3]
        self._target_orientation_buffer[:] = pose[3:6]
        result = self._ik_solver.solve(
            target_pos=self._target_pos_buffer,
            current_angles=self._editing_angles,
            target_orientation=self._target_orientation_buffer,
        )
        self._tcp_ik_missed = not result.success
        if result.success:
            n = len(self.joint_names)
            self._editing_angles[:n] = result.angles[:n]
        if self._tcp_ik_missed != self._gizmo_miss_sent:
            self._gizmo_miss_sent = self._tcp_ik_missed
            self.scene.set_gizmo_miss(self._tcp_ik_missed)
