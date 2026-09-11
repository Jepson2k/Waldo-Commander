"""Opted-in records follow real managed programs and remain bounded."""

import asyncio
import hashlib
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
import waldoctl
from nicegui.testing import User
from waldoctl.setup import Pose, SetupSnapshot

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
)
from waldo_commander.services.run_records import (
    MAX_RECORD_BYTES,
    RunRecord,
    debugging_export,
    load_record,
)
from waldo_commander.services.stepping_client import GUIStepController, StepIO
from waldo_commander.setup import SetupStore


def test_every_command_event_reaches_the_record_with_its_step():
    controller = GUIStepController(uuid4().hex)
    controller.initialize()
    io = StepIO(controller.session_id)
    try:
        for _ in range(300):
            io.emit_event("complete", "delay")
            io.increment_step_count()
        io.emit_event("start", "move_j")
        deadline = time.monotonic() + 5.0
        events = []
        while len(events) < 301 and time.monotonic() < deadline:
            events.extend(controller.poll_events())
            time.sleep(0.01)
        assert len(events) == 301, "no event window, no loss"
        assert [e["step"] for e in events[:300]] == list(range(300))
        assert events[-1]["event"] == "start" and events[-1]["step"] == 300
        assert controller.poll_events() == []
    finally:
        controller.cleanup()


def test_export_removes_personal_values_and_journal_recovers_a_partial_tail(tmp_path):
    record = RunRecord("print('source-secret')", "parol6", directory=tmp_path)
    record.append(
        {
            "event": "command_failed",
            "method": "move_j",
            "code": 53,
            "message": "secret error detail",
        }
    )
    for _ in range(10000):
        record.append(
            {
                "event": "command_started",
                "method": "move_j",
                "arguments": {
                    "positions": [1, 2, 3],
                    "secret-in-key": "sensitive value",
                    "speed": 0.5,
                    "samples": list(range(200)),
                    "password": "unpublishable",
                },
            }
        )
        if record.truncated:
            break
    assert record.truncated
    record.finish("failed", 1)
    assert record.path.stat().st_size <= MAX_RECORD_BYTES
    text = debugging_export(record.path).decode()
    assert '"speed": 0.5' in text
    assert '"code": 53' in text and "secret error detail" not in text
    assert all(
        value not in text
        for value in (
            "secret-in-key",
            "sensitive value",
            "unpublishable",
            "source-secret",
        )
    )
    assert '"record_truncated"' in text
    assert load_record(record.path)[-1]["outcome"] == "failed"
    with record.path.open("ab") as stream:
        stream.write(b'{"event":')
    assert load_record(record.path)[-1]["outcome"] == "failed"


@pytest.mark.integration
async def test_managed_records_capture_nested_calls_results_status_and_stop(
    user: User, tmp_path, monkeypatch
):
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.state import ui_state

    monkeypatch.setenv("WALDO_RUN_RECORD_DIR", str(tmp_path / "records"))
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path / "setups"))
    monkeypatch.setenv("PRIVATE_ACCOUNT_TOKEN", "env-secret-123")
    SetupStore().save("bench", SetupSnapshot(poses={"pick": Pose((1, 2, 3, 0, 0, 0))}))
    (tmp_path / "unrelated.txt").write_text("unrelated-secret-123")
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    assert ui_state.active_textarea is not None
    source = """import asyncio
import os
from parol6 import RobotClient, AsyncRobotClient
from waldoctl.skills import skill
from waldo_commander.setup import load_setup

@skill(id="personal.child", version="1.0.0")
async def child(rbt, seconds=0.1):
    await rbt.delay(seconds)
    return {"elapsed": seconds, "note": "value-secret-123"}

@skill(id="personal.parent", version="1.0.0")
async def parent(rbt, *, offset=2.0):
    joints = await rbt.angles()
    joints[0] += offset
    await rbt.move_j(joints, duration=0.5, timeout=15)
    return await child.async_call(rbt)

setup = load_setup("bench")
with RobotClient() as rbt:
    parent(rbt)
    rbt.select_tool("PNEUMATIC")
    rbt.tool.open()
    rbt.tool.close()
    if os.environ.get("WALDO_STEP_SESSION"):
        assert not rbt.wait_command(999999, timeout=0.05)

async def main():
    async with AsyncRobotClient() as rbt:
        await parent.async_call(rbt, offset=-2.0)
asyncio.run(main())
print("console-secret-123")
if os.environ.get("WALDO_STEP_SESSION"):
    raise RuntimeError("error-secret-123")
"""
    ui_state.active_textarea.value = source
    user.find(marker="editor-more-btn").click()
    user.find(marker="editor-records-btn").click()
    await user.should_see("Record future program runs")
    user.find(marker="record-runs-enabled").click()
    await user.should_see("Export debugging data")
    user.find("Close").click()
    assert script_exec.record_runs
    program = waldoctl.commander.programs.active
    assert program is not None
    await script_exec.start()
    async with asyncio.timeout(40):
        while is_any_program_running():
            await asyncio.sleep(0.05)
    assert script_exec.last_exit_code == 1
    stderr = "\n".join(
        entry.text for entry in program.log.entries if entry.stream == "stderr"
    )
    assert "RuntimeError: error-secret-123" in stderr, stderr
    path = script_exec.last_record
    assert path is not None
    events = load_record(path)
    assert events[0]["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert events[-1]["outcome"] == "failed"
    assert any(
        e["event"] == "controller_context"
        and e["method"] == "tcp_transform"
        and len(e["result"]) == 6
        for e in events
    )
    starts = [e for e in events if e["event"] == "skill_started"]
    assert [e["arguments"] for e in starts] == [
        {"offset": 2.0},
        {"seconds": 0.1},
        {"offset": -2.0},
        {"seconds": 0.1},
    ]
    assert starts[1]["parent_id"] == starts[0]["invocation_id"]
    results = [e["result"] for e in events if e["event"] == "skill_completed"]
    assert len(results) == 4 and all(r["elapsed"] == 0.1 for r in results)
    commands = [
        e for e in events if e["event"] == "command_started" and e["method"] == "move_j"
    ]
    assert len(commands) == 2
    assert commands[0]["parent_id"] == starts[0]["invocation_id"]
    assert len([e for e in events if e["event"] == "command_completed"]) == 6
    assert [
        e["method"]
        for e in events
        if e["event"] == "command_started" and e["method"].startswith("tool.")
    ] == ["tool.open", "tool.close"]
    assert any(
        e["event"] == "command_unconfirmed" and e["index"] == 999999 for e in events
    )
    statuses = [e["snapshot"] for e in events if e["event"] == "status"]
    assert len(statuses) >= 2 and statuses[-1]["seq"] > statuses[0]["seq"]
    assert any(
        e["event"] == "setup_loaded"
        and e["snapshot"]["poses"]["pick"]["values"][:3] == [1, 2, 3]
        for e in events
    )
    exported = debugging_export(path).decode()
    assert all(
        secret not in exported
        for secret in (
            "value-secret",
            "console-secret",
            "env-secret",
            "error-secret",
            "unrelated-secret",
            "personal.parent",
        )
    )

    # The next run is not captured, even if the subprocess inherited a flag.
    script_exec.record_runs = False
    ui_state.active_textarea.value = "print('uncaptured')"
    await script_exec.start()
    async with asyncio.timeout(15):
        while is_any_program_running():
            await asyncio.sleep(0.05)
    assert len(list((tmp_path / "records").glob("*.jsonl"))) == 1

    script_exec.record_runs = True
    ui_state.active_textarea.value = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.delay(30)\n"
    try:
        await script_exec.start()
        assert await waldoctl.commander.client.wait_status(
            lambda s: bool(s.action_current), timeout=15
        )
        await script_exec.signal_pause()
        await script_exec.stop()
        assert script_exec.last_record != path
        stopped = load_record(script_exec.last_record)
        assert stopped[-1]["outcome"] == "stopped"
        assert any(e["event"] == "pause" for e in stopped)
        assert any(e["event"] == "command_returned" for e in stopped)
        assert not any(e["event"] == "command_completed" for e in stopped)
    finally:
        if is_any_program_running():
            await script_exec.stop()
        await waldoctl.commander.client.resume()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permission requirement")
def test_recorded_values_travel_only_over_the_owners_link(tmp_path, monkeypatch):
    monkeypatch.setenv("WALDO_RECORD_VALUES", "1")
    controller = GUIStepController(uuid4().hex)
    try:
        controller.initialize()
        if os.name == "posix":
            # Recorded values travel over a socket only its owner can open.
            from waldo_commander.services.stepping_client import _step_address

            assert (
                Path(_step_address(controller.session_id)).stat().st_mode & 0o077 == 0
            )
        io = StepIO(controller.session_id)
        io.emit_event("command_started", "move_j", arguments={"label": "private-value"})
        io.emit_event("command_completed", "move_j", result="private-result")
        deadline = time.monotonic() + 2.0
        events = []
        while len(events) < 2 and time.monotonic() < deadline:
            events.extend(controller.poll_events())
            time.sleep(0.01)
        assert events[0]["arguments"]["label"] == "private-value"
        assert events[1]["result"] == "private-result"
    finally:
        controller.cleanup()
