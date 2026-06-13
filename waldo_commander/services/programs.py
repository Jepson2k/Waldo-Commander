"""WC-side concrete ``ProgramTabs`` — provides the open/new/close/switch
methods that ``waldoctl.ProgramTabs`` declares as ``NotImplementedError``,
plus helpers for session-wide recording state.

The base ``ProgramTabs`` defines the public observation surface (``items``,
``active_id``, ``active`` / ``get`` / ``find_by_path`` lookups) and the
mutate-in-place invariant. WC owns the action verbs: opening files from
disk, creating new buffers, closing tabs, and switching the active tab.

Programs created here use waldoctl's ``Program`` directly — disk
persistence and reload still live in WC's editor component (file_operations
mediates the actual I/O), so the host application never calls
``program.save()`` / ``program.reload()`` (those remain
``NotImplementedError`` until a future PR moves the I/O into the API).
"""

from __future__ import annotations

from pathlib import Path

import waldoctl
from waldoctl import DryRun, Playback, Program, ProgramTabs


def is_any_program_recording() -> bool:
    """True if any open ``Program`` is currently recording.

    The one-recording-at-a-time invariant is enforced by ``motion_recorder``,
    but consumers that just need "is anything being recorded?" use this
    helper instead of dotting through ``commander.programs.active.recording``
    (which fails when no program is active).

    Tolerates the pre-startup window when the locator isn't registered yet —
    returns ``False`` in that case so call sites in fixtures / smoke checks
    behave the same as the legacy ``recording_state.is_recording == False``.
    """
    try:
        items = waldoctl.commander.programs.items
    except RuntimeError:
        return False
    for p in items:
        if p.recording.is_recording:
            return True
    return False


def is_any_program_running() -> bool:
    """True if any open ``Program`` has its script currently executing.

    The one-execution-at-a-time invariant is enforced by
    ``script_execution`` (it refuses to start a second script while one is
    live). This helper is the read side: anywhere WC previously checked
    the global ``simulation_state.script_running`` flag uses this.

    Runs on the per-tick status / playback paths, so it iterates with an
    early-return loop rather than ``any(genexpr)`` to avoid allocating a
    generator each call (``items`` is a handful of open tabs).

    Tolerates the pre-startup window when the locator isn't registered yet —
    returns ``False`` in that case so call sites in fixtures / smoke checks
    behave the same as the legacy ``simulation_state.script_running == False``.
    """
    try:
        items = waldoctl.commander.programs.items
    except RuntimeError:
        return False
    for p in items:
        if p.execution.is_running:
            return True
    return False


class EditorPrograms(ProgramTabs):
    """Concrete ``ProgramTabs`` backed by WC's editor.

    Overrides the host-application hooks (``open`` / ``new`` / ``close`` /
    ``switch``) with the actual file-system + tab-list logic WC needs.
    Bindable behavior comes from the base ``@bindable_dataclass`` decorator
    — subclass methods don't change which fields fire bindings.
    """

    def new(
        self,
        source: str = "",
        filename: str = "untitled.py",
        file_path: str | None = None,
    ) -> Program:
        """Create a fresh ``Program`` with the given source and append it
        to ``items``. Reassigns ``items`` wholesale so bindings fire.
        """
        program = Program(
            filename=filename,
            file_path=file_path,
            source=source,
            _saved_source=source,
        )
        self.items = [*self.items, program]
        self.notify_changed()
        return program

    def open(self, path: str) -> Program:
        """Load ``path`` from disk into a new ``Program`` and make it active.
        Returns (and re-activates) the existing ``Program`` if one is already
        open for this path.
        """
        existing = self.find_by_path(path)
        if existing is not None:
            self.switch(existing.id)
            return existing
        content = Path(path).read_text(encoding="utf-8")
        program = self.new(source=content, filename=Path(path).name, file_path=path)
        self.switch(program.id)
        return program

    def close(self, id: str) -> None:
        """Remove the ``Program`` with this id. If it was active, the next
        program (or ``None`` if the list is now empty) becomes active.
        """
        if not any(p.id == id for p in self.items):
            return
        self.items = [p for p in self.items if p.id != id]
        if self.active_id == id:
            self.active_id = self.items[0].id if self.items else None
        self.notify_changed()

    def switch(self, id: str) -> None:
        """Make the ``Program`` with this id active. Raises ``KeyError`` if
        the id is not in ``items``.
        """
        if not any(p.id == id for p in self.items):
            raise KeyError(id)
        self.active_id = id
        self.notify_changed()


def active_dry_run() -> DryRun | None:
    """The active program's dry-run state, or ``None`` when no program is open."""
    active = waldoctl.commander.programs.active
    return active.dry_run if active is not None else None


def active_playback() -> Playback | None:
    """The active program's playback state, or ``None`` when no program is open."""
    active = waldoctl.commander.programs.active
    return active.dry_run.playback if active is not None else None
