"""Tests for I/O and gripper functionality."""

import asyncio

import pytest
import waldoctl
from nicegui import Client
from nicegui.testing import User
from waldoctl import ElectricGripperTool

from tests.helpers.wait import (
    enable_sim,
    poll_until,
    wait_for_app_ready,
    wait_for_tool_key,
    wait_until,
)
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_io_outputs_and_the_footer_io_dots(user: User) -> None:
    """HIGH in the I/O tab sets the output on the controller; the footer has
    one dot per line the backend reports, named on hover.

    parol6 has four fixed lines, but a backend that takes its I/O from
    config can report many more, and the first frame from it may not match
    the count the footer was built for.
    """
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

    user.find(marker="tab-io").click()
    await asyncio.sleep(0)
    user.find("HIGH").click()
    outputs = await poll_until(
        lambda: waldoctl.commander.status.io.outputs,
        lambda outs: bool(outs) and int(outs[0]) == 1,
        timeout_s=1.0,
        interval=0.05,
        what="OUTPUT 1 after clicking HIGH",
    )
    assert int(outputs[0]) == 1

    io = waldoctl.commander.status.io
    io.inputs = [1] + [0] * 11
    io.outputs = [0] * 12
    # The status consumer calls this on the page; a face change in between
    # restarts its animation there, which needs the page.
    with Client.instances[ui_state.active_client_id]:
        footer.update_conn_io()

    assert len(footer._io_dots) == 24, "a dot per line the backend reports"
    lit = [i for i, d in enumerate(footer._io_dots) if "io-dot-on" in d.classes]
    assert lit == [0], "and only the high line is lit"


@pytest.mark.integration
async def test_gripper_panel_and_quick_actions(user: User) -> None:
    """With an electric gripper fitted the Gripper tab draws its chart, and
    the control panel's quick actions toggle it: the action button's icon
    keeps a readable colour on either fill, two quick toggles return to the
    open target, and the adjust buttons step the grip current in percent of
    the tool's current range, which the next grip draws."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    client = ui_state.control_panel.client
    try:
        await client.select_tool("SSG-48")
        await wait_for_tool_key("SSG-48")
        tool = client.tool
        assert isinstance(tool, ElectricGripperTool)
        index = await tool.calibrate()
        assert index >= 0 and await client.wait_command(index, timeout=10)

        user.find(marker="tab-gripper").click()
        await asyncio.sleep(0)
        await user.should_see(marker="gripper-chart")

        await user.should_see(marker="btn-tool-action-l")
        # Adjust buttons are shown for electric grippers.
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

        # A second toggle that lands before the first one's jaws arrive still
        # ends on the open target.
        waldoctl.commander.settings.jog.speed = 20
        waldoctl.commander.settings.gripper.target_position = 0.0
        user.find(marker="btn-tool-action-l").click()
        await asyncio.sleep(0.1)
        user.find(marker="btn-tool-action-l").click()
        assert await wait_until(
            lambda: waldoctl.commander.settings.gripper.target_position == 0.0, 2
        )
        assert await client.wait_motion(timeout=15, settle_window=0.1)
        assert (await tool.status()).position == pytest.approx(0, abs=5 / 255)

        step = tool.adjust_step
        assert step is not None
        grip = waldoctl.commander.settings.gripper
        before = grip.current
        user.find(marker="btn-tool-adjust-minus").click()
        assert await wait_until(lambda: grip.current != before, 2, interval=0.05)
        assert grip.current == before - step

        # A slow close keeps the jaws travelling, and drawing current,
        # across many status ticks.
        waldoctl.commander.settings.jog.speed = 10
        user.find(marker="btn-tool-action-l").click()
        drawn = 0.0
        for _ in range(500):
            drawn = waldoctl.commander.status.tool.current
            if drawn:
                break
            await asyncio.sleep(0.01)
        lo, hi = tool.current_range
        assert drawn == round(lo + grip.current / 100 * (hi - lo))
    finally:
        await client.select_tool("NONE")
