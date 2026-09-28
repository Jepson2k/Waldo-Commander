"""Skills in the editor: a call inserted with its arguments as fields, and a
strip above the code for the call under the cursor."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import re
from pathlib import Path
from typing import Any, cast, get_type_hints

import waldoctl
from nicegui import background_tasks, ui
from waldoctl import Commander, Program
from waldoctl.camera import CameraCalibration
from waldoctl.setup import Frame, Pose, PoseValues, SetupSnapshot, validate_name
from waldoctl.signals import DigitalSignal
from waldoctl.tools import ToolStatus

from waldo_commander.camera_sources import FrameSource
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.services.python_source import (
    in_async_scope,
    missing_statements,
    program_setup,
)
from waldo_commander.services.skill_library import (
    SkillCall,
    SkillEntry,
    alias,
    arguments_from_call,
    call_template,
    field_at,
    library,
    parse_skill_call,
    plan_preview,
    replace_argument,
    setup_reference,
    skill_prelude,
)
from waldo_commander.setup import DEFAULT_SETUP_NAME, SetupStore, last_saved_name
from waldo_commander.state import ui_state

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
    "waldo.attach_object": "attach_object",
    "waldo.detach_object": "detach_object",
    "waldo.transfer": "transfer",
    "waldo.transfer_with_signal": "transfer_with_signal",
    "waldo.locate_board": "locate_board",
}
# Skills that edit the world rather than move the arm: nothing to draw, and
# the backend's dry run keeps shapes process-wide, so planning them in this
# process would leak into every later preview.
WORLD_SKILLS = frozenset({"waldo.attach_object", "waldo.detach_object"})

#: The setup section a field of each type names its entry in.
_SECTIONS: dict[Any, str] = {
    Pose: "poses",
    DigitalSignal: "signals",
    CameraCalibration: "cameras",
}
_KINDS: dict[Any, str] = {
    Pose: "pose",
    SetupSnapshot: "the program's setup",
    DigitalSignal: "signal",
    CameraCalibration: "camera calibration",
    FrameSource: "camera source",
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


def field_label(entry: SkillEntry, name: str) -> tuple[str, str]:
    """A field's label and unit, read from its parameter name."""
    for suffix, unit in (("_mm", "mm"), ("_deg", "°"), ("_s", "s")):
        if name.endswith(suffix):
            return _label(name.removesuffix(suffix)), unit
    if entry.skill.spec.id.startswith("waldo."):
        unit = {"timeout": "s", "speed": "0–1", "duration": "s"}.get(name, "")
        return _label(name), unit
    return _label(name), ""


def _reference(kind: Any, variable: str, name: str) -> str:
    if kind is Pose:
        return f'{variable}.resolve("{name}")'
    return f'{variable}.{_SECTIONS[kind]}["{name}"]'


def _default_setup(store: SetupStore) -> str:
    """The setup a program without one starts from: the one saved last, else
    the first saved, else the Setup panel's default name."""
    names = store.names()
    saved = last_saved_name()
    if saved in names:
        return saved
    return names[0] if names else DEFAULT_SETUP_NAME


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


class SkillInserter:
    """Puts a skill call at the editor's cursor with its arguments as fields.

    Setup arguments refer to the program's setup by name, so the call follows
    the setup as it is taught again; the imports and setup load the call
    needs go at the top of the program, once. The fields are the form: Tab
    moves between them, and the strip above the code helps fill the one the
    cursor is in.
    """

    def insert(self, commander: Commander, key: str) -> None:
        program = commander.programs.active
        textarea = ui_state.active_textarea
        if program is None or textarea is None:
            return
        strip = ui_state.editor_panel.skill_strip(program.id)
        try:
            placed = self._write(commander, key, textarea)
        except ValueError as error:
            if strip is not None:
                strip.show_message(str(error))
            return
        if placed is not None and strip is not None:
            strip.on_cursor(*placed)

    @staticmethod
    def _write(commander: Commander, key: str, textarea) -> tuple[int, int] | None:
        """Write the call and its prelude; returns the cursor's ``(line, column)``."""
        if is_any_program_running():
            raise ValueError("Stop the running program before inserting a skill")
        entries, _ = library(commander.robot)
        entry = entries[key]
        if entry.unavailable:
            raise ValueError(entry.unavailable)
        try:
            annotations = get_type_hints(entry.skill.function)
        except Exception as error:  # a plugin's annotations run its code
            raise ValueError(
                f"Cannot read skill annotations: {error}. Use the callable directly in Python."
            ) from error
        found = program_setup(
            str(textarea.value or ""), motion_recorder.insertion_line()
        )
        store = SetupStore(found.directory if found else None)
        variable, name = (
            (found.variable, found.name)
            if found is not None
            else ("setup", _default_setup(store))
        )
        if found is None:
            # A dynamic or shadowed setup cannot be reused. Give the explicit
            # inserted load a fresh name so it cannot overwrite user code.
            used = {
                node.id
                for node in ast.walk(ast.parse(str(textarea.value or "")))
                if isinstance(node, ast.Name)
            }
            suffix = 0
            while variable in used:
                suffix += 1
                variable = f"skill_setup{suffix}"
        try:
            snapshot = store.read_literal(name)
        except (OSError, ValueError) as error:
            logger.debug("Skill fields start without setup %s: %s", name, error)
            snapshot = None
        prelude = missing_statements(
            str(textarea.value or ""),
            skill_prelude(entry, variable, None if found else name, annotations),
        )
        if prelude:
            motion_recorder.insert_prelude("\n".join(prelude))
        async_call = in_async_scope(
            str(textarea.value or ""), motion_recorder.insertion_line()
        )
        template, plain, fields = call_template(
            entry, snapshot, async_call, setup_variable=variable
        )
        first_line, count = motion_recorder.insert_skill_call(plain)
        if count != 1:
            return None
        lines = str(textarea.value or "").split("\n")
        text = lines[first_line - 1]
        indent = len(text) - len(text.lstrip())
        start = sum(len(line) + 1 for line in lines[: first_line - 1])
        textarea.insert_snippet(template, start + indent, start + len(text))
        call = parse_skill_call(text, entries)
        if call is None or not fields or fields[0] not in call.arguments:
            return first_line, len(text) + 1
        # CodeMirror selects the first field with its head at the field's end.
        return first_line, call.arguments[fields[0]][1] + 1


skill_inserter = SkillInserter()


class SkillStrip:
    """The skill call under a tab's cursor, shown above its code.

    It says what the skill does and which field the cursor is in, draws the
    call's path from where the arm is now, and for a field taken from the
    setup lists the setup's entries of that kind; a pose can be taught there
    and then. Hidden and emptied when the cursor is on no skill call.
    """

    def __init__(self, tab: Program, textarea: ui.codemirror) -> None:
        self.tab = tab
        self.textarea = textarea
        self.line = 0
        self.call: SkillCall | None = None
        self._text = ""
        self._field: str | None = None
        self._variable = "setup"
        self._entries: dict[str, SkillEntry] | None = None
        self._hints: dict[str, dict[str, Any]] = {}
        self._note: ui.label | None = None
        self._note_text = ""
        self._names: ui.select | None = None
        self._teach_name: ui.input | None = None
        self._teach_frame: ui.select | None = None
        self._frame_name: ui.input | None = None
        self._chosen_frame = "WRF"
        self._preview_generation = 0
        self.row = ui.column().classes("skill-strip").mark("skill-strip")
        self.row.set_visibility(False)

    # ------------------------------------------------------------ following

    def on_cursor(self, line: int, column: int) -> None:
        """The cursor is at 1-indexed *line* and *column*."""
        text = self._line_text(line)
        call = parse_skill_call(text, self._skills()) if "_skill_" in text else None
        if call is None:
            self._clear_preview()
            if not self._mid_edit(line, text):
                self.hide()
            return
        self._show(line, text, call, field_at(call, column - 1))

    def on_source_changed(self) -> None:
        if self.call is None:
            return
        text = self._line_text(self.line)
        call = parse_skill_call(text, self._skills())
        if call is None:
            self._clear_preview()
            if not self._mid_edit(self.line, text):
                self.hide()
            return
        if text != self._text and is_any_program_recording():
            # Filling in a call is composing it, not time the program waits.
            motion_recorder.stamp_action_clock()
        field = self._field if self._field in call.arguments else None
        self._show(self.line, text, call, field)

    def _mid_edit(self, line: int, text: str) -> bool:
        """Whether the call shown is on *line* but does not parse while a field
        is being typed; the strip stays rather than flicker per keystroke."""
        return (
            self.call is not None
            and line == self.line
            and alias(self.call.entry) in text
        )

    def on_setup_saved(self, directory: Path, name: str, revision: str) -> None:
        found = program_setup(self.tab.source, self.line)
        if (
            self.call is None
            or found is None
            or found.name != name
            or SetupStore(found.directory).directory != directory
        ):
            return
        self._render()
        self._schedule_preview()

    def hide(self) -> None:
        """Hide and empty the strip, and take its path out of the scene."""
        if self.call is None and not self.row.visible:
            return
        shown = self.call is not None
        self.call, self._field, self.line, self._text = None, None, 0, ""
        self._note_text = ""
        self._note = self._names = self._teach_name = None
        self._teach_frame = self._frame_name = None
        self._preview_generation += 1
        self.row.clear()
        self.row.set_visibility(False)
        if shown and ui_state.urdf_scene is not None:
            ui_state.urdf_scene.clear_skill_preview()

    def show_message(self, text: str) -> None:
        """Say why a skill was not inserted, where its strip would be."""
        self.hide()
        with self.row:
            ui.label(text).classes("skill-strip-note").mark("skill-strip-message")
        self.row.set_visibility(True)

    # ------------------------------------------------------------ writing

    def write_field(self, text: str) -> None:
        """Write the field under the cursor as *text*.

        Only that field's text changes, so in the browser the edit falls
        inside the snippet field and Tab still moves on to the next one.
        """
        call, field = self.call, self._field
        if call is None or field is None:
            return
        lines = str(self.textarea.value or "").split("\n")
        if not 0 < self.line <= len(lines) or lines[self.line - 1] != self._text:
            return
        lines[self.line - 1] = replace_argument(self._text, call, field, text)
        self.textarea.value = "\n".join(lines)

    async def teach(self) -> None:
        """Save the arm's current pose to the program's setup and use it here."""
        if self._teach_name is None or self._teach_frame is None:
            return
        name = str(self._teach_name.value or "").strip()
        frame = self._teach_frame.value or "WRF"
        variable, setup_name, _, _ = self._setup()
        if setup_name is None:
            return
        binding = program_setup(self.tab.source, self.line)
        context = (self.line, self._text, self._field)
        try:
            validate_name(name)
            observed = await self._tcp()
            store = SetupStore(binding.directory if binding else None)
            snapshot = self._stored(store, setup_name)
            pose = snapshot.relative_pose(Pose(observed), frame)
            self._chosen_frame = frame
            store.save(setup_name, snapshot.with_pose(name, pose))
        except (OSError, ValueError, TimeoutError) as error:
            self._set_note(str(error))
            return
        if context == (self.line, self._line_text(self.line), self._field):
            self.write_field(_reference(Pose, variable, name))

    async def new_frame(self) -> None:
        """Save a frame at the arm's current pose, to teach poses in."""
        if self._frame_name is None:
            return
        name = str(self._frame_name.value or "").strip()
        _, setup_name, _, _ = self._setup()
        if setup_name is None:
            return
        binding = program_setup(self.tab.source, self.line)
        try:
            validate_name(name)
            observed = await self._tcp()
            store = SetupStore(binding.directory if binding else None)
            updated = self._stored(store, setup_name).with_frame(name, Frame(observed))
            self._chosen_frame = name
            store.save(setup_name, updated)
        except (OSError, ValueError, TimeoutError) as error:
            self._set_note(str(error))

    def _choose(self, name: str | None) -> None:
        call, field = self.call, self._field
        if name is None or call is None or field is None:
            return
        node = call.nodes.get(field)
        if node is not None and setup_reference(node, self._variable) == name:
            return
        kind = self._annotation(call, field)
        if kind in _SECTIONS:
            self.write_field(_reference(kind, self._variable, name))

    # ------------------------------------------------------------ drawing

    def _show(self, line: int, text: str, call: SkillCall, field: str | None) -> None:
        fresh = self.call is None or (line, field) != (self.line, self._field)
        edited = fresh or text != self._text
        self.line, self._text, self.call, self._field = line, text, call, field
        if fresh:
            self._render()
        elif self._names is not None and field is not None and field in call.nodes:
            reference = setup_reference(call.nodes[field], self._variable)
            options = self._names.options
            self._names.set_value(reference if reference in options else None)
        if edited:
            self._schedule_preview()

    def _render(self) -> None:
        call = self.call
        if call is None:
            return
        self._names = self._teach_name = self._teach_frame = self._frame_name = None
        self.row.clear()
        with self.row:
            with ui.row().classes("skill-strip-head"):
                ui.icon(skill_icon(call.key)).classes("skill-strip-icon")
                ui.label(_skill_labels(self._skills())[call.key]).classes(
                    "skill-strip-title"
                ).mark("skill-strip-title")
                ui.label(_summary(call.entry)).classes("skill-strip-summary")
            if self._field is not None:
                self._build_field(call, self._field)
            self._note = (
                ui.label(self._note_text)
                .classes("skill-strip-note")
                .mark("skill-strip-note")
            )
            self._note.set_visibility(bool(self._note_text))
        self.row.set_visibility(True)

    def _build_field(self, call: SkillCall, name: str) -> None:
        kind = self._annotation(call, name)
        label, unit = field_label(call.entry, name)
        described = _KINDS.get(kind) or (
            ""
            if kind is inspect.Parameter.empty
            else re.sub(
                r"(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)",
                r"\1",
                inspect.formatannotation(kind),
            )
        )
        with ui.row().classes("skill-strip-field"):
            ui.label(label).classes("skill-strip-field-label").mark("skill-strip-field")
            ui.label(" · ".join(part for part in (unit, described) if part)).classes(
                "skill-strip-kind"
            )
            if kind not in _SECTIONS:
                return
            variable, setup_name, snapshot, problem = self._setup()
            self._variable = variable
            if setup_name is None or problem:
                ui.label(
                    problem or "The program loads no setup to choose from"
                ).classes("skill-strip-kind")
                return
            names = list(getattr(snapshot, _SECTIONS[kind])) if snapshot else []
            node = call.nodes.get(name)
            current = setup_reference(node, variable) if node is not None else None
            self._names = (
                ui.select(
                    names,
                    label=f"From {setup_name}",
                    value=current if current in names else None,
                    on_change=lambda e: self._choose(e.value),
                )
                .props("dense options-dense")
                .classes("skill-strip-names")
                .mark("skill-strip-names")
            )
            if kind is not Pose:
                return
            self._teach_name = (
                ui.input("Pose", value=current or name)
                .props("dense")
                .classes("skill-strip-input")
                .mark("skill-strip-teach-name")
            )
            frames = ["WRF", *(snapshot.frames if snapshot else ())]
            self._teach_frame = (
                ui.select(
                    frames,
                    label="In frame",
                    value=self._chosen_frame if self._chosen_frame in frames else "WRF",
                )
                .props("dense options-dense")
                .classes("skill-strip-input")
                .mark("skill-strip-teach-frame")
            )
            ui.button("Teach now", icon="my_location", on_click=self.teach).props(
                "dense no-caps color=wc-action text-color=wc-on-bright"
            ).tooltip(
                "Save where the arm is now under this name, and use it here"
            ).mark("skill-strip-teach")
            self._frame_name = (
                ui.input("New frame")
                .props("dense")
                .classes("skill-strip-input")
                .mark("skill-strip-new-frame-name")
            )
            ui.button(
                "New frame", icon="add_location_alt", on_click=self.new_frame
            ).props("dense flat no-caps color=wc-text").tooltip(
                "Save a frame where the arm is now"
            ).mark("skill-strip-new-frame")

    def _set_note(self, text: str) -> None:
        self._note_text = text
        if self._note is not None and not self._note.is_deleted:
            self._note.set_text(text)
            self._note.set_visibility(bool(text))

    # ------------------------------------------------------------ preview

    def _clear_preview(self) -> None:
        self._preview_generation += 1
        # A repaired call can be identical to the last valid text. Forget that
        # comparison so restoring it schedules a new path after invalidation.
        self._text = ""
        if ui_state.urdf_scene is not None:
            ui_state.urdf_scene.clear_skill_preview()

    def _schedule_preview(self) -> None:
        """Draw the call's path from where the arm is, once typing settles."""
        self._preview_generation += 1
        call = self.call
        if call is None:
            return
        if call.key in WORLD_SKILLS:
            self._set_note("")
            if ui_state.urdf_scene is not None:
                ui_state.urdf_scene.clear_skill_preview()
            return
        background_tasks.create(
            self._preview(call, self._preview_generation), name="skill preview"
        )

    async def _preview(self, call: SkillCall, generation: int) -> None:
        await asyncio.sleep(0.25)
        scene = ui_state.urdf_scene
        if (
            generation != self._preview_generation
            or scene is None
            or self.row.is_deleted
            or not ui_state.program_panel_visible
        ):
            return
        variable, _, snapshot, problem = self._setup()
        try:
            arguments = arguments_from_call(call, snapshot, variable)
        except (ValueError, TypeError, KeyError) as error:
            self._set_note(problem or str(error))
            scene.clear_skill_preview()
            return
        commander = waldoctl.commander
        tool = commander.status.tool
        try:
            planned = await plan_preview(
                call.entry,
                arguments,
                commander.robot,
                list(commander.status.joints.angles.rad),
                (tool.key, tool.variant_key),
            )
        except Exception as error:  # planning runs the skill's own code
            logger.debug("No preview for %s: %s", call.key, error)
            if generation == self._preview_generation:
                self._set_note(f"Cannot plan this from the current pose: {error}")
                scene.clear_skill_preview()
            return
        if (
            planned is not None
            and generation == self._preview_generation
            and ui_state.program_panel_visible
        ):
            self._set_note("")
            scene.show_skill_preview(*planned)

    # ------------------------------------------------------------ reading

    def _skills(self) -> dict[str, SkillEntry]:
        if self._entries is None:
            self._entries, _ = library(waldoctl.commander.robot)
        return self._entries

    def _annotation(self, call: SkillCall, name: str) -> Any:
        hints = self._hints.get(call.key)
        if hints is None:
            try:
                hints = get_type_hints(call.entry.skill.function)
            except Exception as error:  # a plugin's annotations run its code
                logger.debug("Annotations of %s unreadable: %s", call.key, error)
                hints = {}
            self._hints[call.key] = hints
        parameter = call.entry.parameters.get(name)
        if name in hints:
            return hints[name]
        return parameter.annotation if parameter else inspect.Parameter.empty

    def _line_text(self, line: int) -> str:
        lines = str(self.textarea.value or "").split("\n")
        return lines[line - 1] if 0 < line <= len(lines) else ""

    def _setup(self) -> tuple[str, str | None, SetupSnapshot | None, str]:
        """The program's setup: ``(variable, name, snapshot, problem)``.

        A setup not saved yet has no snapshot and no problem: teaching a pose
        creates it.
        """
        found = program_setup(self.tab.source, self.line)
        if found is None:
            return "setup", None, None, ""
        variable, name = found.variable, found.name
        try:
            return variable, name, SetupStore(found.directory).read_literal(name), ""
        except FileNotFoundError:
            return variable, name, None, ""
        except (OSError, ValueError) as error:
            return variable, name, None, str(error)

    @staticmethod
    def _stored(store: SetupStore, name: str) -> SetupSnapshot:
        try:
            return store.read_literal(name)
        except FileNotFoundError:
            return SetupSnapshot()

    @staticmethod
    async def _tcp() -> PoseValues:
        observed = await asyncio.wait_for(waldoctl.commander.client.pose(), timeout=2.0)
        if observed is None:
            raise ValueError("No fresh TCP pose is available")
        return cast(PoseValues, tuple(observed))
