"""Tests for I/O and gripper functionality."""

import asyncio

import pytest
from nicegui import Client
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready, wait_for_tool_key
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_io_tab_high_low_buttons_send_commands(user: User) -> None:
    """Clicking HIGH/LOW buttons in the I/O tab should send SET_IO commands.

    Verifies the full integration from UI button to controller by checking
    that the IO output state changes after clicking HIGH/LOW.
    """
    await user.open("/")
    await wait_for_app_ready()

    # Open the I/O tab
    user.find(marker="tab-io").click()
    await asyncio.sleep(0)

    # Click HIGH for OUTPUT 1 and wait for status propagation
    import waldoctl as _wctl

    user.find("HIGH").click()
    for _ in range(20):
        await asyncio.sleep(0.05)
        _outs = _wctl.commander.status.io.outputs
        if _outs and int(_outs[0]) == 1:
            break
    _outs = _wctl.commander.status.io.outputs
    assert _outs and int(_outs[0]) == 1, (
        f"Expected OUTPUT 1 = HIGH (1) after click, got {_outs}"
    )


@pytest.mark.integration
async def test_gripper_panel_layout_elements(user: User) -> None:
    """Gripper panel should show chart and status readouts."""

    await user.open("/")
    await wait_for_app_ready()

    # Set tool to SSG-48 and wait for status loop to propagate
    await ui_state.control_panel.client.select_tool("SSG-48")
    await wait_for_tool_key("SSG-48")

    # Open the Gripper tab
    user.find(marker="tab-gripper").click()
    await asyncio.sleep(0)

    # Combined dual-axis chart should exist
    await user.should_see(marker="gripper-chart")


@pytest.mark.integration
async def test_control_panel_tool_quick_actions(user: User) -> None:
    """Control panel should show tool quick-action box when a tool is active."""
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    # Set tool to SSG-48 and wait for status loop to propagate
    await ui_state.control_panel.client.select_tool("SSG-48")
    await wait_for_tool_key("SSG-48")

    # Tool action L button should be visible
    await user.should_see(marker="btn-tool-action-l")

    # Adjust buttons should be visible for electric grippers
    await user.should_see(marker="btn-tool-adjust-minus")
    await user.should_see(marker="btn-tool-adjust-plus")


@pytest.mark.integration
async def test_the_readout_io_strip_absorbs_a_large_line_count(user: User) -> None:
    """The strip has to fit the panel whatever the backend reports.

    parol6 has four fixed lines, but a backend that takes its I/O from
    config can report many more. One unwrapped row of those widens the
    readout past its own width and drags the rest of the panel with it, so
    the strip wraps and its labels tighten instead.
    """
    import waldoctl as _wctl

    await user.open("/")
    await wait_for_app_ready()

    readout = ui_state.readout_panel
    assert len(readout._io_chips) == 4, "parol6 reports two in and two out"
    assert readout._io_chips[0].text == "DI1", "few enough lines to spell out"

    io = _wctl.commander.status.io
    io.inputs = [0] * 12
    io.outputs = [0] * 12
    # The status consumer calls this on the page; a face change in between
    # restarts its animation there, which needs the page.
    with Client.instances[ui_state.active_client_id]:
        readout.update_conn_io()

    assert len(readout._io_chips) == 24, "a chip per line the backend reports"
    assert readout._io_chips[0].text == "I1", "and terser labels once there are many"
