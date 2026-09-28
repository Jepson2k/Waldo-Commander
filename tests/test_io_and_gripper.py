"""Tests for I/O and gripper functionality."""

import asyncio

import pytest
from nicegui import Client
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    poll_until,
    wait_for_app_ready,
    wait_for_tool_key,
)
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
    """Control panel should show tool quick-action box when a tool is active,
    and the action button's icon keeps a readable colour on either fill."""
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    # Set tool to SSG-48 and wait for status loop to propagate
    await ui_state.control_panel.client.select_tool("SSG-48")
    await wait_for_tool_key("SSG-48")

    # Tool action L button should be visible
    await user.should_see(marker="btn-tool-action-l")

    # Adjust buttons should be visible for electric grippers
    await user.should_see(marker="btn-tool-adjust-minus")
    await user.should_see(marker="btn-tool-adjust-plus")

    # Closing lights the button with the action fill and opening puts it
    # back; the icon's colour follows the fill both ways.
    text_on = {"wc-control": "wc-text", "wc-action": "wc-on-bright"}
    button = next(iter(user.find(marker="btn-tool-action-l").elements))
    seen = set()
    for _ in text_on:
        before = button.props.get("color")
        user.find(marker="btn-tool-action-l").click()
        fill = await poll_until(
            lambda: button.props.get("color"),
            lambda color, before=before: color != before,
            timeout_s=15,
            what="the action button's fill after a click",
        )
        assert button.props.get("text-color") == text_on[fill], button.props
        seen.add(fill)
    assert seen == set(text_on)


@pytest.mark.integration
async def test_the_footer_io_dots_follow_the_line_count(user: User) -> None:
    """One dot per line the backend reports, named on hover.

    parol6 has four fixed lines, but a backend that takes its I/O from
    config can report many more, and the first frame from it may not match
    the count the footer was built for.
    """
    import waldoctl as _wctl

    await user.open("/")
    await wait_for_app_ready()

    footer = ui_state.readout_panel
    assert len(footer._io_dots) == 4, "parol6 reports two in and two out"
    tooltips = [next(iter(d.default_slot.children)).text for d in footer._io_dots]
    assert tooltips == [
        "Digital Input 1",
        "Digital Input 2",
        "Digital Output 1",
        "Digital Output 2",
    ]

    io = _wctl.commander.status.io
    io.inputs = [1] + [0] * 11
    io.outputs = [0] * 12
    # The status consumer calls this on the page; a face change in between
    # restarts its animation there, which needs the page.
    with Client.instances[ui_state.active_client_id]:
        footer.update_conn_io()

    assert len(footer._io_dots) == 24, "a dot per line the backend reports"
    lit = [i for i, d in enumerate(footer._io_dots) if "io-dot-on" in d.classes]
    assert lit == [0], "and only the high line is lit"
