"""The status footer's buttons open the bottom panel on the right tab, and
its last action opens the history."""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_footer_opens_the_bottom_panel_and_the_action_history(
    user: User,
) -> None:
    """The footer's buttons open the bottom panel on their tab. The whole
    action history is tens of kilobytes: it goes to the browser when someone
    opens the menu, not on every command, and it is current then."""
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

    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    action = waldoctl.commander.status.action
    client = ui_state.control_panel.client
    # Long enough to be seen executing: the log records what it sees run,
    # and a command that ends between two status frames leaves no entry.
    target = list(await client.angles())
    target[0] += 5.0
    await client.move_j(target, duration=1.0)
    assert await wait_until(lambda: action.latest is not None, timeout_s=10.0), (
        "the move never reached the action log"
    )
    latest = action.latest
    assert latest is not None
    name = latest.command_name

    entries = next(iter(user.find(marker="footer-history-entries").elements))
    assert name not in entries.content, "the shut menu was redrawn"

    next(iter(user.find(marker="footer-history").elements)).open()
    await asyncio.sleep(0)
    assert name in entries.content, "the opened menu shows the latest action"
