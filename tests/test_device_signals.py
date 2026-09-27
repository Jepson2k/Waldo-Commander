"""Configure channels and exercise actual I/O, timeout, cancellation and preview."""

import asyncio
import socket
import textwrap
import time

import pytest
import waldoctl
from nicegui import ui
from nicegui.testing import User
from parol6 import config as parol6_config
from parol6.client.async_client import AsyncRobotClient
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl import ActionState
from waldoctl.signals import DigitalSignal
from waldoctl.skills import MissingCapability, SkillError, UnresolvedPreview

from tests.helpers.wait import (
    wait_for_app_ready,
    enable_sim,
    ensure_robot_ready_for_motion,
    poll_until,
)
from tests.test_editor_integration import _set_selection
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.services.skill_library import call_source, library
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
        # Same total, moved boundary: channel 0 of three outputs is input 1 here.
        moved_boundary = DigitalSignal("parol6", "output", 0, 1, 3)
        with pytest.raises(ValueError, match="layout"):
            await read_signal.async_call(client, moved_boundary)
        with pytest.raises(ValueError, match="layout"):
            await write_signal.async_call(client, moved_boundary, True)
        assert (await client.io())[2] == 0
        await enable_sim(user)
        await ensure_robot_ready_for_motion()
        from waldo_commander.state import ui_state
        from waldo_commander.services.path_visualizer import path_visualizer

        user.find(marker="tab-program").click()
        textarea = ui_state.active_textarea
        assert textarea is not None
        # The call the editor generates for a saved mapping, run as a selection.
        entries, _ = library(ui_state.active_robot)
        snippet = call_source(
            entries["waldo.write_signal"],
            {
                "signal": SetupStore(tmp_path).load("cell").signals["valve"],
                "value": True,
            },
        )
        textarea.value = (
            "from parol6 import RobotClient\nwith RobotClient() as rbt:\n"
            + textwrap.indent(snippet, "    ")
            + "\n"
        )
        await asyncio.sleep(0)
        program = waldoctl.commander.programs.active
        assert program is not None and "DigitalSignal(**" in program.source
        error = await path_visualizer.update_path_visualization(
            program.source, tab_id=program.id
        )
        assert (
            error is not None
            and "Named signals need an explicit SignalFixture" in error
        )
        call = next(
            number
            for number, line in enumerate(str(textarea.value).split("\n"), start=1)
            if "_skill_waldo_write_signal(" in line
        )
        _set_selection(textarea, call, call)
        await asyncio.sleep(0)
        user.find(marker="editor-run-selection").click()
        editor = ui_state.editor_panel
        async with asyncio.timeout(30):
            await asyncio.sleep(0.1)
            while editor._running_selection or is_any_program_running():
                await asyncio.sleep(0.05)
        from waldo_commander.components.script_execution import script_exec

        assert script_exec.last_exit_code == 0, "\n".join(
            entry.text for entry in waldoctl.commander.programs.active.log.entries
        )
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


@pytest.mark.integration
async def test_a_signal_wait_never_matches_the_frame_it_already_holds(user: User):
    """At 1 Hz the frame a client holds can be a second old; a wait must not
    report the level it shows as a match."""
    await user.open("/")
    await wait_for_app_ready()
    client = waldoctl.commander.client
    signal = DigitalSignal("parol6", "output", 0, 2, 2)
    rate = await client.status_rate()
    assert rate is not None
    try:
        await write_signal.async_call(client, signal, True)
        assert await client.set_status_rate(1.0) > 0
        evaluated = 0

        def second_fresh_frame(_status) -> bool:
            # The first evaluation is the held frame; a frame sent before the
            # rate changed can still follow it.
            nonlocal evaluated
            evaluated += 1
            return evaluated == 3

        assert await client.wait_status(second_fresh_frame, timeout=4)
        # A second before the next frame: the output drops while the held
        # frame still shows it high.
        assert await client.write_io(0, 0) >= 0
        await poll_until(
            client.io,
            lambda levels: levels is not None and levels[2] == 0,
            timeout_s=0.5,
            interval=0.01,
            what="output 0 low",
        )
        try:
            result = await wait_signal.async_call(client, signal, True, timeout=0.3)
        except ConnectionError:
            pass
        else:
            assert result.outcome == "timeout" and not result.observation.value, (
                "the wait matched a level from before the write"
            )
    finally:
        await client.set_status_rate(rate.hz)
        await client.write_io(0, 0)


@pytest.mark.integration
async def test_a_write_stuck_in_the_queue_stops_and_a_held_step_is_not_charged(
    user: User,
):
    """An output write that cannot run within its budget is stopped rather
    than left to land later; time an operator holds a program at a step is
    not the write's budget."""
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    client = waldoctl.commander.client
    signal = DigitalSignal("parol6", "output", 0, 2, 2)
    try:
        await write_signal.async_call(client, signal, True)
        delay = await client.delay(10)
        assert await client.wait_status(lambda s: s.executing_index == delay, timeout=3)
        with pytest.raises(SkillError, match="Digital output completion"):
            await write_signal.async_call(client, signal, True, timeout=0.5)
        assert await client.queue() == [], "the unconfirmed write is still queued"

        user.find(marker="tab-program").click()
        await asyncio.sleep(0)
        textarea = ui_state.active_textarea
        assert textarea is not None
        entries, _ = library(ui_state.active_robot)
        snippet = call_source(
            entries["waldo.write_signal"],
            {"signal": signal, "value": False, "timeout": 0.5},
        )
        textarea.value = (
            "from parol6 import RobotClient\nwith RobotClient() as rbt:\n"
            + textwrap.indent(snippet, "    ")
            + "\n"
        )
        await asyncio.sleep(0)
        await script_exec.start(paused=True)
        await poll_until(
            client.io,
            lambda levels: levels is not None and levels[2] == 0,
            timeout_s=15,
            what="the program's write reaching the output",
        )
        # Held at the step after the write for longer than the write's budget.
        held_until = time.monotonic() + 1.0
        while time.monotonic() < held_until:
            assert is_any_program_running()
            await asyncio.sleep(0.1)
        await script_exec.signal_play()
        async with asyncio.timeout(15):
            while is_any_program_running():
                await asyncio.sleep(0.05)
        assert script_exec.last_exit_code == 0, "\n".join(
            entry.text for entry in waldoctl.commander.programs.active.log.entries
        )
    finally:
        if is_any_program_running():
            await script_exec.stop()
        await client.stop()
        await client.write_io(0, 0)


@pytest.mark.integration
async def test_signal_panel_keeps_edits_across_selection_and_records_its_writes(
    user: User, tmp_path, monkeypatch
):
    """Switching the shown mapping keeps its edit, a write is refused while a
    program holds the robot, and a confirmed write joins a recording."""
    from waldoctl.setup import SetupSnapshot

    from waldo_commander.services.motion_guard import PROGRAM, motion_guard
    from waldo_commander.services.motion_recorder import motion_recorder

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    clamp = DigitalSignal("parol6", "output", 1, 2, 2)
    SetupStore(tmp_path).save(
        "bench",
        SetupSnapshot(
            signals={
                "valve": DigitalSignal("parol6", "output", 0, 2, 2),
                "clamp": clamp,
            }
        ),
    )
    await user.open("/")
    await wait_for_app_ready()
    client = waldoctl.commander.client

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    try:
        await client.write_io(1, 0)
        user.find(marker="tab-setup").click()
        user.find(marker="setup-load").click()
        await user.should_see("Loaded bench")
        user.find(kind=ui.tab, content="Signals").click()
        element("signal-existing").set_value("valve")
        await asyncio.sleep(0)
        element("signal-active-high").set_value(False)
        element("signal-existing").set_value("clamp")
        await asyncio.sleep(0)
        assert element("signal-index").value == 1
        assert element("setup-dirty").visible, "the kept edit is still unsaved"
        user.find(marker="setup-save").click()
        await user.should_see("Saved bench")
        saved = SetupStore(tmp_path).load("bench").signals
        assert not saved["valve"].active_high and saved["clamp"] == clamp

        element("signal-output-value").set_value(True)
        with motion_guard.reserve(PROGRAM):
            user.find(marker="signal-write").click()
            await user.should_see("A program is moving the robot")
        assert (await client.io())[3] == 0

        program = waldoctl.commander.programs.active
        assert program is not None
        motion_recorder.toggle_recording()
        before = program.source
        user.find(marker="signal-write").click()
        await user.should_see("Controller reports logical output: True", retries=30)
        assert "rbt.write_io(1, 1)" not in before
        assert "rbt.write_io(1, 1)" in program.source
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()
        await client.write_io(1, 0)


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
