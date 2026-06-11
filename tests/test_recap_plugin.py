"""RECAP loads into the running app as a zero-edit plugin: its tab mounts, its
tools compose into ``robot.tools`` via the entry-point groups, and a plugin
tool can be selected through the live controller (which requires the planner
subprocess to know plugin tools too)."""

from __future__ import annotations

import pytest
from nicegui.testing import User

pytest.importorskip("recap", reason="recap plugin not installed (dev env only)")

import waldoctl

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_recap_tab_and_tools(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()

    keys = {t.key for t in ui_state.active_robot.tools.available}
    assert {"RECAP_CAMERA", "RECAP_LIDAR", "RECAP_PROBE"} <= keys

    await user.should_see(marker="tab-recap")
    user.find(marker="tab-recap").click()
    await user.should_see(marker="recap-panel")

    # Round-trip a plugin tool through the controller + planner subprocess.
    client = waldoctl.commander.client
    idx = await client.select_tool("RECAP_CAMERA")
    assert await client.wait_command(idx, timeout=10.0)
    idx = await client.select_tool("NONE")
    assert await client.wait_command(idx, timeout=10.0)
