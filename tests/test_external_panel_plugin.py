"""End-to-end test that a third-party Panel plugin renders without source edits.

Simulates the production loading path — ``importlib.metadata`` entry-point
discovery — by injecting a fake ``waldoctl.panels`` entry point that points
at a panel class defined in this test file.  A passing test proves a plugin
package can ship a tab purely via the ``waldoctl.panels`` entry-point group.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from typing import ClassVar

import pytest
from nicegui import ui
from nicegui.testing import User
from waldoctl import Commander, Panel, PanelSlot

from tests.helpers.wait import wait_for_app_ready


class NotesPanel(Panel):
    """Minimal in-test plugin panel: renders a single labelled marker.

    The build/start hooks assert that the live :class:`Commander` exposes
    the surfaces a third-party plugin would actually consume — status,
    programs, settings — so we catch regressions in the public API at the
    same time we verify discovery and mounting.
    """

    id: ClassVar[str] = "notes"
    display_name: ClassVar[str] = "Notes"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_TOP_TAB
    tab_icon: ClassVar[str] = "edit_note"
    tab_tooltip: ClassVar[str] = "Notes"

    start_called: ClassVar[bool] = False
    stop_called: ClassVar[bool] = False
    commander_seen: ClassVar[Commander | None] = None

    def build(self, commander: Commander) -> None:
        type(self).commander_seen = commander
        assert commander.status is not None
        assert commander.programs is not None
        assert commander.settings is not None
        ui.label("notes hello").mark("notes-hello")

    async def start(self, commander: Commander) -> None:
        assert commander is type(self).commander_seen
        type(self).start_called = True

    async def stop(self) -> None:
        type(self).stop_called = True


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject NotesPanel into ``waldoctl.panels`` discovery and reset state."""
    from waldo_commander.state import ui_state

    real = importlib.metadata.entry_points
    fake_ep = importlib.metadata.EntryPoint(
        name="notes",
        value="tests.test_external_panel_plugin:NotesPanel",
        group="waldoctl.panels",
    )

    def fake_entry_points(*, group: str = "") -> object:
        if group == "waldoctl.panels":
            return [fake_ep]
        return real(group=group) if group else real()

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    ui_state.plugin_panels = []
    ui_state._plugin_panels_started = False
    NotesPanel.start_called = False
    NotesPanel.stop_called = False
    NotesPanel.commander_seen = None


@pytest.mark.integration
async def test_external_panel_appears_as_tab(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin-registered Panel surfaces as a tab and its build() renders."""
    _patch_entry_points(monkeypatch)

    await user.open("/")
    await wait_for_app_ready()

    await user.should_see(marker="tab-notes")
    user.find(marker="tab-notes").click()
    await asyncio.sleep(0)
    await user.should_see(marker="notes-hello")


@pytest.mark.integration
async def test_external_panel_start_runs(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin Panel.start runs once UI is built."""
    _patch_entry_points(monkeypatch)

    await user.open("/")
    await wait_for_app_ready()
    # start() is scheduled via asyncio.create_task in index_page; yield to let it run.
    for _ in range(20):
        if NotesPanel.start_called:
            break
        await asyncio.sleep(0.05)
    assert NotesPanel.start_called, "Panel.start was not invoked"
