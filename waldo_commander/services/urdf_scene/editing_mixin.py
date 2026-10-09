"""
Editing Mixin for UrdfScene.

Provides target editing functionality:
- Enter/exit editing mode (changes robot appearance and joint control)
- Joint angle manipulation via rotation rings
- TCP ball for IK-driven positioning
- Context menu and edit bar UI
- Target CRUD operations
"""

import ast
import logging
import math
from typing import Any

import numpy as np
import waldoctl
from nicegui import ui

from waldo_commander.scene3d.interaction import Gesture, finite
from waldo_commander.state import (
    ProgramTarget,
    simulation_state,
    ui_state,
)

from waldo_commander.services.scene_source import target_binding
from waldo_commander.services.timeline import selection_before
from waldo_commander.services.keybindings import keybindings_manager
from waldo_commander.services.programs import is_any_program_running, preview_is_current

from .config import RobotAppearanceMode
from .ik_solver import EditingIKSolver
from .shape_editing_mixin import ShapeEditingMixin

logger = logging.getLogger(__name__)


class EditingMixin(ShapeEditingMixin):
    """Mixin providing target editing functionality for UrdfScene.

    Extends the keep-out shape editing mixin: the scene context menu is
    built here, and its shape branches call straight into that surface."""

    _context_surface: dict | None
    _populate_draw_menu: Any
    _cancel_shape_drawing: Any
    _joint_travel: Any

    # Attributes from UrdfScene
    scene: Any
    urdf_model: Any
    urdf_path: Any
    joint_names: list[str]
    joint_axes: dict[str, np.ndarray]
    joint_groups: dict[str, Any]
    joint_pos_limits: dict[str, dict[str, float | None]]
    _stl_scale: float
    _robot_meshes: list[Any]
    _appearance_mode: RobotAppearanceMode
    _editing_angles: list[float]
    _ik_solver: EditingIKSolver | None
    config: Any
    targets_group: Any
    _target_objects: dict[str, dict[str, Any]]
    context_menu: Any
    _scene_wrapper: Any
    _notify: Any
    _current_tool_offset_z: float
    _editing_rotation: list[float]
    _editing_rotation_set: bool
    _editing_target_id: str | None
    _tcp_drag: int | None
    _tcp_placed_pose: tuple[float, ...] | None
    _tcp_world_p: np.ndarray
    _tcp_world_rpy: np.ndarray
    _last_fk_angles_tuple: tuple[float, ...] | None
    _last_fk_pose: tuple[float, ...] | None
    _handle_tcp_transform_for_ik: Any

    # Methods from other mixins / main class
    set_editing_angles: Any
    get_editing_angles: Any
    set_appearance_mode: Any
    _push_tcp_pose: Any
    _push_interaction: Any
    invalidate_fk_cache: Any
    _update_envelope_from_robot_state: Any
    _apply_joint_angles: Any
    _ensure_ik_solver: Any
    _update_collision_highlight: Any
    suspend_hover: Any
    resume_hover: Any

    def _init_editing_state(self) -> None:
        """Initialize all editing state variables."""
        # Numbers each edit, so a joint control's drag from an earlier edit
        # cannot change the one open now.
        self._teaching = None
        self._edit_session = 0
        self._edit_active = False
        self._editing_tcp_pose: list[float] | None = None
        self._edit_document: tuple | None = None

        self.context_menu: Any | None = None
        self._last_click_coords: tuple[float, float, float] | None = None

        self._target_objects: dict[str, dict[str, Any]] = {}

        self._editing_unified_target: bool = False
        self._editing_target_id: str | None = None
        self._unified_target_mode: str = "cartesian"
        self._joint_ring_touched: bool = False

        # Where the edit started: the pose for the deltas (m, degrees) and
        # the joints, so a confirm that changed nothing writes nothing.
        self._original_editing_pose: list[float] | None = None
        self._initial_editing_angles: list[float] | None = None

        self._edit_bar: Any | None = None
        self._edit_bar_label: Any | None = None
        self._edit_bar_values: Any | None = None
        self._edit_coordinates: list[Any] = []
        self._edit_orientation: Any = None
        self._edit_deltas: list[Any] = []
        self._edit_confirm: Any | None = None
        self._edit_validity: Any | None = None
        self._edit_bar_mode_toggle: Any | None = None
        self._edit_bar_container: Any | None = None
        self._current_editing_type: str | None = None

    # -------------------------------------------------------------------------
    # Core editing mode
    # -------------------------------------------------------------------------

    def enter_editing_mode(self, joint_angles: list[float]) -> None:
        """Enter editing mode at specified joint angles."""
        if self._teaching is not None:
            self.cancel_target_edit()
        self._cancel_shape_drawing()
        self.suspend_hover()
        n = len(self.joint_names)

        # Pad or truncate to joint count
        self._editing_angles = list(joint_angles) + [0.0] * (n - len(joint_angles))
        self._editing_angles = self._editing_angles[:n]

        self._joint_ring_touched = False
        self._editing_rotation_set = False
        self._requested_edit_pose = None
        self._tcp_ik_missed = False

        self.set_appearance_mode(RobotAppearanceMode.EDITING)
        self._apply_joint_angles(self._editing_angles)

        # Force FK recomputation — the cached pose is from LIVE mode
        self.invalidate_fk_cache()
        self._ensure_ik_solver()
        self._edit_session += 1
        self._edit_active = True
        waldoctl.commander.status.editing_mode = True
        self._edit_document = self._edit_document_key()
        if self.scene is not None:
            self.scene.gestures.bump()
        self._push_tcp_pose(force=True)
        self._push_interaction()

    def exit_editing_mode(self) -> None:
        """Return the view to the latest backend pose."""
        self._edit_session += 1
        self._edit_active = False
        waldoctl.commander.status.editing_mode = False
        self._edit_document = None
        if self.scene is not None:
            self.scene.gestures.bump()
        self._push_interaction()

        self._apply_joint_angles(list(waldoctl.commander.status.joints.angles.rad))
        self._editing_tcp_pose = None
        self._requested_edit_pose = None

        if waldoctl.commander.status.simulator_active:
            self.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
        else:
            self.set_appearance_mode(RobotAppearanceMode.LIVE)

        self._joint_ring_touched = False
        self._editing_rotation_set = False

        # The cached FK pose is the editing one.
        self.invalidate_fk_cache()
        self._push_tcp_pose(force=True)
        self.resume_hover()

    @staticmethod
    def _edit_document_key() -> tuple:
        program = waldoctl.commander.programs.active
        tool = waldoctl.commander.status.tool
        return (
            program.id if program else None,
            program.source if program else None,
            program.dry_run.revision if program else None,
            tool.key,
            tool.variant_key,
            is_any_program_running(),
        )

    @property
    def editing_target(self) -> bool:
        """Whether a target edit holds the view in place of the live robot."""
        return self._edit_active

    def check_edit_context(self) -> bool:
        if self._teaching is not None and not self._teaching.check_context():
            return False
        if self._edit_active and self._edit_document != self._edit_document_key():
            self.cancel_target_edit()
            with self.scene.client:
                ui.notify(
                    "Target preview cancelled: the program, tool, or execution state changed.",
                    color="info",
                )
            return False
        return self._edit_active

    def cancel_target_edit(self) -> None:
        if self._edit_active:
            self._end_editing_session()

    def _edit_state(self) -> dict[str, Any] | None:
        """The open target edit the browser draws joint dials for."""
        if not self._edit_active or self._teaching is not None:
            return None
        return {"active": True, "session": self._edit_session}

    def _get_joint_limits(self) -> list[tuple[float, float]]:
        """Get joint limits in radians."""
        limits = []
        for u in range(len(self.joint_names)):
            lo, hi = self._joint_travel(u)
            limits.append(
                (lo if lo is not None else -math.pi, hi if hi is not None else math.pi)
            )
        return limits

    # ---- Joint controls ----

    def _joint_admit(self, gesture: Gesture, args: dict[str, Any]) -> bool:
        index = args.get("joint")
        if (
            self._appearance_mode != RobotAppearanceMode.EDITING
            or self._teaching is not None
            or args.get("session") != self._edit_session
            or not isinstance(index, int)
            or not 0 <= index < len(self._editing_angles)
            or self._tcp_drag is not None
        ):
            return False
        gesture.data.update(
            index=index,
            q0=self._editing_angles[index],
            requested_pose=self._requested_edit_pose,
            ik_missed=self._tcp_ik_missed,
            orientation=(self._editing_rotation_set, list(self._editing_rotation)),
            edit_flags=(self._joint_ring_touched, self._unified_target_mode),
        )
        return True

    def _joint_move(self, gesture: Gesture, args: dict[str, Any]) -> None:
        values = finite(args, "q")
        if values is None or args.get("session") != self._edit_session:
            return
        self._set_editing_joint(gesture.data["index"], values[0])

    def _within_travel(self, index: int, q: float) -> float:
        lo, hi = self._joint_travel(index)
        if lo is not None:
            q = max(lo, q)
        if hi is not None:
            q = min(hi, q)
        return q

    def _joint_finish(
        self, gesture: Gesture, args: dict[str, Any] | None, aborted: bool
    ) -> None:
        index = gesture.data["index"]
        values = None if aborted or args is None else finite(args, "q")
        current = args is not None and args.get("session") == self._edit_session
        if values is not None and current:
            self._set_editing_joint(index, values[0])
        elif self._appearance_mode == RobotAppearanceMode.EDITING:
            self._set_editing_joint(index, gesture.data["q0"])
            self._joint_ring_touched, self._unified_target_mode = gesture.data[
                "edit_flags"
            ]
            self._requested_edit_pose = gesture.data["requested_pose"]
            self._tcp_ik_missed = gesture.data["ik_missed"]
            self._editing_rotation_set, self._editing_rotation = gesture.data[
                "orientation"
            ]
            self._push_tcp_pose(force=True)
            self.scene.set_gizmo_miss(self._tcp_ik_missed)
            if self._current_editing_type:
                self._update_edit_bar_values()
        # Whatever the browser shows now, the joints are where the edit has them.
        if (
            self.scene is not None
            and self._appearance_mode == RobotAppearanceMode.EDITING
        ):
            self.scene.resend_joints()

    def _set_editing_joint(self, index: int, q: float) -> None:
        if self._appearance_mode != RobotAppearanceMode.EDITING:
            return
        q = self._within_travel(index, q)
        if not self._joint_ring_touched:
            self._joint_ring_touched = True
            if not self._editing_target_id:
                self._unified_target_mode = "joint"
        self._tcp_ik_missed = False
        self._requested_edit_pose = None
        self._editing_rotation_set = False
        self._editing_angles[index] = q
        self._apply_joint_angles(self._editing_angles)
        self._push_tcp_pose()
        self._refresh_editing_pose()
        self._update_collision_highlight()
        if self._current_editing_type:
            self._update_edit_bar_values()

    def apply_editing_home(self) -> None:
        """Move editing robot to home position and sync state/UI."""
        if not self.check_edit_context():
            return
        if self._teaching is not None:
            self._notify("Finish surface teaching before homing the preview.")
            return
        home_rad = ui_state.active_robot.joints.home.rad.tolist()
        self._tcp_ik_missed = False
        self._requested_edit_pose = None
        self._editing_rotation_set = False
        self.set_editing_angles(home_rad)
        self._refresh_editing_pose()
        if self._current_editing_type:
            self._update_edit_bar_values()

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
        )
        if result and result.success:
            return list(result.angles)
        return None

    # -------------------------------------------------------------------------
    # Context menu
    # -------------------------------------------------------------------------

    def _populate_context_menu(
        self, hits: list[str], ground: tuple[float, float, float] | None
    ) -> None:
        """Fill the context menu for a right-click on *hits* (names, nearest
        first) where its ray met the floor at *ground*."""
        if not self.context_menu:
            return
        self.context_menu.clear()

        target_id = None
        for name in hits:
            if self._is_envelope_hit(name):
                continue
            if name.startswith("target:"):
                target_id = name.split("target:", 1)[1]
                break

        self._last_click_coords = ground

        shape_name = self._shape_hit_name(hits)
        draft_name = self._draft_hit_name(hits)

        with self.context_menu:
            if shape_name and not target_id:
                self._populate_shape_menu(shape_name)
            elif draft_name and not target_id:
                self._populate_draft_menu(draft_name)
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

                    # A preview still recalculating is checked again at the
                    # click; the menu stays open across it. Only what holds
                    # for this source disables an item here.
                    program = waldoctl.commander.programs.active
                    current = program is not None and preview_is_current(program)
                    reason = self._target_preview_reason(tid) if current else None
                    item = ui.menu_item("Edit Target...", on_click=make_edit()).mark(
                        "scene-edit-target"
                    )
                    item.set_enabled(
                        not self._editing_unified_target
                        and reason is None
                        and not is_any_program_running()
                    )
                    if reason:
                        item.tooltip(reason)
                    ui.menu_item("Delete Target", on_click=make_delete()).set_enabled(
                        not self._editing_unified_target
                    )
            else:
                if self._last_click_coords is not None:
                    x_mm, y_mm = [c * 1000 for c in self._last_click_coords[:2]]
                    ui.menu_item(
                        f"Place Target Here ({x_mm:.0f}, {y_mm:.0f})...",
                        on_click=lambda: self._show_unified_target_editor(
                            use_click_position=True
                        ),
                    ).mark("scene-target-here")
                ui.menu_item(
                    "Target at robot position…",
                    on_click=lambda: self._show_unified_target_editor(
                        use_click_position=False
                    ),
                ).mark("scene-target-at-robot")
                with ui.menu_item("Create keep-out", auto_close=False).mark(
                    "scene-create-keepout"
                ):
                    with ui.item_section().props("side"):
                        ui.icon("chevron_right")
                    with ui.menu().props(
                        'anchor="top end" self="top start" auto-close'
                    ):
                        self._populate_shape_add_menu(
                            self._last_click_coords or (0.3, 0.0, 0.0)
                        )
                        if ground is not None:
                            self._populate_draw_menu(ground, self._context_surface)
                with ui.menu_item("Installation", auto_close=False):
                    with ui.item_section().props("side"):
                        ui.icon("chevron_right")
                    with ui.menu().props(
                        'anchor="top end" self="top start" auto-close'
                    ):
                        self._populate_installation_menu()
            plane = self._context_surface or (
                {"origin": list(ground), "normal": [0, 0, 1], "label": "Floor"}
                if not shape_name and not target_id and ground is not None
                else None
            )
            if plane:
                ui.separator()
                ui.menu_item(
                    "Align view to surface",
                    on_click=lambda p=plane: self.scene.run_method(
                        "camera_action", "surface", p
                    ),
                ).mark("scene-align-surface")
                revision = self.object_history.context()

                def teach():
                    from waldo_commander.components.surface_teaching import (
                        SurfaceTeaching,
                    )

                    try:
                        SurfaceTeaching(self, plane, revision)
                    except ValueError as error:
                        ui.notify(str(error), color="warning")

                ui.separator()
                ui.menu_item("Teach on this surface…", on_click=teach).mark(
                    "scene-teach-surface"
                )
            if self._context_surface and (shape_name or target_id):
                with ui.menu_item("Draw keep-out", auto_close=False):
                    with ui.menu().props(
                        'anchor="top end" self="top start" auto-close'
                    ):
                        self._populate_draw_menu(ground, self._context_surface)
            from waldo_commander.components.world_library import show_world_library

            ui.separator()
            ui.menu_item(
                "Workcell library…", on_click=lambda: show_world_library(self)
            ).mark("scene-workcell-library")

    def _is_envelope_hit(self, object_name: str) -> bool:
        """Check if object is the workspace envelope."""
        return object_name == "envelope:hull"

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
        if is_any_program_running():
            ui.notify("Stop execution before editing a target.", color="warning")
            return
        # A surface-teaching session gives way before this edit's state is set.
        self.cancel_target_edit()
        endpoint = None
        if edit_target_id:
            try:
                endpoint = self._target_endpoint(edit_target_id)
            except ValueError as error:
                ui.notify(str(error), color="warning")
                return

        self._editing_unified_target = True
        self._editing_target_id = edit_target_id
        self._unified_target_mode = "cartesian"
        self._joint_ring_touched = False

        # Determine initial angles
        if edit_target_id:
            target = self._find_target_by_id(edit_target_id)
            if target is not None and target.pose and endpoint is not None:
                initial_angles, self._original_editing_pose = self._literal_start(
                    target.line_number, endpoint
                )
            else:
                initial_angles = self._get_robot_angles_rad()
        elif use_click_position and self._last_click_coords:
            click_pos = list(self._last_click_coords)
            ik_result = self._ik_for_position(click_pos)
            initial_angles = (
                ik_result if ik_result is not None else self._get_robot_angles_rad()
            )
        else:
            initial_angles = self._get_robot_angles_rad()

        self.enter_editing_mode(initial_angles)
        self._initial_editing_angles = list(self._editing_angles)
        if use_click_position and self._last_click_coords is not None:
            assert self._ik_solver is not None
            fk = self._ik_solver.forward_kinematics(self._editing_angles)
            self._requested_edit_pose = tuple(self._last_click_coords) + tuple(fk[3:6])
            self._tcp_ik_missed = ik_result is None
            self.scene.set_gizmo_miss(self._tcp_ik_missed)
            self._push_tcp_pose(force=True)
        self._refresh_editing_pose()
        if self._original_editing_pose is None:
            requested = self._requested_edit_pose
            self._original_editing_pose = (
                list(requested[:3]) + [math.degrees(v) for v in requested[3:]]
                if requested
                else self._get_editing_tcp_pose()
            )
        self._push_interaction()

        bar_type = "pose_edit" if edit_target_id else "unified"
        self._create_edit_bar(bar_type)

        self._refresh_editing_pose()

    def _delete_target(self, target_id: str) -> None:
        program = waldoctl.commander.programs.active
        if program is None or not preview_is_current(program):
            ui.notify(
                "Wait for the preview to finish before deleting this target.",
                color="warning",
            )
            return
        ui_state.editor_panel.delete_target_code(target_id)

    def _target_preview_reason(self, target_id: str) -> str | None:
        try:
            self._target_endpoint(target_id)
        except ValueError as error:
            return str(error)
        return None

    def _target_endpoint(self, target_id: str) -> list[float]:
        """The joints the preview reached at *target_id*; ValueError says why
        it cannot be edited in the scene."""
        reason = ui_state.editor_panel.target_edit_reason(target_id)
        if reason:
            raise ValueError(reason)
        target = self._find_target_by_id(target_id)
        if target is None:
            raise ValueError("This target no longer has a source binding.")
        line = target.line_number
        program = waldoctl.commander.programs.active
        if program is None or not preview_is_current(program):
            raise ValueError("Recalculate the preview before editing this target.")
        dry = program.dry_run
        record = dry.commanded
        if record is None or record.stop != "completed":
            raise ValueError(
                "This preview is incomplete; direct target editing is unavailable."
            )
        binding = target_binding(program.source, line, len(self.joint_names))
        for keyword in binding.call.keywords:
            if keyword.arg == "r":
                try:
                    radius = ast.literal_eval(keyword.value)
                except (ValueError, TypeError):
                    radius = None
                if type(radius) not in (int, float) or radius != 0:
                    raise ValueError(
                        "Blended targets have no reached corner; edit their coordinates in the program."
                    )
        blocks = [
            b
            for b in record.blocks
            if 0 <= b.command < len(dry.commands)
            and dry.commands[b.command].line_number == line
        ]
        if len(blocks) != 1 or not blocks[0].rows or blocks[0].error is not None:
            raise ValueError(
                "This target has no unambiguous completed endpoint; edit it in the program."
            )
        block = blocks[0]
        row = block.start_row + block.rows - 1
        if record.valid is not None and not record.valid[row]:
            raise ValueError(
                "This target was not reached; direct editing is unavailable."
            )
        selected = selection_before(dry.tool_selections, block.command)
        if selected is not None:
            live = waldoctl.commander.status.tool
            if (selected.tool_key, selected.variant_key or "") != (
                live.key,
                live.variant_key or "",
            ):
                raise ValueError(
                    "This target uses another tool. Select that tool before editing its preview."
                )
        return record.joints_rad[row].astype(float).tolist()

    def _literal_start(
        self, line: int, reached: list[float]
    ) -> tuple[list[float], list[float] | None]:
        """The joints an edit of the literal target at *line* starts from, and
        its pose (m, °) when written as one: the literal itself, so untouched
        coordinates keep their exact values, else the preview's *reached*
        joints, a control tick short of it."""
        program = waldoctl.commander.programs.active
        assert program is not None
        binding = target_binding(program.source, line, len(self.joint_names))
        values = [float(ast.literal_eval(e)) for e in binding.vector.elts]
        if binding.kind == "joints":
            return [math.radians(v) for v in values], None
        pose = [v / 1000 for v in values[:3]] + values[3:6]
        if not self._ensure_ik_solver() or self._ik_solver is None:
            return reached, pose
        position = np.array(pose[:3], dtype=np.float64)
        orientation = np.radians(np.array(values[3:6], dtype=np.float64))
        result = self._ik_solver.solve(
            target_pos=position,
            current_angles=reached,
            target_orientation=orientation,
        )
        if not result.success:
            return reached, pose
        fk = self._ik_solver.forward_kinematics(result.angles)
        turn = (fk[3:6] - orientation + math.pi) % (2 * math.pi) - math.pi
        if np.max(np.abs(fk[:3] - position)) > 1e-6 or np.max(np.abs(turn)) > 1e-5:
            return reached, pose
        return list(result.angles), pose

    def _valid_edit_request(self) -> bool:
        if self._tcp_ik_missed:
            ui.notify(
                "The requested pose is unreachable. Adjust it or cancel before saving.",
                color="warning",
            )
            return False
        return self.check_edit_context() and self._editing_unified_target

    def _unchanged_existing_target(self) -> bool:
        """An edit of an existing target that moved nothing: its literals stay
        as written rather than being rewritten from the preview's row."""
        return (
            self._editing_target_id is not None
            and self._requested_edit_pose is None
            and self._editing_angles == self._initial_editing_angles
        )

    def _confirm_unified_as_cartesian(self) -> None:
        """Confirm as cartesian target."""
        if not self._valid_edit_request():
            return
        if self._unchanged_existing_target():
            self._end_editing_session()
            return

        pose = self._get_editing_tcp_pose()
        if pose is None:
            return

        if self._editing_target_id:
            target = self._find_target_by_id(self._editing_target_id)
            if target:
                if not ui_state.editor_panel.sync_code_from_target(
                    self._editing_target_id,
                    pose,
                    joint_angles_deg=[math.degrees(q) for q in self._editing_angles],
                ):
                    return
                if self._editing_target_id in self._target_objects:
                    group = self._target_objects[self._editing_target_id].get("group")
                    if group:
                        group.move(pose[0], pose[1], pose[2])
        else:
            self._add_target_with_pose(pose, "cartesian")

        self._end_editing_session()

    def _confirm_unified_as_joint(self) -> None:
        """Confirm as joint target (converts move_l→move_j if editing existing)."""
        if not self._valid_edit_request():
            return
        if self._unchanged_existing_target():
            self._end_editing_session()
            return

        if self._editing_target_id:
            target = self._find_target_by_id(self._editing_target_id)
            angles_deg = [math.degrees(q) for q in self._editing_angles]
            if target:
                pose = target.pose
                if not ui_state.editor_panel.sync_code_from_target(
                    self._editing_target_id,
                    pose,
                    move_type="joints",
                    joint_angles_deg=angles_deg,
                ):
                    return
            self._end_editing_session()
            return

        self._insert_joint_target_from_editing()
        self._end_editing_session()

    def _end_editing_session(self) -> None:
        """End editing and clean up."""
        if self._teaching is not None:
            teaching, self._teaching = self._teaching, None
            teaching.close()
        self._editing_unified_target = False
        self._editing_target_id = None
        self._unified_target_mode = "cartesian"
        self._joint_ring_touched = False
        self._original_editing_pose = None
        self._initial_editing_angles = None
        self._hide_edit_bar()
        self.exit_editing_mode()

    def _handle_keyboard(self, e) -> None:
        """Escape cancels a target edit or a keep-out move."""
        # In the editor, Escape closes the editor's own popups.
        if keybindings_manager.editor_focused:
            return
        if e.key == "Escape" and e.action.keydown:
            if self._edit_active:
                self._end_editing_session()
            if self._shape_move_active:
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
                    "wc-target-edit-bar absolute left-3 top-3 z-50 pointer-events-auto"
                )

        self._edit_coordinates = []
        self._edit_deltas = []
        with self._edit_bar_container:
            # A card, not a plain column: its fields update at drag rate and
            # a bare-div ancestor re-renders the whole page each time.
            self._edit_bar = ui.card().classes("overlay-card gap-0 p-2")
            with self._edit_bar:
                with ui.row().classes("items-center w-full flex-nowrap"):
                    self._edit_bar_label = ui.label("").classes("text-xs font-medium")
                    ui.space()
                    ui.button(icon="close", on_click=self._on_edit_bar_cancel).props(
                        "flat dense size=sm color=wc-text-muted"
                    ).mark("edit-bar-cancel")
                    self._edit_confirm = (
                        ui.button(icon="check", on_click=self._on_edit_bar_confirm)
                        .props("flat dense size=sm color=wc-action")
                        .mark("edit-bar-confirm")
                    )
                self._edit_bar_values = ui.column().classes("gap-0")
                with self._edit_bar_values:
                    with ui.row().classes("flex-nowrap gap-1"):
                        for index in range(3):
                            self._target_coordinate_field(index)
                    self._edit_orientation = (
                        ui.expansion("Orientation")
                        .props("dense header-class=text-xs")
                        .classes("w-full")
                        .mark("target-orientation")
                    )
                    with self._edit_orientation, ui.row().classes("flex-nowrap gap-1"):
                        for index in range(3, 6):
                            self._target_coordinate_field(index)
                self._edit_validity = ui.label("Unreachable · adjust position").classes(
                    "text-xs text-wc-error"
                )
                self._edit_validity.set_visibility(False)

        self._current_editing_type = editing_type
        self._update_edit_bar_content(editing_type)
        self._refresh_editing_pose()
        self._update_edit_bar_values()

    def _target_coordinate_field(self, index: int) -> None:
        axis = ("x", "y", "z", "rx", "ry", "rz")[index]
        with ui.column().classes("gap-0"):
            number = (
                ui.number(axis.upper(), format="%.1f", step=0.1)
                .props(
                    'dense borderless hide-bottom-space suffix="'
                    + ("mm" if index < 3 else "°")
                    + '"'
                )
                .classes("w-20")
                .mark(f"target-coordinate-{axis}")
            )
            state = {"focused": False, "dirty": False, "updating": False}
            number.on("focus", lambda: state.update(focused=True, dirty=False))
            number.on_value_change(
                lambda: state["focused"]
                and not state["updating"]
                and state.update(dirty=True)
            )

            def submit():
                if state["dirty"]:
                    state["dirty"] = False
                    self._set_target_coordinate(index, number.value)

            def blur():
                state["focused"] = False
                submit()

            number.on("keydown.enter", submit)
            number.on("blur", blur)
            self._edit_coordinates.append((number, state))
            self._edit_deltas.append(
                ui.label("").classes("text-xs text-wc-text-muted font-mono")
            )

    def _update_edit_bar_content(self, editing_type: str) -> None:
        """Update edit bar label."""
        if not self._edit_bar_label:
            return
        labels = {
            "joint": "Target · World",
            "unified": "Target · World",
            "pose_edit": "Target · World",
        }
        self._edit_bar_label.text = labels.get(editing_type, "Editing Target")

    def _set_target_coordinate(self, index: int, value: float | None) -> None:
        # A drag in progress owns the request until it lets go.
        if (
            value is None
            or not math.isfinite(value)
            or self._tcp_drag is not None
            or not self.check_edit_context()
        ):
            self._update_edit_bar_values()
            return
        requested = self._requested_edit_pose or self._tcp_placed_pose
        if requested is None:
            return
        self._tcp_world_p[:] = requested[:3]
        self._tcp_world_rpy[:] = requested[3:6]
        self._tcp_drag_mode = "translate" if index < 3 else "rotate"
        if index >= 3:
            self._tcp_world_rpy[index - 3] = math.radians(value)
        else:
            self._tcp_world_p[index] = value / 1000
        self._handle_tcp_transform_for_ik()
        self._push_tcp_pose(force=True)

    def _update_edit_bar_values(self) -> None:
        if not self._edit_bar_values:
            return
        requested = self._requested_edit_pose or self._tcp_placed_pose
        if requested is None:
            return
        original = self._original_editing_pose or requested
        for index, (number, state) in enumerate(self._edit_coordinates):
            rotation = index >= 3
            value = (
                math.degrees(requested[index]) if rotation else requested[index] * 1000
            )
            if not state["focused"] or not state["dirty"]:
                state["updating"] = True
                number.set_value(round(value, 1))
                state["updating"] = False
            delta = (
                (value - original[index] + 180) % 360 - 180
                if rotation
                else value - original[index] * 1000
            )
            self._edit_deltas[index].set_text(f"Δ {delta:+.1f}")
        if self._edit_confirm:
            self._edit_confirm.set_enabled(not self._tcp_ik_missed)
        if self._edit_validity:
            self._edit_validity.set_visibility(self._tcp_ik_missed)

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
        self._edit_coordinates = []
        self._edit_deltas = []
        self._edit_orientation = None
        self._edit_confirm = None
        self._edit_validity = None

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

    def _get_editing_tcp_pose(self) -> list[float] | None:
        """Preview TCP in metres and degrees, independent of backend status."""
        return list(self._editing_tcp_pose) if self._editing_tcp_pose else None

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
        ui_state.editor_panel.add_joint_target_code(
            [math.degrees(q) for q in self._editing_angles]
        )

    def _refresh_editing_pose(self) -> tuple[float, ...] | None:
        """Follow the editing joints with the TCP readout and envelope; the
        FK pose (m, rad) the gizmo's frame was placed from is reused."""
        if not self._edit_active or self._ik_solver is None:
            return None
        key = tuple(self._editing_angles[: len(self.joint_names)])
        if key != self._last_fk_angles_tuple or self._last_fk_pose is None:
            self._last_fk_pose = tuple(
                float(v)
                for v in self._ik_solver.forward_kinematics(self._editing_angles)[:6]
            )
            self._last_fk_angles_tuple = key
            self._last_fk_angles_raw = None
        fk = self._last_fk_pose
        self._editing_tcp_pose = [float(v) for v in fk[:3]] + [
            math.degrees(v) for v in fk[3:6]
        ]
        self._update_envelope_from_robot_state(tuple(self._editing_tcp_pose[:3]))
        return fk
