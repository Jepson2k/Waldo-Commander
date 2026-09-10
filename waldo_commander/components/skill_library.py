"""Installed Python skill discovery, explicit call insertion and one-shot runs."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import time
from typing import Any, ClassVar, Literal, get_args, get_origin, get_type_hints

from nicegui import ui
from waldoctl import Commander, Panel, PanelSlot
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.tools import ToolType

from waldo_commander.services.skill_library import SkillEntry, call_source, library
from waldo_commander.setup import SetupStore


def _label(name: str) -> str:
    return name.rsplit(".", 1)[-1].replace("_", " ").capitalize()


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
        entries, diagnostics = library(commander.client.skill_capabilities)
        readers: dict[str, Any] = {}
        running = False

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
            return entries[choice.value]

        def source(*, synchronous: bool = False) -> str:
            arguments = {name: read() for name, read in readers.items()}
            is_async = asynchronous.value and not synchronous
            snippet = call_source(
                entry(), arguments, async_call=asynchronous.value and not synchronous
            )
            if "tool.gripper" in entry().skill.spec.requires:
                tool_status = commander.status.tool
                tool = commander.robot.tools[tool_status.key]
                if tool.tool_type != ToolType.GRIPPER:
                    raise ValueError(
                        "Select a supported gripper before inserting or running this skill"
                    )
                snippet = (
                    f"_skill_tool_index = {'await ' if is_async else ''}rbt.select_tool({tool_status.key!r}, variant_key={tool_status.variant_key!r})\n"
                    f"if _skill_tool_index < 0 or not {'await ' if is_async else ''}rbt.wait_command(_skill_tool_index, timeout=10.0):\n"
                    "    raise RuntimeError('Tool selection was not confirmed')\n"
                    + snippet
                )
            return snippet

        def refresh_source() -> None:
            try:
                code.content = source()
                message.set_text(entry().unavailable)
            except (ValueError, TypeError, KeyError, OSError, SyntaxError) as error:
                code.content = ""
                message.set_text(str(error))

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
            try:
                recording = next(
                    (p for p in commander.programs.items if p.recording.is_recording),
                    None,
                )
                text = (
                    f"from {commander.robot.backend_package} import RobotClient\n\nwith RobotClient() as rbt:\n"
                    + textwrap.indent(snippet, "    ")
                    + "\n"
                )
                program = commander.programs.new(
                    source=text, filename=f"{entry().skill.spec.id}.py"
                )
                commander.programs.switch(program.id)
                if ui_state._program_tab is not None:
                    ui_state._program_tab.parent_slot.parent.set_value("program")
                started_at = time.time()
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
                if handle["proc"].returncode != 0 or script_exec.last_exit_code != 0:
                    message.set_text("Skill did not complete; see the program log")
                    return
                if (
                    recording is not None
                    and recording in commander.programs.items
                    and recording.recording.is_recording
                ):
                    commander.programs.switch(recording.id)
                    motion_recorder.record_completed_skill(
                        snippet, started_at=started_at
                    )
                message.set_text("Skill completed")
            finally:
                running = False
                run_button.set_enabled(bool(choice.value) and not entry().unavailable)

        with ui.column().classes("w-full h-full min-h-0 flex-nowrap gap-2"):
            ui.label("Skills").classes("panel-heading")
            choice = (
                ui.select(
                    {key: _label(key) for key in entries},
                    value=next(iter(entries), None),
                    label="Installed skill",
                )
                .props("dense")
                .classes("w-full")
                .mark("skill-choice")
            )
            with ui.column().classes(
                "skill-library-form-scroll w-full flex-1 min-h-0 overflow-y-auto overflow-x-hidden flex-nowrap gap-2"
            ):
                for diagnostic in diagnostics:
                    ui.label(diagnostic).classes("text-warning text-caption").mark(
                        "skill-diagnostic"
                    )
                description = ui.label().classes("text-caption whitespace-pre-line")
                message = ui.label().classes("text-caption").mark("skill-message")
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
            if not choice.value:
                description.set_text("No compatible skill plugins are installed")
                insert_button.disable()
                run_button.disable()
                return
            candidate = entry()
            description.set_text(
                candidate.description.split("\n\n", 1)[0].replace("\n", " ")
            )
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
                    t in (Pose, SetupSnapshot) for t in annotations.values()
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
                    sum(t in (Pose, SetupSnapshot) for t in annotations.values()) > 1
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
                                selected_store.load(widget.value or shared_setup.value)
                            )
                            setup.on_value_change(refresh_source)
                            shared_setup.on_value_change(refresh_source)
                        else:
                            resource = {
                                Pose: "pose",
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
                                    snapshot = selected_store.load(
                                        setup_widget.value or shared_setup.value
                                    )
                                    options = list(snapshot.poses)
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
                                    selected_store.load(
                                        s.value or shared_setup.value
                                    ).resolve(p.value)
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

        choice.on_value_change(rebuild)
        rebuild()
