"""Contextual object selection and authored-world controls."""

from __future__ import annotations

from dataclasses import replace
import math
from collections.abc import Callable
from typing import Any

from .shape_drawing_mixin import ShapeDrawingMixin
from .shape_editing_mixin import RESIZE_AXES

from nicegui import ui
from waldoctl.shapes import INSTALL_PREFIX, SHAPE_PREFIX, param_names

from waldo_commander.services.scene_history import SceneHistory


def _shown(value: float, scale: float) -> float:
    """*value* as an inspector field shows it."""
    return round(value * scale, 3)


class _InspectorControls:
    """The built inspector's controls that follow edit status and values."""

    def __init__(self) -> None:
        self.locked = False
        self.editable_elements: list[Any] = []
        self.values: dict[str, tuple[Any, Callable[[Any], Any]]] = {}
        self.shown: dict[str, Any] = {}
        self.undo: Any = None
        self.redo: Any = None
        self.feedback: Any = None
        self.names: Any = None

    def editable(self, element: Any, readonly: bool) -> Any:
        element.set_enabled(not readonly)
        self.editable_elements.append(element)
        return element

    def track(self, element: Any, key: str, value: Callable[[Any], Any]) -> None:
        self.values[key] = (element, value)


class ObjectEditingMixin(ShapeDrawingMixin):
    inspection: Any
    _scene_wrapper: Any
    cancel_target_edit: Any

    def _init_object_editing(self) -> None:
        self.object_history = SceneHistory(self)
        self._init_shape_drawing()
        self._selected_objects: list[str] = []
        self._hidden_objects: set[str] = set()
        self._locked_objects: set[str] = set()
        self._object_inspector = None
        self._object_inspector_key = None
        self._inspector_controls = _InspectorControls()
        self._object_orientation = None
        self._object_orientation_selection: tuple[str, ...] = ()
        self._object_mode = "translate"
        self._object_overlaps: list[str] = []
        self._show_object_history = False
        self._isolated_view = False
        self._object_renames = ()

    def _set_object_renames(self, renames):
        self._object_renames = renames
        self._push_interaction()

    def _on_isolation_view(self, event):
        isolated = bool(event.args.get("isolated", False))
        if isolated != self._isolated_view:
            self._isolated_view = isolated
            self._refresh_object_inspector()

    def _isolation_action(self):
        if self._isolated_view or self._selected_objects:
            ui.button(
                "Restore surrounding objects"
                if self._isolated_view
                else "Isolate selected objects",
                icon="filter_center_focus",
                on_click=lambda: self.scene.run_method(
                    "camera_action",
                    "restore_objects" if self._isolated_view else "isolate",
                ),
            ).props("flat dense no-caps color=wc-text").mark(
                "object-isolate"
            ).set_enabled(
                self._isolated_view
                or any(
                    item["id"] in self._selected_objects
                    for item in self._object_entities()["items"]
                )
            )

    def _object_entities(self) -> dict:
        handle = self._shape_handle()
        return {
            "revision": handle.revision,
            "selected": self._selected_objects,
            "renames": [
                [f"{SHAPE_PREFIX}{before}", f"{SHAPE_PREFIX}{after}"]
                for before, after in self._object_renames
            ],
            "items": [
                {"id": name, "node": node.id}
                for name, node in self._shape_objects.items()
                if name.startswith((SHAPE_PREFIX, INSTALL_PREFIX))
                and name not in self._hidden_objects
                and node.glow
            ],
        }

    def _object_shape(self, key):
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
        self._object_overlaps = list(dict.fromkeys(hits))
        selected = hits[:1]
        if args.get("extend") and selected:
            selected = (
                [k for k in self._selected_objects if k != selected[0]]
                if selected[0] in self._selected_objects
                else [*self._selected_objects, *selected]
            )
        self._select_objects(selected)

    def _select_objects(self, selected) -> None:
        self._cancel_shape_drawing()
        self._show_object_history = False
        self._end_shape_move()
        self._selected_objects = [
            key for key in selected if self._object_shape(key) is not None
        ]
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
            self._start_shape_move(key[len(SHAPE_PREFIX) :])
        self._push_interaction()
        self._refresh_object_inspector()

    def _prune_object_marks(self, handle) -> None:
        """Hidden and locked follow a rename and go with their object, so a
        later object given the same name starts out shown and unlocked."""
        renamed = {
            f"{SHAPE_PREFIX}{before}": f"{SHAPE_PREFIX}{after}"
            for before, after in self._object_renames
        }
        existing = {f"{SHAPE_PREFIX}{s.name}" for s in handle.shapes} | {
            f"{INSTALL_PREFIX}{s.name}" for s in handle.installation
        }

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

    def _refresh_object_inspector(self, *, reset_values: bool = False) -> None:
        """Show the selection's inspector; *reset_values* puts every field
        back to the object's value after an edit was refused."""
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
        self._selected_objects = [key for key, _ in selected]
        history = self.object_history
        status = (
            history.busy,
            history.feedback,
            len(history.undo_stack),
            len(history.redo_stack),
            handle is not None and handle.confirmed,
        )
        # What the card is built from; status and shape values are updated
        # in place, so an edit does not rebuild the field being typed in.
        structure = (
            tuple((k, s.kind, s.attachment is not None) for k, s in selected),
            frozenset(self._hidden_objects),
            self._isolated_view,
            frozenset(self._locked_objects),
            self._object_mode,
            self._show_object_history,
            tuple(self._object_overlaps),
            self._attachment_form,
            None if selected else status,
        )
        if structure == self._object_inspector_key:
            if selected and self._object_inspector is not None:
                self._update_object_inspector(selected, status, reset_values)
            return
        self._object_inspector_key = structure
        controls = self._inspector_controls = _InspectorControls()
        selection = tuple(entity for entity, _ in selected)
        orientation = self._object_orientation
        orientation_open = (
            selection == self._object_orientation_selection
            and orientation is not None
            and orientation.value
        )
        self._object_orientation_selection = selection
        self._object_orientation = None
        if self._object_inspector is not None:
            self._object_inspector.delete()
            self._object_inspector = None
        if not selected:
            history_visible = self._show_object_history and (
                history.undo_stack or history.redo_stack
            )
            if history_visible or self._hidden_objects or self._isolated_view:
                with self.scene.client, self._scene_wrapper:
                    self._object_inspector = (
                        ui.row()
                        .classes(
                            "overlay-card absolute right-3 top-3 p-2 pointer-events-auto items-center"
                        )
                        .style("z-index:35")
                    )
                    with self._object_inspector:
                        if history_visible:
                            ui.label("Object history").classes("text-sm")
                            ui.button(
                                "Undo", on_click=lambda: self._restore_objects(False)
                            ).props("flat no-caps color=wc-action").mark(
                                "object-undo"
                            ).set_enabled(bool(history.undo_stack) and not history.busy)
                            ui.button(
                                "Redo", on_click=lambda: self._restore_objects(True)
                            ).props("flat no-caps color=wc-action").mark(
                                "object-redo"
                            ).set_enabled(bool(history.redo_stack) and not history.busy)
                            ui.button(
                                icon="close", on_click=lambda: self._select_objects([])
                            ).props("flat round dense color=wc-text").tooltip("Dismiss")
                        self._show_hidden_action()
                        self._isolation_action()
            return
        controls.locked = any(
            not k.startswith(SHAPE_PREFIX)
            or k in self._locked_objects
            or s.attachment is not None
            for k, s in selected
        )
        readonly = history.busy or controls.locked
        with self.scene.client, self._scene_wrapper:
            self._object_inspector = (
                ui.column()
                .classes(
                    "wc-object-inspector overlay-card absolute p-2 pointer-events-auto"
                )
                .style("width:260px;overflow:auto;z-index:35;gap:4px;visibility:hidden")
                .mark("object-inspector")
            )
            with self._object_inspector:
                with ui.row().classes("w-full items-center no-wrap"):
                    ui.label(
                        selected[0][1].kind.capitalize()
                        if len(selected) == 1
                        else f"{len(selected)} objects"
                    ).classes("font-medium flex-1")
                    ui.button(
                        icon="center_focus_strong",
                        on_click=lambda: self.scene.run_method(
                            "camera_action", "focus"
                        ),
                    ).props("flat round dense color=wc-text").tooltip(
                        "Focus selection"
                    ).mark("object-focus")
                    ui.button(
                        icon="close", on_click=lambda: self._select_objects([])
                    ).props("flat round dense color=wc-text").tooltip(
                        "Clear selection"
                    ).mark("object-close")
                if len(self._object_overlaps) > 1:
                    with ui.expansion("Objects under pointer").classes("w-full"):
                        for overlap in self._object_overlaps:
                            ui.button(
                                overlap.split(":", 1)[-1],
                                on_click=lambda _, k=overlap: self._select_objects([k]),
                            ).props("flat no-caps color=wc-text")
                if len(selected) > 1:
                    controls.names = ui.label(
                        " · ".join(s.name for _, s in selected)
                    ).classes("text-sm")
                    ui.label(
                        "World axes · rotation about selection center. Select one object to resize."
                    ).classes("text-xs text-wc-text-muted")
                if len(selected) == 1:
                    entity, shape = selected[0]
                    name = (
                        ui.input("Name", value=shape.name)
                        .props("dense outlined")
                        .classes("w-full")
                        .mark("object-name")
                    )
                    controls.editable(name, readonly)
                    controls.track(name, "name", lambda s: s.name)
                    name.on(
                        "blur",
                        lambda: self._object_patch(
                            entity, name=str(name.value).strip()
                        ),
                    )
                    name.on(
                        "keydown.enter",
                        lambda: self._object_patch(
                            entity, name=str(name.value).strip()
                        ),
                    )
                    if entity.startswith(INSTALL_PREFIX):
                        ui.label("Installation · read only").classes(
                            "text-xs text-wc-text-muted"
                        )
                    elif shape.attachment is not None:
                        ui.label(
                            "Flange attachment · reconcile or detach from Object actions"
                        ).classes("text-xs text-wc-text-muted")
                    mode = (
                        ui.toggle(
                            {
                                "translate": "Move",
                                **(
                                    {"resize": "Resize"}
                                    if shape.kind in RESIZE_AXES
                                    else {}
                                ),
                            },
                            value=self._object_mode,
                            on_change=lambda e: self._set_object_mode(e.value),
                        )
                        .props(
                            "no-caps dense color=wc-control text-color=wc-text toggle-color=wc-action toggle-text-color=wc-on-bright"
                        )
                        .mark("object-transform")
                    )
                    controls.editable(mode, readonly)
                    resizing = self._object_mode == "resize"
                    if resizing:
                        ui.label(
                            "Drag a face · Shift: resize from center · Esc: cancel"
                            if shape.kind == "box"
                            else "Drag a radius handle · Esc: cancel"
                            if shape.kind == "sphere"
                            else "Drag radius / end handles · Esc: cancel"
                        ).classes("text-xs text-wc-text-muted")
                        self._object_dimensions(shape, entity, readonly, controls)
                    for axes, values, offset, scale in (
                        ()
                        if resizing
                        else (
                            (("X", "Y", "Z"), shape.pose[:3], 0, 1000),
                            (("Rx", "Ry", "Rz"), shape.pose[3:], 3, 180 / math.pi),
                        )
                    ):
                        section = (
                            ui.expansion("Orientation", value=orientation_open)
                            .props("dense header-class=text-xs")
                            .classes("w-full wc-object-orientation")
                            .mark("object-orientation")
                            if offset == 3
                            else ui.column().classes("w-full gap-0")
                        )
                        if offset == 3:
                            self._object_orientation = section
                        with section, ui.row().classes("w-full no-wrap gap-1"):
                            for index, (axis, value) in enumerate(zip(axes, values)):
                                field = (
                                    ui.number(
                                        f"{axis} · {'mm' if offset == 0 else '°'}",
                                        value=_shown(value, scale),
                                    )
                                    .props("dense borderless hide-bottom-space")
                                    .classes("min-w-0 flex-1")
                                    .mark(f"object-{axis.lower()}")
                                )
                                controls.editable(field, readonly)
                                controls.track(
                                    field,
                                    f"pose{offset + index}",
                                    lambda s, i=offset + index, k=scale: _shown(
                                        s.pose[i], k
                                    ),
                                )

                                async def callback(
                                    _, f=field, i=offset + index, k=scale
                                ):
                                    await self._object_pose(entity, i, f.value, k)

                                field.on("blur", callback)
                                field.on("keydown.enter", callback)
                    with ui.expansion("More properties").classes("w-full"):
                        if not resizing:
                            self._object_dimensions(shape, entity, readonly, controls)
                        collision = (
                            ui.switch(
                                "Participates in collision checks",
                                value=shape.collision,
                                on_change=lambda e: self._object_patch(
                                    entity, collision=bool(e.value)
                                ),
                            )
                            .props("dense")
                            .mark("object-collision")
                        )
                        controls.editable(collision, readonly)
                        controls.track(collision, "collision", lambda s: s.collision)
                        margin = (
                            ui.number(
                                "Clearance · mm (blank = default)",
                                value=None
                                if shape.margin is None
                                else _shown(shape.margin, 1000),
                            )
                            .props("dense outlined")
                            .classes("w-full")
                            .mark("object-margin")
                        )
                        controls.editable(margin, readonly)
                        controls.track(
                            margin,
                            "margin",
                            lambda s: None
                            if s.margin is None
                            else _shown(s.margin, 1000),
                        )
                        margin.on(
                            "blur",
                            lambda: self._object_number(
                                entity,
                                "margin",
                                margin.value,
                                1000,
                                optional=True,
                            ),
                        )
                    if self._attachment_form and self._attachment_form[0] == entity:
                        self._attachment_section(entity, shape)
                with (
                    ui.column()
                    .classes("w-full sticky bottom-0 bg-wc-surface")
                    .style("gap:4px;flex-shrink:0;padding-top:4px")
                ):
                    with ui.row().classes("w-full items-center no-wrap").style("gap:0"):
                        controls.editable(
                            ui.button(
                                icon="content_copy", on_click=self._duplicate_objects
                            )
                            .props("flat round dense color=wc-text")
                            .tooltip("Duplicate selection")
                            .mark("object-duplicate"),
                            readonly,
                        )
                        with (
                            ui.button(icon="more_horiz")
                            .props("flat round dense color=wc-text")
                            .tooltip("Object actions")
                            .mark("object-actions")
                        ):
                            with ui.menu():
                                controls.editable(
                                    ui.menu_item(
                                        "Delete", on_click=self._delete_objects
                                    ).mark("object-delete"),
                                    readonly,
                                )
                                hidden = all(
                                    k in self._hidden_objects for k, _ in selected
                                )
                                ui.menu_item(
                                    "Show" if hidden else "Hide",
                                    on_click=self._toggle_object_visibility,
                                ).mark("object-hide")
                                locked = all(
                                    k in self._locked_objects for k, _ in selected
                                )
                                ui.menu_item(
                                    "Unlock" if locked else "Lock",
                                    on_click=self._toggle_object_lock,
                                ).mark("object-lock")
                                self._attachment_actions(selected, history.busy)
                                if not self._isolated_view:
                                    self._isolation_action()
                        ui.space()
                        controls.undo = (
                            ui.button(
                                icon="undo",
                                on_click=lambda: self._restore_objects(False),
                            )
                            .props("flat round dense color=wc-text")
                            .tooltip("Undo object edit")
                            .mark("object-undo")
                        )
                        controls.redo = (
                            ui.button(
                                icon="redo",
                                on_click=lambda: self._restore_objects(True),
                            )
                            .props("flat round dense color=wc-text")
                            .tooltip("Redo object edit")
                            .mark("object-redo")
                        )
                    controls.feedback = (
                        ui.label()
                        .classes("text-xs text-wc-text-muted")
                        .mark("object-feedback")
                    )
                    if any(k in self._hidden_objects for k, _ in selected):
                        ui.label(
                            "Hidden in this view · collision remains active"
                        ).classes("text-xs text-wc-text-muted")
                    self._show_hidden_action()
                    if self._isolated_view:
                        self._isolation_action()
        self._update_object_inspector(selected, status, True)

    def _attachment_actions(self, selected, busy) -> None:
        """Object actions that open the attachment form for one object."""
        if len(selected) != 1 or not selected[0][0].startswith(SHAPE_PREFIX):
            return
        entity, shape = selected[0]
        enabled = not busy and entity not in self._locked_objects
        if shape.attachment is None:
            ui.menu_item(
                "Attach to flange",
                on_click=lambda: self._open_attachment_form(shape.name),
            ).mark("object-attach").set_enabled(enabled)
            return
        ui.menu_item(
            "Reconcile attachment",
            on_click=lambda: self._open_attachment_form(shape.name),
        ).mark("object-reconcile").set_enabled(enabled)
        ui.menu_item(
            "Detach to world",
            on_click=lambda: self._open_attachment_form(shape.name, detach=True),
        ).mark("object-detach").set_enabled(enabled)

    def _update_object_inspector(self, selected, status, reset_values) -> None:
        """Bring the built card up to *status* and the selection's values."""
        controls = self._inspector_controls
        busy, feedback, undo, redo, confirmed = status
        readonly = busy or controls.locked
        for element in controls.editable_elements:
            element.set_enabled(not readonly)
        if controls.undo is not None and controls.redo is not None:
            controls.undo.set_enabled(bool(undo) and not busy)
            controls.redo.set_enabled(bool(redo) and not busy)
        if controls.feedback is not None:
            controls.feedback.set_text(
                feedback
                or ("" if confirmed else "Waiting for controller confirmation…")
            )
        if controls.names is not None:
            controls.names.set_text(" · ".join(s.name for _, s in selected))
        if len(selected) != 1:
            return
        shape = selected[0][1]
        for key, (element, value) in controls.values.items():
            current = value(shape)
            if reset_values or controls.shown.get(key) != current:
                controls.shown[key] = current
                element.set_value(current)

    def _object_dimensions(self, shape, entity, readonly, controls):
        ui.label("Dimensions · mm").classes("text-xs text-wc-text-muted")
        with ui.row().classes("w-full no-wrap"):
            for parameter in param_names(type(shape)):
                field = (
                    ui.number(
                        parameter,
                        value=_shown(getattr(shape, parameter), 1000),
                    )
                    .props("dense outlined hide-bottom-space")
                    .classes("min-w-0 flex-1")
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

    def _show_hidden_action(self):
        if self._hidden_objects:
            ui.button(
                f"Show {len(self._hidden_objects)} hidden",
                icon="visibility",
                on_click=self._show_hidden_objects,
            ).props("flat dense no-caps color=wc-text").mark("object-show-hidden")

    def _show_hidden_objects(self):
        for key in self._hidden_objects:
            if node := self._shape_objects.get(key):
                node.visible(True)
        self._hidden_objects.clear()
        self._select_objects(self._selected_objects)

    def _set_object_mode(self, mode):
        if mode not in ("translate", "resize"):
            return
        self._object_mode = mode
        self._shape_move_session += 1
        self._push_interaction()
        self._refresh_object_inspector()

    async def _commit_objects(self, label, shapes, revision, *, renames=(), draft=None):
        refused = False
        try:
            for current in self._shape_handle().shapes:
                if (
                    f"{SHAPE_PREFIX}{current.name}" in self._locked_objects
                    and current not in shapes
                ):
                    raise ValueError(f"Unlock {current.name} before editing it")
            await self.object_history.commit(
                label, shapes, revision=revision, renames=renames, draft=draft
            )
            if self.object_history.expected == self.object_history.context():
                self._shape_move_document = self.object_history.document()
            return True
        except (ValueError, TypeError) as error:
            refused = True
            self._notify(str(error), warning=True)
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
            not k.startswith(SHAPE_PREFIX) or k in self._locked_objects
            for k in self._selected_objects
        ):
            return
        shapes = [
            s
            for s in handle.shapes
            if f"{SHAPE_PREFIX}{s.name}" not in self._selected_objects
        ]
        if await self._commit_objects("Delete objects", shapes, revision):
            self._show_object_history = True
            self._refresh_object_inspector()

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
            self._show_object_history = not self._selected_objects
        except ValueError as error:
            self._notify(str(error), warning=True)
        self._push_interaction()
        self._refresh_object_inspector()

    def _toggle_object_visibility(self):
        show = all(k in self._hidden_objects for k in self._selected_objects)
        for key in self._selected_objects:
            if show:
                self._hidden_objects.discard(key)
            else:
                self._hidden_objects.add(key)
            if node := self._shape_objects.get(key):
                node.visible(show)
        self._select_objects(self._selected_objects)

    def _toggle_object_lock(self):
        unlock = all(k in self._locked_objects for k in self._selected_objects)
        for key in self._selected_objects:
            if unlock:
                self._locked_objects.discard(key)
            else:
                self._locked_objects.add(key)
        self._select_objects(self._selected_objects)
