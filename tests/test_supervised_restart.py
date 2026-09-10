"""Restart entry discovery must not execute module initialization or old locals."""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from waldo_commander.services.supervised_restart import discover_entries, execute_entry


def test_entries_execute_in_fresh_globals_and_do_not_run_the_main_sequence(tmp_path):
    source = """from waldoctl.restart import restart_entry as entry
values = []
@entry
def first():
    values.append(1)
    return len(values)
@entry
async def second():
    values.append(2)
    return values
if __name__ == "__main__":
    raise RuntimeError("The whole original program was replayed")
"""
    assert [e.name for e in discover_entries(source)] == ["first", "second"]
    assert execute_entry(source, "program.py", "first") == 1
    assert execute_entry(source, "program.py", "first") == 1
    assert execute_entry(source, "program.py", "second") == [2]
    assert discover_entries("from missing_dependency import anything\n" + source)
    with pytest.raises(ValueError, match="No declared"):
        execute_entry(source, "program.py", "missing")
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
from waldoctl.restart import restart_entry
values = []
@restart_entry
def after_pick():
    """Continue after checking the held part."""
    values.append(1)
    with RobotClient() as rbt:
        joints = rbt.angles()
        joints[0] += 3
        rbt.move_j(joints, duration=0.5, timeout=15)
    with open({str(marker)!r}, 'a') as log:
        log.write(f'sync:{{len(values)}}\\n')
@restart_entry
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
    await user.should_see("Controller ready", retries=50)
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
    assert control_lease.held_by(BROWSER, ui_state.active_client_id)
    await user.should_see("You've taken control from the AI")
    assert marker.read_text() == "sync:1\n"
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
    assert marker.read_text() == "sync:1\nasync:1\n"
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
        assert marker.read_text() == "sync:1\nasync:1\n"
    finally:
        if is_any_program_running():
            await script_exec.stop()
        await client.set_status_rate(20)

    # A reviewed state cannot survive a TCP edit, stale source, pending
    # command, or loss of reference. Refusals never launch the entry.
    reference = await fresh_state(client)
    tcp = await client.tcp_transform()
    changed = list(tcp)
    changed[0] += 1
    assert await client.set_tcp_transform(*changed) > 0
    assert not await selected("after_pick", reference)
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
    assert marker.read_text() == "sync:1\nasync:1\n"
    assert not is_any_program_running()

    # Ordinary Start ignores even an inherited entry setting.
    monkeypatch.setenv("WALDO_RESTART_ENTRY", "after_pick")
    ui_state.active_textarea.value = "print('ordinary beginning')"
    await ensure_robot_ready_for_motion()
    assert await script_exec.start()
    await finished()
    assert script_exec.last_exit_code == 0
