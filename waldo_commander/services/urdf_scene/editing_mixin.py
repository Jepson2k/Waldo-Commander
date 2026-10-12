"""
Editing Mixin for UrdfScene.

Provides target editing functionality:
- Enter/exit editing mode: a target edit moves a marker at the target with
  the gizmo while the arm stays live; surface teaching previews the arm
- Context menu and target CRUD; a target edit writes its line as it goes
"""

import ast
import logging
import math
from typing import Any

import numpy as np
import waldoctl
from nicegui import ui

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
    _solve_target_reach: Any
    _gizmo_miss_sent: bool | None
    _gizmo_choices: dict[str, str]
    _gizmo_frames: dict[str, dict[str, Any]]
    _keep_tool_vertical: bool
    _gizmo_alignment_state: Any
    _gizmo_shown: Any
    _vertical_rotation: Any

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
        # A target edit moves the target, not the arm; surface teaching
        # previews the arm at its joints.
        self._edit_target_first: bool = False
        # The pose (m, rad) the edit last wrote into the program.
        self._edit_start_pose: tuple[float, ...] | None = None
        # The line the edit writes, and the target marker it moves: the
        # target's id changes as the preview runs again, its line does not.
        self._edit_line: int | None = None
        self._edit_marker: Any | None = None
        # The source as the edit last left it: a change from anywhere else
        # ends the edit.
        self._edit_source: str | None = None

    # -------------------------------------------------------------------------
    # Core editing mode
    # -------------------------------------------------------------------------

    def enter_editing_mode(
        self, joint_angles: list[float], *, preview_arm: bool = True
    ) -> None:
        """Enter editing mode at *joint_angles*; with *preview_arm* the arm is
        drawn at them, otherwise it stays live and only the target moves."""
        if self._teaching is not None:
            self.cancel_target_edit()
        self._cancel_shape_drawing()
        self.suspend_hover()
        n = len(self.joint_names)

        # Pad or truncate to joint count
        self._editing_angles = list(joint_angles) + [0.0] * (n - len(joint_angles))
        self._editing_angles = self._editing_angles[:n]

        self._requested_edit_pose = None
        self._tcp_ik_missed = False
        self._edit_target_first = not preview_arm

        if preview_arm:
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

        self._editing_tcp_pose = None
        self._requested_edit_pose = None
        self._edit_target_first = False

        if self._appearance_mode == RobotAppearanceMode.EDITING:
            self._apply_joint_angles(list(waldoctl.commander.status.joints.angles.rad))
            if waldoctl.commander.status.simulator_active:
                self.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
            else:
                self.set_appearance_mode(RobotAppearanceMode.LIVE)

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
        if (
            self._edit_active
            and self._edit_document != self._edit_document_key()
            and self._edit_source is not None
            and self._program_text() == self._edit_source
        ):
            # The program changed by this edit's own writes.
            self._edit_document = self._edit_document_key()
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
        """The open target edit, whose marker the browser draws the gizmo on."""
        if not self._edit_active or self._teaching is not None:
            return None
        marker = self._edit_marker
        return {
            "active": True,
            "session": self._edit_session,
            "marker": marker.id if marker is not None else None,
        }

    def _get_joint_limits(self) -> list[tuple[float, float]]:
        """Get joint limits in radians."""
        limits = []
        for u in range(len(self.joint_names)):
            lo, hi = self._joint_travel(u)
            limits.append(
                (lo if lo is not None else -math.pi, hi if hi is not None else math.pi)
            )
        return limits

    def apply_editing_home(self) -> None:
        """Move editing robot to home position and sync state/UI."""
        if not self.check_edit_context():
            return
        if self._teaching is not None:
            self._notify("Finish surface teaching before homing the preview.")
            return
        home_rad = ui_state.active_robot.joints.home.rad.tolist()
        if self._ensure_ik_solver() is None or self._ik_solver is None:
            return
        self._editing_angles[:] = home_rad[: len(self._editing_angles)]
        self._requested_edit_pose = tuple(
            float(v)
            for v in self._ik_solver.forward_kinematics(self._editing_angles)[:6]
        )
        self._tcp_ik_missed = False
        self.scene.set_gizmo_miss(False)
        self._refresh_editing_pose()
        self._push_tcp_pose(force=True)
        self._write_target_edit()

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
        if "gizmo:tcp" in hits and self._gizmo_shown():
            self._populate_gizmo_menu()
            return

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

    def _populate_gizmo_menu(self) -> None:
        state = self._gizmo_alignment_state(refresh=True)
        self._push_interaction()
        session = (self._edit_active, self._edit_session)

        def current() -> bool:
            return (
                session == (self._edit_active, self._edit_session)
                and self._gizmo_shown()
                and (not self._edit_active or self.check_edit_context())
            )

        def align(key: str) -> None:
            if not current():
                return
            self.scene.gestures.bump()
            self._gizmo_choices["target" if self._edit_active else "live"] = key
            self._push_interaction()

        def vertical() -> None:
            if not current() or not self._editing_unified_target:
                return
            self.scene.gestures.bump()
            self._keep_tool_vertical = not self._keep_tool_vertical
            if self._keep_tool_vertical and self._requested_edit_pose is not None:
                from pinokin import so3_from_rpy, so3_rpy

                pose = self._requested_edit_pose
                rotation, rpy = np.empty((3, 3)), np.empty(3)
                so3_from_rpy(pose[3], pose[4], pose[5], rotation)
                so3_rpy(self._vertical_rotation(rotation), rpy)
                self._requested_edit_pose = pose[:3] + tuple(rpy)
                self._solve_target_reach()
                self._refresh_editing_pose()
                self._write_target_edit()
                self._push_tcp_pose(force=True)
            self._push_interaction()

        def item(text, icon, checked, callback, mark):
            with (
                ui.menu_item(on_click=callback)
                .mark(mark)
                .props(f"role=menuitemcheckbox aria-checked={str(checked).lower()}")
            ):
                with ui.item_section().props("avatar"):
                    ui.icon(icon)
                ui.item_section(text)
                with ui.item_section().props("side"):
                    ui.icon("check").set_visibility(checked)

        with self.context_menu:
            ui.item("Align to").classes("text-wc-text-muted text-xs").props("dense")
            for key, name, icon in [
                ("world", "World", "public"),
                ("tool", "Tool", "build"),
                *(
                    (key, frame["name"], "place")
                    for key, frame in self._gizmo_frames.items()
                ),
            ]:
                item(
                    name,
                    icon,
                    key == state["key"],
                    lambda k=key: align(k),
                    f"scene-gizmo-align-{key}",
                )
            if self._editing_unified_target:
                ui.separator()
                item(
                    "Keep tool vertical",
                    "vertical_align_bottom",
                    self._keep_tool_vertical,
                    vertical,
                    "scene-gizmo-vertical",
                )

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

        solver = self._ensure_ik_solver()
        if solver is None:
            ui.notify(
                "Target editing needs the robot model's kinematics.", color="warning"
            )
            return
        live = self._get_robot_angles_rad()
        initial_angles = live
        start_pose: tuple[float, ...] | None = None
        if edit_target_id:
            target = self._find_target_by_id(edit_target_id)
            if target is not None and target.pose and endpoint is not None:
                initial_angles, literal = self._literal_start(
                    target.line_number, endpoint
                )
                if literal is not None:
                    start_pose = tuple(literal[:3]) + tuple(
                        math.radians(v) for v in literal[3:6]
                    )
        if start_pose is None:
            fk = solver.forward_kinematics(initial_angles)
            start_pose = tuple(float(v) for v in fk[:6])
            # A target placed on the floor keeps the tool's current orientation.
            if not edit_target_id and use_click_position and self._last_click_coords:
                start_pose = tuple(self._last_click_coords) + start_pose[3:6]

        if edit_target_id:
            line = ui_state.editor_panel.target_line(edit_target_id)
        else:
            # A new target is in the program from the moment it is placed.
            line = self._add_target_with_pose(
                [*start_pose[:3], *(math.degrees(v) for v in start_pose[3:6])],
                "cartesian",
            )
            edit_target_id = f"pending_{line}"
        if line is None:
            return
        self._editing_unified_target = True
        self._keep_tool_vertical = False
        self._editing_target_id = edit_target_id
        self._edit_line = line
        self.enter_editing_mode(initial_angles, preview_arm=False)
        self._edit_source = self._program_text()
        self._requested_edit_pose = start_pose
        self._edit_start_pose = start_pose
        self._gizmo_miss_sent = None
        self._solve_target_reach()
        self._refresh_editing_pose()
        self.adopt_edit_marker()
        self._push_tcp_pose(force=True)
        self._push_interaction()

    def adopt_edit_marker(self) -> None:
        """Move the edited target's own marker, under whatever id the target
        has now that the preview may have run again."""
        if not self._editing_unified_target or self._edit_line is None:
            return
        program = waldoctl.commander.programs.active
        target = next(
            (
                t
                for t in (program.dry_run.targets if program else ())
                if t.line_number == self._edit_line
            ),
            None,
        )
        drawn = self._target_objects.get(target.id) if target else None
        if target is None or drawn is None:
            return
        self._editing_target_id = target.id
        if drawn["group"] is self._edit_marker:
            return
        self._edit_marker = drawn["group"]
        pose = self._requested_edit_pose
        if pose is not None:
            self._edit_marker.move(pose[0], pose[1], pose[2])
        self._push_interaction()

    @staticmethod
    def _program_text() -> str | None:
        textarea = ui_state.active_textarea
        return None if textarea is None else str(textarea.value or "")

    def _write_target_edit(self) -> None:
        """Write where the target is now into its line, as one undoable edit.
        Released out of the arm's reach, it goes back to where it was last
        written instead."""
        pose = self._requested_edit_pose
        if (
            not self._editing_unified_target
            or self._edit_line is None
            or pose is None
            or pose == self._edit_start_pose
            or not self.check_edit_context()
        ):
            return
        if self._tcp_ik_missed:
            self._requested_edit_pose = self._edit_start_pose
            self._solve_target_reach()
            self._refresh_editing_pose()
            self.scene.set_gizmo_miss(self._tcp_ik_missed)
            self._push_tcp_pose(force=True)
            return
        written = self._get_editing_tcp_pose()
        if written is None or not ui_state.editor_panel.sync_code_at_line(
            self._edit_line,
            written,
            joint_angles_deg=[math.degrees(q) for q in self._editing_angles],
        ):
            return
        self._edit_start_pose = pose
        self._edit_source = self._program_text()

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

    def _end_editing_session(self) -> None:
        """Stop editing; what the edit wrote stays."""
        if self._teaching is not None:
            teaching, self._teaching = self._teaching, None
            teaching.close()
        marker, self._edit_marker = self._edit_marker, None
        written = self._edit_start_pose
        if marker is not None and not marker.deleted and written is not None:
            marker.move(written[0], written[1], written[2])
        self._editing_unified_target = False
        self._keep_tool_vertical = False
        self._editing_target_id = None
        self._edit_start_pose = None
        self._edit_line = None
        self._edit_source = None
        self.exit_editing_mode()

    def _handle_keyboard(self, e) -> None:
        """Escape ends a target edit, keeping what it wrote, or a keep-out
        move."""
        # In the editor, Escape closes the editor's own popups.
        if keybindings_manager.editor_focused or not e.action.keydown:
            return
        if e.key == "Escape":
            if self._edit_active:
                self._end_editing_session()
            if self._shape_move_active:
                self._end_shape_move()

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

    def _add_target_with_pose(self, pose: list[float], move_type: str) -> int | None:
        """Insert a move to *pose* (m, °) into the program; its line."""
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
        return line_number

    def _refresh_editing_pose(self) -> tuple[float, ...] | None:
        """Follow the edit with the TCP readout and envelope: the target pose
        (m, rad) of a target edit, else the FK pose of the editing joints,
        reused from the gizmo's placement."""
        if not self._edit_active or self._ik_solver is None:
            return None
        if self._edit_target_first:
            pose = self._requested_edit_pose
            if pose is None:
                return None
            self._editing_tcp_pose = [float(v) for v in pose[:3]] + [
                math.degrees(v) for v in pose[3:6]
            ]
            self._update_envelope_from_robot_state(tuple(self._editing_tcp_pose[:3]))
            return pose
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
