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


@pytest.mark.integration
async def test_proposed_edit_flashes_its_lines(user: User) -> None:
    """A freshly proposed edit flashes its changed lines (``cm-line-flash``),
    the way the motion recorder flashes an inserted line — and approving does
    NOT spawn a second flash (only a genuinely new edit flashes)."""
    from waldo_commander.components.editor_decorations import decorations

    await user.open("/")
    await wait_for_app_ready()
    ui_state.program_panel_visible = True  # flash the lines, not the tab

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = _BEFORE

    def _flash_specs(textarea):
        return [s for s in textarea.decorations if s.get("class") == "cm-line-flash"]

    edit_id = p.edits.propose(_DIFF, "tweak y")
    await asyncio.sleep(0)

    textarea = ui_state.active_textarea
    assert textarea is not None
    assert _flash_specs(textarea), "a freshly proposed edit must flash its line"
    flashes_after_propose = len(decorations._active_flashes)

    # Approve: applies the edit but must not re-flash — only a new proposal does.
    user.find(marker=f"approve-edit-{edit_id.value}").click()
    await asyncio.sleep(0)
    assert p.source == _AFTER
    assert len(decorations._active_flashes) == flashes_after_propose, (
        "approve/reject must not add a flash; only a newly proposed edit flashes"
    )


@pytest.mark.integration
async def test_interior_additions_render_at_their_own_positions(user: User) -> None:
    """Additions between context lines must render where they occur — one
    widget per contiguous ``+`` run — not collapse into a single widget after
    the hunk's trailing context."""
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "a\nb\nc\n"
    p.edits.propose("@@ -1,3 +1,5 @@\n a\n+x\n b\n+y\n c\n")
    await asyncio.sleep(0)

    textarea = ui_state.active_textarea
    add_specs = [s for s in textarea.decorations if s.get("class") == "cm-edit-add"]
    # Two separate runs: "x" before line 2 ("b" at offset 2), "y" before
    # line 3 ("c" at offset 4).
    assert [(s["position"], s["text"]) for s in add_specs] == [
        (2, "+ x"),
        (4, "+ y"),
    ]
    p.edits.reject(p.edits.pending[0].id)


@pytest.mark.integration
async def test_crlf_source_widget_offsets_match_codemirror_units(user: User) -> None:
    """CodeMirror normalizes every line break to one UTF-16 unit; widget
    offsets must count them that way or anchors drift +1 per preceding CRLF
    line."""
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "a\r\nb\r\nc\r\n"
    p.edits.propose("@@ -3,1 +3,1 @@\n-c\n+C\n")
    await asyncio.sleep(0)

    textarea = ui_state.active_textarea
    add_specs = [s for s in textarea.decorations if s.get("class") == "cm-edit-add"]
    # In CM units each "X\r\n" line is 2 (char + one normalized break), so the
    # widget after removed line 3 sits at offset 6 — not 9 (CRLF counted as 2).
    assert len(add_specs) == 1
    assert add_specs[0]["position"] == 6
    p.edits.reject(p.edits.pending[0].id)


@pytest.mark.integration
async def test_human_keystroke_does_not_snap_overlay_to_stale_coords(
    user: User,
) -> None:
    """Human typing must not re-push diff-absolute coordinates: pushed specs
    stay put server-side (CodeMirror's decoration StateField maps them through
    document edits client-side); only edit-flow changes re-push."""
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "a\nb\nc\n"
    p.edits.propose("@@ -3,1 +3,1 @@\n-c\n+C\n")
    await asyncio.sleep(0)

    textarea = ui_state.active_textarea
    specs_before = _diff_specs(textarea)
    assert specs_before, "propose must render the overlay"

    # The human inserts a line above the hunk through the editor's real
    # content-change path.
    from nicegui import Client as NgClient

    editor = ui_state.editor_panel
    with NgClient.instances[ui_state.active_client_id]:
        editor._on_tab_content_change(p, "inserted\na\nb\nc\n")
    await asyncio.sleep(0)

    assert _diff_specs(textarea) == specs_before, (
        "a keystroke re-pushed the overlay from stale diff-absolute coords"
    )
    p.edits.reject(p.edits.pending[0].id)
