"""STL import in the scene panel."""

from pathlib import Path
from uuid import uuid4

from nicegui import events, run, ui
from waldoctl.shapes import Box

from waldo_commander.services.fixture_meshes import (
    FixtureMesh,
    MAX_STL_BYTES,
    UNITS,
    mesh_size,
    stl_bounds,
)
from waldo_commander.services import world_files


class MeshImport:
    def __init__(self, view, host, at) -> None:
        self.view, self.at = view, at
        self.data: bytes | None = None
        self.bounds = None
        self.filename = ""
        self.unit = "mm"
        self.busy = False
        self._upload_generation = 0
        with host:
            self.card = ui.column().classes("scene-library w-full").mark("mesh-import")
            self.card.on("keydown.escape", self.close)
            with self.card:
                with ui.row().classes("w-full items-center no-wrap"):
                    ui.label("Fixture mesh").classes("scene-section flex-1")
                    self.add = (
                        ui.button(icon="add", on_click=self.place)
                        .props("flat round dense color=wc-action")
                        .tooltip("Place it in the scene")
                        .mark("mesh-add")
                    )
                    ui.button(icon="close", on_click=self.close).props(
                        "flat round dense color=wc-text"
                    ).tooltip("Close").mark("mesh-import-close")
                ui.upload(
                    on_upload=self.upload,
                    on_rejected=lambda: self.feedback.set_text(
                        "Choose a single STL file"
                    ),
                    auto_upload=True,
                    multiple=False,
                    max_file_size=MAX_STL_BYTES,
                ).props('accept=".stl" max-files=1 flat bordered').classes(
                    "w-full"
                ).mark("mesh-upload").on("removed", self.clear_upload, args=[])
                with ui.button_group().props("flat"):
                    self.units = {
                        unit: ui.button(unit, on_click=lambda u=unit: self.set_units(u))
                        .tooltip(f"STL coordinates in {unit}")
                        .mark(f"mesh-units-{unit}")
                        for unit in UNITS
                    }
                self.size = ui.label().classes("text-sm").mark("mesh-size")
                self.feedback = (
                    ui.label().classes("text-xs text-wc-error").mark("mesh-error")
                )
        self.refresh()

    def close(self) -> None:
        self._upload_generation += 1
        self.data, self.bounds, self.filename = None, None, ""
        if self.view._mesh_import is self:
            self.view._mesh_import = None
        if not self.card.is_deleted:
            self.card.delete()

    def clear_upload(self) -> None:
        self._upload_generation += 1
        self.data, self.bounds = None, None
        self.filename = ""
        self.feedback.set_text("")
        self.refresh()

    async def upload(self, event: events.UploadEventArguments) -> None:
        self.clear_upload()
        generation = self._upload_generation
        try:
            if event.file.size() > MAX_STL_BYTES:
                raise ValueError("STL files must be at most 10 MiB")
            data = await event.file.read()
            if generation != self._upload_generation or self.card.is_deleted:
                return
            bounds = await run.io_bound(stl_bounds, data)
            if generation != self._upload_generation or self.card.is_deleted:
                return
            self.filename = Path(event.file.name.replace("\\", "/")).name
            self.data, self.bounds = data, bounds
        except (ValueError, OSError) as error:
            if generation != self._upload_generation or self.card.is_deleted:
                return
            self.feedback.set_text(str(error))
        self.refresh()

    def set_units(self, unit: str) -> None:
        self.unit = unit
        self.refresh()

    def refresh(self) -> None:
        valid = False
        self.size.set_text("")
        if self.bounds is not None:
            self.feedback.set_text("")
            try:
                size = mesh_size(self.bounds, UNITS[self.unit])
                self.size.set_text(" × ".join(f"{v * 1000:g}" for v in size) + " mm")
                valid = True
            except ValueError as error:
                self.feedback.set_text(str(error))
        self.add.set_enabled(valid and not self.busy)
        for unit, button in self.units.items():
            on = unit == self.unit
            button.props(
                f"flat color={'wc-action' if on else 'wc-text'} "
                f"aria-pressed={'true' if on else 'false'}"
            )

    async def place(self) -> None:
        if self.busy or self.data is None or self.bounds is None:
            return
        data, bounds, generation = self.data, self.bounds, self._upload_generation
        self.busy = True
        self.refresh()
        path = None
        try:
            view = self.view
            revision = view.object_history.context()
            handle = view.object_history.guard(revision)
            scale = UNITS[self.unit]
            size = mesh_size(self.bounds, scale)
            name = view._fresh_fixture_name(Path(self.filename).stem or "Fixture")
            proxy_name = view._fresh_shape_name(f"{name}-proxy")
            low, high = self.bounds
            center = [(a + (b - a) / 2) * scale for a, b in zip(low, high)]
            x, y, _ = self.at
            pose = (x - center[0], y - center[1], -low[2] * scale, 0.0, 0.0, 0.0)
            proxy = Box(
                name=proxy_name,
                x=size[0],
                y=size[1],
                z=size[2],
                pose=(x, y, size[2] / 2, 0.0, 0.0, 0.0),
            )
            asset = f"assets/{uuid4().hex}.stl"
            path = world_files.library_dir() / asset
            await run.io_bound(path.parent.mkdir, parents=True, exist_ok=True)
            await run.io_bound(path.write_bytes, data)
            if self.card.is_deleted or generation != self._upload_generation:
                return
            fixture = FixtureMesh(asset, scale, pose, name, (proxy_name,), bounds)
            if not await view._commit_objects(
                "Import fixture",
                [*handle.shapes, proxy],
                revision,
                meshes=(*view.fixture_meshes, fixture),
            ):
                raise ValueError(view.object_history.feedback)
            path = None
            view._select_objects([fixture.key])
            self.close()
        except (ValueError, OSError) as error:
            if not self.card.is_deleted:
                self.feedback.set_text(str(error))
        finally:
            if path is not None:
                await run.io_bound(path.unlink, missing_ok=True)
            self.busy = False
            if not self.card.is_deleted:
                self.refresh()
