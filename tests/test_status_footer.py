"""The status footer's buttons open the bottom panel on the right tab."""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_footer_buttons_open_the_bottom_panel(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.bottom_panel
    assert not panel.visible, "the bottom panel starts hidden"

    user.find(marker="footer-events").click()
    await asyncio.sleep(0)
    assert panel.visible and panel.tabs.value == "diagnostics"
    await user.should_see(marker="diagnostics-panel")

    user.find(marker="footer-log").click()
    await asyncio.sleep(0)
    assert panel.visible and panel.tabs.value == "log"
    await user.should_see(marker="response-log")

    user.find(marker="bottom-panel-close").click()
    await asyncio.sleep(0)
    assert not panel.visible
    await user.should_not_see(marker="response-log")
