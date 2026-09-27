"""A skill's typed form, opened from the editor's Insert menu, that inserts a fixed call."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
from typing import Any, Literal, get_args, get_origin, get_type_hints

from nicegui import background_tasks, ui
from waldoctl import Commander
from waldoctl.camera import CameraCalibration
from waldoctl.recordings import Demonstration
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal
from waldoctl.tools import ToolStatus

from waldo_commander.camera_sources import CommanderCameraSource, FrameSource
from waldo_commander.services.skill_library import (
    SkillEntry,
    call_source,
    library,
    plan_preview,
)
from waldo_commander.setup import SetupStore
from waldo_commander.vision import LocalizationLimits

logger = logging.getLogger(__name__)

# The skills the editor offers, in the order an operator reaches for them,
# with their diagrams. Gripper and signal skills are left out: the Gripper and
# I/O tabs do those live, and their one-line rbt commands are already in the
# menu. A skill from another package is offered after these.
SKILL_ICONS: dict[str, str] = {
    "waldo.approach": "approach",
    "waldo.retract": "retract",
    "waldo.park": "park",
    "waldo.align_tool_axis": "align_tool_axis",
    "waldo.transfer": "transfer",
    "waldo.transfer_with_signal": "transfer_with_signal",
    "waldo.locate_board": "locate_board",
}


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


def menu_skills(ids) -> list[str]:
    """The skills the editor offers, known ones first in their order."""
    known = [key for key in SKILL_ICONS if key in ids]
    others = sorted(key for key in ids if not key.startswith("waldo."))
    return known + others


def skill_icon(key: str) -> str:
    known = SKILL_ICONS.get(key)
    return f"img:/static/icons/skills/{known}.svg" if known else "extension"


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


def selected_tool_preamble(tool: ToolStatus) -> str:
    """Bind the tool the arm carries in a program run from the editor.

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


class SkillDialog:
    """A skill's typed form, where the side panels open.

    Seamless, so the path the form describes stays in sight and the scene,
    the readout and the E-stop stay usable while it is open. Insert puts a
    call with fixed arguments at the editor's cursor, or at the recording
    cursor while recording.
    """

    def open(self, commander: Commander, key: str) -> None:
        entries, _ = library(commander.robot)
        labels = _skill_labels(entries)
        readers: dict[str, Any] = {}
        selected: str | None = key
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
            if generation != preview_generation or scene is None or dialog.is_deleted:
                return
            tool = commander.status.tool
            try:
                planned = await plan_preview(
                    entries[key],
                    arguments,
                    commander.robot,
                    list(commander.status.joints.angles.rad),
                    (tool.key, tool.variant_key),
                )
            except Exception as error:
                logger.debug("No preview for %s: %s", key, error)
                if generation == preview_generation:
                    # The last path drawn belongs to arguments no longer in the form.
                    scene.clear_skill_preview()
                    if explain:
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
            dialog.close()

        with (
            ui.dialog()
            .props("seamless position=left")
            .classes("skill-dialog-host")
            .mark("skill-dialog") as dialog,
            ui.card().classes("task-dialog skill-dialog"),
        ):
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                detail_icon = ui.icon("extension").classes("skill-detail-icon")
                title = ui.label().classes("panel-heading").mark("skill-title")
                ui.space()
                ui.button(icon="close", on_click=dialog.close).props(
                    "flat dense round"
                ).tooltip("Close").mark("skill-close")
            # Sized to its content so Insert follows a short form, and
            # shrinking to scroll so it stays in view under a long one.
            with ui.column().classes(
                "panel-body skill-library-form-scroll flex-nowrap gap-2"
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
                    ui.expansion("Python call")
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
                    ui.button("Insert", on_click=insert)
                    .props("dense")
                    .mark("skill-insert")
                )

        def closed(event) -> None:
            if not event.value:
                clear_preview()
                dialog.delete()

        dialog.on_value_change(closed)

        def rebuild() -> None:
            form.clear()
            readers.clear()
            preview_note.set_text("")
            candidate = entry()
            title.set_text(labels[candidate.skill.spec.id])
            detail_icon.set_name(skill_icon(candidate.skill.spec.id))
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
                return
            insert_button.set_enabled(not candidate.unavailable)
            with form:
                store = SetupStore()
                names = store.names()
                needs_setup = any(
                    t in (Pose, SetupSnapshot, DigitalSignal, CameraCalibration)
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
                        t in (Pose, SetupSnapshot, DigitalSignal, CameraCalibration)
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
                    if annotation is FrameSource:
                        ui.label("Uses the active camera.").classes(
                            "text-caption"
                        ).mark("skill-camera-source")
                        readers[name] = CommanderCameraSource
                    elif annotation is Demonstration:
                        ui.label(
                            "Replays come from recording: press Record, move the arm by hand or from another client, and keep the captured lines Raw. In Python, pass load_demonstration(path)."
                        ).classes("text-caption")
                        insert_button.disable()
                        # Falls through to refresh_source: it is the only writer
                        # of the snippet and the message.
                        break
                    elif annotation == LocalizationLimits | None:
                        readers[name] = lambda: None
                        ui.label("Uses default detection limits.").classes(
                            "text-caption"
                        )
                    elif annotation in (
                        Pose,
                        SetupSnapshot,
                        DigitalSignal,
                        CameraCalibration,
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
                                CameraCalibration: "camera",
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
                                        else snapshot.cameras
                                        if kind is CameraCalibration
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
                                    ).cameras[p.value]
                                    if kind is CameraCalibration
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

        rebuild()
        dialog.open()


skill_dialog = SkillDialog()
