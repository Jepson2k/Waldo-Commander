"""Installed Python skill discovery, explicit call insertion and one-shot runs."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
import time
from typing import Any, ClassVar, Literal, get_args, get_origin, get_type_hints

from nicegui import background_tasks, run, ui
from waldoctl import Commander, Panel, PanelSlot
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal
from waldoctl.tools import ToolStatus

from waldo_commander.services.skill_library import (
    SkillEntry,
    call_source,
    library,
    plan_preview,
)
from waldo_commander.setup import SetupStore

logger = logging.getLogger(__name__)

# Grouped by what the arm does rather than by module, in the order an
# operator reaches for them. A skill WC does not know is shown under Plugins.
SKILL_TILES: dict[str, tuple[str, str]] = {
    "waldo.retract": ("Move", "retract"),
    "waldo.approach": ("Move", "approach"),
    "waldo.park": ("Move", "park"),
    "waldo.align_tool_axis": ("Move", "align_tool_axis"),
    "waldo.gripper_open": ("Hold", "gripper_open"),
    "waldo.gripper_close": ("Hold", "gripper_close"),
    "waldo.read_signal": ("Carry and sense", "read_signal"),
    "waldo.wait_signal": ("Carry and sense", "wait_signal"),
    "waldo.write_signal": ("Carry and sense", "write_signal"),
}
_PLUGINS = "Plugins"


def _label(name: str) -> str:
    return name.rsplit(".", 1)[-1].replace("_", " ").capitalize()


def _skill_labels(ids) -> dict[str, str]:
    """Option labels for skill ids, qualified only where they would collide.

    Two plugins can each provide a `retract`; shown by trailing name alone they
    are two identical entries and the user cannot tell which one they are about
    to insert.
    """
    labels: dict[str, str] = {}
    seen: dict[str, list[str]] = {}
    for key in ids:
        seen.setdefault(_label(key), []).append(key)
    for label, keys in seen.items():
        for key in keys:
            namespace = key.rsplit(".", 1)[0] if "." in key else ""
            labels[key] = (
                f"{label} ({namespace})" if len(keys) > 1 and namespace else label
            )
    return labels


def _tile_groups(ids) -> list[tuple[str, list[str]]]:
    order = list(SKILL_TILES)
    groups: dict[str, list[str]] = {}
    for group, _ in SKILL_TILES.values():
        groups.setdefault(group, [])
    groups[_PLUGINS] = []
    for key in ids:
        groups[SKILL_TILES.get(key, (_PLUGINS, ""))[0]].append(key)
    for group, keys in groups.items():
        keys.sort(key=lambda k: order.index(k) if k in order else len(order))
    return [(group, keys) for group, keys in groups.items() if keys]


def _tile_icon(key: str) -> str:
    known = SKILL_TILES.get(key)
    return f"img:/static/icons/skills/{known[1]}.svg" if known else "extension"


def _summary(entry: SkillEntry) -> str:
    return entry.description.split("\n\n", 1)[0].replace("\n", " ")


def _loaded(store, name: str | None) -> SetupSnapshot:
    """The named setup, or a request to save one.

    With no setup saved there is no name to load, and the store's name-format
    complaint tells the user nothing about what to do next.
    """
    if not name:
        raise ValueError("Save a setup in the Setup panel to fill this field")
    return store.load(name)


def _pose(snapshot: SetupSnapshot, name: str | None):
    """A pose from the setup, or a request to teach one."""
    if not name:
        raise ValueError("This setup has no poses; teach one in the Setup panel")
    return snapshot.resolve(name)


def _selected_tool_preamble(tool: ToolStatus) -> str:
    """Bind the tool the arm carries in the program the panel runs.

    That program opens a fresh client, and a client knows no tool until it
    selects one — so a skill that reads ``rbt.tool`` would refuse even with
    the right gripper fitted. Selecting the arm's current tool first gives
    the run the same view as the operator's session; a skill that needs no
    tool is unaffected.
    """
    if tool.key in ("", "NONE"):
        return ""
    return (
        f"if rbt.select_tool({tool.key!r}, variant_key={tool.variant_key!r}) < 0:\n"
        "    raise RuntimeError('Tool selection was refused')\n"
    )


class SkillLibraryPanel(Panel):
    id: ClassVar[str] = "skills"
    display_name: ClassVar[str] = "Skills"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_TOP_TAB
    tab_icon: ClassVar[str] = "extension"
    tab_tooltip: ClassVar[str] = "Reusable Python skills"
    order: ClassVar[int] = 25
    default_width: ClassVar[int] = 460
    default_height: ClassVar[int] = 580
    min_width: ClassVar[int] = 380
    min_height: ClassVar[int] = 380
    resizable: ClassVar[bool] = True

    def build(self, commander: Commander) -> None:
        entries, diagnostics = library(commander.robot)
        labels = _skill_labels(entries)
        readers: dict[str, Any] = {}
        tiles: dict[str, ui.button] = {}
        running = False
        selected: str | None = None
        preview_generation = 0

        def field_label(name: str) -> str:
            for suffix, unit in (("_mm", "mm"), ("_deg", "°"), ("_s", "s")):
                if name.endswith(suffix):
                    return f"{_label(name.removesuffix(suffix))} ({unit})"
            if entry().skill.spec.id.startswith("waldo."):
                return {
                    "timeout": "Timeout (s)",
                    "speed": "Speed (0–1)",
                    "duration": "Duration (s)",
                }.get(name, _label(name))
            return _label(name)

        def entry() -> SkillEntry:
            assert selected is not None
            return entries[selected]

        async def preview(
            key: str, arguments: dict[str, Any], *, explain: bool = False
        ) -> None:
            """Draw what *key* would do from here, once the pointer has settled.

            With *explain*, a call the planner refuses says why: the same
            refusal awaits it on the robot.
            """
            nonlocal preview_generation
            preview_generation += 1
            generation = preview_generation
            await asyncio.sleep(0.25)
            from waldo_commander.state import ui_state

            scene = ui_state.urdf_scene
            if generation != preview_generation or scene is None:
                return
            tool = commander.status.tool
            try:
                planned = await run.io_bound(
                    plan_preview,
                    entries[key],
                    arguments,
                    commander.robot,
                    list(commander.status.joints.angles.rad),
                    (tool.key, tool.variant_key),
                )
            except Exception as error:
                # A skill with nothing to draw is the ordinary case on hover,
                # not a fault: most need arguments before they have a motion.
                logger.debug("No preview for %s: %s", key, error)
                if explain and generation == preview_generation:
                    preview_note.set_text(
                        f"Cannot plan this from the current pose: {error}"
                    )
                return
            if planned is not None and generation == preview_generation:
                preview_note.set_text("")
                scene.show_skill_preview(*planned)

        def clear_preview() -> None:
            nonlocal preview_generation
            preview_generation += 1
            from waldo_commander.state import ui_state

            if ui_state.urdf_scene is not None:
                ui_state.urdf_scene.clear_skill_preview()

        def source(*, synchronous: bool = False) -> str:
            arguments = {name: read() for name, read in readers.items()}
            return call_source(
                entry(), arguments, async_call=asynchronous.value and not synchronous
            )

        def refresh_source() -> None:
            try:
                code.content = source()
                message.set_text(entry().unavailable)
                arguments = {name: read() for name, read in readers.items()}
            except (ValueError, TypeError, KeyError, OSError, SyntaxError) as error:
                code.content = ""
                message.set_text(str(error))
                preview_note.set_text("")
                clear_preview()
                return
            assert selected is not None
            background_tasks.create(preview(selected, arguments, explain=True))

        def insert() -> None:
            from waldo_commander.services.motion_recorder import motion_recorder
            from waldo_commander.services.programs import is_any_program_running

            if is_any_program_running():
                message.set_text("Stop the running program before inserting code")
                return
            if commander.programs.active is None:
                message.set_text("Open a program before inserting a call")
                return
            try:
                snippet = source()
            except (ValueError, TypeError, KeyError, OSError, SyntaxError) as error:
                message.set_text(str(error))
                return
            motion_recorder.insert_skill_call(snippet)
            clear_preview()
            message.set_text("Inserted Python skill call")

        async def run_once() -> None:
            nonlocal running
            from waldo_commander.components.script_execution import script_exec
            from waldo_commander.services.control_lease import require_browser_control
            from waldo_commander.services.motion_recorder import motion_recorder
            from waldo_commander.services.programs import is_any_program_running
            from waldo_commander.state import ui_state

            if running or is_any_program_running():
                message.set_text("Wait for the running program to finish")
                return
            if not require_browser_control(ui_state.active_client_id):
                message.set_text("Take browser control before running a skill")
                return
            if not (commander.status.connected or commander.status.simulator_active):
                message.set_text("Connect the robot or enable the simulator first")
                return
            try:
                snippet = source(synchronous=True)
            except (ValueError, TypeError, KeyError, OSError, SyntaxError) as error:
                message.set_text(str(error))
                return
            running = True
            run_button.disable()
            clear_preview()
            try:
                recording = next(
                    (p for p in commander.programs.items if p.recording.is_recording),
                    None,
                )
                text = (
                    f"from {commander.robot.backend_package} import RobotClient\n\nwith RobotClient() as rbt:\n"
                    + textwrap.indent(
                        _selected_tool_preamble(commander.status.tool) + snippet, "    "
                    )
                    + "\n"
                )
                program = commander.programs.new(
                    source=text, filename=f"{entry().skill.spec.id}.py"
                )
                commander.programs.switch(program.id)
                if ui_state._program_tab is not None:
                    ui_state._program_tab.parent_slot.parent.set_value("program")
                started_at = time.time()

                def still_recording() -> bool:
                    return (
                        recording is not None
                        and recording in commander.programs.items
                        and recording.recording.is_recording
                    )

                completed = False
                try:
                    await script_exec.start()
                    handle = script_exec.script_handle
                    if handle is None or not script_exec.is_launching_tab(program.id):
                        message.set_text("Skill could not start; see the program log")
                        return
                    message.set_text(
                        "Running in the program editor; use its pause/stop controls"
                    )
                    while program.execution.is_running:
                        await asyncio.sleep(0.1)
                    completed = (
                        handle["proc"].returncode == 0
                        and script_exec.last_exit_code == 0
                    )
                finally:
                    # The recorder writes into the active program's textarea and
                    # the editor blocks tab switches while recording, so the
                    # recording program must be active again however the run
                    # ended.
                    if still_recording():
                        assert recording is not None
                        commander.programs.switch(recording.id)
                if not completed:
                    message.set_text("Skill did not complete; see the program log")
                    return
                if still_recording():
                    motion_recorder.record_completed_skill(
                        snippet, started_at=started_at
                    )
                message.set_text("Skill completed")
            finally:
                running = False
                run_button.set_enabled(selected is not None and not entry().unavailable)

        def select(key: str) -> None:
            nonlocal selected
            selected = key
            for other, tile in tiles.items():
                if other == key:
                    tile.classes(add="skill-tile-selected")
                else:
                    tile.classes(remove="skill-tile-selected")
            grid_view.set_visibility(False)
            detail_view.set_visibility(True)
            rebuild()

        def show_grid() -> None:
            clear_preview()
            detail_view.set_visibility(False)
            grid_view.set_visibility(True)

        def end_hover() -> None:
            # Clicking a tile hides the grid under the pointer, and the leave
            # that follows must not take away the path the detail view drew.
            if not detail_view.visible:
                clear_preview()

        def make_tile(key: str) -> ui.button:
            candidate = entries[key]
            tile = (
                ui.button(
                    labels[key],
                    icon=_tile_icon(key),
                    color=None,
                    on_click=lambda: select(key),
                )
                .props("flat no-caps stack")
                .classes("skill-tile")
                .mark(f"skill-tile-{key}")
            )
            tile.tooltip(candidate.unavailable or _summary(candidate))
            if candidate.unavailable:
                tile.classes("skill-tile-unavailable")
            tile.on("mouseenter", lambda: preview(key, {}))
            tile.on("mouseleave", end_hover)
            return tile

        with ui.column().classes("w-full h-full min-h-0 flex-nowrap gap-2"):
            with (
                ui.column()
                .classes(
                    "w-full flex-1 min-h-0 overflow-y-auto overflow-x-hidden flex-nowrap gap-2"
                )
                .mark("skill-grid") as grid_view
            ):
                ui.label("Skills").classes("panel-heading")
                for diagnostic in diagnostics:
                    ui.label(diagnostic).classes("text-warning text-caption").mark(
                        "skill-diagnostic"
                    )
                if not entries:
                    ui.label("No compatible skill plugins are installed").classes(
                        "panel-note"
                    )
                else:
                    ui.label("Point at a skill to see its motion.").classes(
                        "panel-note"
                    )
                for group, keys in _tile_groups(entries):
                    ui.label(group).classes("skill-group-heading")
                    with ui.element("div").classes("skill-grid"):
                        for key in keys:
                            tiles[key] = make_tile(key)
            with (
                ui.column()
                .classes("w-full flex-1 min-h-0 flex-nowrap gap-2")
                .mark("skill-detail") as detail_view
            ):
                with ui.row().classes("w-full items-center no-wrap gap-1"):
                    ui.button(icon="arrow_back", on_click=show_grid).props(
                        "flat dense round"
                    ).tooltip("All skills").mark("skill-back")
                    detail_icon = ui.icon("extension").classes("skill-detail-icon")
                    title = ui.label().classes("panel-heading").mark("skill-title")
                # Sized to its content so the actions follow a short form, and
                # shrinking to scroll so they stay in view under a long one.
                with ui.column().classes(
                    "skill-library-form-scroll w-full flex-initial min-h-0 overflow-y-auto overflow-x-hidden flex-nowrap gap-2"
                ):
                    description = ui.label().classes("text-caption whitespace-pre-line")
                    message = ui.label().classes("text-caption").mark("skill-message")
                    preview_note = (
                        ui.label().classes("panel-note").mark("skill-preview-note")
                    )
                    form = ui.element("div").classes(
                        "w-full shrink-0 grid grid-cols-2 gap-x-3 gap-y-2"
                    )
                    with (
                        ui.expansion("Python call", icon="code")
                        .classes("w-full")
                        .mark("skill-python-details")
                    ):
                        asynchronous = ui.checkbox(
                            "Insert async call", value=False, on_change=refresh_source
                        ).mark("skill-async")
                        code = (
                            ui.code("", language="python")
                            .classes("w-full shrink-0 overflow-x-auto")
                            .mark("skill-call-preview")
                        )
                        api_details = ui.label().classes("panel-note")
                with ui.row().classes("panel-actions"):
                    insert_button = (
                        ui.button("Insert call", on_click=insert)
                        .props("dense")
                        .mark("skill-insert")
                    )
                    run_button = (
                        ui.button("Run once", on_click=run_once)
                        .props("dense flat")
                        .mark("skill-run")
                    )
                ui.label(
                    "Run once opens the Program tab with pause and stop controls."
                ).classes("text-caption")

        def rebuild() -> None:
            form.clear()
            readers.clear()
            preview_note.set_text("")
            candidate = entry()
            title.set_text(labels[candidate.skill.spec.id])
            detail_icon.set_name(_tile_icon(candidate.skill.spec.id))
            description.set_text(_summary(candidate))
            api_details.set_text(
                f"{candidate.skill.spec.id} · v{candidate.skill.spec.version} · API {candidate.skill.spec.api_version}"
            )
            try:
                annotations = get_type_hints(candidate.skill.function)
            except Exception as error:
                message.set_text(
                    f"Cannot read skill annotations: {error}. Use the callable directly in Python."
                )
                insert_button.disable()
                run_button.disable()
                return
            insert_button.set_enabled(not candidate.unavailable)
            run_button.set_enabled(not candidate.unavailable and not running)
            with form:
                store = SetupStore()
                names = store.names()
                needs_setup = any(
                    t in (Pose, SetupSnapshot, DigitalSignal)
                    for t in annotations.values()
                )
                shared_setup = (
                    ui.select(names, label="Setup", value=names[0] if names else None)
                    .props("dense")
                    .classes("w-full")
                    .mark("skill-shared-setup")
                )
                shared_setup.classes("col-span-2")
                shared_setup.set_visibility(needs_setup)
                with ui.expansion("Setup overrides", icon="tune").classes(
                    "w-full"
                ) as overrides:
                    ui.label("Use a different setup for individual arguments.").classes(
                        "panel-note"
                    )
                    override_fields = ui.column().classes("w-full gap-2")
                overrides.classes("col-span-2")
                overrides.set_visibility(
                    sum(
                        t in (Pose, SetupSnapshot, DigitalSignal)
                        for t in annotations.values()
                    )
                    > 1
                )
                for name, parameter in candidate.parameters.items():
                    annotation = annotations.get(name, parameter.annotation)
                    default = (
                        parameter.default
                        if parameter.default is not inspect.Parameter.empty
                        else None
                    )
                    if annotation in (
                        Pose,
                        SetupSnapshot,
                        DigitalSignal,
                    ):
                        with override_fields:
                            setup = (
                                ui.select(
                                    {"": "Use shared setup", **{n: n for n in names}},
                                    label=_label(name),
                                    value="",
                                )
                                .props("dense")
                                .classes("w-full")
                                .mark(f"skill-{name}-setup")
                            )
                        if annotation is SetupSnapshot:
                            readers[name] = lambda widget=setup, selected_store=store: (
                                _loaded(
                                    selected_store, widget.value or shared_setup.value
                                )
                            )
                            setup.on_value_change(refresh_source)
                            shared_setup.on_value_change(refresh_source)
                        else:
                            resource = {
                                Pose: "pose",
                                DigitalSignal: "signal",
                            }[annotation]
                            pose = (
                                ui.select([], label=_label(name))
                                .props("dense")
                                .classes("w-full")
                                .mark(f"skill-{name}-{resource}")
                            )

                            def set_poses(
                                setup_widget=setup,
                                pose_widget=pose,
                                selected_store=store,
                                kind=annotation,
                            ):
                                try:
                                    snapshot = _loaded(
                                        selected_store,
                                        setup_widget.value or shared_setup.value,
                                    )
                                    options = list(
                                        snapshot.poses
                                        if kind is Pose
                                        else snapshot.signals
                                    )
                                    pose_widget.set_options(
                                        options, value=options[0] if options else None
                                    )
                                except (OSError, ValueError) as error:
                                    message.set_text(str(error))
                                refresh_source()

                            setup.on_value_change(lambda _, update=set_poses: update())
                            shared_setup.on_value_change(
                                lambda _, update=set_poses: update()
                            )
                            pose.on_value_change(refresh_source)
                            readers[name] = (
                                lambda s=setup,
                                p=pose,
                                selected_store=store,
                                kind=annotation: (
                                    _pose(
                                        _loaded(
                                            selected_store,
                                            s.value or shared_setup.value,
                                        ),
                                        p.value,
                                    )
                                    if kind is Pose
                                    else _loaded(
                                        selected_store,
                                        s.value or shared_setup.value,
                                    ).signals[p.value]
                                )
                            )
                            set_poses()
                    elif annotation is bool or isinstance(default, bool):
                        checkbox = ui.checkbox(
                            _label(name), value=bool(default), on_change=refresh_source
                        ).mark(f"skill-arg-{name}")
                        readers[name] = lambda widget=checkbox: widget.value
                    elif annotation in (int, float) or type(default) in (int, float):
                        number = (
                            ui.number(
                                field_label(name),
                                value=default,
                                on_change=refresh_source,
                            )
                            .props("dense")
                            .classes("w-full")
                            .mark(f"skill-arg-{name}")
                        )

                        def read_number(widget=number, kind=annotation):
                            value = widget.value
                            if value is None:
                                raise ValueError("Fill all required numeric arguments")
                            if kind is int and int(value) != value:
                                raise ValueError("Expected a whole number")
                            return int(value) if kind is int else float(value)

                        readers[name] = read_number
                    elif get_origin(annotation) is Literal:
                        select = (
                            ui.select(
                                list(get_args(annotation)),
                                value=default,
                                label=_label(name),
                                on_change=refresh_source,
                            )
                            .props("dense")
                            .classes("w-full")
                            .mark(f"skill-arg-{name}")
                        )
                        readers[name] = lambda widget=select: widget.value
                    else:
                        literal = annotation is not str and not isinstance(default, str)
                        field = (
                            ui.input(
                                _label(name) + (" (Python literal)" if literal else ""),
                                value=repr(default) if literal else default or "",
                                on_change=refresh_source,
                            )
                            .props("dense")
                            .classes("w-full")
                            .mark(f"skill-arg-{name}")
                        )
                        readers[name] = lambda widget=field, parse=literal: (
                            ast.literal_eval(widget.value) if parse else widget.value
                        )
            refresh_source()

        detail_view.set_visibility(False)
