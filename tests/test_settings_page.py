"""Tests for settings page functionality."""

import asyncio

import pytest
from nicegui.testing import User
from nicegui import ui, app as ng_app
from typing import Any

from waldo_commander.state import ui_state
from tests.helpers.wait import wait_for_app_ready, wait_for_tool_key, wait_until

# Access storage via getattr to satisfy static type checkers (NiceGUI has no typed attr)
app_storage: Any = getattr(ng_app, "storage")


@pytest.mark.integration
async def test_settings_tab_accessible(user: User) -> None:
    """Test that Settings tab is accessible in the control panel.

    Verifies that the Settings tab can be found and clicked to reveal
    the settings content with serial port selection.
    """
    await user.open("/")
    await wait_for_app_ready()

    # Settings is embedded in the control panel (bottom-left HUD)
    # The control panel has tabs: "Joint Jog", "Cartesian Jog", "Settings"
    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    # Verify the Settings tab panel is now showing by checking for expected content
    # The Serial Port section should be visible
    await user.should_see("Serial Port")
    await user.should_see("Show Route")
    await user.should_see("Theme")
    await user.should_see("Tool")
    await user.should_see("Select end effector tool")


@pytest.mark.integration
async def test_serial_port_select_exists(user: User) -> None:
    """Test that the serial port select dropdown exists in Settings.

    Note: The port select auto-saves on change (no Set Port button needed).
    We verify the select element exists with the correct label.
    """
    await user.open("/")
    await wait_for_app_ready()

    # Navigate to Settings tab
    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    # Find the serial port select - it has label="Port"
    port_select = user.find(kind=ui.select, content="Port")
    assert port_select is not None, "Serial port select should exist in Settings"


@pytest.mark.integration
async def test_show_route_toggle_changes_state(user: User) -> None:
    """Test that toggling Show Route updates commander.settings.view.paths_visible."""
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()

    # Navigate to Settings tab
    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    # Get initial state
    initial_visible = waldoctl.commander.settings.view.paths_visible

    # Find and toggle the Show Route switch (by marker, not content)
    show_route_switch = user.find(marker="switch-show-route")
    show_route_switch.click()
    await asyncio.sleep(0)

    # State should have toggled
    assert waldoctl.commander.settings.view.paths_visible != initial_visible, (
        f"Expected paths_visible to toggle from {initial_visible}"
    )


@pytest.mark.integration
async def test_workspace_envelope_mode_changes(user: User) -> None:
    """Test that changing workspace envelope mode updates commander.settings.view.envelope_mode."""

    await user.open("/")
    await wait_for_app_ready()

    # Navigate to Settings tab
    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    # Find the Workspace Envelope select (by marker)
    envelope_select = user.find(marker="select-envelope-mode")
    assert envelope_select is not None, "Envelope mode select should exist"

    import waldoctl
    from waldoctl import EnvelopeMode

    envelope_mode = waldoctl.commander.settings.view.envelope_mode
    assert isinstance(envelope_mode, EnvelopeMode), (
        f"Expected EnvelopeMode, got {envelope_mode}"
    )

    # Drive a real change through the select and verify it propagates to
    # commander.settings.view (select option keys are the EnvelopeMode values).
    select_el = next(iter(envelope_select.elements))

    async def set_and_verify(mode: EnvelopeMode) -> None:
        select_el.set_value(mode.value)
        for _ in range(20):
            await asyncio.sleep(0.1)
            if waldoctl.commander.settings.view.envelope_mode == mode:
                return
        assert waldoctl.commander.settings.view.envelope_mode == mode, (
            f"Envelope mode should be {mode} after selecting {mode.value!r}"
        )

    await set_and_verify(EnvelopeMode.OFF)
    await set_and_verify(EnvelopeMode.ON)


@pytest.mark.integration
async def test_tool_selection_changes_tool(user: User) -> None:
    """Test that selecting a tool updates storage and sends SET_TOOL to backend.

    Cycles through registered tools verifying each selection persists to storage.
    """
    await user.open("/")
    await wait_for_app_ready()

    # Navigate to Settings tab
    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    # The tool select exists (by marker)
    tool_select = user.find(marker="select-tool")
    assert tool_select is not None, "Tool select should exist"

    # Native count stays 5; robot.tools may compose plugin tools on top.
    native_tools = [t.key for t in ui_state.active_robot.native_tools.available]
    assert len(native_tools) == 5, f"Expected 5 native tools, got {native_tools}"
    available_tools = [t.key for t in ui_state.active_robot.tools.available]
    for expected in ("NONE", "PNEUMATIC", "SSG-48", "MSG", "VACUUM"):
        assert expected in available_tools, f"{expected} not in {available_tools}"

    async def select_and_verify(tool: str) -> None:
        select_el.set_value(tool)
        for _ in range(20):
            await asyncio.sleep(0.1)
            if app_storage.general.get("selected_tool") == tool:
                return
        assert app_storage.general.get("selected_tool") == tool, (
            f"Storage should reflect {tool} after selection"
        )

    select_el = next(iter(tool_select.elements))
    await select_and_verify("PNEUMATIC")
    await select_and_verify("SSG-48")
    await select_and_verify("VACUUM")


@pytest.mark.integration
async def test_variant_selector_appears_for_tools_with_variants(user: User) -> None:
    """Test that variant dropdown appears for tools with variants and hides for those without."""
    await user.open("/")
    await wait_for_app_ready()

    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    tool_select = user.find(marker="select-tool")
    select_el = next(iter(tool_select.elements))

    # SSG-48 has variants (finger, pinch) — selector should appear
    select_el.set_value("SSG-48")
    await asyncio.sleep(0.1)
    variant_select = user.find(marker="select-tool-variant")
    assert len(variant_select.elements) == 1, (
        "Variant selector should appear for SSG-48"
    )
    await user.should_see("Variant")

    # NONE has no variants — selector should be disabled but still visible
    select_el.set_value("NONE")
    await asyncio.sleep(0.1)
    variant_select = user.find(marker="select-tool-variant")
    assert len(variant_select.elements) == 1, (
        "Variant selector should still be visible for NONE (but disabled)"
    )


@pytest.mark.integration
async def test_tcp_offset_inputs_appear_for_tools(user: User) -> None:
    """Test that TCP offset inputs appear for non-NONE tools and hide for NONE."""
    await user.open("/")
    await wait_for_app_ready()

    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    tool_select = user.find(marker="select-tool")
    select_el = next(iter(tool_select.elements))

    def offset_x_disabled() -> bool:
        """The tool select rebuilds the offset inputs only after the
        controller confirms the change, so read them once it has."""
        return "disable" in next(iter(user.find(marker="tcp-offset-x").elements)).props

    # PNEUMATIC — offset inputs should appear with X/Y/Z fields
    select_el.set_value("PNEUMATIC")
    await wait_for_tool_key("PNEUMATIC", timeout_s=5.0)
    await user.should_see("TCP Offset")
    assert await wait_until(lambda: not offset_x_disabled()), (
        "a fitted tool's offset is editable"
    )

    # NONE — offset inputs should still be visible, and refuse edits: there
    # is no tool to offset from.
    select_el.set_value("NONE")
    await wait_for_tool_key("NONE", timeout_s=5.0)
    assert await wait_until(offset_x_disabled), (
        "with no tool fitted the offset must not be editable"
    )


@pytest.mark.integration
async def test_tcp_offset_reaches_the_controller_and_survives_a_tool_change(
    user: User,
) -> None:
    """An offset typed into Settings is the offset the controller plans
    with, not a browser-local number: it lands via ``set_tcp_offset`` and
    reads back; a tool change (which resets the controller's offset) gets
    the remembered offset pushed again; and an offset another client set
    is adopted when the page opens instead of being clobbered."""
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    client = ui_state.control_panel.client

    user.find(kind=ui.tab, content="Settings").click()
    await asyncio.sleep(0)
    tool_select = user.find(marker="select-tool")

    async def select_tool(key: str) -> None:
        """Pick a tool and let the change settle: the tool select awaits the
        controller's completion (which zeroes its offset) before rebuilding
        the offset inputs, so an edit made mid-change would be reset."""
        next(iter(tool_select.elements)).set_value(key)
        await wait_for_tool_key(key, timeout_s=5.0)
        await asyncio.sleep(0.3)

    async def controller_offset(expected: list[float]) -> list[float]:
        got: list[float] = []
        for _ in range(50):
            got = [float(v) for v in await client.tcp_offset()]
            if got == expected:
                return got
            await asyncio.sleep(0.1)
        return got

    await select_tool("PNEUMATIC")
    await user.should_see("TCP Offset")
    # The client-side edit event, as NiceGUI names it: the element's own
    # listener adopts the value, the page's listener pushes it.
    user.find(marker="tcp-offset-x").trigger("update:modelValue", 12.5)
    assert await controller_offset([12.5, 0.0, 0.0]) == [12.5, 0.0, 0.0]

    # NONE and back: select_tool zeroes the controller's offset, the page
    # re-applies the remembered one for the re-selected tool.
    await select_tool("NONE")
    assert await controller_offset([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]
    await select_tool("PNEUMATIC")
    assert await controller_offset([12.5, 0.0, 0.0]) == [12.5, 0.0, 0.0]

    # Set out of band (a program, another client), reopen the page: the
    # controller's non-zero offset wins and the inputs show it.
    await client.set_tcp_offset(1.0, 2.0, 3.0)
    assert await controller_offset([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]
    # The old tab's disconnect clears the active slot before the reload.
    ui_state.active_client_id = None
    await user.open("/")
    await wait_for_app_ready()
    user.find(kind=ui.tab, content="Settings").click()
    await asyncio.sleep(0)
    await user.should_see("TCP Offset")
    shown = None
    for _ in range(50):
        shown = next(iter(user.find(marker="tcp-offset-x").elements)).value
        if shown == 1.0:
            break
        await asyncio.sleep(0.1)
    assert shown == 1.0, f"X input shows {shown!r}, controller has 1.0"
    assert await controller_offset([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]


@pytest.mark.integration
async def test_theme_selection_exists(user: User) -> None:
    """Test that theme toggle exists and has expected options."""
    await user.open("/")
    await wait_for_app_ready()

    # Navigate to Settings tab
    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    # The theme toggle should exist
    await user.should_see("Theme")
