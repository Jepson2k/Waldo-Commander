"""Integration test for the LLM-edit diff overlay in the WC editor.

Edits are proposed via the waldoctl API directly (no MCP round-trip needed for
these UI assertions).
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
async def test_diff_overlay_lifecycle_flash_and_positions(user: User) -> None:
    """Propose renders the banner and the overlay and flashes the changed
    line; reject leaves source and editor alone and clears the overlay;
    approve pushes the new source into CodeMirror and clears the overlay;
    neither adds a flash. Additions render at their own positions, offsets
    count a CRLF break once and astral characters as one Python index, and a
    keystroke does not re-push the overlay from stale coordinates."""
    from nicegui import Client as NgClient

    from waldo_commander.components.editor_decorations import decorations

    await user.open("/")
    await wait_for_app_ready()
    ui_state.program_panel_visible = True  # flash the lines, not the tab

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = _BEFORE

    def _flash_specs(textarea):
        return [s for s in textarea.decorations if s.get("class") == "cm-line-flash"]

    # ---- propose: banner + decorations appear, the line flashes --------------
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
    assert _flash_specs(textarea), "a freshly proposed edit must flash its line"
    flashes = len(decorations._active_flashes)

    # ---- reject: nothing applied, editor + overlay untouched/cleared ---------
    textarea_value_before = textarea.value
    user.find(marker=f"reject-edit-{edit_id.value}").click()
    await asyncio.sleep(0)

    assert p.edits.pending == []
    assert p.source == _BEFORE  # source untouched
    assert textarea.value == textarea_value_before  # editor untouched
    assert _diff_specs(textarea) == []  # overlay cleared
    assert len(decorations._active_flashes) == flashes, "reject must not flash"

    # ---- re-propose + approve: applied, pushed to the editor, overlay cleared -
    edit_id = p.edits.propose(_DIFF, "tweak y")
    await asyncio.sleep(0)
    flashes = len(decorations._active_flashes)
    user.find(marker=f"approve-edit-{edit_id.value}").click()
    await asyncio.sleep(0)

    assert p.source == _AFTER
    assert p.edits.pending == []
    # The must-fix: approve must push the new source into CodeMirror, otherwise
    # the pane shows stale text and the next keystroke destroys the edit.
    assert textarea.value == _AFTER
    assert _diff_specs(textarea) == []
    assert len(decorations._active_flashes) == flashes, (
        "approve must not add a flash; only a newly proposed edit flashes"
    )

    # ---- interior additions: one widget per contiguous ``+`` run, where it
    # occurs, not one collapsed after the hunk's trailing context ------------
    p.source = "a\nb\nc\n"
    p.edits.propose("@@ -1,3 +1,5 @@\n a\n+x\n b\n+y\n c\n")
    await asyncio.sleep(0)
    add_specs = [s for s in textarea.decorations if s.get("class") == "cm-edit-add"]
    # "x" before line 2 ("b" at offset 2), "y" before line 3 ("c" at offset 4).
    assert [(s["position"], s["text"]) for s in add_specs] == [
        (2, "+ x"),
        (4, "+ y"),
    ]
    p.edits.reject(p.edits.pending[0].id)

    # ---- CRLF sources: NiceGUI takes Python indices into the normalized
    # document, including after astral characters; the browser owns the
    # conversion to UTF-16 -------------------------------------------------------
    for first in ("a", "😀a"):
        p.source = f"{first}\r\nb\r\nc\r\n"
        p.edits.propose("@@ -3,1 +3,1 @@\n-c\n+C\n")
        await asyncio.sleep(0)
        add_specs = [s for s in textarea.decorations if s.get("class") == "cm-edit-add"]
        # The widget follows removed line 3: each normalized break counts once.
        assert len(add_specs) == 1, first
        assert add_specs[0]["position"] == len(f"{first}\nb\nc\n"), first
        p.edits.reject(p.edits.pending[0].id)

    # ---- a keystroke: pushed specs stay put server-side (CodeMirror maps them
    # through document edits client-side); only edit-flow changes re-push ---
    p.source = "a\nb\nc\n"
    p.edits.propose("@@ -3,1 +3,1 @@\n-c\n+C\n")
    await asyncio.sleep(0)
    specs_before = _diff_specs(textarea)
    assert specs_before, "propose must render the overlay"
    editor = ui_state.editor_panel
    with NgClient.instances[ui_state.active_client_id]:
        editor._on_tab_content_change(p, "inserted\na\nb\nc\n")
    await asyncio.sleep(0)
    assert _diff_specs(textarea) == specs_before, (
        "a keystroke re-pushed the overlay from stale diff-absolute coords"
    )
    p.edits.reject(p.edits.pending[0].id)
