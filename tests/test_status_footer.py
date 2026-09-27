"""The status footer's buttons open the bottom panel on the right tab, and
its last action opens the history."""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import enable_sim, wait_for_app_ready, wait_until
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


@pytest.mark.integration
async def test_the_action_history_is_drawn_when_its_menu_opens(user: User) -> None:
    """The whole history is tens of kilobytes: it goes to the browser when
    someone opens the menu, not on every command, and it is current then."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    action = waldoctl.commander.status.action
    await ui_state.control_panel.client.home()
    assert await wait_until(lambda: action.latest is not None, timeout_s=10.0), (
        "homing never reached the action log"
    )
    latest = action.latest
    assert latest is not None
    name = latest.command_name

    entries = next(iter(user.find(marker="footer-history-entries").elements))
    assert name not in entries.content, "the shut menu was redrawn"

    next(iter(user.find(marker="footer-history").elements)).open()
    await asyncio.sleep(0)
    assert name in entries.content, "the opened menu shows the latest action"
