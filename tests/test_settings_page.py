"""Tests for settings page functionality."""

import asyncio
from typing import Any

import pytest
from nicegui import app as ng_app
from nicegui.testing import User

from tests.helpers.wait import (
    poll_until,
    wait_for_app_ready,
    wait_for_tool_key,
    wait_until,
)
from waldo_commander.state import ui_state

# Access storage via getattr to satisfy static type checkers (NiceGUI has no typed attr)
app_storage: Any = getattr(ng_app, "storage")


@pytest.mark.integration
async def test_settings_dialog_rows_drive_their_settings(user: User) -> None:
    """The gear opens the Settings dialog with every category's rows built.
    Show Route and the envelope mode reach ``commander.settings.view``; the
    tool select persists each choice, shows a variant row only for a tool
    with variants, and offers a TCP correction for fitted tools and the bare
    flange alike."""
    import waldoctl
    from waldoctl import EnvelopeMode

    await user.open("/")
    await wait_for_app_ready()

    assert not ui_state.settings_content.dialog.value
    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    assert ui_state.settings_content.dialog.value, "the gear opens the dialog"

    # Rows from the first category and the last, so the whole dialog is
    # present rather than just the category on screen.
    await user.should_see("Serial port")
    await user.should_see(marker="select-serial-port")
    await user.should_see("Show route")
    await user.should_see("Tool")
    await user.should_see("Select end effector tool")
    # Categorised, most-reached-for first: the port an operator sets before
    # anything else works leads, and the restart-scoped settings come last.
    await user.should_see(marker="settings-cat-connection")
    await user.should_see(marker="settings-group-connection")
    await user.should_see(marker="settings-cat-advanced")
    await user.should_see(marker="settings-group-advanced")

    initial_visible = waldoctl.commander.settings.view.paths_visible
    user.find(marker="switch-show-route").click()
    await asyncio.sleep(0)
    assert waldoctl.commander.settings.view.paths_visible != initial_visible, (
        f"Expected paths_visible to toggle from {initial_visible}"
    )

    envelope_mode = waldoctl.commander.settings.view.envelope_mode
    assert isinstance(envelope_mode, EnvelopeMode), (
        f"Expected EnvelopeMode, got {envelope_mode}"
    )
    # Select option keys are the EnvelopeMode values.
    envelope_select = next(iter(user.find(marker="select-envelope-mode").elements))
    for mode in (EnvelopeMode.OFF, EnvelopeMode.ON):
        envelope_select.set_value(mode.value)
        await poll_until(
            lambda: waldoctl.commander.settings.view.envelope_mode,
            lambda m, mode=mode: m == mode,
            timeout_s=2.0,
            what=f"envelope mode {mode} after selecting {mode.value!r}",
        )

    # Native count stays 5; robot.tools may compose plugin tools on top.
    native_tools = [t.key for t in ui_state.active_robot.native_tools.available]
    assert len(native_tools) == 5, f"Expected 5 native tools, got {native_tools}"
    available_tools = [t.key for t in ui_state.active_robot.tools.available]
    for expected in ("NONE", "PNEUMATIC", "SSG-48", "MSG", "VACUUM"):
        assert expected in available_tools, f"{expected} not in {available_tools}"

    select_el = next(iter(user.find(marker="select-tool").elements))
    for tool in ("PNEUMATIC", "SSG-48", "VACUUM"):
        select_el.set_value(tool)
        await poll_until(
            lambda: app_storage.general.get("selected_tool"),
            lambda stored, tool=tool: stored == tool,
            timeout_s=2.0,
            what=f"storage to reflect {tool} after selection",
        )

    # SSG-48 has variants (finger, pinch); NONE has none, so it should not
    # occupy a Settings row.
    select_el.set_value("SSG-48")
    await wait_for_tool_key("SSG-48", timeout_s=5)
    await user.should_see("Variant")
    assert len(user.find(marker="select-tool-variant").elements) == 1, (
        "Variant selector should appear for SSG-48"
    )
    select_el.set_value("NONE")
    await wait_for_tool_key("NONE", timeout_s=5)
    await user.should_not_see("Variant")

    def offset_x_disabled() -> bool:
        """The tool select rebuilds the offset inputs only after the
        controller confirms the change, so read them once it has."""
        return "disable" in next(iter(user.find(marker="tcp-offset-x").elements)).props

    select_el.set_value("PNEUMATIC")
    await wait_for_tool_key("PNEUMATIC", timeout_s=5.0)
    await user.should_see("TCP offset")
    await poll_until(
        offset_x_disabled,
        lambda disabled: not disabled,
        what="a fitted tool's offset editable",
    )
    select_el.set_value("NONE")
    await wait_for_tool_key("NONE", timeout_s=5.0)
    await poll_until(
        offset_x_disabled,
        lambda disabled: not disabled,
        what="the bare flange's TCP correction editable",
    )

    user.find(marker="settings-close").click()
    await asyncio.sleep(0)
    assert not ui_state.settings_content.dialog.value


@pytest.mark.integration
async def test_tcp_offset_reaches_the_controller_and_survives_a_tool_change(
    user: User,
    monkeypatch,
) -> None:
    """An offset typed into Settings is the offset the controller plans
    with, not a browser-local number: it lands via ``set_tcp_offset`` and
    reads back; a tool change (which resets the controller's offset) gets
    the remembered offset pushed again; and an offset another client set
    is adopted when the page opens instead of being clobbered."""
    from waldo_commander.services import tcp_calibration
    from waldo_commander.state import ui_state

    confirmed = asyncio.Event()
    release_readback = asyncio.Event()
    apply = tcp_calibration.apply_tcp_calibration

    async def held_readback(client, calibration, **kwargs):
        result = await apply(client, calibration, **kwargs)
        if calibration.values[0] == 12.5 and not confirmed.is_set():
            confirmed.set()
            await release_readback.wait()
        return result

    monkeypatch.setattr(tcp_calibration, "apply_tcp_calibration", held_readback)

    await user.open("/")
    await wait_for_app_ready()
    client = ui_state.control_panel.client

    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    tool_select = user.find(marker="select-tool")

    def offset_x():
        """The X input as it stands now — a tool change replaces the row."""
        return next(iter(user.find(marker="tcp-offset-x").elements))

    async def select_tool(key: str) -> None:
        """Pick a tool and let the change settle: the tool select awaits the
        controller's completion (which zeroes its offset) before rebuilding
        the offset inputs, so an edit made mid-change would be reset."""
        replaced = offset_x()
        next(iter(tool_select.elements)).set_value(key)
        await wait_for_tool_key(key, timeout_s=5.0)
        await poll_until(
            offset_x,
            lambda el: el is not replaced,
            timeout_s=5.0,
            what=f"the offset inputs rebuilt for {key}",
        )

    async def expect_controller_offset(expected: list[float]) -> None:
        await poll_until(
            client.tcp_offset,
            lambda got: [float(v) for v in got] == expected,
            timeout_s=5.0,
            what=f"controller TCP offset {expected}",
        )

    try:
        await select_tool("PNEUMATIC")
        await user.should_see("TCP offset")
        # The client-side edit event, as NiceGUI names it: the element's own
        # listener adopts the value, the page's listener pushes it.
        user.find(marker="tcp-offset-x").trigger("update:modelValue", 12.5)
        await expect_controller_offset([12.5, 0.0, 0.0])

        # NONE and back: select_tool zeroes the controller's offset, the page
        # re-applies the remembered one for the re-selected tool.
        await asyncio.wait_for(confirmed.wait(), timeout=5)
        next(iter(tool_select.elements)).set_value("NONE")
        await asyncio.sleep(0)
        release_readback.set()
        await wait_for_tool_key("NONE", timeout_s=5)
        await expect_controller_offset([0.0, 0.0, 0.0])
        await select_tool("PNEUMATIC")
        await expect_controller_offset([12.5, 0.0, 0.0])

        # A program's tool changes must update Settings without restoring
        # browser offsets or sending another tool-selection command.
        for key in ("NONE", "PNEUMATIC"):
            index = await client.select_tool(key)
            assert await client.wait_command(index, timeout=5)
            await wait_for_tool_key(key, timeout_s=5)
            await poll_until(
                lambda: next(iter(tool_select.elements)).value,
                lambda shown, expected=key: shown == expected,
                timeout_s=5,
                what=f"Settings adopting {key} from the controller",
            )
            await poll_until(
                lambda: offset_x().value,
                lambda shown: shown == 0.0,
                timeout_s=5,
                what="Settings adopting the controller's reset TCP",
            )
            await expect_controller_offset([0.0, 0.0, 0.0])

        # Set out of band (a program, another client), reopen the page: the
        # controller's offset wins and the inputs show it.
        await client.set_tcp_offset(1.0, 2.0, 3.0)
        await expect_controller_offset([1.0, 2.0, 3.0])
        # The old tab's disconnect clears the active slot before the reload.
        ui_state.active_client_id = None
        await user.open("/")
        await wait_for_app_ready()
        user.find(marker="tab-settings").click()
        await asyncio.sleep(0)
        await user.should_see("TCP offset")
        await poll_until(
            lambda: offset_x().value,
            lambda shown: shown == 1.0,
            timeout_s=5.0,
            what="the X input showing the controller's 1.0",
        )
        await expect_controller_offset([1.0, 2.0, 3.0])
    finally:
        release_readback.set()
        # The fake-serial controller is shared with every later test, and
        # nothing resets it between them: a tool fitted and a shifted TCP
        # would move their robot too.
        await client.set_tcp_offset(0.0, 0.0, 0.0)
        await client.select_tool("NONE")


@pytest.mark.integration
async def test_settings_follows_controller_variants_and_setup_applied_tcp(
    user: User,
) -> None:
    """Settings binds TCP edits to the tool the controller actually carries,
    and follows a variant another client selected."""
    await user.open("/")
    await wait_for_app_ready()
    client = ui_state.control_panel.client
    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)

    def shown(marker: str):
        return next(iter(user.find(marker=marker).elements))

    async def completed(index: int) -> None:
        assert index >= 0 and await client.wait_command(index, timeout=5)

    async def expect_transform(expected: list[float]) -> None:
        await poll_until(
            client.tcp_transform,
            lambda got: [round(float(v), 3) for v in got] == expected,
            timeout_s=5.0,
            what=f"controller TCP transform {expected}",
        )

    try:
        # A program fits a tool that has variants without naming one.
        await completed(await client.select_tool("SSG-48"))
        await wait_for_tool_key("SSG-48", timeout_s=5)
        await poll_until(
            lambda: shown("select-tool").value,
            lambda v: v == "SSG-48",
            timeout_s=5,
            what="Settings adopting SSG-48",
        )
        await user.should_see("TCP offset")
        user.find(marker="tcp-offset-x").trigger("update:modelValue", 3.0)
        await expect_transform([3.0, 0.0, 0.0, 0.0, 0.0, 0.0])

        # Variants selected elsewhere are followed, not just tool keys.
        await completed(await client.select_tool("PNEUMATIC", variant_key="horizontal"))
        await wait_for_tool_key("PNEUMATIC", timeout_s=5)
        await poll_until(
            lambda: shown("select-tool-variant").value,
            lambda v: v == "horizontal",
            timeout_s=5,
            what="the variant select adopting horizontal",
        )
        await completed(await client.select_tool("PNEUMATIC", variant_key="vertical"))
        await poll_until(
            lambda: shown("select-tool-variant").value,
            lambda v: v == "vertical",
            timeout_s=5,
            what="the variant select adopting vertical",
        )
        assert app_storage.general.get("tool_variant_PNEUMATIC") == "vertical"
    finally:
        await client.set_tcp_transform()
        await client.select_tool("NONE")


@pytest.mark.integration
async def test_program_tcp_change_survives_a_settings_nudge(user: User) -> None:
    """A transform set outside Settings -- by another client, or by a program
    on the same tool -- is where the next nudge starts from, and a finished
    run shows it in the inputs."""
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running

    await user.open("/")
    await wait_for_app_ready()
    client = ui_state.control_panel.client

    def shown(marker: str):
        return next(iter(user.find(marker=marker).elements))

    async def expect_transform(expected: list[float]) -> None:
        await poll_until(
            client.tcp_transform,
            lambda got: [round(float(v), 3) for v in got] == expected,
            timeout_s=5.0,
            what=f"controller TCP transform {expected}",
        )

    try:
        index = await client.select_tool("NONE")
        assert index >= 0 and await client.wait_command(index, timeout=5)
        user.find(marker="tab-settings").click()
        await user.should_see("TCP offset")

        index = await client.set_tcp_transform(10.0, 0.0, 0.0, 0.0, 0.0, 30.0)
        assert index >= 0 and await client.wait_command(index, timeout=5)
        user.find(marker="tcp-offset-y").trigger("update:modelValue", 1.0)
        await expect_transform([10.0, 1.0, 0.0, 0.0, 0.0, 30.0])

        user.find(marker="tab-program").click()
        await asyncio.sleep(0)
        textarea = ui_state.active_textarea
        assert textarea is not None
        textarea.value = (
            "from parol6 import RobotClient\n"
            "with RobotClient() as rbt:\n"
            "    index = rbt.set_tcp_transform(20.0, 1.0, 0.0, 0.0, 0.0, 45.0)\n"
            "    assert rbt.wait_command(index, timeout=5)\n"
        )
        await script_exec.start()
        async with asyncio.timeout(30):
            while is_any_program_running():
                await asyncio.sleep(0.05)
        assert script_exec.last_exit_code == 0

        user.find(marker="tab-settings").click()
        await poll_until(
            lambda: [
                round(float(shown(f"tcp-offset-{axis}").value), 3)
                for axis in ("x", "yaw")
            ],
            lambda values: values == [20.0, 45.0],
            timeout_s=5.0,
            what="Settings showing the transform the program set",
        )
        user.find(marker="tcp-offset-z").trigger("update:modelValue", 2.0)
        await expect_transform([20.0, 1.0, 2.0, 0.0, 0.0, 45.0])
    finally:
        await client.set_tcp_transform()


async def _reconciled() -> None:
    """Wait for every page's offset reconcile to finish, pushes included."""
    from nicegui import background_tasks

    assert await wait_until(
        lambda: not any(
            t.get_name() == "tcp-offset-reconcile"
            for t in background_tasks.running_tasks
        ),
        timeout_s=10.0,
    ), "the TCP offset reconcile never finished"


async def _reopen(user: User) -> None:
    # The old tab's disconnect clears the active slot before the reload.
    ui_state.active_client_id = None
    await user.open("/")
    await wait_for_app_ready()


@pytest.mark.integration
async def test_opening_a_page_adopts_only_what_is_safe(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page load carries no human intent to drive. With an AI session
    holding control, the browser's remembered offset must not replace the
    controller's, even on a controller this app has never told: the AI plans
    its Cartesian moves with the TCP it has. And the controller's offset
    belongs to the tool it carries: a page that remembers a different tool
    must not file that offset under its own."""
    from waldo_commander.components import settings
    from waldo_commander.services.control_lease import MCP, control_lease

    await user.open("/")
    await wait_for_app_ready()
    client = ui_state.control_panel.client
    monkeypatch.setattr(settings, "_pushed_offset_tools", set())
    try:
        index = await client.select_tool("PNEUMATIC")
        assert await client.wait_command(index, timeout=5.0)
        await client.set_tcp_offset(0.0, 0.0, 0.0)
        app_storage.general["selected_tool"] = "PNEUMATIC"
        app_storage.general["tcp_offset_PNEUMATIC"] = {"x": 5.0, "y": 0.0, "z": 0.0}
        control_lease.seize(MCP, "settings-review", "AI")

        await _reopen(user)
        await _reconciled()

        assert [float(v) for v in await client.tcp_offset()] == [0.0, 0.0, 0.0]
        assert control_lease.held_by(MCP, "settings-review")
        control_lease.reset()

        index = await client.select_tool("SSG-48")
        assert await client.wait_command(index, timeout=5.0)
        await client.set_tcp_offset(1.0, 2.0, 3.0)
        await poll_until(
            client.tcp_offset,
            lambda got: [float(v) for v in got] == [1.0, 2.0, 3.0],
            what="the SSG-48 offset on the controller",
        )
        app_storage.general["selected_tool"] = "PNEUMATIC"
        app_storage.general["tcp_offset_PNEUMATIC"] = {"x": 0.0, "y": 0.0, "z": 0.0}

        await _reopen(user)
        await _reconciled()

        assert app_storage.general["tcp_offset_PNEUMATIC"] == {
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
        }
        assert [float(v) for v in await client.tcp_offset()] == [1.0, 2.0, 3.0]
    finally:
        control_lease.reset()
        await client.set_tcp_offset(0.0, 0.0, 0.0)
        await client.select_tool("NONE")


@pytest.mark.integration
async def test_an_edit_queued_when_the_page_goes_is_dropped(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edits go out one at a time, newest last. One still waiting when its
    page disconnects has nobody left to see it land, so it never does."""
    page = await user.open("/")
    await wait_for_app_ready()
    client = ui_state.control_panel.client
    content = ui_state.settings_content
    assert content is not None

    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    replaced = next(iter(user.find(marker="tcp-offset-x").elements))
    next(iter(user.find(marker="select-tool").elements)).set_value("PNEUMATIC")
    await wait_for_tool_key("PNEUMATIC", timeout_s=5.0)
    await poll_until(
        lambda: next(iter(user.find(marker="tcp-offset-x").elements)),
        lambda el: el is not replaced,
        what="the offset inputs rebuilt for PNEUMATIC",
    )
    await _reconciled()

    gate = asyncio.Event()
    real_set = client.set_tcp_transform

    async def held_set(*values: float) -> int:
        await gate.wait()
        return await real_set(*values)

    monkeypatch.setattr(client, "set_tcp_transform", held_set)
    try:
        user.find(marker="tcp-offset-x").trigger("update:modelValue", 5.0)
        assert await wait_until(lambda: content._tcp_pushing), "the first edit went out"
        user.find(marker="tcp-offset-x").trigger("update:modelValue", 7.0)
        assert await wait_until(lambda: content._tcp_push_next is not None), (
            "the second edit waits behind the first"
        )

        page.handle_disconnect(next(iter(page._socket_to_document_id)))
        gate.set()
        assert await wait_until(lambda: not content._tcp_pushing), (
            "the push loop never finished"
        )
        assert [float(v) for v in await client.tcp_offset()] == [5.0, 0.0, 0.0]
    finally:
        gate.set()
        await client.set_tcp_offset(0.0, 0.0, 0.0)
        await client.select_tool("NONE")
