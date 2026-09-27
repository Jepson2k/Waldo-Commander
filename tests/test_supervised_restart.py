"""Restart entry discovery must not execute module initialization or old locals."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import waldoctl
from nicegui.testing import User

from waldo_commander.services.supervised_restart import discover_entries, execute_entry


def test_entries_execute_in_fresh_globals_and_do_not_run_the_main_sequence(tmp_path):
    source = """values = []
def before_restart():
    values.append(0)
def first():
    values.append(1)
    return len(values)
async def second():
    values.append(2)
    return values
def _helper():
    return 3
def needs(part):
    return part
def steps():
    yield 1
if __name__ == "__main__":
    raise RuntimeError("The whole original program was replayed")
"""
    assert [e.name for e in discover_entries(source)] == ["first", "second"]
    # The hook runs first, in the same fresh globals, on every restart.
    assert execute_entry(source, "program.py", "first") == 2
    assert execute_entry(source, "program.py", "first") == 2
    assert execute_entry(source, "program.py", "second") == [0, 2]
    for name in ("missing", "_helper", "needs", "steps", "before_restart"):
        with pytest.raises(ValueError, match="restart from"):
            execute_entry(source, "program.py", name)
    with pytest.raises(ValueError, match="before_restart"):
        discover_entries("def before_restart(part): pass\n" + source)
    assert discover_entries("from missing_dependency import anything\n" + source)
    marker = tmp_path / "initialization-ran"
    for initialization in (
        f"open({str(marker)!r}, 'w').write('ran')",
        f"def helper(value=open({str(marker)!r}, 'w').write('ran')): pass",
        f"def helper(value: open({str(marker)!r}, 'w').write('ran')): pass",
        f"value: open({str(marker)!r}, 'w').write('ran') = 1",
    ):
        with pytest.raises(ValueError, match="initialization"):
            execute_entry(initialization + "\n" + source, "program.py", "first")
        assert not marker.exists()

    # A skill binds its name to a Skill that needs a client, so it is not a
    # place to restart from; the plain function beside it is.
    with_skill = """from waldoctl.skills import skill
@skill(id="demo.after_pick", version="1.0.0")
def after_pick(rbt):
    return 1
def main():
    return 2
"""
    assert [e.name for e in discover_entries(with_skill)] == ["main"]


@pytest.mark.integration
async def test_supervised_restart_selects_a_fresh_entry_and_refuses_changed_state(
    user: User,
    tmp_path,
    monkeypatch,
):
    from tests.helpers.wait import (
        enable_sim,
        ensure_robot_ready_for_motion,
        wait_for_app_ready,
    )
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.services.run_records import load_record
    from waldo_commander.services.supervised_restart import fresh_state, source_digest
    from waldo_commander.state import ui_state

    monkeypatch.setenv("WALDO_RUN_RECORD_DIR", str(tmp_path / "runs"))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    assert ui_state.active_textarea is not None
    marker = tmp_path / "entries.txt"
    source = f'''import os
from parol6 import RobotClient, AsyncRobotClient
values = []
def before_restart():
    values.append(1)
def after_pick():
    """Continue after checking the held part."""
    values.append(1)
    with RobotClient() as rbt:
        joints = rbt.angles()
        joints[0] += 3
        rbt.move_j(joints, duration=0.5, timeout=15)
    with open({str(marker)!r}, 'a') as log:
        log.write(f'sync:{{len(values)}}\\n')
async def after_place():
    values.append(1)
    async with AsyncRobotClient() as rbt:
        joints = await rbt.angles()
        joints[0] -= 3
        await rbt.move_j(joints, duration=0.5, timeout=15)
    with open({str(marker)!r}, 'a') as log:
        log.write(f'async:{{len(values)}}\\n')
if __name__ == '__main__':
    if os.environ.get('WALDO_STEP_SESSION'):
        raise RuntimeError('Interrupted original sequence')
'''

    async def finished():
        async with asyncio.timeout(25):
            while is_any_program_running():
                await asyncio.sleep(0.05)

    ui_state.active_textarea.value = source
    program = waldoctl.commander.programs.active
    assert program is not None
    script_exec.record_runs = True
    assert await script_exec.start()
    await finished()
    assert script_exec.last_exit_code == 1
    assert not marker.exists()
    before = await client.angles()
    initial_state = await fresh_state(client)
    initial_state.require_ready()
    user.find(marker="editor-more-btn").click()
    user.find(marker="editor-restart-btn").click()
    await user.should_see("Previous run: failed · same source")
    await user.should_see("Controller ready · simulator", retries=50)
    assert not marker.exists()
    user.find(marker="restart-physical-confirmation").click()
    from waldo_commander.services.control_lease import BROWSER, MCP, control_lease

    control_lease.seize(MCP, "restart-review", "Review MCP")
    user.find(marker="restart-start").click()
    async with asyncio.timeout(25):
        while not marker.exists():
            await asyncio.sleep(0.05)
    await finished()
    assert script_exec.last_exit_code == 0
    # The entry's commands are counted from its own start, not the plan's.
    assert program.dry_run.playback.executing_command == -1, (
        "a restart highlighted a command of the full program"
    )
    assert control_lease.held_by(BROWSER, ui_state.active_client_id)
    await user.should_see("You've taken control from the AI")
    assert marker.read_text() == "sync:2\n"
    assert (await client.angles())[0] == pytest.approx(before[0] + 3, abs=0.1)
    events = load_record(script_exec.last_record)
    assert any(e["event"] == "restart_selected" for e in events)
    assert any(
        e["event"] == "entry_returned" and e["method"] == "after_pick" for e in events
    )

    async def selected(entry, reference):
        return await script_exec.start(
            restart_entry=entry,
            restart_reference=reference,
            reviewed_source_digest=source_digest(source),
        )

    script_exec.record_runs = False
    reference = await fresh_state(client)
    assert await selected("after_place", reference)
    await finished()
    assert script_exec.last_exit_code == 0
    assert marker.read_text() == "sync:2\nasync:2\n"
    assert (await client.angles())[0] == pytest.approx(before[0], abs=0.1)

    reference = await fresh_state(client)
    assert await client.set_status_rate(1) > 0
    launch = asyncio.create_task(selected("after_pick", reference))
    try:
        async with asyncio.timeout(3):
            while not is_any_program_running():
                await asyncio.sleep(0)
        await script_exec.stop()
        assert not await launch
        assert marker.read_text() == "sync:2\nasync:2\n"
    finally:
        if is_any_program_running():
            await script_exec.stop()
        await client.set_status_rate(20)

    # A reviewed state cannot survive a mode switch, TCP edit, stale source,
    # pending command, or loss of reference. Refusals never launch the entry.
    reference = await fresh_state(client)
    assert reference.simulator_active
    assert not await selected(
        "after_pick", replace(reference, simulator_active=False)
    ), "a review of the other mode authorized this one"
    tcp = await client.tcp_transform()
    changed = list(tcp)
    changed[0] += 1
    assert await client.set_tcp_transform(*changed) > 0
    outcome_before = script_exec.last_outcome
    record_before = script_exec.last_record
    log_before = [entry.text for entry in program.log.entries]
    assert not await selected("after_pick", reference)
    # A refused restart leaves the interrupted run's context for review.
    assert script_exec.last_outcome == outcome_before
    assert script_exec.last_record == record_before
    assert [entry.text for entry in program.log.entries] == log_before
    assert await client.set_tcp_transform(*tcp) > 0
    reference = await fresh_state(client)
    ui_state.active_textarea.value = source + "\n# Edited after review\n"
    assert not await selected("after_pick", reference)
    ui_state.active_textarea.value = source
    assert await client.pause() > 0
    await client.delay(20)
    assert not await selected("after_pick", reference)
    assert await client.stop() > 0
    assert await client.resume() > 0
    await client.reset_state()
    assert not await selected("after_pick", reference)
    assert marker.read_text() == "sync:2\nasync:2\n"
    assert not is_any_program_running()

    # A lost queue readback is not an empty queue.
    from unittest.mock import AsyncMock

    with monkeypatch.context() as patched:
        patched.setattr(client, "queue", AsyncMock(return_value=None))
        with pytest.raises(ConnectionError):
            await fresh_state(client)

    # Ordinary Start ignores even an inherited entry setting.
    monkeypatch.setenv("WALDO_RESTART_ENTRY", "after_pick")
    ui_state.active_textarea.value = "print('ordinary beginning')"
    await ensure_robot_ready_for_motion()
    assert await script_exec.start()
    await finished()
    assert script_exec.last_exit_code == 0


@pytest.mark.integration
async def test_a_restart_in_preflight_is_one_launch_and_stop_cancels_it(
    user: User, tmp_path, monkeypatch, caplog
):
    """Between Start and the subprocess a restart reads the controller's
    state. Nothing was marked running then, so a second Start launched a
    second program and Stop answered that nothing was running."""
    from tests.helpers.wait import (
        enable_sim,
        ensure_robot_ready_for_motion,
        wait_for_app_ready,
    )
    from waldo_commander.components import script_execution as launching
    from waldo_commander.components.playback import playback
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.control_lease import BROWSER, MCP, control_lease
    from waldo_commander.services.motion_guard import motion_guard
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.services.supervised_restart import fresh_state, source_digest
    from waldo_commander.state import ui_state

    monkeypatch.setenv("WALDO_RUN_RECORD_DIR", str(tmp_path / "runs"))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    source = (
        "from parol6 import RobotClient\n\n"
        "def before_restart():\n    pass\n\n"
        "def after_pick():\n"
        "    with RobotClient() as rbt:\n        rbt.delay(0.1)\n\n"
        "if __name__ == '__main__':\n    after_pick()\n"
    )
    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = source
    reference = await fresh_state(client)
    gate, reading = asyncio.Event(), asyncio.Event()
    read_state = launching.fresh_state

    async def held_fresh_state(c):
        reading.set()
        await gate.wait()
        return await read_state(c)

    monkeypatch.setattr(launching, "fresh_state", held_fresh_state)

    def restart():
        return script_exec.start(
            restart_entry="after_pick",
            restart_reference=reference,
            reviewed_source_digest=source_digest(source),
        )

    async def reading_state() -> asyncio.Task:
        gate.clear()
        reading.clear()
        launch = asyncio.create_task(restart())
        await asyncio.wait_for(reading.wait(), 5)
        return launch

    launch = await reading_state()
    try:
        assert not launch.done()
        stop_btn = playback.stop_btn
        assert stop_btn is not None and stop_btn.visible, (
            "Stop is hidden while the launch reads the controller"
        )
        assert not await script_exec.start(), "a second Start launched in preflight"
        assert not await restart(), "a second restart launched in preflight"
        await script_exec.stop()
        gate.set()
        assert not await launch, "Stop did not cancel the launch in preflight"
        assert not is_any_program_running()
        assert script_exec.script_handle is None
        assert not stop_btn.visible

        # A Stop the controller never confirms keeps the launch reserved:
        # Start is refused and Stop stays offered until a retry confirms.
        launch = await reading_state()
        with monkeypatch.context() as patched:
            patched.setattr(client, "stop", AsyncMock(return_value=0))
            with pytest.raises(TimeoutError):
                await script_exec.stop()
        assert not await launch
        assert stop_btn.visible, "an unconfirmed Stop cannot be retried"
        assert not await script_exec.start(), "Start ran over an unconfirmed Stop"
        await script_exec.stop()
        assert motion_guard.busy_reason() is None, "a confirmed retry released nothing"
        assert not stop_btn.visible

        # Control passing to the AI while the launch reads the controller
        # ends the launch: the browser that started it no longer drives.
        launch = await reading_state()
        control_lease.seize(MCP, "preflight", "Review MCP")
        gate.set()
        assert not await launch, "the launch went on after losing control"
        assert not is_any_program_running()
        control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")

        # A mode switch stops a launch as it stops a run.
        launch = await reading_state()
        user.find(marker="btn-robot-toggle").click()
        async with asyncio.timeout(10):
            while not launch.done():
                await asyncio.sleep(0.05)
        assert not launch.result(), "the mode switch left the launch running"
        # The switch goes on after the launch ends; back to the simulator only
        # once it has landed, or the suite's controller is left on the robot.
        async with asyncio.timeout(10):
            while waldoctl.commander.status.simulator_active:
                await asyncio.sleep(0.05)
        await enable_sim(user)
        # Each refusal above is reported as an error, and the switch to the
        # robot finds no serial port on a test machine.
        expected = (
            "Controller stop is unconfirmed",
            "Control changed hands while the program was starting",
            "Serial connection error",
        )
        records = caplog.get_records("call")
        records[:] = [
            r for r in records if not any(text in r.getMessage() for text in expected)
        ]
    finally:
        gate.set()
        if not launch.done():
            launch.cancel()
        if is_any_program_running():
            await script_exec.stop()
        await enable_sim(user)
