"""Configure channels and exercise actual I/O, timeout, cancellation and preview."""

import asyncio
import socket

import pytest
import waldoctl
from nicegui import ui
from nicegui.testing import User
from parol6 import config as parol6_config
from parol6.client.async_client import AsyncRobotClient
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl import ActionState
from waldoctl.signals import DigitalSignal
from waldoctl.skills import MissingCapability, UnresolvedPreview

from tests.helpers.wait import (
    wait_for_app_ready,
    enable_sim,
    ensure_robot_ready_for_motion,
)
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.setup import SetupStore, export_snapshot
from waldo_commander.skills.signals import (
    SignalFixture,
    read_signal,
    wait_signal,
    write_signal,
)


@pytest.mark.integration
async def test_saved_named_output_readback_wait_cancellation_and_disconnection(
    user: User, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    await user.open("/")
    await wait_for_app_ready()
    client = waldoctl.commander.client

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    try:
        await client.write_io(0, 0)
        user.find(marker="tab-setup").click()
        user.find(kind=ui.tab, content="Signals").click()
        element("signal-name").set_value("valve")
        element("signal-direction").set_value("output")
        element("signal-active-high").set_value(False)
        before = await client.io()
        user.find(marker="signal-set").click()
        await user.should_see("Mapping added to the setup; Save to persist it.")
        user.find(marker="setup-save").click()
        await user.should_see("Saved bench")
        setup = SetupStore(tmp_path).load("bench")
        signal = setup.signals["valve"]
        assert await client.io() == before, "saving a mapping wrote an output"
        exported = {}
        exec(export_snapshot(setup), exported)
        assert exported["setup"].signals["valve"].decode([0, 0, 0, 0, 1])

        user.find(marker="signal-read").click()
        await user.should_see("Observed logical value: True", retries=30)
        user.find(marker="signal-write").click()
        await user.should_see("Controller reports logical output: False", retries=30)
        assert (await client.io())[2] == 1
        observation = await write_signal.async_call(client, signal, True)
        assert observation.value and observation.source == "controller"
        assert (await client.io())[2] == 0
        result = await wait_signal.async_call(client, signal, False, timeout=0.2)
        assert result.outcome == "timeout" and result.observation.value
        # The stream is the source: a level set while the wait is running is
        # seen on the tick it is broadcast, without a poll interval to cross.
        waiting = asyncio.create_task(
            wait_signal.async_call(client, signal, False, timeout=5)
        )
        await asyncio.sleep(0)
        await write_signal.async_call(client, signal, False)
        matched = await waiting
        assert matched.outcome == "matched" and not matched.observation.value
        assert matched.elapsed_s < 1.0
        await write_signal.async_call(client, signal, True)

        delay = await client.delay(10)
        assert await client.wait_status(lambda s: s.executing_index == delay, timeout=3)
        waiting = asyncio.create_task(
            wait_signal.async_call(client, signal, False, timeout=5)
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert await client.wait_status(
            lambda s: s.action_state == ActionState.IDLE, timeout=3
        ), "cancelled signal wait did not request native stop"

        # Rebinding a mapping saved for another robot is an unsaved edit, and
        # no widget value changes when it happens — so without the panel being
        # told, Load discards the rebinding without asking.
        from waldoctl.setup import SetupSnapshot

        SetupStore(tmp_path).save(
            "cell",
            SetupSnapshot(signals={"valve": DigitalSignal("par6", "output", 0, 2, 2)}),
        )
        user.find(marker="tab-setup").click()
        user.find(kind=ui.tab, content="Signals").click()
        element("setup-name").set_value("cell")
        user.find(marker="setup-load").click()
        await user.should_see("Loaded cell")
        await user.should_not_see(marker="setup-dirty")
        user.find(marker="signal-bind").click()
        await asyncio.sleep(0)
        await user.should_see(marker="setup-dirty")
        user.find(marker="setup-save").click()
        await user.should_see("Saved cell")
        await user.should_not_see(marker="setup-dirty")
        assert SetupStore(tmp_path).load("cell").signals["valve"].backend == "parol6"

        wrong_layout = DigitalSignal("parol6", "output", 0, 3, 2)
        with pytest.raises(ValueError, match="layout"):
            await write_signal.async_call(client, wrong_layout, True)
        assert (await client.io())[2] == 0
        await enable_sim(user)
        await ensure_robot_ready_for_motion()
        from waldo_commander.state import ui_state
        from waldo_commander.services.path_visualizer import path_visualizer

        user.find(marker="tab-program").click()
        assert ui_state.active_textarea is not None
        ui_state.active_textarea.value = (
            "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
        )
        user.find(marker="tab-skills").click()
        element("skill-choice").set_value("waldo.write_signal")
        user.find(marker="skill-insert").click()
        await user.should_see("Inserted Python skill call")
        program = waldoctl.commander.programs.active
        assert program is not None and "DigitalSignal(**" in program.source
        error = await path_visualizer.update_path_visualization(
            program.source, tab_id=program.id
        )
        assert (
            error is not None
            and "Named signals need an explicit SignalFixture" in error
        )
        user.find(marker="skill-run").click()
        await user.should_see("Skill completed", retries=300)
        assert (await client.io())[2] == 1, (
            "generated Python did not write the selected mapping"
        )
        await write_signal.async_call(client, signal, True)
        with pytest.raises(MissingCapability):
            await read_signal.async_call(
                client, DigitalSignal("par6", "input", 0, 2, 2)
            )
        # A controller that broadcasts nothing is lost communication, not a
        # level that never arrived: the wait has no observation to report.
        quiet_ports = []
        for _ in range(2):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", 0))
                quiet_ports.append(probe.getsockname()[1])
        monkeypatch.setattr(parol6_config, "MCAST_PORT", quiet_ports[1])
        absent = AsyncRobotClient(port=quiet_ports[0], timeout=0.02, retries=0)
        try:
            with pytest.raises(ConnectionError):
                await wait_signal.async_call(absent, signal, True, timeout=0.3)
        finally:
            await absent.close()
    finally:
        await client.stop()
        await client.write_io(0, 0)


def test_preview_uses_explicit_signal_values_and_preserves_timeout_branches():
    preview = PathPreviewClient(DryRunRobotClient)
    signal = DigitalSignal("parol6", "output", 0, 2, 2)
    with pytest.raises(UnresolvedPreview):
        read_signal(preview, signal)
    observation = write_signal(preview, signal, True, fixture=SignalFixture(True))
    assert observation.value and observation.source == "fixture"
    elapsed = preview.sim_time_s
    result = wait_signal(preview, signal, True, timeout=4, fixture=SignalFixture(False))
    assert result.outcome == "timeout" and not result.observation.value
    assert preview.sim_time_s - elapsed == pytest.approx(4)
