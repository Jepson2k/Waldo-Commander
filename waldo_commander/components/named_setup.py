"""Teach and edit static setup snapshots without changing a running program."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, cast

from nicegui import context, ui
from waldoctl import Commander, Panel, PanelSlot
from waldoctl.setup import Frame, Parameter, Pose, PoseValues, SetupSnapshot

from waldo_commander.components.tcp_calibration import TcpCalibrationEditor
from waldo_commander.services.python_source import insert_prelude
from waldo_commander.setup import (
    SetupStore,
    add_save_listener,
    export_snapshot,
    merge_snapshots,
)


class NamedSetupPanel(Panel):
    id: ClassVar[str] = "setup"
    display_name: ClassVar[str] = "Setup"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_TOP_TAB
    tab_icon: ClassVar[str] = "architecture"
    tab_tooltip: ClassVar[str] = "Named frames, poses and parameters"
    order: ClassVar[int] = 20
    default_width: ClassVar[int] = 440
    min_width: ClassVar[int] = 360
    min_height: ClassVar[int] = 420
    resizable: ClassVar[bool] = True

    def __init__(self) -> None:
        self._drop_save_listener: Callable[[], None] = lambda: None

    async def stop(self) -> None:
        self._drop_save_listener()

    def build(self, commander: Commander) -> None:
        # Only the latest page's panel follows saves; an earlier build's
        # elements belong to a dead client.
        self._drop_save_listener()
        store = SetupStore()
        snapshot = SetupSnapshot()
        persisted = snapshot
        # The saved setup `persisted` came from, and that file's revision then:
        # Save replaces nothing else without asking.
        loaded_name: str | None = None
        loaded_revision: str | None = None
        writing = False
        baselines: dict[str, tuple] = {}
        fields: dict[str, list] = {}
        initial_values: dict[str, tuple] = {}
        loading = False

        def inform(message: str) -> None:
            status.set_text(message)

        def refresh() -> None:
            frame_options = ["WRF", *snapshot.frames]
            for selector in (frame_parent, pose_frame):
                selector.set_options(
                    frame_options,
                    value=selector.value if selector.value in frame_options else "WRF",
                )
            for selector, entries in (
                (frame_existing, snapshot.frames),
                (pose_existing, snapshot.poses),
                (parameter_existing, snapshot.parameters),
            ):
                selector.set_options(
                    list(entries),
                    value=selector.value if selector.value in entries else None,
                )
            summary.refresh()
            tcp_editor.refresh()

        def set_snapshot(updated: SetupSnapshot) -> None:
            nonlocal snapshot
            changed = [
                kind
                for kind, before, after in (
                    ("tcp", snapshot.tcp_calibrations, updated.tcp_calibrations),
                )
                if before != after
            ]
            snapshot = updated
            for kind in changed:
                remember(kind)
            refresh()

        def load(name: str) -> None:
            nonlocal loaded_name, loaded_revision
            try:
                loaded, revision = store.read(name)
            except (OSError, ValueError) as error:
                inform(str(error))
                return
            loaded_name, loaded_revision = name, revision
            adopt(loaded)
            inform(f"Loaded {name}")

        def adopt(saved_snapshot: SetupSnapshot) -> None:
            """Show *saved_snapshot* as the clean copy of ``loaded_name``."""
            nonlocal snapshot, persisted, loading
            snapshot = persisted = saved_snapshot
            setup_name.set_value(loaded_name)
            loading = True
            for kind, widgets in fields.items():
                for widget, value in zip(widgets, initial_values[kind]):
                    widget.set_value(value)
            tcp_editor.reset()
            refresh()
            # Another session (or a script) can write a setup after this panel
            # was built; without the options the dropdown drops a value it
            # does not list and shows nothing for the setup just loaded.
            saved.set_options(store.names(), value=loaded_name)
            for selector, entries, select in (
                (frame_existing, snapshot.frames, select_frame),
                (pose_existing, snapshot.poses, select_pose),
                (parameter_existing, snapshot.parameters, select_parameter),
                (tcp_editor.existing, snapshot.tcp_calibrations, select_tcp),
            ):
                if entries:
                    selector.set_value(next(iter(entries)))
                    select(selector.value)
            loading = False
            remember()

        #: The entry each tab's fields are currently showing, so a refused
        #: switch can put the selector back on it.
        shown: dict[str, str | None] = {}

        def signature(kind: str) -> tuple:
            values = tuple(field.value for field in fields[kind])
            if kind == "tcp":
                # A measurement can bind or re-bind the same numbers.
                return (*values, tcp_editor.draft_state())
            return values

        def remember(kind: str | None = None) -> None:
            for key in [kind] if kind else fields:
                baselines[key] = signature(key)
            update_dirty()

        def update_dirty() -> None:
            dirty.set_visibility(
                snapshot != persisted
                or any(signature(k) != baselines.get(k) for k in fields)
            )

        def pending_snapshot(kinds=None) -> SetupSnapshot:
            updated = snapshot
            for kind in fields if kinds is None else kinds:
                if signature(kind) == baselines.get(kind):
                    continue
                if kind == "frames":
                    updated = updated.with_frame(
                        frame_name.value,
                        Frame(values(frame_values), frame_parent.value),
                    )
                elif kind == "poses":
                    updated = updated.with_pose(
                        pose_name.value, Pose(values(pose_values), pose_frame.value)
                    )
                elif kind == "parameters":
                    updated = updated.with_parameter(parameter_name.value, parameter())
                elif kind == "tcp":
                    updated = updated.with_tcp_calibration(
                        tcp_editor.name.value, tcp_editor.calibration()
                    )
            return updated

        def keep_current(kind: str) -> bool:
            """Commit the pending edit before the selection moves off it.

            The committed entry is part of the working snapshot, so the frame
            option lists, the entry selectors and the resolved-pose table are
            rebuilt with it -- otherwise a frame the user just kept is absent
            from every list until some unrelated Keep or Remove rebuilds them.
            """
            nonlocal snapshot
            if loading or not fields:
                return True
            try:
                snapshot = pending_snapshot([kind])
            except (ValueError, TypeError) as error:
                inform(f"Keep the current edit valid before switching: {error}")
                return False
            refresh()
            return True

        def save() -> None:
            name = setup_name.value
            try:
                updated = pending_snapshot()
                on_disk = store.revision(name)
            except (OSError, ValueError, TypeError) as error:
                inform(str(error))
                return
            if on_disk is None or on_disk == (
                loaded_revision if name == loaded_name else None
            ):
                write(name, updated)
            else:
                confirm_replace(name, updated)

        def write(name: str, updated: SetupSnapshot) -> bool:
            nonlocal snapshot, persisted, loaded_name, loaded_revision, writing
            writing = True
            try:
                revision = store.save(name, updated)
            except (OSError, ValueError, TypeError) as error:
                inform(str(error))
                return False
            finally:
                writing = False
            snapshot = persisted = updated
            loaded_name, loaded_revision = name, revision
            remember()
            refresh()
            saved.set_options(store.names(), value=name)
            inform(f"Saved {name}")
            return True

        def merge(name: str) -> None:
            try:
                theirs, _ = store.read(name)
                merged = merge_snapshots(
                    persisted if name == loaded_name else SetupSnapshot(),
                    pending_snapshot(),
                    theirs,
                )
            except (OSError, ValueError, TypeError) as error:
                inform(f"Could not merge into {name}: {error}")
                return
            if write(name, merged):
                adopt(merged)
                inform(f"Merged your edits into {name}")

        def confirm_replace(name: str, updated: SetupSnapshot) -> None:
            edited = dirty.visible
            prompt.clear()
            with prompt, ui.card().classes("task-dialog"):
                ui.label(
                    f"{name} changed on disk since it was loaded"
                    if name == loaded_name
                    else f"{name} is already saved and is not loaded here"
                ).classes("panel-heading")
                ui.label(
                    "Merge keeps the saved entries you did not edit. "
                    "Overwrite replaces the whole setup with this one."
                    if edited
                    else "Load it to edit the saved setup, or overwrite it "
                    "with this one."
                ).classes("text-caption")
                with ui.row().classes("panel-actions"):
                    ui.button("Cancel", on_click=prompt.close).props("flat").mark(
                        "setup-replace-cancel"
                    )
                    ui.button(
                        "Overwrite",
                        on_click=lambda: (prompt.close(), write(name, updated)),
                    ).props("flat").mark("setup-replace-overwrite")
                    if edited:
                        ui.button(
                            "Merge", on_click=lambda: (prompt.close(), merge(name))
                        ).mark("setup-replace-merge")
                    else:
                        ui.button(
                            "Load it", on_click=lambda: (prompt.close(), load(name))
                        ).mark("setup-replace-load")
            prompt.open()

        def show_loaded() -> None:
            saved.set_value(loaded_name if loaded_name in saved.options else None)

        def pick(name: str | None) -> None:
            if name is not None and name != loaded_name:
                request_load(name)

        def request_load(name: str) -> None:
            if not dirty.visible:
                load(name)
                return
            prompt.clear()
            with prompt, ui.card().classes("task-dialog"):
                ui.label("Discard unsaved setup edits?").classes("panel-heading")
                with ui.row().classes("panel-actions"):
                    ui.button("Keep editing", on_click=prompt.close).props("flat")
                    ui.button(
                        "Discard and load",
                        on_click=lambda: (load(name), prompt.close()),
                    )
            prompt.open()

        def on_saved(directory: Path, name: str, revision: str) -> None:
            if writing or directory != store.directory:
                return
            saved.set_options(store.names(), value=saved.value)
            if name != loaded_name or revision == loaded_revision:
                return
            if dirty.visible:
                inform(f"{name} was saved elsewhere; Save will ask before replacing it")
            else:
                load(name)

        self._drop_save_listener = add_save_listener(on_saved)
        context.client.on_delete(self._drop_save_listener)

        # One prompt, refilled for each question, so a dismissed one leaves no
        # stale buttons behind.
        prompt = ui.dialog()
        # A picked setup that was not loaded leaves the list on the one that is.
        prompt.on_value_change(lambda e: None if e.value else show_loaded())

        def insert_load() -> None:
            program = commander.programs.active
            if program is None or program.execution.is_running:
                inform("Open a stopped program before inserting a load call")
                return
            try:
                store.load(setup_name.value)
            except (OSError, ValueError) as error:
                inform(f"Save the setup first: {error}")
                return
            source = f"from waldo_commander.setup import load_setup\nsetup = load_setup({setup_name.value!r})\n\n"
            try:
                new_source = insert_prelude(program.source, source)
            except ValueError as error:
                inform(str(error))
                return
            from waldo_commander.state import ui_state

            textarea = ui_state.textareas_by_tab.get(program.id)
            if textarea is not None:
                textarea.set_value(new_source)
            program.source = new_source
            inform("Inserted setup load at the start of the active program")

        def export() -> None:
            try:
                ui.download(
                    export_snapshot(pending_snapshot()).encode("utf-8"),
                    "setup_snapshot.py",
                )
                inform("Exported the current fixed snapshot")
            except (ValueError, TypeError) as error:
                inform(str(error))

        with ui.column().classes("w-full h-full min-h-0 flex-nowrap gap-2"):
            with ui.row().classes("w-full items-center justify-between shrink-0"):
                ui.label("Setup").classes("panel-heading")
                dirty = (
                    ui.label("Unsaved changes")
                    .classes("text-amber-300 text-caption")
                    .mark("setup-dirty")
                )
                dirty.set_visibility(False)
            with ui.row().classes("w-full items-center"):
                setup_name = (
                    ui.input("Setup name", value="bench")
                    .props("dense")
                    .classes("grow")
                    .mark("setup-name")
                )
                saved = (
                    ui.select(
                        store.names(),
                        label="Saved",
                        on_change=lambda e: pick(e.value),
                    )
                    .props("dense")
                    .classes("grow")
                    .mark("setup-saved")
                )
            with ui.row():
                ui.button(
                    "Load", on_click=lambda: request_load(setup_name.value)
                ).props("dense flat").mark("setup-load")
                ui.button("Save setup", on_click=save).props("dense").mark("setup-save")
                ui.button("Insert load call", on_click=insert_load).props(
                    "dense flat"
                ).mark("setup-insert-load")
                ui.button("Export snapshot", on_click=export).props("dense flat").mark(
                    "setup-export"
                )
            status = (
                ui.label("Save setup keeps all edited fields.")
                .classes("text-caption")
                .mark("setup-status")
            )

            with ui.tabs().classes("w-full shrink-0") as tabs:
                frames_tab = ui.tab("Frames")
                poses_tab = ui.tab("Poses")
                params_tab = ui.tab("Parameters")
                tcp_tab = ui.tab("TCP")

            def coordinates(prefix: str) -> list[ui.number]:
                with ui.grid(columns=3).classes("w-full"):
                    return [
                        ui.number(label, value=0.0, format="%.3f")
                        .props("dense")
                        .classes("w-full")
                        .mark(f"{prefix}-{key}")
                        for key, label in zip(
                            ("x", "y", "z", "rx", "ry", "rz"),
                            (
                                "X (mm)",
                                "Y (mm)",
                                "Z (mm)",
                                "Roll (°)",
                                "Pitch (°)",
                                "Yaw (°)",
                            ),
                        )
                    ]

            def values(inputs: list[ui.number]) -> PoseValues:
                if any(item.value is None for item in inputs):
                    raise ValueError("Fill all six pose coordinates")
                return cast(PoseValues, tuple(float(item.value) for item in inputs))

            async def teach(inputs: list[ui.number], reference: ui.select) -> None:
                try:
                    observed = await asyncio.wait_for(
                        commander.client.pose(), timeout=2.0
                    )
                    if observed is None:
                        raise ValueError("No fresh TCP pose is available")
                    # Frame edits pending on the Frames tab are saved in the
                    # same write as this pose, so resolve against them.
                    local = pending_snapshot(["frames"]).relative_pose(
                        Pose(cast(PoseValues, tuple(observed))), reference.value
                    )
                    for element, number in zip(inputs, local.values):
                        element.set_value(number)
                    inform("Captured TCP. Save setup to keep it.")
                except (OSError, ValueError, TimeoutError) as error:
                    inform(str(error))

            def set_frame() -> None:
                nonlocal snapshot
                try:
                    snapshot = snapshot.with_frame(
                        frame_name.value,
                        Frame(values(frame_values), frame_parent.value),
                    )
                    refresh()
                    remember("frames")
                    inform(f"Frame {frame_name.value} updated. Save setup to keep it.")
                except ValueError as error:
                    inform(str(error))

            def set_pose() -> None:
                nonlocal snapshot
                try:
                    snapshot = snapshot.with_pose(
                        pose_name.value, Pose(values(pose_values), pose_frame.value)
                    )
                    refresh()
                    remember("poses")
                    inform(f"Pose {pose_name.value} updated. Save setup to keep it.")
                except ValueError as error:
                    inform(str(error))

            def parameter() -> Parameter:
                raw = parameter_value.value
                if parameter_type.value == "number":
                    value = float(raw)
                elif parameter_type.value == "integer":
                    value = int(raw)
                elif parameter_type.value == "boolean":
                    if raw.lower() not in ("true", "false"):
                        raise ValueError("Use true or false for a boolean")
                    value = raw.lower() == "true"
                else:
                    value = raw
                return Parameter(value, parameter_unit.value)

            def set_parameter() -> None:
                nonlocal snapshot
                try:
                    snapshot = snapshot.with_parameter(
                        parameter_name.value, parameter()
                    )
                    refresh()
                    remember("parameters")
                    inform(
                        f"Parameter {parameter_name.value} updated. Save setup to keep it."
                    )
                except (ValueError, TypeError) as error:
                    inform(str(error))

            def remove(kind: str, name: str) -> None:
                nonlocal snapshot
                if kind == "frames":
                    for selector, dependent in (
                        (pose_frame, "poses"),
                        (frame_parent, "frames"),
                    ):
                        if selector.value == name and signature(
                            dependent
                        ) != baselines.get(dependent):
                            inform(
                                f"Keep or discard the pending {dependent[:-1]} in "
                                f"{name} before removing the frame"
                            )
                            return
                try:
                    snapshot = snapshot.without(kind, name)
                    refresh()
                    remember(kind)
                    inform(f"Removed {name}; save to persist")
                except (KeyError, ValueError) as error:
                    inform(str(error))

            def select_frame(name: str | None) -> None:
                if name not in snapshot.frames:
                    return
                if not keep_current("frames"):
                    # The selector has already moved to *name*; NiceGUI
                    # suppresses a same-value change, so leaving it there would
                    # make re-picking this entry do nothing for the rest of the
                    # session. Put it back on the entry the fields still show.
                    frame_existing.set_value(shown.get("frames"))
                    return
                shown["frames"] = name
                entry = snapshot.frames[name]
                frame_name.set_value(name)
                frame_parent.set_value(entry.parent)
                for element, number in zip(frame_values, entry.values):
                    element.set_value(number)
                remember("frames")

            def select_pose(name: str | None) -> None:
                if name not in snapshot.poses:
                    return
                if not keep_current("poses"):
                    # The selector has already moved to *name*; NiceGUI
                    # suppresses a same-value change, so leaving it there would
                    # make re-picking this entry do nothing for the rest of the
                    # session. Put it back on the entry the fields still show.
                    pose_existing.set_value(shown.get("poses"))
                    return
                shown["poses"] = name
                entry = snapshot.poses[name]
                pose_name.set_value(name)
                pose_frame.set_value(entry.frame)
                for element, number in zip(pose_values, entry.values):
                    element.set_value(number)
                remember("poses")

            def select_parameter(name: str | None) -> None:
                if name not in snapshot.parameters:
                    return
                if not keep_current("parameters"):
                    # The selector has already moved to *name*; NiceGUI
                    # suppresses a same-value change, so leaving it there would
                    # make re-picking this entry do nothing for the rest of the
                    # session. Put it back on the entry the fields still show.
                    parameter_existing.set_value(shown.get("parameters"))
                    return
                shown["parameters"] = name
                entry = snapshot.parameters[name]
                parameter_name.set_value(name)
                parameter_type.set_value(
                    {float: "number", int: "integer", bool: "boolean", str: "text"}[
                        type(entry.value)
                    ]
                )
                parameter_value.set_value(
                    str(entry.value).lower()
                    if isinstance(entry.value, bool)
                    else str(entry.value)
                )
                parameter_unit.set_value(entry.unit)
                remember("parameters")

            def select_tcp(name: str | None) -> None:
                if name not in snapshot.tcp_calibrations:
                    return
                if not keep_current("tcp"):
                    tcp_editor.existing.set_value(shown.get("tcp"))
                    return
                shown["tcp"] = name
                tcp_editor.load(name)
                remember("tcp")

            with ui.tab_panels(tabs, value=frames_tab).classes(
                "w-full flex-1 min-h-0 overflow-y-auto gap-2"
            ):
                with ui.tab_panel(frames_tab).classes("p-0"):
                    frame_existing = (
                        ui.select(
                            [],
                            label="Edit frame",
                            on_change=lambda e: select_frame(e.value),
                        )
                        .props("dense")
                        .classes("w-full")
                        .mark("setup-frame-existing")
                    )
                    with ui.row().classes("w-full"):
                        frame_name = (
                            ui.input("Frame name", value="fixture")
                            .props("dense")
                            .classes("flex-1 min-w-0")
                            .mark("setup-frame-name")
                        )
                        frame_parent = (
                            ui.select(["WRF"], value="WRF", label="Parent")
                            .props("dense")
                            .classes("w-36")
                            .mark("setup-frame-parent")
                        )
                    frame_values = coordinates("setup-frame")
                    with ui.row():
                        ui.button(
                            "Use current TCP",
                            on_click=lambda: teach(frame_values, frame_parent),
                        ).props("dense flat").mark("setup-teach-frame")
                        ui.button("Keep frame", on_click=set_frame).props("dense").mark(
                            "setup-set-frame"
                        )
                        ui.button(
                            icon="delete",
                            on_click=lambda: remove("frames", frame_name.value),
                        ).props("dense flat").tooltip("Remove frame").mark(
                            "setup-remove-frame"
                        )
                with ui.tab_panel(poses_tab).classes("p-0"):
                    pose_existing = (
                        ui.select(
                            [],
                            label="Edit pose",
                            on_change=lambda e: select_pose(e.value),
                        )
                        .props("dense")
                        .classes("w-full")
                        .mark("setup-pose-existing")
                    )
                    with ui.row().classes("w-full"):
                        pose_name = (
                            ui.input("Pose name", value="pick")
                            .props("dense")
                            .classes("flex-1 min-w-0")
                            .mark("setup-pose-name")
                        )
                        pose_frame = (
                            ui.select(["WRF"], value="WRF", label="Frame")
                            .props("dense")
                            .classes("w-36")
                            .mark("setup-pose-frame")
                        )
                    pose_values = coordinates("setup-pose")
                    with ui.row():
                        ui.button(
                            "Use current TCP",
                            on_click=lambda: teach(pose_values, pose_frame),
                        ).props("dense flat").mark("setup-teach-pose")
                        ui.button("Keep pose", on_click=set_pose).props("dense").mark(
                            "setup-set-pose"
                        )
                        ui.button(
                            icon="delete",
                            on_click=lambda: remove("poses", pose_name.value),
                        ).props("dense flat").tooltip("Remove pose")
                with ui.tab_panel(params_tab).classes("p-0"):
                    parameter_existing = (
                        ui.select(
                            [],
                            label="Edit parameter",
                            on_change=lambda e: select_parameter(e.value),
                        )
                        .props("dense")
                        .classes("w-full")
                    )
                    parameter_name = (
                        ui.input("Parameter name", value="clearance")
                        .props("dense")
                        .mark("setup-parameter-name")
                    )
                    parameter_type = (
                        ui.select(
                            ["number", "integer", "text", "boolean"],
                            value="number",
                            label="Type",
                        )
                        .props("dense")
                        .mark("setup-parameter-type")
                    )
                    with ui.row():
                        parameter_value = (
                            ui.input("Value", value="30")
                            .props("dense")
                            .mark("setup-parameter-value")
                        )
                        parameter_unit = (
                            ui.input("Unit", value="mm")
                            .props("dense")
                            .mark("setup-parameter-unit")
                        )
                    with ui.row():
                        ui.button("Keep parameter", on_click=set_parameter).props(
                            "dense"
                        ).mark("setup-set-parameter")
                        ui.button(
                            icon="delete",
                            on_click=lambda: remove("parameters", parameter_name.value),
                        ).props("dense flat").tooltip("Remove parameter")

                with ui.tab_panel(tcp_tab).classes("p-0"):
                    tcp_editor = TcpCalibrationEditor(
                        commander,
                        lambda: snapshot,
                        set_snapshot,
                        lambda: pending_snapshot(["frames"]),
                        update_dirty,
                    )

            @ui.refreshable
            def summary() -> None:
                rows = []
                for name, pose in snapshot.poses.items():
                    world = snapshot.resolve(pose)
                    rows.append(
                        {
                            "name": name,
                            "frame": pose.frame,
                            "world": ", ".join(f"{v:.2f}" for v in world.values),
                        }
                    )
                if rows:
                    ui.table(
                        columns=[
                            {"name": key, "label": label, "field": key, "align": "left"}
                            for key, label in (
                                ("name", "Pose"),
                                ("frame", "Frame"),
                                ("world", "WRF · mm / degrees"),
                            )
                        ],
                        rows=rows,
                        row_key="name",
                    ).props("dense flat").classes("w-full").mark("setup-resolved-poses")

            with ui.expansion("Resolved poses", icon="table_chart").classes(
                "w-full shrink-0"
            ):
                summary()

            fields.update(
                {
                    "frames": [frame_name, frame_parent, *frame_values],
                    "poses": [pose_name, pose_frame, *pose_values],
                    "parameters": [
                        parameter_name,
                        parameter_type,
                        parameter_value,
                        parameter_unit,
                    ],
                    "tcp": [tcp_editor.name, *tcp_editor.coordinates],
                }
            )
            initial_values.update(
                {
                    kind: tuple(field.value for field in widgets)
                    for kind, widgets in fields.items()
                }
            )
            remember()
            for widgets in fields.values():
                for field in widgets:
                    field.on_value_change(update_dirty)
            tcp_editor.existing.on_value_change(lambda e: select_tcp(e.value))
