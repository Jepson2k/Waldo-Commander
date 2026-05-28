"""Integration tests for the LLM-edit diff overlay in the WC editor.

Edits are proposed via the waldoctl API directly (no MCP round-trip
needed for these UI assertions). Tests then check that:
1. The pending-edit banner appears with Approve / Reject buttons.
2. CodeMirror ``decorations`` carry add/remove specs at the right
   positions.
3. Clicking Approve mutates the program source and clears the
   decorations + banner.
"""

from __future__ import annotations

import asyncio

import pytest
from nicegui.testing import User

import waldoctl
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_propose_edit_renders_banner_and_decorations(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "x = 1\ny = 2\nz = 3\n"

    edit_id = p.edits.propose("@@ -2,1 +2,1 @@\n-y = 2\n+y = 20\n", "tweak y")
    # Let the listener (which runs inline on notify_changed) finish.
    await asyncio.sleep(0)

    # Banner now contains a row with approve / reject buttons.
    await user.should_see(marker=f"approve-edit-{edit_id.value}")
    await user.should_see(marker=f"reject-edit-{edit_id.value}")

    # Decorations on the active textarea include one "remove" line and
    # one "add" widget.
    textarea = ui_state.active_textarea
    assert textarea is not None
    specs = list(textarea.decorations)
    remove_specs = [s for s in specs if s.get("class") == "cm-edit-remove"]
    add_specs = [s for s in specs if s.get("class") == "cm-edit-add"]
    assert len(remove_specs) == 1
    assert remove_specs[0]["line"] == 2
    assert len(add_specs) == 1
    assert add_specs[0]["text"].endswith("y = 20")


@pytest.mark.integration
async def test_approve_button_applies_diff_and_clears_overlay(
    user: User,
) -> None:
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "x = 1\ny = 2\nz = 3\n"
    edit_id = p.edits.propose("@@ -2,1 +2,1 @@\n-y = 2\n+y = 20\n", "tweak y")
    await asyncio.sleep(0)

    user.find(marker=f"approve-edit-{edit_id.value}").click()
    await asyncio.sleep(0)

    assert p.source == "x = 1\ny = 20\nz = 3\n"
    assert p.edits.pending == []
    textarea = ui_state.active_textarea
    assert textarea is not None
    diff_specs = [
        s
        for s in textarea.decorations
        if s.get("class") in ("cm-edit-add", "cm-edit-remove")
    ]
    assert diff_specs == []


@pytest.mark.integration
async def test_reject_button_drops_edit_without_applying(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "x = 1\ny = 2\nz = 3\n"
    edit_id = p.edits.propose("@@ -2,1 +2,1 @@\n-y = 2\n+y = 20\n", "tweak y")
    await asyncio.sleep(0)

    user.find(marker=f"reject-edit-{edit_id.value}").click()
    await asyncio.sleep(0)

    assert p.source == "x = 1\ny = 2\nz = 3\n"  # untouched
    assert p.edits.pending == []
