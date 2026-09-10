"""Choose portable project contents and review an archive before importing it."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import waldoctl
from nicegui import run, ui

from waldo_commander.project import find_project
from waldo_commander.services.portable_projects import (
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    current_requirements,
    dependency_report,
    export_project,
    import_project,
    inspect_project,
)
from waldo_commander.services.run_records import debugging_export, record_directory
from waldo_commander.services.world_files import library_dir
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


def _read_selected(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Selected file is too large: {path.name}")
    return data


def show_portable_projects(
    program_dir: Path, open_program: Callable[[str], Awaitable[None]]
) -> None:
    active = waldoctl.commander.programs.active
    try:
        project = find_project(active.file_path if active else None)
    except (OSError, ValueError) as error:
        ui.notify(f"Project unavailable: {error}", color="warning")
        return
    store = SetupStore(project / "setups" if project else None)
    recording_dir = (
        project / "recordings"
        if project
        else Path(
            os.environ.get("WALDO_RECORDING_DIR")
            or Path.home() / ".waldo-commander" / "recordings"
        )
    )
    world_dir = project / "worlds" if project else library_dir()
    recordings = {p.stem: p for p in recording_dir.glob("*.json")}
    worlds = {p.stem: p for p in world_dir.glob("*.json")}
    records = sorted(
        record_directory().glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:20]
    records_by_name = {p.stem: p for p in records}
    programs = {p.id: p for p in waldoctl.commander.programs.items}
    pending: bytes | None = None
    destination: Path | None = None
    manifest = None

    with (
        ui.dialog() as dialog,
        ui.card().classes("task-dialog w-[780px] max-w-full flex-nowrap"),
    ):
        ui.label("Projects").classes("text-lg font-semibold")
        with ui.tabs().classes("w-full") as tabs:
            export_tab = ui.tab("Export")
            import_tab = ui.tab("Import")
        with ui.tab_panels(tabs, value=export_tab).classes(
            "w-full min-h-0 overflow-y-auto"
        ):
            with ui.tab_panel(export_tab).classes("gap-2"):
                ui.label(
                    f"Data source: {project.name if project else 'shared saved data'}"
                ).classes("text-sm")
                program_choice = (
                    ui.select(
                        {key: value.filename for key, value in programs.items()},
                        multiple=True,
                        value=[active.id] if active else [],
                        label="Open programs",
                    )
                    .props("dense use-chips")
                    .classes("w-full")
                    .mark("project-programs")
                )
                setup_choice = (
                    ui.select(
                        store.names(),
                        multiple=True,
                        value=[],
                        label="Setups & calibration",
                    )
                    .props("dense use-chips")
                    .classes("w-full")
                    .mark("project-setups")
                )
                recording_choice = (
                    ui.select(
                        sorted(recordings),
                        multiple=True,
                        value=[],
                        label="Recordings",
                    )
                    .props("dense use-chips")
                    .classes("w-full")
                    .mark("project-recordings")
                )
                world_choice = (
                    ui.select(
                        sorted(worlds), multiple=True, value=[], label="Saved worlds"
                    )
                    .props("dense use-chips")
                    .classes("w-full")
                    .mark("project-worlds")
                )
                record_choice = (
                    ui.select(
                        {
                            key: f"{datetime.fromtimestamp(path.stat().st_mtime, UTC).astimezone():%b %d %H:%M:%S} · {key[:8]}"
                            for key, path in records_by_name.items()
                        },
                        multiple=True,
                        value=[],
                        label="Debug records",
                    )
                    .props("dense use-chips")
                    .classes("w-full")
                    .mark("project-debug-records")
                )
                with (
                    ui.expansion("Package requirements", icon="tune")
                    .classes("w-full")
                    .mark("project-requirements-details")
                ):
                    requirements = (
                        ui.textarea(
                            "Requirements (one per line)",
                            value="\n".join(
                                current_requirements(
                                    ui_state.active_robot.backend_package
                                )
                            ),
                        )
                        .props("dense rows=3")
                        .classes("w-full")
                        .mark("project-requirements")
                    )
                    ui.label(
                        "Include helper modules in Programs. Environment files are excluded."
                    ).classes("text-sm")

                async def download():
                    try:
                        files = {}
                        for key in program_choice.value:
                            program = programs[key]
                            root = find_project(program.file_path)
                            relative = (
                                Path(program.file_path)
                                .resolve()
                                .relative_to(root / "programs")
                                .as_posix()
                                if root and program.file_path
                                else program.filename
                            )
                            name = f"programs/{relative}"
                            if name in files:
                                raise ValueError(
                                    f"Selected programs have the same export path: {relative}"
                                )
                            files[name] = program.source.encode("utf-8")
                        selected_paths = {
                            **{
                                f"setups/{name}.json": store.directory / f"{name}.json"
                                for name in setup_choice.value
                            },
                            **{
                                f"recordings/{name}.json": recordings[name]
                                for name in recording_choice.value
                            },
                            **{
                                f"worlds/{name}.json": worlds[name]
                                for name in world_choice.value
                            },
                        }
                        debug_paths = {
                            f"debug/{name}.json": records_by_name[name]
                            for name in record_choice.value
                        }
                        requested = [
                            line.strip()
                            for line in requirements.value.splitlines()
                            if line.strip()
                        ]

                        def build():
                            files.update(
                                {
                                    name: _read_selected(path)
                                    for name, path in selected_paths.items()
                                }
                            )
                            files.update(
                                {
                                    name: debugging_export(path)
                                    for name, path in debug_paths.items()
                                }
                            )
                            return export_project(files, requirements=requested)

                        data = await run.io_bound(build)
                        ui.download.content(
                            data,
                            filename="waldo-project.zip",
                            media_type="application/zip",
                        )
                        ui.notify(
                            f"Exported {len(files)} selected files", color="positive"
                        )
                    except (OSError, ValueError, KeyError) as error:
                        ui.notify(f"Export failed: {error}", color="warning")

                ui.button("Download selected files", on_click=download).mark(
                    "project-export"
                )
            with ui.tab_panel(import_tab).classes("gap-2"):
                ui.label("Inspect an archive, then import into a new folder.").classes(
                    "text-sm"
                )
                summary = ui.label("Choose a project archive to inspect.").mark(
                    "project-import-summary"
                )
                file_table = (
                    ui.table(
                        columns=[
                            {
                                "name": "path",
                                "label": "File",
                                "field": "path",
                                "align": "left",
                            },
                            {"name": "size", "label": "Bytes", "field": "size"},
                        ],
                        rows=[],
                        row_key="path",
                        pagination=6,
                    )
                    .props("dense")
                    .classes("w-full")
                    .mark("project-import-files")
                )
                dependencies = (
                    ui.label("")
                    .classes("whitespace-pre-line text-sm")
                    .mark("project-dependencies")
                )
                name = (
                    ui.input("New folder name", value="project")
                    .props("dense")
                    .classes("w-full")
                    .mark("project-folder-name")
                )

                async def uploaded(event):
                    nonlocal pending, destination, manifest
                    pending = None
                    destination = None
                    manifest = None
                    import_button.disable()
                    open_button.disable()
                    choice.set_options([], value=None)
                    file_table.rows = []
                    file_table.update()
                    dependencies.text = ""
                    try:
                        if event.file.size() > MAX_ARCHIVE_BYTES:
                            raise ValueError("Project archive is too large")
                        data = await event.file.read()
                        result = await run.io_bound(inspect_project, data)
                        if result is None:
                            return
                        inspected, _ = result
                        issues = dependency_report(inspected["requirements"])
                        pending = data
                        manifest = inspected
                        file_table.rows = inspected["files"]
                        file_table.update()
                        summary.text = f"Validated {len(inspected['files'])} files. Ready to import."
                        dependencies.text = (
                            "\n".join(issues)
                            if issues
                            else "Listed package versions are available."
                        )
                        import_button.enable()
                    except (OSError, ValueError) as error:
                        summary.text = f"Archive refused: {error}"

                ui.upload(
                    on_upload=uploaded,
                    auto_upload=True,
                    max_file_size=MAX_ARCHIVE_BYTES,
                ).props("accept=.zip").classes("w-full").mark("project-upload")

                async def import_files():
                    nonlocal destination
                    if pending is None or manifest is None:
                        return
                    import_button.disable()
                    try:
                        destination = await run.io_bound(
                            import_project,
                            pending,
                            program_dir / "projects",
                            name=name.value,
                        )
                        if destination is None:
                            return
                        entries = [
                            e["path"]
                            for e in manifest["files"]
                            if e["path"].startswith("programs/")
                        ]
                        choice.set_options(
                            entries, value=entries[0] if entries else None
                        )
                        open_button.set_enabled(bool(entries))
                        summary.text = f"Imported to {destination.name} · {len(manifest['files'])} files"
                    except (OSError, ValueError) as error:
                        summary.text = f"Import failed: {error}"
                        import_button.enable()

                async def open_selected():
                    if destination is not None and choice.value:
                        await open_program(str(destination / choice.value))
                        dialog.close()

                import_button = ui.button(
                    "Import into new folder", on_click=import_files
                ).mark("project-import")
                import_button.disable()
                choice = (
                    ui.select([], label="Imported program")
                    .classes("w-full")
                    .mark("project-open-choice")
                )
                open_button = ui.button(
                    "Open selected program", on_click=open_selected
                ).mark("project-open")
                open_button.disable()
        ui.button("Close", on_click=dialog.close).props("flat").classes("shrink-0")
    dialog.on("hide", dialog.delete)
    dialog.open()
