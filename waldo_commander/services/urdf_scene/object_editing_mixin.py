"""Contextual object selection and authored-world controls."""

from __future__ import annotations

from dataclasses import replace
import json
import math
from collections.abc import Callable
from typing import Any

from .config import DRAFT_PREFIX
from .mesh_editing_mixin import MeshEditingMixin
from waldo_commander.services.fixture_meshes import MESH_PREFIX, FixtureMesh
from .shape_editing_mixin import _PLACEABLE, RESIZE_AXES, object_label

from nicegui import background_tasks, ui
from waldoctl.shapes import INSTALL_PREFIX, SHAPE_PREFIX, param_names

from waldo_commander.common.theme import OPACITY
from waldo_commander.services.scene_history import SceneHistory
from waldo_commander.state import ui_state

# What the scene panel's list shows each kind of object as.
_KIND_ICONS = {
    "box": "check_box_outline_blank",
    "cylinder": "sym_o_database",
    "sphere": "radio_button_unchecked",
}
_DIMENSION_PREFIXES = {"radius": "R", "length": "L"}
# A button that opens a menu shows its tooltip where the menu cannot open:
# above one at the panel's top, beside one that may open either way.
_TIP_ABOVE = 'anchor="top middle" self="bottom middle"'
_TIP_BESIDE = 'anchor="center left" self="center right"'

_RESIZE_TIPS = {
    "box": "Resize: drag a face; Shift resizes from the centre",
    "cylinder": "Resize: drag the radius or an end",
    "sphere": "Resize: drag the radius",
}


def _shown(value: float, scale: float) -> float:
    """*value* as an inspector field shows it."""
    return round(value * scale, 3)


def _selection_title(shapes) -> str:
    """What the selection is, in the words the scene's menus use."""
    if len(shapes) == 1:
        return object_label(shapes[0])
    if all(object_label(s) == "Keep-out" for s in shapes):
        return f"{len(shapes)} keep-outs"
    return f"{len(shapes)} objects"


class _InspectorControls:
    """The built inspector's controls that follow edit status and values."""

    def __init__(self) -> None:
        self.locked = False
        self.editable_elements: list[Any] = []
        self.values: dict[str, tuple[Any, Callable[[Any], Any]]] = {}
        self.shown: dict[str, Any] = {}
        self.feedback: Any = None
        self.isolate: Any = None
        self.modes: dict[str, Any] = {}
        self.resize_centre: Any = None
        self.attachment: list[Any] = []

    def editable(self, element: Any, readonly: bool) -> Any:
        element.set_enabled(not readonly)
        self.editable_elements.append(element)
        return element

    def track(self, element: Any, key: str, value: Callable[[Any], Any]) -> None:
        self.values[key] = (element, value)


class ObjectEditingMixin(MeshEditingMixin):
    inspection: Any
    _install_ground: set[str]
    _current_tool: str
    cancel_target_edit: Any
    _editing_unified_target: bool
    _end_editing_session: Any

    def _init_object_editing(self) -> None:
        self._init_fixture_meshes()
        self.object_history = SceneHistory(self)
        self._init_shape_drawing()
        self._selected_objects: list[str] = []
        self._multi_select = False
        self._resize_centre = False
        self._hidden_objects: set[str] = set()
        self._locked_objects: set[str] = set()
        self._mounted_panel = None
        self._object_tree_key = None
        self._tree_rows: dict[str, Any] = {}
        self._tree_selected: tuple[str, ...] = ()
        self._object_inspector_key = None
        self._inspector_controls = _InspectorControls()
        self._object_mode = "translate"
        self._isolated_view = False
        self._object_renames = ()

    def _set_object_renames(self, renames):
        self._object_renames = renames
        self._push_interaction()

    def _on_isolation_view(self, event):
        isolated = bool(event.args.get("isolated", False))
        if isolated != self._isolated_view:
            self._isolated_view = isolated
            self.object_history.feedback = ""
            self._refresh_object_inspector()

    def _toggle_isolation(self):
        self.scene.run_method(
            "camera_action", "restore_objects" if self._isolated_view else "isolate"
        )

    def _object_entities(self) -> dict:
        handle = self._shape_handle()
        proxies = {
            f"{SHAPE_PREFIX}{name}"
            for fixture in self.fixture_meshes
            for name in fixture.proxy_names
        }
        return {
            "revision": handle.revision,
            "selected": self._selected_objects,
            "renames": [
                [f"{SHAPE_PREFIX}{before}", f"{SHAPE_PREFIX}{after}"]
                for before, after in self._object_renames
            ],
            "items": [
                {
                    "id": name,
                    "node": node.id,
                    "fixture": {
                        "fill": float(OPACITY["opacity-fixture-fill"]),
                        "edge": float(OPACITY["opacity-fixture-edge"]),
                    }
                    if name in proxies
                    else None,
                }
                for name, node in self._shape_objects.items()
                if name.startswith((SHAPE_PREFIX, INSTALL_PREFIX, MESH_PREFIX))
                and name not in self._hidden_objects
                and node.glow
            ],
        }

    def _object_shape(self, key):
        if key.startswith(MESH_PREFIX):
            return self._fixture(key)
        handle = self._shape_handle()
        if handle is None:
            return None
        layer = handle.shapes if key.startswith(SHAPE_PREFIX) else handle.installation
        prefix = SHAPE_PREFIX if key.startswith(SHAPE_PREFIX) else INSTALL_PREFIX
        return next((s for s in layer if f"{prefix}{s.name}" == key), None)

    def _on_object_selection(self, event) -> None:
        args = event.args
        handle = self._shape_handle()
        if not isinstance(args, dict) or args.get("revision") != handle.revision:
            self._push_interaction()
            return
        hits = [
            h["id"]
            for h in args.get("hits", [])
            if isinstance(h, dict) and h.get("id") in self._shape_objects
        ]
        if not hits and self._editing_unified_target:
            # A click on nothing puts a target edit away, keeping it.
            self._end_editing_session()
        if hits:
            key = hits[0]
            for fixture in self.fixture_meshes:
                if fixture.key in hits and key in self._fixture_keys(fixture):
                    key = fixture.key
                    break
            self._pick_from_tree(key, {"ctrlKey": args.get("extend")})
        else:
            self._select_objects([])

    def _select_objects(self, selected) -> None:
        before = set(self._selected_objects)
        self._cancel_shape_drawing()
        self._end_shape_move()
        self._selected_objects = [
            key
            for key in self._expand_fixture_selection(selected)
            if self._object_shape(key) is not None
        ]
        if set(self._selected_objects) != before:
            self.object_history.feedback = ""
        if self._attachment_form and self._selected_objects != [
            self._attachment_form[0]
        ]:
            self._attachment_form = None
        if self._object_mode == "resize" and (
            len(self._selected_objects) != 1
            or any(
                self._object_shape(key).kind not in RESIZE_AXES
                for key in self._selected_objects
            )
        ):
            self._object_mode = "translate"
        if self._selected_objects:
            self.inspection.clear_cursor()
            self.cancel_target_edit()
        if self._selected_objects and all(map(self._movable, self._selected_objects)):
            key = self._selected_objects[0]
            self._start_shape_move(self._move_key(self._object_shape(key)))
        self._push_interaction()
        self._refresh_object_inspector()
        # A newly selected object opens the panel; one only refreshed, as an
        # undo or a rename does, leaves it as the user left it.
        if (
            set(self._selected_objects) - before
            and (panel := self._scene_panel()) is not None
        ):
            panel.open()

    def _prune_object_marks(self, handle) -> None:
        """Hidden and locked follow a rename and go with their object, so a
        later object given the same name starts out shown and unlocked."""
        if (
            handle.confirmed
            and not self.object_history.busy
            and not self._fixture_restore_pending
        ):
            names = {s.name for s in handle.shapes}
            meshes = tuple(
                replace(m, proxy_names=tuple(n for n in m.proxy_names if n in names))
                for m in self.fixture_meshes
            )
            if meshes != self.fixture_meshes:
                self._set_fixture_meshes(meshes)
        renamed = {
            f"{SHAPE_PREFIX}{before}": f"{SHAPE_PREFIX}{after}"
            for before, after in self._object_renames
        }
        existing = (
            {m.key for m in self.fixture_meshes}
            | {f"{SHAPE_PREFIX}{s.name}" for s in handle.shapes}
            | {f"{INSTALL_PREFIX}{s.name}" for s in handle.installation}
        )

        def kept(marks: set[str]) -> set[str]:
            return {k if k in existing else renamed.get(k, k) for k in marks} & existing

        self._selected_objects = [
            k if k in existing else renamed.get(k, k) for k in self._selected_objects
        ]

        self._locked_objects = kept(self._locked_objects)
        hidden = kept(self._hidden_objects)
        for key in hidden - self._hidden_objects:
            if node := self._shape_objects.get(key):
                node.visible(False)
        self._hidden_objects = hidden

    def _scene_panel(self):
        """The page's scene panel, its toolbar and body filled by this scene;
        None on a page without one."""
        panel = ui_state.scene_panel
        if (
            panel is None
            or panel.body.is_deleted
            or panel.body.client is not self.scene.client
        ):
            return None
        if self._mounted_panel is not panel:
            self._mount_scene_panel(panel)
        return panel

    def _mount_scene_panel(self, panel) -> None:
        from waldo_commander.components.world_library import show_world_library

        self._mounted_panel = panel
        self._object_tree_key = None
        self._object_inspector_key = None
        flat = "flat round dense color=wc-text"
        with panel.tools:
            self._multi_select_button = (
                ui.button(icon="library_add_check", on_click=self._toggle_multi_select)
                .props(flat)
                .tooltip("Select several — taps add to or remove from the selection")
                .mark("scene-multi-select")
            )
            self._show_multi_select()
            ui.button(
                icon="upload_file", on_click=lambda: self._open_mesh_import()
            ).props(flat).tooltip("Import a fixture mesh (STL)").mark(
                "scene-import-mesh"
            )
            with (
                ui.button(icon="add_box").props(flat).mark("scene-draw") as draw,
                ui.menu().props('anchor="bottom right" self="top right"'),
            ):
                for kind in _PLACEABLE:
                    with (
                        ui.menu_item(on_click=lambda k=kind: self._draw_on_floor(k))
                        .props("dense")
                        .mark(f"scene-panel-draw-{kind}"),
                        ui.row().classes("items-center no-wrap gap-2"),
                    ):
                        ui.icon(_KIND_ICONS[kind], size="20px")
                        ui.label(kind.capitalize())
            with draw:
                ui.tooltip("Draw a keep-out on the floor").props(_TIP_ABOVE)
            ui.button(
                icon="inventory_2", on_click=lambda: show_world_library(self)
            ).props(flat).tooltip("Workcells: save, load and insert").mark(
                "scene-workcells"
            )
            # The wrapper keeps the reason accessible while Undo is disabled.
            with ui.element("span").classes("inline-flex"):
                undo = (
                    ui.button(
                        icon="undo", on_click=lambda: self._restore_objects(False)
                    )
                    .props(flat)
                    .mark("object-undo")
                )
                self._undo_tooltip = ui.tooltip("Undo object edit · Ctrl+Z").mark(
                    "object-undo-tooltip"
                )
            self._history_buttons = (
                undo,
                ui.button(icon="redo", on_click=lambda: self._restore_objects(True))
                .props(flat)
                .tooltip("Redo object edit · Ctrl+Shift+Z")
                .mark("object-redo"),
            )
        with panel.body:
            self._scene_feedback = (
                ui.label().classes("text-xs text-wc-error").mark("scene-feedback")
            )
            self._fixture_feedback = (
                ui.label()
                .classes("text-xs text-wc-warning")
                .mark("fixture-recovery-feedback")
            )
            self._library_host = ui.column().classes("w-full gap-0")
            self._object_tree = (
                ui.column().classes("scene-tree w-full").mark("scene-tree")
            )
            self._object_properties = ui.column().classes("scene-properties w-full")

    def _show_multi_select(self) -> None:
        self._multi_select_button.props(
            f"flat round dense color={'wc-action' if self._multi_select else 'wc-text'} "
            f"aria-pressed={'true' if self._multi_select else 'false'}"
        )

    def _toggle_multi_select(self) -> None:
        self._multi_select = not self._multi_select
        self.object_history.feedback = ""
        self._show_multi_select()
        self._refresh_object_inspector()

    def _draw_on_floor(self, kind: str) -> None:
        self._start_shape_drawing(
            kind,
            {"origin": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0], "label": "Floor"},
            self.object_history.context(),
        )

    def _refresh_object_inspector(self, *, reset_values: bool = False) -> None:
        """Bring the scene panel up to the scene: its list of objects and the
        selection's properties; *reset_values* puts every field back to the
        object's value after an edit was refused."""
        if self.scene is None or self.scene.is_deleted:
            return
        self.object_history.validate()
        handle = self._shape_handle()
        if handle is not None:
            self._prune_object_marks(handle)
        selected = [
            (key, shape)
            for key in self._selected_objects
            if (shape := self._object_shape(key)) is not None
        ]
        remaining = [key for key, _ in selected]
        if remaining != self._selected_objects:
            self.object_history.feedback = ""
        self._selected_objects = remaining
        panel = self._scene_panel()
        if panel is None:
            return
        history = self.object_history
        status = (
            history.busy,
            history.feedback,
            len(history.undo_stack),
            len(history.redo_stack),
            handle is not None and handle.confirmed,
        )
        self._scene_feedback.set_text(history.feedback)
        self._scene_feedback.set_visibility(bool(history.feedback) and not selected)
        self._fixture_feedback.set_text(self._fixture_restore_error)
        self._fixture_feedback.set_visibility(bool(self._fixture_restore_error))
        undo, redo = self._history_buttons
        undo.set_enabled(bool(history.undo_stack) and not history.busy)
        redo.set_enabled(bool(history.redo_stack) and not history.busy)
        self._undo_tooltip.set_text(
            "Undo history cleared — the world or program changed elsewhere"
            if history.invalidated
            else "Undo object edit · Ctrl+Z"
        )
        self._refresh_object_tree(handle, panel)
        if any(isinstance(s, FixtureMesh) for _, s in selected):
            self._fixture_inspector(selected, status)
            return
        # What the properties are built from; status and shape values are
        # updated in place, so an edit does not rebuild the field being typed in.
        structure = (
            tuple(
                (k, s.kind, s.attachment is not None, s.collision) for k, s in selected
            ),
            frozenset(k for k, _ in selected if k in self._hidden_objects),
            frozenset(k for k, _ in selected if k in self._locked_objects),
            self._attachment_form,
            None if selected else self._isolated_view,
        )
        if structure == self._object_inspector_key:
            if selected:
                self._update_object_inspector(selected, status, reset_values)
            return
        self._object_inspector_key = structure
        controls = self._inspector_controls = _InspectorControls()
        properties = self._object_properties
        properties.clear()
        if not selected:
            if self._isolated_view:
                with properties, ui.row().classes("w-full items-center no-wrap"):
                    ui.label("Isolated").classes("scene-section flex-1")
                    ui.button(
                        "Show all",
                        icon="flip_to_front",
                        on_click=self._toggle_isolation,
                    ).props("flat dense no-caps color=wc-action").mark("object-isolate")
            return
        controls.locked = any(
            not k.startswith(SHAPE_PREFIX)
            or k in self._locked_objects
            or s.attachment is not None
            for k, s in selected
        )
        readonly = history.busy or controls.locked
        flat = "flat round dense color=wc-text"
        with properties, ui.column().classes("w-full gap-1").mark("object-inspector"):
            with ui.row().classes("w-full items-center no-wrap gap-0"):
                if len(selected) == 1:
                    entity, shape = selected[0]
                    name = (
                        ui.input(value=shape.name)
                        .props("dense borderless hide-bottom-space")
                        .classes("scene-name flex-1 min-w-0")
                        .tooltip(object_label(shape))
                        .mark("object-name")
                    )
                    controls.editable(name, readonly)
                    controls.track(name, "name", lambda s: s.name)
                    for event in ("blur", "keydown.enter"):
                        name.on(
                            event,
                            lambda: self._object_patch(
                                entity, name=str(name.value).strip()
                            ),
                        )
                else:
                    with (
                        ui.label(_selection_title([s for _, s in selected]))
                        .classes("scene-name flex-1 min-w-0 truncate")
                        .mark("object-title")
                    ):
                        ui.tooltip(
                            "They move and turn together. Select one to edit its values."
                        )
                if why := self._readonly_reason(selected):
                    ui.icon("lock", size="18px").classes(
                        "text-wc-text-muted px-1"
                    ).tooltip(why).mark("object-readonly")
                ui.button(
                    icon="center_focus_strong",
                    on_click=lambda: self.scene.run_method("camera_action", "focus"),
                ).props(flat).tooltip("Focus · F").mark("object-focus")
                controls.isolate = (
                    ui.button(icon="flip_to_front", on_click=self._toggle_isolation)
                    .props(flat)
                    .tooltip("Isolate; again to show all")
                    .mark("object-isolate")
                )
                controls.editable(
                    ui.button(icon="content_copy", on_click=self._duplicate_objects)
                    .props(flat)
                    .tooltip("Duplicate")
                    .mark("object-duplicate"),
                    readonly,
                )
                with (
                    ui.button(icon="more_vert")
                    .props(flat)
                    .mark("object-actions") as more,
                    ui.menu(),
                ):
                    hidden = all(k in self._hidden_objects for k, _ in selected)
                    ui.menu_item(
                        "Show" if hidden else "Hide",
                        on_click=lambda: self._set_hidden(
                            [k for k, _ in selected], not hidden
                        ),
                    ).mark("object-hide")
                    locked = all(k in self._locked_objects for k, _ in selected)
                    ui.menu_item(
                        "Unlock" if locked else "Lock",
                        on_click=lambda: self._set_locked(
                            [k for k, _ in selected], not locked
                        ),
                    ).mark("object-lock")
                    self._attachment_actions(selected, controls, history.busy)
                    ui.separator()
                    controls.editable(
                        ui.menu_item("Delete", on_click=self._delete_objects)
                        .classes("text-wc-error")
                        .mark("object-delete"),
                        readonly,
                    )
                with more:
                    ui.tooltip("More").props(_TIP_BESIDE)
            if len(selected) == 1:
                entity, shape = selected[0]
                modes = [("translate", "open_with", "Move and turn with the handles")]
                if shape.kind in RESIZE_AXES:
                    modes.append(("resize", "open_in_full", _RESIZE_TIPS[shape.kind]))
                with (
                    ui.button_group()
                    .props("unelevated rounded")
                    .classes("self-start")
                    .mark("object-transform")
                ):
                    for value, icon, tip in modes:
                        controls.modes[value] = controls.editable(
                            ui.button(
                                icon=icon,
                                on_click=lambda v=value: self._set_object_mode(v),
                            )
                            .props("dense unelevated")
                            .tooltip(tip)
                            .mark(f"object-mode-{value}"),
                            readonly,
                        )
                    if shape.kind in RESIZE_AXES:
                        controls.resize_centre = controls.editable(
                            ui.button(
                                icon="zoom_out_map", on_click=self._toggle_resize_centre
                            )
                            .props(flat)
                            .tooltip("Resize from the centre (or hold Shift)")
                            .mark("object-resize-centre"),
                            readonly,
                        )
                for icon, tip, axes, values, offset, scale in (
                    (
                        "my_location",
                        "Position · mm",
                        ("X", "Y", "Z"),
                        shape.pose[:3],
                        0,
                        1000,
                    ),
                    (
                        "rotate_right",
                        "Rotation · °",
                        ("X", "Y", "Z"),
                        shape.pose[3:],
                        3,
                        180 / math.pi,
                    ),
                ):
                    with ui.row().classes(
                        "scene-field-row w-full no-wrap items-center"
                    ):
                        ui.icon(icon, size="18px").classes("scene-field-icon").tooltip(
                            tip
                        )
                        for index, (axis, value) in enumerate(zip(axes, values)):
                            field = (
                                ui.number(value=_shown(value, scale), prefix=axis)
                                .props("dense borderless hide-bottom-space")
                                .classes(
                                    f"scene-field axis-{axis.lower()} min-w-0 flex-1"
                                )
                                .mark(
                                    f"object-{('' if offset == 0 else 'r') + axis.lower()}"
                                )
                            )
                            controls.editable(field, readonly)
                            controls.track(
                                field,
                                f"pose{offset + index}",
                                lambda s, i=offset + index, k=scale: _shown(
                                    s.pose[i], k
                                ),
                            )

                            async def callback(_, f=field, i=offset + index, k=scale):
                                await self._object_pose(entity, i, f.value, k)

                            field.on("blur", callback)
                            field.on("keydown.enter", callback)
                self._object_dimensions(shape, entity, readonly, controls)
                with ui.row().classes("scene-field-row w-full no-wrap items-center"):
                    collision = (
                        ui.switch(
                            "Collision",
                            value=shape.collision,
                            on_change=lambda e: self._object_patch(
                                entity, collision=bool(e.value)
                            ),
                        )
                        .props("dense")
                        .tooltip("Checked for collisions")
                        .mark("object-collision")
                    )
                    controls.editable(collision, readonly)
                    controls.track(collision, "collision", lambda s: s.collision)
                    margin = (
                        ui.number(
                            value=None
                            if shape.margin is None
                            else _shown(shape.margin, 1000),
                            prefix="Clearance",
                            suffix="mm",
                            placeholder="default",
                        )
                        .props("dense borderless hide-bottom-space")
                        .classes("scene-field min-w-0 flex-1")
                        .tooltip(
                            "Extra distance kept from the arm; blank for the default"
                        )
                        .mark("object-margin")
                    )
                    controls.editable(margin, readonly)
                    controls.track(
                        margin,
                        "margin",
                        lambda s: None if s.margin is None else _shown(s.margin, 1000),
                    )
                    margin.on(
                        "blur",
                        lambda: self._object_number(
                            entity, "margin", margin.value, 1000, optional=True
                        ),
                    )
                if self._attachment_form and self._attachment_form[0] == entity:
                    self._attachment_section(entity, shape)
            controls.feedback = (
                ui.label().classes("text-xs text-wc-error").mark("object-feedback")
            )
        self._update_object_inspector(selected, status, True)

    def _readonly_reason(self, selected) -> str:
        """Why the selection's values cannot be edited, or ""."""
        if any(not k.startswith(SHAPE_PREFIX) for k, _ in selected):
            return "Installation · from the robot config"
        if any(k in self._locked_objects for k, _ in selected):
            return "Locked · unlock from More"
        if any(s.attachment is not None for _, s in selected):
            return "Attached to the flange · reconcile or detach from More"
        return ""

    def _refresh_object_tree(self, handle, panel) -> None:
        """The list of the scene's objects, rebuilt when what it lists changes;
        the selection is lit on the rows in place."""
        layers = (
            ()
            if handle is None
            else (
                ("Keep-outs", SHAPE_PREFIX, tuple(handle.shapes)),
                (
                    "Installation",
                    INSTALL_PREFIX,
                    tuple(
                        s
                        for s in handle.installation
                        if f"{INSTALL_PREFIX}{s.name}" not in self._install_ground
                    ),
                ),
                (
                    "Proposed installation",
                    DRAFT_PREFIX,
                    tuple(handle.installation_draft),
                ),
            )
        )
        tool = self._current_tool
        key = (
            tuple(
                (
                    title,
                    prefix,
                    tuple(
                        (s.name, s.kind, s.attachment is not None, s.collision)
                        for s in shapes
                    ),
                )
                for title, prefix, shapes in layers
            ),
            frozenset(self._hidden_objects),
            frozenset(self._locked_objects),
            tool,
            tuple((m.name, m.proxy_names) for m in self.fixture_meshes),
        )
        if key != self._object_tree_key:
            self._object_tree_key = key
            self._tree_rows = {}
            self._tree_selected = ()
            tree = self._object_tree
            tree.clear()
            with tree:
                self._tree_robot_row(tool)
                for title, prefix, shapes in layers:
                    if prefix == SHAPE_PREFIX:
                        grouped = {
                            n for m in self.fixture_meshes for n in m.proxy_names
                        }
                        self._fixture_tree()
                        shapes = tuple(s for s in shapes if s.name not in grouped)
                        if not shapes and self.fixture_meshes:
                            continue
                    self._tree_group(title, prefix, shapes)
        selected = tuple(self._selected_objects)
        if selected != self._tree_selected:
            for row_key, row in self._tree_rows.items():
                if (row_key in selected) != (row_key in self._tree_selected):
                    row.classes(
                        **{
                            "add" if row_key in selected else "remove": (
                                "scene-row--selected"
                            )
                        }
                    )
            self._tree_selected = selected
        panel.note(len(self._hidden_objects), self._isolated_view)

    def _tree_group(self, title: str, prefix: str, shapes) -> None:
        if not shapes and prefix != SHAPE_PREFIX:
            return
        keys = [f"{prefix}{s.name}" for s in shapes]
        with ui.row().classes("scene-group w-full items-center no-wrap"):
            ui.label(title).classes("flex-1")
            ui.label(str(len(shapes))).classes("scene-count")
            if keys and prefix != DRAFT_PREFIX:
                # Any hidden shows them all again: a hidden one is still kept out.
                hidden = any(k in self._hidden_objects for k in keys)
                ui.button(
                    icon="visibility_off" if hidden else "visibility",
                    on_click=lambda ks=keys, h=hidden: self._set_hidden(ks, not h),
                ).props("flat round dense size=sm color=wc-text").classes(
                    "scene-toggle" + (" on" if hidden else "")
                ).tooltip("Show all" if hidden else "Hide all").mark(
                    f"scene-hide-all-{prefix.rstrip(':')}"
                )
        if prefix == SHAPE_PREFIX and not shapes:
            ui.label("Draw one with + above, or right-click the floor").classes(
                "scene-empty"
            )
        for shape in shapes:
            self._tree_row(prefix, shape)

    def _tree_robot_row(self, tool: str) -> None:
        robot = ui_state.active_robot
        try:
            tool_name = robot.tools[tool].display_name if tool.upper() != "NONE" else ""
        except KeyError:
            tool_name = tool
        with (
            ui.row()
            .classes("scene-row w-full items-center no-wrap")
            .mark("scene-robot")
        ):
            ui.icon("precision_manufacturing", size="18px").classes("scene-row-kind")
            ui.label(robot.name).classes("scene-row-name")
            if tool_name:
                ui.label(tool_name).classes("scene-row-detail truncate").mark(
                    "scene-robot-tool"
                )
            ui.space()
            ui.button(
                icon="center_focus_strong",
                on_click=lambda: self.scene.run_method(
                    "camera_action", "focus", "robot"
                ),
            ).props("flat round dense size=sm color=wc-text").classes(
                "scene-toggle"
            ).tooltip("Focus the robot").mark("scene-robot-focus")

    def _tree_row(self, prefix: str, shape) -> None:
        key = f"{prefix}{shape.name}"
        hidden = key in self._hidden_objects
        # A marker is one word: a name with spaces is marked with hyphens.
        mark = shape.name.replace(" ", "-")
        row = (
            ui.row()
            .classes(
                "scene-row w-full items-center no-wrap"
                + (" scene-row--hidden" if hidden else "")
                + (" scene-row--proposed" if prefix == DRAFT_PREFIX else "")
            )
            .mark(f"scene-row-{mark}")
        )
        with row:
            ui.icon(_KIND_ICONS.get(shape.kind, "category"), size="18px").classes(
                "scene-row-kind"
            ).tooltip(object_label(shape))
            ui.label(shape.name).classes("scene-row-name truncate")
            if shape.attachment is not None:
                ui.icon("link", size="16px").classes("text-wc-text-muted").tooltip(
                    "Attached to the flange"
                )
            ui.space()
            if prefix == DRAFT_PREFIX:
                with (
                    ui.button(icon="more_vert")
                    .props("flat round dense size=sm color=wc-text")
                    .classes("scene-toggle on"),
                    ui.menu(),
                ):
                    self._populate_draft_menu(shape.name)
                return
            if prefix == SHAPE_PREFIX:
                locked = key in self._locked_objects
                ui.button(icon="lock" if locked else "lock_open").props(
                    "flat round dense size=sm color=wc-text"
                ).classes("scene-toggle" + (" on" if locked else "")).tooltip(
                    "Unlock" if locked else "Lock"
                ).on("click.stop", lambda: self._set_locked([key], not locked)).mark(
                    f"scene-lock-{mark}"
                )
            else:
                ui.icon("lock", size="16px").classes("scene-toggle on px-1").tooltip(
                    "Installation · from the robot config"
                )
            ui.button(icon="visibility_off" if hidden else "visibility").props(
                "flat round dense size=sm color=wc-text"
            ).classes("scene-toggle" + (" on" if hidden else "")).tooltip(
                "Show" if hidden else "Hide · still checked for collisions"
            ).on("click.stop", lambda: self._set_hidden([key], not hidden)).mark(
                f"scene-eye-{mark}"
            )
        self._tree_rows[key] = row
        row.on(
            "click",
            lambda e: self._pick_from_tree(key, e.args),
            ["ctrlKey", "metaKey", "shiftKey"],
        )
        # A finger has no hover to end, so only a mouse lights the object.
        glow = f"getElement({self.scene.id}).glow_object"
        row.on(
            "pointerenter",
            js_handler=f"(e) => e.pointerType === 'mouse' && {glow}({json.dumps(key)})",
        )
        row.on("pointerleave", js_handler=f"() => {glow}(null)")

    def _pick_from_tree(self, key: str, args) -> None:
        extend = (
            self._multi_select
            or isinstance(args, dict)
            and any(args.get(k) for k in ("ctrlKey", "metaKey", "shiftKey"))
        )
        if not extend:
            self._select_objects([key])
        elif key in self._selected_objects:
            self._select_objects(
                [
                    k
                    for k in self._selected_objects
                    if k not in self._expand_fixture_selection([key])
                ]
            )
        else:
            self._select_objects([*self._selected_objects, key])

    def _attachment_actions(self, selected, controls, busy) -> None:
        """Object actions that open the attachment form for one object; they
        stay open to an attached object, which the fields are not."""
        if len(selected) != 1 or not selected[0][0].startswith(SHAPE_PREFIX):
            return
        entity, shape = selected[0]
        if entity in self._locked_objects:
            busy = True
        items = (
            [("Attach to flange", False, "object-attach")]
            if shape.attachment is None
            else [
                ("Reconcile attachment", False, "object-reconcile"),
                ("Detach to world", True, "object-detach"),
            ]
        )
        for text, detach, marker in items:
            item = ui.menu_item(
                text,
                on_click=lambda d=detach: self._open_attachment_form(
                    shape.name, detach=d
                ),
            ).mark(marker)
            item.set_enabled(not busy)
            if entity not in self._locked_objects:
                controls.attachment.append(item)

    def _update_object_inspector(self, selected, status, reset_values) -> None:
        """Bring the built properties up to *status* and the selection's values."""
        controls = self._inspector_controls
        busy, feedback, _, _, _ = status
        readonly = busy or controls.locked
        for element in controls.editable_elements:
            element.set_enabled(not readonly)
        for item in controls.attachment:
            item.set_enabled(not busy)
        self._show_modes(controls, selected)
        if controls.feedback is not None:
            controls.feedback.set_text(feedback)
            controls.feedback.set_visibility(bool(feedback))
        if len(selected) != 1:
            return
        shape = selected[0][1]
        for key, (element, value) in controls.values.items():
            current = value(shape)
            if reset_values or controls.shown.get(key) != current:
                controls.shown[key] = current
                element.set_value(current)

    def _show_modes(self, controls, selected) -> None:
        """Light the isolation and handle-mode buttons as things stand; they
        change in place, so a button keeps the keyboard focus it was pressed with."""
        shown = {item["id"] for item in self._object_entities()["items"]}
        state = (
            self._isolated_view,
            self._isolated_view or any(k in shown for k, _ in selected),
            self._object_mode,
            self._resize_centre,
        )
        if controls.shown.get("modes") == state:
            return
        controls.shown["modes"] = state
        if controls.isolate is not None:
            controls.isolate.props(
                f"flat round dense color={'wc-action' if self._isolated_view else 'wc-text'}"
            )
            controls.isolate.set_enabled(state[1])
        for value, button in controls.modes.items():
            button.props(
                "color=wc-action text-color=wc-on-bright"
                if value == self._object_mode
                else "color=wc-control text-color=wc-text"
            )
        if controls.resize_centre is not None:
            controls.resize_centre.set_visibility(self._object_mode == "resize")
            controls.resize_centre.props(
                f"flat round dense color={'wc-action' if self._resize_centre else 'wc-text'} "
                f"aria-pressed={'true' if self._resize_centre else 'false'}"
            )

    def _object_dimensions(self, shape, entity, readonly, controls):
        with ui.row().classes("scene-field-row w-full no-wrap items-center"):
            ui.icon("straighten", size="18px").classes("scene-field-icon").tooltip(
                "Size · mm"
            )
            for parameter in param_names(type(shape)):
                field = (
                    ui.number(
                        value=_shown(getattr(shape, parameter), 1000),
                        prefix=_DIMENSION_PREFIXES.get(parameter, parameter),
                    )
                    .props("dense borderless hide-bottom-space")
                    .classes(f"scene-field axis-{parameter} min-w-0 flex-1")
                    .tooltip(parameter.capitalize())
                    .mark(f"object-dim-{parameter}")
                )
                controls.editable(field, readonly)
                controls.track(
                    field,
                    f"dim-{parameter}",
                    lambda s, p=parameter: _shown(getattr(s, p), 1000),
                )

                async def callback(_, f=field, p=parameter):
                    await self._object_number(entity, p, f.value, 1000)

                field.on("blur", callback)
                field.on("keydown.enter", callback)

    def _set_object_mode(self, mode):
        if mode not in ("translate", "resize"):
            return
        self._object_mode = mode
        self.object_history.feedback = ""
        self._shape_move_session += 1
        self._push_interaction()
        self._refresh_object_inspector()

    def _toggle_resize_centre(self) -> None:
        self._resize_centre = not self._resize_centre
        self.object_history.feedback = ""
        self._shape_move_session += 1
        self._push_interaction()
        self._refresh_object_inspector()

    async def _commit_objects(
        self, label, shapes, revision, *, renames=(), draft=None, meshes=None
    ):
        refused = False
        try:
            for current in self._shape_handle().shapes:
                if (
                    f"{SHAPE_PREFIX}{current.name}" in self._locked_objects
                    and current not in shapes
                ):
                    raise ValueError(f"Unlock {current.name} before editing it")
            renamed = dict(renames)
            remaining = {s.name for s in shapes}
            meshes = tuple(
                replace(
                    m,
                    proxy_names=tuple(
                        renamed.get(n, n)
                        for n in m.proxy_names
                        if renamed.get(n, n) in remaining
                    ),
                )
                for m in (self.fixture_meshes if meshes is None else meshes)
            )
            members = [n for m in meshes for n in m.proxy_names]
            if len(members) != len(set(members)):
                raise ValueError("Each proxy must belong to only one fixture")
            for current in self.fixture_meshes:
                if current.key in self._locked_objects and current not in meshes:
                    raise ValueError(f"Unlock {current.name} before editing it")
            await self.object_history.commit(
                label,
                shapes,
                revision=revision,
                renames=renames,
                draft=draft,
                meshes=meshes,
            )
            if self.object_history.expected == self.object_history.context():
                self._shape_move_document = self.object_history.document()
            return True
        except (ValueError, TypeError) as error:
            refused = True
            self.object_history.feedback = str(error)
            if (panel := self._scene_panel()) is not None:
                panel.open()
            return False
        finally:
            self._push_interaction()
            self._refresh_object_inspector(reset_values=refused)

    async def _object_patch(self, key, **changes):
        shape = self._object_shape(key)
        if (
            shape is None
            or key in self._locked_objects
            or not key.startswith(SHAPE_PREFIX)
        ):
            return
        try:
            updated = replace(shape, **changes)
            if updated == shape:
                return
            shapes = [
                updated if s.name == shape.name else s
                for s in self._shape_handle().shapes
            ]
            renamed = shape.name != updated.name
            if (
                await self._commit_objects(
                    "Edit object",
                    shapes,
                    self.object_history.context(),
                    renames=((shape.name, updated.name),) if renamed else (),
                )
                and renamed
            ):
                # The selection followed the rename; its move restarts on the
                # new name, and a selection made meanwhile stands.
                self._select_objects(self._selected_objects)
        except (ValueError, TypeError) as error:
            self._notify(str(error), warning=True)
            self._refresh_object_inspector(reset_values=True)
            return
        self._refresh_object_inspector()

    async def _object_pose(self, key, index, value, scale):
        shape = self._object_shape(key)
        if shape is None:
            return
        try:
            entered = float(value)
        except (TypeError, ValueError):
            self._notify("Enter a finite coordinate", warning=True)
            self._refresh_object_inspector(reset_values=True)
            return
        # Leaving a field unchanged commits nothing; it shows a rounded value.
        if _shown(shape.pose[index], scale) == round(entered, 3):
            return
        pose = list(shape.pose)
        pose[index] = entered / scale
        await self._object_patch(key, pose=tuple(pose))

    async def _object_number(self, key, parameter, value, scale, optional=False):
        shape = self._object_shape(key)
        if shape is None:
            return
        current = getattr(shape, parameter)
        if optional and value in (None, ""):
            if current is not None:
                await self._object_patch(key, **{parameter: None})
            return
        try:
            entered = float(value)
        except (TypeError, ValueError):
            self._notify("Enter a finite dimension", warning=True)
            self._refresh_object_inspector(reset_values=True)
            return
        if current is not None and _shown(current, scale) == round(entered, 3):
            return
        await self._object_patch(key, **{parameter: entered / scale})

    async def _duplicate_objects(self):
        handle = self._shape_handle()
        revision = self.object_history.context()
        shapes = list(handle.shapes)
        selected = []
        taken: set[str] = set()
        for key in self._selected_objects:
            shape = self._object_shape(key)
            if (
                shape is None
                or not key.startswith(SHAPE_PREFIX)
                or key in self._locked_objects
            ):
                return
            name = self._fresh_shape_name(f"{shape.name}-copy", taken)
            taken.add(name)
            shapes.append(
                replace(
                    shape,
                    name=name,
                    pose=(
                        shape.pose[0] + 0.025,
                        shape.pose[1] + 0.025,
                        *shape.pose[2:],
                    ),
                )
            )
            selected.append(f"{SHAPE_PREFIX}{name}")
        if await self._commit_objects("Duplicate objects", shapes, revision):
            self._select_objects(selected)

    async def _delete_objects(self):
        handle = self._shape_handle()
        revision = self.object_history.context()
        if any(
            not k.startswith((SHAPE_PREFIX, MESH_PREFIX)) or k in self._locked_objects
            for k in self._selected_objects
        ):
            return
        shapes = [
            s
            for s in handle.shapes
            if f"{SHAPE_PREFIX}{s.name}" not in self._selected_objects
        ]
        meshes = tuple(
            m for m in self.fixture_meshes if m.key not in self._selected_objects
        )
        await self._commit_objects("Delete objects", shapes, revision, meshes=meshes)

    def undo_edit(self, *, redo: bool = False) -> None:
        """Ctrl+Z (Ctrl+Shift+Z or Ctrl+Y to redo) in the scene: a keep-out
        edit while there is one to undo, else the program's last edit, which
        a target edit is. A program edit clears the keep-out history, so the
        two never interleave."""
        self.cancel_target_edit()
        history = self.object_history
        history.validate()
        if not history.busy and (history.redo_stack if redo else history.undo_stack):
            background_tasks.create(self._restore_objects(redo))
            return
        textarea = ui_state.active_textarea
        if textarea is not None:
            textarea.redo() if redo else textarea.undo()

    async def _restore_objects(self, redo):
        previous = {s.name for s in self._shape_handle().shapes}
        try:
            await self.object_history.restore(redo=redo)
            restored = [
                f"{SHAPE_PREFIX}{s.name}"
                for s in self._shape_handle().shapes
                if s.name not in previous
            ]
            self._select_objects(restored or self._selected_objects)
        except ValueError as error:
            self._notify(str(error), warning=True)
        self._push_interaction()
        self._refresh_object_inspector()

    def _set_hidden(self, keys, hidden: bool) -> None:
        """Hide *keys* from this view, or show them again; a hidden object
        is still checked for collisions."""
        for key in keys:
            if hidden:
                self._hidden_objects.add(key)
            else:
                self._hidden_objects.discard(key)
            if node := self._shape_objects.get(key):
                node.visible(not hidden)
        self._marks_changed(keys)

    def _set_locked(self, keys, locked: bool) -> None:
        for key in keys:
            if locked:
                self._locked_objects.add(key)
            else:
                self._locked_objects.discard(key)
        self._marks_changed(keys)

    def _marks_changed(self, keys) -> None:
        """A selected object hidden or locked takes its handles anew; any
        other leaves a drawing, a move and a typed value where they are."""
        self.object_history.feedback = ""
        if set(keys) & set(self._selected_objects):
            self._select_objects(self._selected_objects)
            return
        self._push_interaction()
        self._refresh_object_inspector()
