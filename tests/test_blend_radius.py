"""Blend radius setting flows into recorded and inserted move code."""

import asyncio

import numpy as np
import pytest
from nicegui import app as ng_app
from nicegui import ui
from nicegui.testing import User, UserInteraction

import waldoctl

from tests.helpers.wait import enable_sim, wait_for_app_ready
from waldo_commander.state import ui_state


async def _set_blend_radius(user: User, value: float) -> None:
    number_el = next(iter(user.find(marker="settings-blend-radius").elements))
    number_el.set_value(value)
    for _ in range(20):
        if ng_app.storage.general.get("jog_blend_r") == value:
            return
        await asyncio.sleep(0.05)
    assert ng_app.storage.general.get("jog_blend_r") == value


def _click_palette_item(user: User, title: str) -> None:
    """Click a command-palette menu item by title. The item's text lives on a
    child item_section, so content-find the section and click its menu item."""
    section = min(
        user.find(kind=ui.item_section, content=title).elements, key=lambda e: e.id
    )
    item = next(a for a in section.ancestors() if isinstance(a, ui.menu_item))
    UserInteraction(user, {item}, None).click()


@pytest.mark.integration
async def test_blend_radius_setting_controls_r_in_generated_code(user: User) -> None:
    """A positive blend radius emits ``r=`` at the recorded/inserted
    code-generation paths; the default 0 keeps generated code r-free."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    assert textarea is not None

    # Default (0): palette insert emits no r argument
    textarea.value = ""
    _click_palette_item(user, "rbt.move_j(...)")
    await asyncio.sleep(0)
    assert "rbt.move_j(" in textarea.value
    assert "r=" not in textarea.value

    # Raise the setting through the real settings row
    user.find(kind=ui.tab, content="Settings").click()
    await asyncio.sleep(0)
    await _set_blend_radius(user, 5)

    # Insert-command palette: move_j and move_l carry r=5
    textarea.value = ""
    _click_palette_item(user, "rbt.move_j(...)")
    await asyncio.sleep(0)
    _click_palette_item(user, "rbt.move_l(...)")
    await asyncio.sleep(0)
    assert textarea.value.count(", r=5)") == 2, textarea.value

    # Capture Current Pose: recorded move_l carries r=5
    user.find(marker="editor-capture-pose-btn").click()
    await asyncio.sleep(0)
    assert textarea.value.count(", r=5)") == 3, textarea.value

    # Gizmo target insert: both move_l and move_j branches carry r=5
    editor = ui_state.editor_panel
    assert editor is not None
    editor.add_target_code([100.0, 0.0, 200.0, 0.0, 0.0, 0.0], "cartesian")
    editor.add_target_code([10.0, -60.0, 100.0, 5.0, 5.0, 5.0], "joints")
    assert textarea.value.count(", r=5)") == 5, textarea.value

    # Recording-start anchor: pin the dry-run end away from the robot so the
    # anchor move_j is inserted, then start recording via the record button.
    # No await between the two — the sync click handler runs inline, so a
    # background simulation cannot overwrite the pinned value.
    tab = waldoctl.commander.programs.active
    assert tab is not None
    tab.dry_run.final_joints_rad = list(np.radians([10.0, -60.0, 100.0, 5.0, 5.0, 5.0]))
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0)
    anchor_lines = [
        ln for ln in textarea.value.splitlines() if "Recording start position" in ln
    ]
    assert anchor_lines and ", r=5)" in anchor_lines[0], textarea.value
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0)

    # Non-integer radii keep their decimal; back to 0 removes r entirely
    await _set_blend_radius(user, 2.5)
    textarea.value = ""
    _click_palette_item(user, "rbt.move_l(...)")
    await asyncio.sleep(0)
    assert ", r=2.5)" in textarea.value

    await _set_blend_radius(user, 0)
    textarea.value = ""
    _click_palette_item(user, "rbt.move_l(...)")
    await asyncio.sleep(0)
    assert "r=" not in textarea.value
