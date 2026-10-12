"""Fixture visuals share the scene's selection, transforms and history."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import numpy as np
from nicegui import app, background_tasks, run, ui
from waldoctl.shapes import Box, SHAPE_PREFIX, pose_matrix

from waldo_commander.common.theme import SceneColors
from waldo_commander.services.fixture_meshes import (
    FixtureMesh,
    MESH_PREFIX,
    STORAGE_KEY,
    asset_path,
    mesh_size,
    restore_meshes,
)
from .shape_drawing_mixin import ShapeDrawingMixin


class MeshEditingMixin(ShapeDrawingMixin):
    _scene_panel: Any
    _library_host: Any
    _tree_rows: Any
    _tree_row: Any
    _pick_from_tree: Any
    _set_hidden: Any
    _set_locked: Any
    _object_properties: Any
    _object_inspector_key: Any
    _delete_objects: Any
    _toggle_isolation: Any
    _isolated_view: bool

    def _init_fixture_meshes(self) -> None:
        self.fixture_meshes: tuple[FixtureMesh, ...] = ()
        self._drawn_meshes: dict[str, FixtureMesh] = {}
        self._mesh_import = None
        self._unavailable_fixture_data = []
        self._fixture_restore_error = ""
        data = app.storage.general.get(STORAGE_KEY, [])
        self._fixture_restore_pending = bool(data)
        if data:
            background_tasks.create(self._restore_fixture_meshes(data))

    async def _restore_fixture_meshes(self, data) -> None:
        restored = await run.io_bound(restore_meshes, data)
        if restored is None or not self._fixture_restore_pending:
            return
        meshes, unavailable, errors = restored
        self._fixture_restore_pending = False
        self.fixture_meshes = meshes
        self._unavailable_fixture_data = unavailable
        self._fixture_restore_error = (
            "Unavailable fixtures (metadata retained; restore the assets and reload): "
            + "; ".join(errors)
            if errors
            else ""
        )
        self._render_fixture_meshes()
        self._refresh_object_inspector()

    def _set_fixture_meshes(self, meshes: tuple[FixtureMesh, ...]) -> None:
        from waldo_commander.state import ui_state

        app.storage.general[STORAGE_KEY] = [
            *(m.to_dict() for m in meshes),
            *self._unavailable_fixture_data,
        ]
        views = [self]
        if ui_state.urdf_scene is not None and ui_state.urdf_scene is not self:
            views.append(ui_state.urdf_scene)
        for view in views:
            view._fixture_restore_pending = False
            view.fixture_meshes = meshes
            view._unavailable_fixture_data = self._unavailable_fixture_data
            view._fixture_restore_error = self._fixture_restore_error
            view._render_fixture_meshes()
            view._refresh_object_inspector()

    def _fixture(self, key: str) -> FixtureMesh | None:
        return next((m for m in self.fixture_meshes if m.key == key), None)

    def _fixture_keys(self, fixture: FixtureMesh) -> list[str]:
        names = {s.name for s in self._shape_handle().shapes}
        return [
            fixture.key,
            *(f"{SHAPE_PREFIX}{n}" for n in fixture.proxy_names if n in names),
        ]

    def _expand_fixture_selection(self, keys) -> list[str]:
        expanded = []
        for key in keys:
            fixture = self._fixture(key)
            expanded.extend(self._fixture_keys(fixture) if fixture else [key])
        return list(dict.fromkeys(expanded))

    def _fresh_fixture_name(self, stem: str) -> str:
        taken = {m.name for m in self.fixture_meshes}
        taken.update(
            item.get("name")
            for item in self._unavailable_fixture_data
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        taken.update(s.name for s in self._shape_handle().shapes)
        name, number = stem, 2
        while name in taken:
            name, number = f"{stem}-{number}", number + 1
        return name

    def _open_mesh_import(self, at=(0.3, 0.0, 0.0)) -> None:
        from waldo_commander.components.mesh_import import MeshImport
        from waldo_commander.components.world_library import close_world_library

        panel = self._scene_panel()
        if panel is None:
            return
        if self._mesh_import is not None:
            self._mesh_import.close()
        close_world_library(self)
        panel.open()
        self._mesh_import = MeshImport(self, self._library_host, at)

    def _render_fixture_meshes(self) -> None:
        if self.scene is None or self.scene.is_deleted:
            return
        desired = {m.key: m for m in self.fixture_meshes}
        for key in self._drawn_meshes.keys() - desired.keys():
            self._shape_objects.pop(key).delete()
        for key, fixture in desired.items():
            last = self._drawn_meshes.get(key)
            node = self._shape_objects.get(key)
            if (
                node is not None
                and last
                and (last.asset, last.scale) != (fixture.asset, fixture.scale)
            ):
                node.delete()
                node = None
            if node is None:
                path = asset_path(fixture.asset)
                url = f"/fixture-assets/{hashlib.sha256(str(path).encode()).hexdigest()}.stl"
                if not any(getattr(route, "path", None) == url for route in app.routes):
                    app.add_static_file(url_path=url, local_file=path)
                with self.scene:
                    node = self.scene.stl(url).scale(fixture.scale).with_name(key)
                    node.material(SceneColors.OBJECT_HEX).hover_effect("glow")
                self._shape_objects[key] = node
            if last != fixture:
                transform = pose_matrix(fixture.pose)
                node.move(*fixture.pose[:3]).rotate_R(transform[:3, :3].tolist())
            node.visible(key not in self._hidden_objects)
        self._drawn_meshes = desired

    def _fixture_tree(self) -> None:
        for fixture in self.fixture_meshes:
            keys = self._fixture_keys(fixture)
            hidden = any(k in self._hidden_objects for k in keys)
            locked = any(k in self._locked_objects for k in keys)
            mark = fixture.name.replace(" ", "-")
            with (
                ui.row()
                .classes(
                    "scene-row w-full items-center no-wrap"
                    + (" scene-row--hidden" if hidden else "")
                )
                .mark(f"scene-mesh-{mark}") as row
            ):
                ui.icon("category", size="18px").classes("scene-row-kind").tooltip(
                    "Fixture"
                )
                ui.label(fixture.name).classes("scene-row-name truncate")
                if len(keys) == 1:
                    ui.icon("warning_amber", size="18px").classes(
                        "text-wc-warning"
                    ).tooltip("No keep-out: this fixture is visual only").mark(
                        "mesh-no-keepout"
                    )
                ui.space()
                ui.button(icon="lock" if locked else "lock_open").props(
                    "flat round dense color=wc-text"
                ).tooltip("Unlock fixture" if locked else "Lock fixture").on(
                    "click.stop",
                    lambda _, ks=keys, on=locked: self._set_locked(ks, not on),
                ).mark(f"scene-mesh-lock-{mark}")
                ui.button(icon="visibility_off" if hidden else "visibility").props(
                    "flat round dense color=wc-text"
                ).tooltip(
                    "Show fixture"
                    if hidden
                    else "Hide fixture · proxies still checked for collisions"
                ).on(
                    "click.stop",
                    lambda _, ks=keys, on=hidden: self._set_hidden(ks, not on),
                ).mark(f"scene-mesh-eye-{mark}")
            self._tree_rows[fixture.key] = row
            row.on(
                "click",
                lambda e, k=fixture.key: self._pick_from_tree(k, e.args),
                ["ctrlKey", "metaKey", "shiftKey"],
            )
            with ui.column().classes("w-full pl-5 gap-0"):
                for shape in self._shape_handle().shapes:
                    if shape.name in fixture.proxy_names:
                        self._tree_row(SHAPE_PREFIX, shape)

    def _fixture_inspector(self, selected, status) -> None:
        fixtures = [s for _, s in selected if isinstance(s, FixtureMesh)]
        single = (
            fixtures[0]
            if len(fixtures) == 1
            and set(self._selected_objects) == set(self._fixture_keys(fixtures[0]))
            else None
        )
        key = (
            tuple(self._selected_objects),
            status,
            frozenset(self._locked_objects),
            frozenset(self._hidden_objects),
            self._isolated_view,
        )
        if self._object_inspector_key == key:
            return
        self._object_inspector_key = key
        self._object_properties.clear()
        readonly = status[0] or any(
            k in self._locked_objects
            or not k.startswith((SHAPE_PREFIX, MESH_PREFIX))
            or s.attachment is not None
            for k, s in selected
        )
        flat = "flat round dense color=wc-text"
        with (
            self._object_properties,
            ui.column().classes("w-full gap-1").mark("object-inspector"),
        ):
            with ui.row().classes("w-full items-center no-wrap gap-0"):
                if single:
                    name = (
                        ui.input(value=single.name)
                        .props("dense borderless hide-bottom-space")
                        .classes("scene-name flex-1 min-w-0")
                        .mark("object-name")
                    )
                    name.set_enabled(not readonly)
                    for event in ("blur", "keydown.enter"):
                        name.on(
                            event,
                            lambda: self._rename_fixture(
                                single, str(name.value).strip()
                            ),
                        )
                    ui.button(
                        icon="add_box", on_click=lambda: self._add_fixture_proxy(single)
                    ).props(flat).tooltip("Add a keep-out box to this fixture").mark(
                        "mesh-add-proxy"
                    ).set_enabled(not readonly)
                else:
                    ui.label(f"{len(selected)} objects").classes("scene-name flex-1")
                ui.button(icon="flip_to_front", on_click=self._toggle_isolation).props(
                    flat
                ).tooltip(
                    "Show all" if self._isolated_view else "Isolate selection"
                ).mark("object-isolate")
                with (
                    ui.button(icon="more_vert")
                    .props(flat)
                    .tooltip("More")
                    .mark("object-actions"),
                    ui.menu(),
                ):
                    ui.menu_item("Delete", on_click=self._delete_objects).classes(
                        "text-wc-error"
                    ).mark("object-delete").set_enabled(not readonly)
            ui.label(status[1]).classes("text-xs text-wc-text-muted").mark(
                "object-feedback"
            ).set_visibility(bool(status[1]))

    async def _rename_fixture(self, fixture: FixtureMesh, name: str) -> None:
        if name == fixture.name:
            return
        if not name or name != self._fresh_fixture_name(name):
            self.object_history.feedback = "Choose a unique, nonempty fixture name"
            self._refresh_object_inspector()
            return
        renamed = replace(fixture, name=name)
        if await self._commit_objects(
            "Rename fixture",
            list(self._shape_handle().shapes),
            self.object_history.context(),
            meshes=tuple(renamed if m == fixture else m for m in self.fixture_meshes),
        ):
            self._select_objects([renamed.key])

    async def _add_fixture_proxy(self, fixture: FixtureMesh) -> None:
        size = mesh_size(fixture.bounds, fixture.scale)
        center = np.mean(fixture.bounds, axis=0) * fixture.scale
        transform = pose_matrix(fixture.pose)
        center = transform[:3, :3] @ center + transform[:3, 3]
        name = self._fresh_shape_name(f"{fixture.name}-proxy")
        proxy = Box(
            name=name,
            x=size[0],
            y=size[1],
            z=size[2],
            pose=(*center, *fixture.pose[3:]),
        )
        updated = replace(fixture, proxy_names=(*fixture.proxy_names, name))
        if await self._commit_objects(
            "Add fixture proxy",
            [*self._shape_handle().shapes, proxy],
            self.object_history.context(),
            meshes=tuple(updated if m == fixture else m for m in self.fixture_meshes),
        ):
            self._select_objects([f"{SHAPE_PREFIX}{name}"])
