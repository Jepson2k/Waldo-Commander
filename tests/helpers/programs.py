"""Test helpers for ``commander.programs`` and per-program state."""

from __future__ import annotations

import waldoctl
from waldoctl import Program


def ensure_active_program() -> Program | None:
    """Ensure an active ``Program`` exists; create a stub if not.

    Returns the active program (or ``None`` if the Commander locator
    hasn't been registered yet — pre-startup window in test fixtures).
    """
    try:
        programs = waldoctl.commander.programs
    except RuntimeError:
        return None
    if programs.active is None:
        stub = Program(filename="test.py")
        programs.items = [*programs.items, stub]
        programs.active_id = stub.id
    return programs.active


def set_active_recording(is_recording: bool) -> None:
    """Set the active program's ``recording.is_recording`` flag.

    Always ensures an active program exists so tests that flip recording
    state — including the ``False`` case — find a Program for downstream
    code (e.g. ``motion_recorder._start_recording``) to write into.
    """
    active = ensure_active_program()
    if active is None:
        return  # locator not registered yet
    active.recording.is_recording = is_recording


def clear_all_programs() -> None:
    """Clear every open program and its dry-run/recording state.

    No-op when the Commander locator isn't registered yet (module-scope
    fixtures may run before a test commander exists). Clearing ``items``
    discards all per-program recording state, so no separate flag reset is
    needed.
    """
    try:
        programs = waldoctl.commander.programs
    except RuntimeError:
        return
    for p in programs.items:
        p.dry_run.path_segments = []
        p.dry_run.targets = []
        p.dry_run.tool_actions = []
        p.dry_run.tool_selections = []
    programs.items.clear()
    programs.active_id = None
