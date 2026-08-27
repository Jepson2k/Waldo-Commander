"""
Editing Mixin for UrdfScene.

Provides target editing functionality:
- Enter/exit editing mode (changes robot appearance and joint control)
- Joint angle manipulation via rotation rings
- TCP ball for IK-driven positioning
- Context menu and edit bar UI
- Target CRUD operations
"""

import asyncio
import logging
import math
from typing import Any

import numpy as np
import waldoctl
from nicegui import ui

from waldo_commander.common.theme import SceneColors
from waldo_commander.state import (
    ProgramTarget,
    robot_state,
    simulation_state,
    ui_state,
)

from .config import RobotAppearanceMode
from .ik_solver import EditingIKSolver
from .loader import normalize_axis

logger = logging.getLogger(__name__)


class EditingMixin:
    """Mixin providing target editing functionality for UrdfScene."""

    # Attributes from UrdfScene
    scene: Any
    urdf_model: Any
    urdf_path: Any
    joint_names: list[str]
    joint_axes: dict[str, np.ndarray]
    joint_groups: dict[str, Any]
    joint_trafos: dict
    joint_pos_limits: dict[str, dict[str, float | None]]
    _stl_scale: float
    _robot_meshes: list[Any]
    _appearance_mode: RobotAppearanceMode
    _editing_angles: list[float]
    _pre_edit_angles: list[float]
    _tcp_ball: Any
    _tcp_ball_dragging: bool
    _ik_solver: EditingIKSolver | None
    config: Any
    targets_group: Any
    _target_objects: dict[str, dict[str, Any]]
    context_menu: Any
    _scene_wrapper: Any
    _current_tool_offset_z: float
    _editing_rotation: list[float]
    _editing_rotation_set: bool
    _editing_target_id: str | None
    _right_click_start_pos: tuple[float, float] | None

    # Methods from other mixins / main class
    set_editing_angles: Any
    get_editing_angles: Any
    set_appearance_mode: Any
    _ensure_tcp_ball: Any
    _update_tcp_ball_position: Any
    enable_tcp_transform_controls: Any
    invalidate_fk_cache: Any
    _update_envelope_from_robot_state: Any
    _apply_joint_angles: Any
    _ensure_ik_solver: Any
    _update_collision_highlight: Any

    def _init_editing_state(self) -> None:
        """Initialize all editing state variables."""
        self._joint_control_groups: dict[int, Any] = {}
        self._joint_controls_suspended: bool = False

        self.context_menu: Any | None = None
        self._last_click_coords: tuple[float, float, float] | None = None
        self._right_click_start_pos: tuple[float, float] | None = None
        self._right_click_drag_threshold: float = 5.0
        self._pending_context_menu_event: Any | None = None

        self._target_objects: dict[str, dict[str, Any]] = {}

        self._editing_unified_target: bool = False
        self._editing_target_id: str | None = None
        self._unified_target_mode: str = "cartesian"
        self._joint_ring_touched: bool = False
        self._editing_target_type: str = "cartesian"

        # Original values for delta display
        self._original_editing_pose: list[float] | None = None
        self._original_editing_joints: list[float] | None = None

        self._edit_bar: Any | None = None
        self._edit_bar_label: Any | None = None
        self._edit_bar_values: Any | None = None
        self._edit_bar_mode_toggle: Any | None = None
        self._edit_bar_container: Any | None = None
        self._current_editing_type: str | None = None

        self._cached_joint_axes_letters: list[str] | None = None

    # -------------------------------------------------------------------------
    # Core editing mode
    # -------------------------------------------------------------------------

    def enter_editing_mode(self, joint_angles: list[float]) -> None:
        """Enter editing mode at specified joint angles."""
        n = len(self.joint_names)
        self._pre_edit_angles = list(waldoctl.commander.status.joints.angles.rad[:n])

        # Pad or truncate to joint count
        self._editing_angles = list(joint_angles) + [0.0] * (n - len(joint_angles))
        self._editing_angles = self._editing_angles[:n]

        self._editing_target_type = "cartesian"
        self._joint_ring_touched = False
        self._editing_rotation_set = False

        waldoctl.commander.status.editing_mode = True
        self.set_appearance_mode(RobotAppearanceMode.EDITING)
        self._apply_joint_angles(self._editing_angles)

        # Force FK recomputation — the cached pose is from LIVE mode
        self.invalidate_fk_cache()

        self._ensure_tcp_ball()
        if self._tcp_ball:
            self._tcp_ball.visible(True)
            self._tcp_ball.material(SceneColors.TCP_ACTIVE_HEX, 0.9)
        self._update_tcp_ball_position()
        self.enable_tcp_transform_controls("translate")

    def exit_editing_mode(self) -> None:
        """Exit editing mode and restore pre-edit state."""
        self._disable_joint_transform_controls()

        if self._tcp_ball:
            self._tcp_ball.material(SceneColors.TCP_INACTIVE_HEX, 0.9)

        self._apply_joint_angles(self._pre_edit_angles)

        if waldoctl.commander.status.simulator_active:
            self.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
        else:
            self.set_appearance_mode(RobotAppearanceMode.LIVE)

        self._joint_ring_touched = False
        self._editing_target_type = "cartesian"
        self._joint_controls_suspended = False
        self._editing_rotation_set = False

        waldoctl.commander.status.editing_mode = False

        # Snap TCP ball back to robot's live position
        self.invalidate_fk_cache()
        self._update_tcp_ball_position()

    def _cleanup_editing(self) -> None:
        """Clean up editing state."""
        self._disable_joint_transform_controls()

    # -------------------------------------------------------------------------
    # Joint controls
    # -------------------------------------------------------------------------

    def _get_joint_axes_letters(self) -> list[str]:
        """Get rotation axis letter for each joint (cached)."""
        if self._cached_joint_axes_letters is not None:
            return self._cached_joint_axes_letters

        axis_letters: list[str] = []
        for joint_name in self.joint_names:
            if joint_name in self.joint_axes:
                vec = self.joint_axes[joint_name]
            else:
                joint = next(
                    (j for j in self.urdf_model.joints if j.name == joint_name), None
                )
                raw_axis = getattr(joint, "axis", None) if joint else None
                vec = normalize_axis(raw_axis)
            idx = int(np.argmax(np.abs(vec[:3])))
            axis_letters.append(["X", "Y", "Z"][idx])

        self._cached_joint_axes_letters = axis_letters
        return axis_letters

    def _get_joint_limits(self) -> list[tuple[float, float]]:
        """Get joint limits in radians."""
        limits = []
        for joint_name in self.joint_names:
            joint_limits = self.joint_pos_limits.get(joint_name, {})
            min_val = joint_limits.get("min", -math.pi)
            max_val = joint_limits.get("max", math.pi)
            limits.append(
                (
                    min_val if min_val is not None else -math.pi,
                    max_val if max_val is not None else math.pi,
                )
            )
        return limits

    def _enable_joint_transform_controls(self) -> None:
        """Enable rotation controls on each joint."""
        if not self.scene or not self.joint_groups:
            return
        self._joint_control_groups = {}
        axes = self._get_joint_axes_letters()
        rotation_snap = math.radians(5.0)

        for i, joint_name in enumerate(self.joint_names):
            group = self.joint_groups.get(joint_name)
            if not group:
                continue
            group.with_name(f"edit_joint_group:{i}")
            axis = axes[i] if i < len(axes) else "Z"
            group.enable_transform_controls(
                mode="rotate",
                size=0.6,
                visible_axes=[axis],
                space="local",
                rotation_snap=rotation_snap,
            )
            self._joint_control_groups[i] = group

    def _disable_joint_transform_controls(self) -> None:
        """Disable rotation controls on all joints."""
        if not self.scene or not self._joint_control_groups:
            return
        for _, group in list(self._joint_control_groups.items()):
            group.disable_transform_controls()
        self._joint_control_groups.clear()

    def _on_joint_group_transform(self, e) -> None:
        """Handle joint ring rotation events."""
        if self._appearance_mode != RobotAppearanceMode.EDITING:
            return
        if not self.scene or self._joint_controls_suspended:
            return

        if not self._joint_ring_touched:
            self._joint_ring_touched = True
            self._editing_target_type = "joint"
            self._unified_target_mode = "joint"

        object_name = getattr(e, "object_name", "") or ""
        if object_name.startswith("edit_joint_group:"):
            prefix = "edit_joint_group:"
        elif object_name.startswith("ghost_joint_group:"):
            prefix = "ghost_joint_group:"
        else:
            return

        try:
            joint_index = int(object_name.split(prefix)[1])
        except (ValueError, IndexError):
            return

        axes = self._get_joint_axes_letters()
        axis = axes[joint_index] if joint_index < len(axes) else "Z"

        rx = e.rx if e.rx is not None else 0.0
        ry = e.ry if e.ry is not None else 0.0
        rz = e.rz if e.rz is not None else 0.0

        angle_change = rx if axis == "X" else (ry if axis == "Y" else rz)

        if 0 <= joint_index < len(self._editing_angles):
            self._editing_angles[joint_index] = angle_change
            self._update_tcp_ball_position()
            self._sync_robot_state_from_editing()
            self._update_collision_highlight()
            if self._current_editing_type:
                self._update_edit_bar_values(self._current_editing_type)

    def _on_ik_solved(self, e) -> None:
        """Handle IK solution event."""
        args = e.args if hasattr(e, "args") else {}
        if args.get("chain_id") != "ghost_ik":
            return
        angles = args.get("angles", [])
        if angles is not None and len(angles) >= len(self.joint_names):
            self._editing_angles = list(angles)
            self._sync_robot_state_from_editing()
            self._update_collision_highlight()
            if self._current_editing_type:
                self._update_edit_bar_values(self._current_editing_type)

    def apply_editing_home(self) -> None:
        """Move editing robot to home position and sync state/UI."""
        home_rad = ui_state.active_robot.joints.home.rad.tolist()
        self.set_editing_angles(home_rad)
        self._sync_robot_state_from_editing()
        if self._current_editing_type:
            self._update_edit_bar_values(self._current_editing_type)

    # -------------------------------------------------------------------------
    # IK
    # -------------------------------------------------------------------------

    def _ik_for_position(self, target_pos: list[float]) -> list[float] | None:
        """Solve IK for target position. Returns joint angles in radians."""
        if not self._ensure_ik_solver():
            return None
        assert self._ik_solver is not None

        current_angles = self._get_robot_angles_rad()
        result = self._ik_solver.solve(
            target_pos=np.array(target_pos, dtype=np.float64),
            current_angles=current_angles,
            throttle=False,
        )
        if result and result.success:
            return list(result.angles)
        return None

    # -------------------------------------------------------------------------
    # Context menu
    # -------------------------------------------------------------------------

    def _populate_context_menu(self, e) -> None:
        """Populate context menu based on click target."""
        if not self.context_menu:
            return

        hits = getattr(e, "hits", []) or []
        self.context_menu.clear()

        target_id = None
        for h in hits:
            name = getattr(h, "object_name", "") or ""
            if self._is_envelope_hit(name):
                continue
            if name.startswith("target:"):
                target_id = name.split("target:", 1)[1]
                break

        ground_point = getattr(e, "ground_point", None)
        if ground_point:
            self._last_click_coords = (
                float(ground_point.x),
                float(ground_point.y),
                float(ground_point.z),
            )

        shape_name = self._shape_hit_name(hits)

        with self.context_menu:
            if shape_name and not target_id:
                self._populate_shape_menu(shape_name)
            elif target_id:
                target = self._find_target_by_id(target_id)
                if target:
                    ui.item(f"Target (Line {target.line_number})").classes(
                        "font-bold text-sm"
                    )
                    ui.separator()
                    tid = target_id

                    def make_edit(t=tid):
                        return lambda: self._show_unified_target_editor(
                            edit_target_id=t
                        )

                    def make_delete(t=tid):
                        return lambda: self._delete_target(t)

                    ui.menu_item("Edit Target...", on_click=make_edit())
                    ui.menu_item("Delete Target", on_click=make_delete())
            else:
                ui.item("Add Target").classes("font-bold text-sm")
                ui.separator()
                ui.menu_item(
                    "Place Target at Robot Position...",
                    on_click=lambda: self._show_unified_target_editor(
                        use_click_position=False
                    ),
                )
                if self._last_click_coords and self._last_click_coords != (
                    0.0,
                    0.0,
                    0.0,
                ):
                    x_mm, y_mm = [c * 1000 for c in self._last_click_coords[:2]]
                    ui.menu_item(
                        f"Place Target Here ({x_mm:.0f}, {y_mm:.0f})...",
                        on_click=lambda: self._show_unified_target_editor(
                            use_click_position=True
                        ),
                    )
                self._populate_shape_add_menu(self._last_click_coords or (0.3, 0.0, 0.0))

    def _is_envelope_hit(self, object_name: str) -> bool:
        """Check if object is the workspace envelope."""
        return object_name == "envelope:hull"

    def _delete_target(self, target_id: str) -> None:
        """Delete a target after confirmation."""

        def confirm():
            ui_state.editor_panel.delete_target_code(target_id)
            dialog.close()

        dialog = ui.dialog()
        with dialog, ui.card():
            ui.label("Delete Target?")
            with ui.row():
                ui.button("Cancel", on_click=dialog.close)
                ui.button("Delete", on_click=confirm, color="negative")
        dialog.open()

    # -------------------------------------------------------------------------
    # Unified target editor
    # -------------------------------------------------------------------------

    def _show_unified_target_editor(
        self,
        use_click_position: bool = False,
        edit_target_id: str | None = None,
    ) -> None:
        """Show target editor for creating or editing targets."""
        if self._editing_unified_target:
            return

        self._editing_unified_target = True
        self._editing_target_id = edit_target_id
        self._unified_target_mode = "cartesian"
        self._joint_ring_touched = False

        # Determine initial angles
        if edit_target_id:
            target = self._find_target_by_id(edit_target_id)
            if target and target.pose:
                self._original_editing_pose = list(target.pose)
                initial_angles = self._ik_for_position(target.pose[:3])
                if initial_angles is None:
                    initial_angles = self._get_robot_angles_rad()
            else:
                initial_angles = self._get_robot_angles_rad()
        elif use_click_position and self._last_click_coords:
            click_pos = list(self._last_click_coords)
            ik_result = self._ik_for_position(click_pos)
            if ik_result is None and abs(click_pos[2]) < 0.001:
                tcp_z = self._get_current_tcp_z()
                if tcp_z is not None:
                    click_pos[2] = tcp_z
                    ik_result = self._ik_for_position(click_pos)
            initial_angles = (
                ik_result if ik_result is not None else self._get_robot_angles_rad()
            )
        else:
            initial_angles = self._get_robot_angles_rad()

        self._original_editing_joints = [math.degrees(a) for a in initial_angles]
        self.enter_editing_mode(initial_angles)

        bar_type = "pose_edit" if edit_target_id else "unified"
        self._create_edit_bar(bar_type)

        async def enable_controls():
            await asyncio.sleep(0.15)
            if not self._editing_unified_target:
                return
            self._ensure_ik_solver()
            self.enable_tcp_transform_controls("translate")
            self._enable_joint_transform_controls()
            self._sync_robot_state_from_editing()

        with self.scene:
            ui.timer(0.0, enable_controls, once=True)

    def _confirm_unified_as_cartesian(self) -> None:
        """Confirm as cartesian target."""
        if not self._editing_unified_target:
            return

        pose = self._get_editing_tcp_pose()
        if pose is None:
            return

        if self._editing_target_id:
            target = self._find_target_by_id(self._editing_target_id)
            if target:
                if self._editing_target_id in self._target_objects:
                    group = self._target_objects[self._editing_target_id].get("group")
                    if group:
                        group.move(pose[0], pose[1], pose[2])
                target.pose = pose
                ui_state.editor_panel.sync_code_from_target(
                    self._editing_target_id, pose
                )
        else:
            self._add_target_with_pose(pose, "cartesian")

        self._end_editing_session()

    def _confirm_unified_as_joint(self) -> None:
        """Confirm as joint target (converts move_l→move_j if editing existing)."""
        if not self._editing_unified_target:
            return

        if self._editing_target_id:
            target = self._find_target_by_id(self._editing_target_id)
            n = len(self.joint_names)
            angles_deg = list(waldoctl.commander.status.joints.angles.deg[:n])
            if target:
                pose = target.pose
                ui_state.editor_panel.sync_code_from_target(
                    self._editing_target_id,
                    pose,
                    move_type="joints",
                    joint_angles_deg=angles_deg,
                )
            self._end_editing_session()
            return

        self._insert_joint_target_from_editing()
        self._end_editing_session()

    def _end_editing_session(self) -> None:
        """End editing and clean up."""
        self._editing_unified_target = False
        self._editing_target_id = None
        self._unified_target_mode = "cartesian"
        self._joint_ring_touched = False
        self._original_editing_joints = None
        self._original_editing_pose = None
        self._cleanup_editing()
        self._hide_edit_bar()
        self.exit_editing_mode()

    def _handle_keyboard(self, e) -> None:
        """Handle keyboard events."""
        if e.key == "Escape" and e.action.keydown:
            if self._editing_unified_target:
                self._end_editing_session()
            if getattr(self, "_shape_move_active", None):
                self._end_shape_move()

    # -------------------------------------------------------------------------
    # Edit bar UI
    # -------------------------------------------------------------------------

    def _create_edit_bar(self, editing_type: str) -> None:
        """Create the edit confirmation bar."""
        if self._edit_bar:
            self._update_edit_bar_content(editing_type)
            return

        if not self._edit_bar_container:
            parent: Any = self._scene_wrapper if self._scene_wrapper else ui
            with parent:
                self._edit_bar_container = ui.element("div").classes(
                    "absolute bottom-4 left-1/2 -translate-x-1/2 z-50 pointer-events-auto"
                )

        with self._edit_bar_container:
            self._edit_bar = ui.row().classes(
                "overlay-card items-center gap-3 px-3 py-2"
            )
            with self._edit_bar:
                self._edit_bar_label = ui.label("").classes("text-sm font-medium")
                self._edit_bar_values = ui.row().classes(
                    "gap-2 items-center flex-1 flex-nowrap"
                )
                ui.space()
                ui.button(icon="close", on_click=self._on_edit_bar_cancel).props(
                    "round flat color=red"
                )
                ui.button(icon="check", on_click=self._on_edit_bar_confirm).props(
                    "round color=positive"
                )

        self._current_editing_type = editing_type
        self._update_edit_bar_content(editing_type)
        self._sync_robot_state_from_editing()
        self._update_edit_bar_values(editing_type)

    def _update_edit_bar_content(self, editing_type: str) -> None:
        """Update edit bar label."""
        if not self._edit_bar_label:
            return
        labels = {
            "joint": "Editing Joint Target",
            "unified": "Place Target",
            "pose_edit": "Editing Target Position",
        }
        self._edit_bar_label.text = labels.get(editing_type, "Editing Target")

    def _update_edit_bar_values(self, editing_type: str) -> None:
        """Update edit bar delta values."""
        if not self._edit_bar_values:
            return
        self._edit_bar_values.clear()

        if not self._editing_target_id:
            return

        def fmt(delta: float, unit: str = "") -> str:
            if abs(delta) < 0.1:
                return f"0.0{unit}"
            return f"{'+' if delta > 0 else ''}{delta:.1f}{unit}"

        def color(delta: float) -> str:
            if abs(delta) < 0.1:
                return "text-gray-400"
            return "text-green-400" if delta > 0 else "text-red-400"

        with self._edit_bar_values:
            show_joints = editing_type == "joint" or (
                editing_type == "unified" and self._unified_target_mode == "joint"
            )

            if show_joints:
                # commander.status degrees are already synced from the editing angles
                n = len(self.joint_names)
                angles_deg = list(waldoctl.commander.status.joints.angles.deg[:n])
                orig_deg = self._original_editing_joints or [0.0] * n
                for i, angle in enumerate(angles_deg):
                    delta = angle - orig_deg[i]
                    ui.label(f"ΔJ{i + 1}: {fmt(delta, '°')}").classes(
                        f"text-xs font-mono whitespace-nowrap {color(delta)}"
                    )
            else:
                tcp_pos_mm = [0.0, 0.0, 0.0]
                if self._ik_solver:
                    fk = self._ik_solver.forward_kinematics(self.get_editing_angles())
                    if fk is not None:
                        offset = self._current_tool_offset_z
                        tcp_pos_mm = [
                            fk[0] * 1000,
                            fk[1] * 1000,
                            (fk[2] + offset) * 1000,
                        ]

                orig_mm = [
                    p * 1000 for p in (self._original_editing_pose or [0.0] * 3)[:3]
                ]
                for i, label in enumerate(["X", "Y", "Z"]):
                    delta = tcp_pos_mm[i] - orig_mm[i]
                    ui.label(f"Δ{label}: {fmt(delta, 'mm')}").classes(
                        f"text-xs font-mono whitespace-nowrap {color(delta)}"
                    )

    def _hide_edit_bar(self) -> None:
        """Hide and clean up the edit bar."""
        self._current_editing_type = None
        if self._edit_bar:
            self._edit_bar.delete()
            self._edit_bar = None
        if self._edit_bar_container:
            self._edit_bar_container.delete()
            self._edit_bar_container = None
        self._edit_bar_label = None
        self._edit_bar_values = None

    def _on_edit_bar_cancel(self) -> None:
        """Handle cancel button."""
        self._end_editing_session()

    def _on_edit_bar_confirm(self) -> None:
        """Handle confirm button."""
        if not self._editing_unified_target:
            return
        if self._unified_target_mode == "joint":
            self._confirm_unified_as_joint()
        else:
            self._confirm_unified_as_cartesian()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _find_target_by_id(self, target_id: str) -> ProgramTarget | None:
        """Find a target by ID in the active program's dry-run targets."""
        active = waldoctl.commander.programs.active
        if active is None:
            return None
        for t in active.dry_run.targets:
            if t.id == target_id:
                return t
        return None

    def _get_robot_angles_rad(self) -> list[float]:
        """Get robot angles in radians."""
        n = len(self.joint_names)
        if len(waldoctl.commander.status.joints.angles) >= n:
            return list(waldoctl.commander.status.joints.angles.rad[:n])
        return [0.0] * n

    def _get_current_tcp_z(self) -> float | None:
        """Get current TCP z position in meters via FK."""
        if not self._ensure_ik_solver() or not self._ik_solver:
            return None
        fk = self._ik_solver.forward_kinematics(self._get_robot_angles_rad())
        return float(fk[2]) if fk is not None else None

    def _get_editing_tcp_pose(self) -> list[float] | None:
        """Get TCP pose from commander.status (already synced from editing angles).

        Returns [x, y, z, rx, ry, rz] with position in meters and rotation in degrees.
        """
        # Subtract tool offset since sync adds it to z
        offset = self._current_tool_offset_z
        return [
            waldoctl.commander.status.pose.x / 1000,  # mm -> m
            waldoctl.commander.status.pose.y / 1000,
            (waldoctl.commander.status.pose.z / 1000) - offset,
            waldoctl.commander.status.pose.rx,
            waldoctl.commander.status.pose.ry,
            waldoctl.commander.status.pose.rz,
        ]

    def _add_target_with_pose(self, pose: list[float], move_type: str) -> None:
        """Add target and insert code."""
        pose_mm = [
            pose[0] * 1000,
            pose[1] * 1000,
            pose[2] * 1000,
            pose[3] if len(pose) > 3 else 0.0,
            pose[4] if len(pose) > 4 else 0.0,
            pose[5] if len(pose) > 5 else 0.0,
        ]

        line_number = ui_state.editor_panel.add_target_code(pose_mm, move_type)
        if line_number:
            new_target = ProgramTarget(
                id=f"pending_{line_number}",
                line_number=line_number,
                pose=pose,
                move_type=move_type,
                scene_object_id="",
            )
            active = waldoctl.commander.programs.active
            if active is not None:
                active.dry_run.targets = [*active.dry_run.targets, new_target]
            simulation_state.notify_changed()

    def _insert_joint_target_from_editing(self) -> None:
        """Insert joint target code from the editing-synced commander.status angles."""
        n = len(self.joint_names)
        ui_state.editor_panel.add_joint_target_code(
            list(waldoctl.commander.status.joints.angles.deg[:n])
        )

    def _sync_robot_state_from_editing(self) -> None:
        """Sync ``commander.status`` joints/pose with the editing values."""
        if not waldoctl.commander.status.editing_mode:
            return

        try:
            angles_rad = self.get_editing_angles()
            waldoctl.commander.status.joints.angles.set_rad(np.asarray(angles_rad))

            if self._ik_solver:
                fk = self._ik_solver.forward_kinematics(angles_rad)
                if fk is not None:
                    offset = self._current_tool_offset_z
                    waldoctl.commander.status.pose.x = fk[0] * 1000
                    waldoctl.commander.status.pose.y = fk[1] * 1000
                    waldoctl.commander.status.pose.z = (fk[2] + offset) * 1000
                    if len(fk) >= 6:
                        robot_state.orientation.set_rad(np.asarray(fk[3:6]))
                        # Scalar fields are needed for UI binding
                        waldoctl.commander.status.pose.rx = robot_state.orientation.deg[
                            0
                        ]
                        waldoctl.commander.status.pose.ry = robot_state.orientation.deg[
                            1
                        ]
                        waldoctl.commander.status.pose.rz = robot_state.orientation.deg[
                            2
                        ]
                    self._update_envelope_from_robot_state()
        except (TypeError, ValueError, AttributeError) as e:
            logger.debug("_sync_robot_state_from_editing failed: %s", e)
