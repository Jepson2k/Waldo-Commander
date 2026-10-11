"""The workcell library: save the editable objects under a name, load a saved
workcell in their place, or insert either into the program as code.

It opens in the scene panel, not a dialog: the objects being saved or loaded
and the code being changed stay in view and editable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from weakref import WeakKeyDictionary

import waldoctl
from nicegui import background_tasks, ui
from waldoctl.shapes import Shape, ShapeWorld

from waldo_commander.services import world_files
from waldo_commander.services.motion_recorder import (
    motion_recorder,
    shape_imports,
    shapes_to_code,
)
from waldo_commander.services.python_source import in_async_scope
from waldo_commander.services.scene_history import SceneHistory
from waldo_commander.state import ui_state

_TONES = {
    "muted": "text-wc-text-muted",
    "positive": "text-wc-positive",
    "warning": "text-wc-warning",
    "error": "text-wc-error",
}

_libraries: WeakKeyDictionary[object, WorkcellLibrary] = WeakKeyDictionary()


def show_world_library(view) -> None:
    """Open the library in *view*'s scene panel, in place of one open there."""
    if (library := _libraries.get(view)) is not None:
        library.close()
    panel = view._scene_panel()
    if panel is None:
        return
    panel.open()
    _libraries[view] = WorkcellLibrary(view, view._library_host)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _editable(shapes: Sequence[Shape]) -> tuple[Shape, ...]:
    # A flange attachment is bound to this controller session's epoch, so it
    # is neither saved nor inserted, and a loaded workcell keeps what is held.
    return tuple(s for s in shapes if s.attachment is None)


def _scene_shapes() -> Sequence[Shape]:
    handle = waldoctl.commander.scene
    return handle.shapes if handle is not None else ()


def _say(
    label: ui.label, text: str, tone: str = "muted", size: str = "text-xs"
) -> None:
    label.set_text(text)
    # Replacing the classes would drop the one that hides an empty label.
    label.classes(
        replace=f"{size} break-words {_TONES[tone]}" + ("" if text else " hidden")
    )
    label.set_visibility(bool(text))


@dataclass(frozen=True)
class _Entry:
    """A saved workcell as read from disk, or why it cannot be loaded here."""

    name: str
    shapes: tuple[Shape, ...] = ()
    revision: str | None = None
    installation: int = 0
    problem: str = ""

    def summary(self) -> str:
        if self.problem:
            return self.problem
        names = [s.name for s in self.shapes]
        listed = ", ".join(names[:3]) + (
            f" and {len(names) - 3} more" if len(names) > 3 else ""
        )
        text = f"{_plural(len(names), 'object')}: {listed}" if names else "No objects"
        if self.installation:
            text += "; its installation shapes are not loaded"
        return text


def _read(name: str) -> _Entry:
    try:
        world, revision = world_files.read_literal_entry(name)
        shapes = _editable(world.program)
        SceneHistory.validate_names(shapes)
    except (OSError, ValueError, TypeError, KeyError) as error:
        return _Entry(name, problem=str(error))
    return _Entry(name, shapes, revision, len(world.installation))


def _read_all(names: list[str]) -> list[_Entry]:
    return [_read(name) for name in names]


@dataclass(frozen=True)
class _Row:
    caption: ui.label
    note: ui.label
    load: ui.button
    insert: ui.button


class WorkcellLibrary:
    """The library, at the top of the scene panel over the list of objects."""

    def __init__(self, view, host: ui.element) -> None:
        self.view = view
        self.history = view.object_history
        self.entries: dict[str, _Entry] = {}
        self.rows: dict[str, _Row] = {}
        # The saved workcell the Replace warning shown is for: (name, revision).
        self.reviewed: tuple[str, str] | None = None
        self.name_ok = False
        self.has_objects = False
        self.listing = 0
        self.loading = False
        self.closed = False
        with host:
            self.card = (
                ui.column().classes("scene-library w-full").mark("workcell-library")
            )
            self.card.on("keydown.escape", self.close)
            with self.card:
                self._build()
                ui.timer(0.5, self._follow)
        self._follow()
        background_tasks.create(self._list(), name="workcell library")

    def _build(self) -> None:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label("Workcells").classes("scene-section flex-1")
            ui.button(icon="close", on_click=self.close).props(
                "flat round dense size=sm color=wc-text"
            ).tooltip("Close · Esc").mark("workcell-close")
        with ui.row().classes("w-full items-center no-wrap gap-1"):
            self.name = (
                ui.input(
                    placeholder="Save the objects as…", on_change=self._name_changed
                )
                .props("dense outlined hide-bottom-space autofocus")
                .classes("flex-1 min-w-0")
                .mark("workcell-name")
            )
            self.name.on("keydown.enter", self._save)
            self.save = (
                ui.button("Save", on_click=self._save)
                .props("unelevated dense no-caps")
                .mark("workcell-save")
            )
            self._offer("Save", "wc-action")
            self.insert = (
                ui.button(icon="code", on_click=self._insert_current)
                .props("flat round dense color=wc-text")
                .tooltip("Insert the objects into the program, below its cursor")
                .mark("workcell-insert")
            )
        self.held = ui.label().mark("workcell-held")
        _say(self.held, "")
        self.feedback = ui.label().mark("workcell-feedback")
        _say(self.feedback, "")
        self.list = ui.column().classes("w-full gap-0").mark("workcell-saved")
        with self.list:
            ui.label("Reading the library…").classes("text-xs text-wc-text-muted")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if _libraries.get(self.view) is self:
            del _libraries[self.view]
        if not self.card.is_deleted:
            self.card.delete()

    def _follow(self) -> None:
        """Keep Save and Insert true to the scene, which stays editable."""
        shapes = _scene_shapes()
        editable = _editable(shapes)
        held = len(shapes) - len(editable)
        _say(
            self.held,
            f"{_plural(held, 'flange attachment')} left out of Save and Insert"
            if held
            else "",
        )
        self.has_objects = bool(editable)
        self._enable()

    def _enable(self) -> None:
        self.save.set_enabled(self.has_objects and self.name_ok)
        self.insert.set_enabled(self.has_objects)

    def _offer(self, action: str, color: str) -> None:
        """Label Save with what it will do: Save, or Replace a different save."""
        self.save.set_text(action)
        self.save.props(f"color={color} text-color=wc-on-bright")

    # ------------------------------------------------------------------ save

    def _unchanged(self, name: str, revision: str) -> bool:
        """Whether the saved *name* at *revision* already holds the objects."""
        entry = self.entries.get(name)
        return (
            entry is not None
            and entry.revision == revision
            and not entry.problem
            and entry.shapes == _editable(_scene_shapes())
        )

    def _name_changed(self) -> None:
        self.reviewed = None
        self.name_ok = False
        self._offer("Save", "wc-action")
        name = str(self.name.value or "").strip()
        try:
            revision = world_files.entry_revision(name) if name else None
        except (OSError, ValueError) as error:
            _say(self.feedback, str(error), "error", "text-sm")
        else:
            self.name_ok = bool(name)
            if revision is None or self._unchanged(name, revision):
                _say(self.feedback, "")
            else:
                self.reviewed = (name, revision)
                self._offer("Replace", "wc-warning-fill")
                _say(
                    self.feedback,
                    f"Replace overwrites the saved {name}.",
                    "warning",
                    "text-sm",
                )
        self._enable()

    def _save(self) -> None:
        name = str(self.name.value or "").strip()
        if not name:
            return
        try:
            handle = self.history.guard(self.history.context())
            shapes = _editable(handle.shapes)
            if not shapes:
                raise ValueError("Add an object to the scene before saving.")
            revision = world_files.entry_revision(name)
            if (
                revision is not None
                and self.reviewed != (name, revision)
                and not self._unchanged(name, revision)
            ):
                # Saved, or saved again, elsewhere since the name was typed:
                # say so before overwriting it.
                self._name_changed()
                return
            world_files.save_entry(name, ShapeWorld(program=shapes))
        except (OSError, ValueError) as error:
            _say(self.feedback, str(error), "error", "text-sm")
            return
        self.name.set_value("")
        _say(self.feedback, f"Saved {name}", "positive", "text-sm")
        background_tasks.create(self._list(), name="workcell library")

    # ---------------------------------------------------------------- insert

    def _insert(self, shapes: tuple[Shape, ...]) -> int:
        """Insert *shapes* as code at the program's cursor; the line it starts on."""
        editor = ui_state.active_textarea
        if editor is None or editor.client is not self.view.scene.client:
            raise ValueError("Open a program before inserting objects.")
        first_line, _ = motion_recorder.insert_scene_code(
            lambda source, line: ("await " if in_async_scope(source, line) else "")
            + shapes_to_code(shapes),
            imports=lambda source, line: shape_imports(shapes, source, line),
        )
        return first_line

    def _insert_current(self) -> None:
        try:
            handle = self.history.guard(self.history.context())
            shapes = _editable(handle.shapes)
            line = self._insert(shapes)
        except ValueError as error:
            _say(self.feedback, str(error), "error", "text-sm")
            return
        _say(
            self.feedback,
            f"Inserted {_plural(len(shapes), 'object')} at line {line}",
            "positive",
            "text-sm",
        )

    async def _insert_saved(self, name: str) -> None:
        entry, row = self.entries[name], self.rows[name]
        self._clear_notes()
        try:
            self.history.guard(self.history.context())
            if world_files.entry_revision(name) != entry.revision:
                await self._reread(name)
                return
            line = self._insert(entry.shapes)
        except (OSError, ValueError) as error:
            _say(row.note, str(error), "error")
            return
        _say(row.note, f"Inserted at line {line}", "positive")

    # ------------------------------------------------------------------ load

    async def _load(self, name: str) -> None:
        entry, row = self.entries[name], self.rows[name]
        view = self.view
        self._clear_notes()
        try:
            revision = self.history.context()
            handle = self.history.guard(revision)
            if world_files.entry_revision(name) != entry.revision:
                await self._reread(name)
                return
            selection = list(view._selected_objects)
            self.loading = True
            self._enable_rows()
            _say(row.note, "Loading…")
            applied = await view._commit_objects(
                "Load workcell",
                [
                    *entry.shapes,
                    *(s for s in handle.shapes if s.attachment is not None),
                ],
                revision,
            )
        except (OSError, ValueError) as error:
            if not self.closed:
                _say(row.note, str(error), "error")
            return
        finally:
            self.loading = False
            if not self.closed:
                self._enable_rows()
        if self.closed or view.scene.is_deleted:
            return
        if not applied:
            _say(row.note, self.history.feedback, "error")
            return
        # Unless the user has picked something meanwhile, nothing is selected
        # among the loaded objects.
        if view._selected_objects == selection:
            view._select_objects([])
        _say(row.note, "Loaded. Undo restores the previous objects.", "positive")

    # ------------------------------------------------------------------ list

    async def _list(self) -> None:
        self.listing += 1
        listing = self.listing
        try:
            names = world_files.list_entries()
        except OSError as error:
            names, failure = [], str(error)
        else:
            failure = ""
        entries = await asyncio.to_thread(_read_all, names)
        if self.closed or listing != self.listing:
            return
        self.entries = {entry.name: entry for entry in entries}
        self.rows = {}
        self.list.clear()
        with self.list:
            if failure or not entries:
                ui.label(failure or "Nothing saved yet").classes(
                    "text-xs text-wc-text-muted"
                ).mark("workcell-empty")
            for entry in entries:
                self._row(entry)
        self._enable_rows()
        if str(self.name.value or "").strip():
            self._name_changed()

    def _row(self, entry: _Entry) -> None:
        name = entry.name
        with (
            ui.row()
            .classes("scene-row w-full items-center no-wrap")
            .mark("workcell-entry", f"workcell-entry:{name}")
        ):
            ui.icon("inventory_2", size="18px").classes("scene-row-kind")
            with ui.column().classes("flex-1 min-w-0 gap-0"):
                ui.label(name).classes("scene-row-name truncate")
                caption = ui.label().mark(
                    "workcell-preview", f"workcell-preview:{name}"
                )
                _say(caption, entry.summary(), "warning" if entry.problem else "muted")
                note = ui.label().mark("workcell-note", f"workcell-note:{name}")
                _say(note, "")
            load = (
                ui.button(icon="file_open", on_click=lambda: self._load(name))
                .props("flat round dense size=sm color=wc-action")
                .tooltip(
                    f"Load {name} in place of the editable objects; undo restores them"
                )
                .mark("workcell-load", f"workcell-load:{name}")
            )
            insert = (
                ui.button(icon="code", on_click=lambda: self._insert_saved(name))
                .props("flat round dense size=sm color=wc-text")
                .tooltip(f"Insert {name} into the program")
                .mark("workcell-insert-saved", f"workcell-insert-saved:{name}")
            )
        self.rows[name] = _Row(caption, note, load, insert)

    async def _reread(self, name: str) -> None:
        """Read *name* again after it changed on disk, and say so on its row."""
        entry = await asyncio.to_thread(_read, name)
        row = self.rows.get(name)
        if self.closed or row is None:
            return
        self.entries[name] = entry
        self._enable_rows()
        _say(row.caption, entry.summary(), "warning" if entry.problem else "muted")
        _say(
            row.note,
            "Changed on disk since it was listed. Check it, then try again.",
            "warning",
        )

    def _clear_notes(self) -> None:
        for row in self.rows.values():
            _say(row.note, "")

    def _enable_rows(self) -> None:
        for name, row in self.rows.items():
            usable = not self.entries[name].problem and not self.loading
            row.load.set_enabled(usable)
            row.insert.set_enabled(usable)
