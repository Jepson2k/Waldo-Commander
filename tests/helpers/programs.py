"""Test helpers for ``commander.programs`` and per-program state."""

from __future__ import annotations

import waldoctl
from waldoctl import Program


def set_active_recording(is_recording: bool) -> None:
    """Set the active program's ``recording.is_recording`` flag.

    Creates a stub active program if none exists, so unit tests that need
    to flip the recording bit without going through the editor's
    tab-creation path can do so. Idempotent for the "stop recording" case
    when no programs are open.
    """
    try:
        programs = waldoctl.commander.programs
    except RuntimeError:
        return  # locator not registered yet
    if programs.active is None:
        if not is_recording:
            return
        stub = Program(filename="test.py")
        programs.items = [*programs.items, stub]
        programs.active_id = stub.id
    programs.active.recording.is_recording = is_recording
