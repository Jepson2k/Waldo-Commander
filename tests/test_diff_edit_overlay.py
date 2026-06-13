"""Integration test for the LLM-edit diff overlay in the WC editor.

Edits are proposed via the waldoctl API directly (no MCP round-trip needed for
these UI assertions). One workflow covers the whole lifecycle: propose renders
the banner + decorations; reject leaves source, the CodeMirror value, and the
overlay untouched/cleared; re-propose + approve mutates the source AND pushes it
into the CodeMirror widget (the regression that froze the editor on approve)
while clearing the overlay.
"""

from __future__ import annotations

import asyncio

import pytest
from nicegui.testing import User

import waldoctl
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import ui_state

_DIFF = "@@ -2,1 +2,1 @@\n-y = 2\n+y = 20\n"
_BEFORE = "x = 1\ny = 2\nz = 3\n"
_AFTER = "x = 1\ny = 20\nz = 3\n"


def _diff_specs(textarea):
    return [
        s
        for s in textarea.decorations
        if s.get("class") in ("cm-edit-add", "cm-edit-remove")
    ]


@pytest.mark.integration
async def test_diff_overlay_propose_reject_approve_lifecycle(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = _BEFORE

    # ---- propose: banner + decorations appear --------------------------------
    edit_id = p.edits.propose(_DIFF, "tweak y")
    await asyncio.sleep(0)  # let the inline notify listener run

    await user.should_see(marker=f"approve-edit-{edit_id.value}")
    await user.should_see(marker=f"reject-edit-{edit_id.value}")

    textarea = ui_state.active_textarea
    assert textarea is not None
    remove_specs = [
        s for s in textarea.decorations if s.get("class") == "cm-edit-remove"
    ]
    add_specs = [s for s in textarea.decorations if s.get("class") == "cm-edit-add"]
    assert len(remove_specs) == 1 and remove_specs[0]["line"] == 2
    assert len(add_specs) == 1 and add_specs[0]["text"].endswith("y = 20")

    # ---- reject: nothing applied, editor + overlay untouched/cleared ---------
    textarea_value_before = textarea.value
    user.find(marker=f"reject-edit-{edit_id.value}").click()
    await asyncio.sleep(0)

    assert p.edits.pending == []
    assert p.source == _BEFORE  # source untouched
    assert textarea.value == textarea_value_before  # editor untouched
    assert _diff_specs(textarea) == []  # overlay cleared

    # ---- re-propose + approve: applied, pushed to the editor, overlay cleared -
    edit_id = p.edits.propose(_DIFF, "tweak y")
    await asyncio.sleep(0)
    user.find(marker=f"approve-edit-{edit_id.value}").click()
    await asyncio.sleep(0)

    assert p.source == _AFTER
    assert p.edits.pending == []
    # The must-fix: approve must push the new source into CodeMirror, otherwise
    # the pane shows stale text and the next keystroke destroys the edit.
    assert textarea.value == _AFTER
    assert _diff_specs(textarea) == []
