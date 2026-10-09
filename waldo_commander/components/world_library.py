"""Save, reload, and insert the editable workcell from the scene."""

from __future__ import annotations

import asyncio

from nicegui import ui
from waldoctl.shapes import ShapeWorld

from waldo_commander.services import world_files
from waldo_commander.services.motion_recorder import (
    motion_recorder,
    shape_imports,
    shapes_to_code,
)
from waldo_commander.services.python_source import in_async_scope
from waldo_commander.state import ui_state


def show_world_library(view) -> None:
    history = view.object_history
    revision = history.context()
    try:
        handle = history.guard(revision)
        entries = world_files.list_entries()
    except (OSError, ValueError) as error:
        ui.notify(str(error), color="warning")
        return
    view._select_objects([])
    view.cancel_target_edit()
    # A flange attachment is bound to this controller session's epoch, so it
    # is neither saved nor inserted, and a loaded workcell keeps what is held.
    snapshot = tuple(s for s in handle.shapes if s.attachment is None)
    held = len(handle.shapes) - len(snapshot)

    saved_revision = None
    loaded = None
    load_generation = 0

    with view.scene.client, ui.context.client.content, ui.dialog() as dialog:
        with (
            ui.card()
            .classes("task-dialog w-[30rem] max-w-full")
            .mark("workcell-library")
        ):
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("Workcell library").classes("text-lg font-medium flex-1")
                ui.button(icon="close", on_click=dialog.close).props(
                    "flat round dense color=wc-text"
                ).mark("workcell-close")
            ui.label(
                "Save editable objects with their dimensions, poses and properties."
            ).classes("text-sm text-wc-text-muted")
            with ui.tabs().props("dense no-caps").classes("w-full") as tabs:
                current_tab = ui.tab("Current workcell").mark("workcell-current-tab")
                saved_tab = ui.tab("Saved workcells").mark("workcell-saved-tab")
            with ui.tab_panels(tabs, value=current_tab).classes(
                "w-full bg-transparent"
            ):
                with ui.tab_panel(current_tab).classes("p-0"):
                    ui.label(f"{len(snapshot)} editable objects").classes("font-medium")
                    if held:
                        ui.label(
                            f"{held} flange attachment{'s' if held > 1 else ''} not included"
                        ).classes("text-xs text-wc-text-muted")
                    with ui.column().classes("w-full max-h-40 overflow-auto"):
                        for shape in snapshot:
                            ui.label(f"{shape.name} · {shape.kind}").classes("text-sm")
                    name = (
                        ui.input("Save as", placeholder="bench-workcell")
                        .props("dense outlined")
                        .classes("w-full")
                        .mark("workcell-name")
                    )
                    overwrite = (
                        ui.checkbox("Replace the existing saved workcell")
                        .props("dense")
                        .mark("workcell-overwrite")
                    )
                    overwrite.set_visibility(False)

                    def name_changed():
                        nonlocal saved_revision
                        overwrite.set_value(False)
                        try:
                            saved_revision = world_files.entry_revision(
                                str(name.value).strip()
                            )
                        except (OSError, ValueError):
                            saved_revision = None
                        overwrite.set_visibility(saved_revision is not None)

                    name.on_value_change(name_changed)

                    async def save():
                        value = str(name.value).strip()
                        try:
                            history.guard(revision)
                            if world_files.entry_revision(value) != saved_revision:
                                raise ValueError(
                                    "This saved workcell changed elsewhere. Re-enter its name to review the replacement."
                                )
                            if saved_revision is not None and not overwrite.value:
                                raise ValueError(
                                    "Select Replace to overwrite this saved workcell."
                                )
                            world_files.save_entry(value, ShapeWorld(program=snapshot))
                            saved.set_options(world_files.list_entries(), value=value)
                            feedback.set_text(f"Saved {value}")
                            name_changed()
                        except (OSError, ValueError) as error:
                            feedback.set_text(str(error))

                    ui.button("Save workcell", icon="save", on_click=save).props(
                        "unelevated no-caps color=wc-action text-color=wc-on-bright"
                    ).mark("workcell-save")
                    ui.separator()
                    ui.label(
                        "Insert a copy into the program at the current cursor. The saved file is not required to run it."
                    ).classes("text-sm text-wc-text-muted")

                    def insert():
                        try:
                            history.guard(revision)
                            editor = ui_state.active_textarea
                            if editor is None or editor.client is not view.scene.client:
                                raise ValueError(
                                    "Open a program before inserting the workcell."
                                )
                            motion_recorder.insert_scene_code(
                                lambda original, line: shape_imports(
                                    snapshot, original, line
                                )
                                + ("await " if in_async_scope(original, line) else "")
                                + shapes_to_code(snapshot)
                            )
                            dialog.close()
                        except ValueError as error:
                            feedback.set_text(str(error))

                    ui.button(
                        "Insert objects into program", icon="code", on_click=insert
                    ).props("flat no-caps color=wc-action").mark("workcell-insert")
                with ui.tab_panel(saved_tab).classes("p-0"):
                    saved = (
                        ui.select(entries, label="Saved workcell")
                        .props("dense outlined")
                        .classes("w-full")
                        .mark("workcell-saved")
                    )
                    preview = (
                        ui.label("Choose a saved workcell.")
                        .classes("text-sm whitespace-pre-line max-h-40 overflow-auto")
                        .mark("workcell-preview")
                    )

                    async def inspect_saved(event):
                        nonlocal loaded, load_generation
                        load_generation += 1
                        generation = load_generation
                        loaded = None
                        load.disable()
                        selected = event.value
                        if not selected:
                            preview.set_text("Choose a saved workcell.")
                            return
                        try:
                            world, file_revision = await asyncio.to_thread(
                                world_files.read_literal_entry, selected
                            )
                            if dialog.is_deleted or generation != load_generation:
                                return
                            shapes = tuple(
                                s for s in world.program if s.attachment is None
                            )
                            history.validate_names(shapes)
                            loaded = (selected, shapes, file_revision)
                            preview.set_text(
                                f"{len(shapes)} editable objects\n"
                                + "\n".join(f"{s.name} · {s.kind}" for s in shapes)
                                + (
                                    "\nInstallation entries are read only and will not be loaded."
                                    if world.installation
                                    else ""
                                )
                            )
                            load.enable()
                        except (OSError, ValueError, TypeError, KeyError) as error:
                            if not dialog.is_deleted and generation == load_generation:
                                preview.set_text(str(error))

                    saved.on_value_change(inspect_saved)

                    async def apply():
                        if loaded is None:
                            return
                        selected, shapes, file_revision = loaded
                        try:
                            if world_files.entry_revision(selected) != file_revision:
                                raise ValueError(
                                    "This saved workcell changed elsewhere. Select it again before loading."
                                )
                            load.disable()
                            applied = await view._commit_objects(
                                "Load workcell",
                                [
                                    *shapes,
                                    *(
                                        s
                                        for s in handle.shapes
                                        if s.attachment is not None
                                    ),
                                ],
                                revision,
                            )
                            if (
                                dialog.is_deleted
                                or not dialog.value
                                or view.scene.is_deleted
                            ):
                                return
                            if applied:
                                view._select_objects([])
                                view._show_object_history = True
                                view._refresh_object_inspector()
                                dialog.close()
                            else:
                                feedback.set_text(history.feedback)
                        except (OSError, ValueError) as error:
                            if not dialog.is_deleted and dialog.value:
                                feedback.set_text(str(error))
                        finally:
                            if not load.is_deleted:
                                load.enable()

                    load = (
                        ui.button(
                            "Replace editable objects",
                            icon="folder_open",
                            on_click=apply,
                        )
                        .props(
                            "unelevated no-caps color=wc-action text-color=wc-on-bright"
                        )
                        .mark("workcell-load")
                    )
                    load.disable()
                    ui.label(
                        "Loading replaces the current editable objects. Undo restores them."
                    ).classes("text-sm text-wc-text-muted")
            feedback = (
                ui.label()
                .classes("text-sm text-wc-text-muted")
                .mark("workcell-feedback")
            )
    dialog.on("hide", lambda: dialog.is_deleted or dialog.delete())
    view._object_dialogs.append((dialog, history.document()))
    dialog.open()
