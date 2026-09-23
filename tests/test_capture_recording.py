"""While recording, motion nobody in WC commanded is written into the program."""

import ast
import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui import run
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.components.capture_review import capture_review
from waldo_commander.demonstrations import span_to_lines
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state

PROGRAM = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.home()\n"


@pytest.mark.integration
async def test_uncommanded_motion_is_captured_and_a_skill_run_is_recorded_once(
    user: User, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    program = waldoctl.commander.programs.active
    assert program is not None
    textarea = ui_state.active_textarea
    textarea.value = PROGRAM
    program.dry_run.playback.active_cursor_line = 3
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert is_any_program_recording()
    try:
        before = str(textarea.value)
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8.0
        # A move no UI hook sees: what another client, MCP or a hand on the
        # arm looks like to the recorder.
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(lambda: capture_review.visible, timeout_s=20), (
            "the captured span never reached the program"
        )
        captured = str(textarea.value)[len(before) :]
        assert "rbt.move_" in captured, captured
        assert element("capture-summary").text.startswith("Captured · ")
        ast.parse(str(textarea.value))

        # The lines plan to where the arm actually ended up.
        preview = await run.cpu_bound(
            _run_simulation_isolated,
            PROGRAM.rsplit("    rbt.home()", 1)[0] + captured,
            np.radians(start),
        )
        assert preview["error"] is None, preview["error"]
        final = np.degrees(preview["final_joints_rad"])
        assert final == pytest.approx(target, abs=0.5)

        # Trimming rewrites the same lines for the shorter span.
        capture = motion_recorder.capture
        assert capture is not None
        duration = capture.recording.duration_s
        element("capture-trim").set_value({"min": 0.0, "max": round(duration / 2, 2)})
        assert await wait_until(
            lambda: motion_recorder.capture is not None
            and motion_recorder.capture.conversion.recorded_duration_s < duration,
            timeout_s=20,
        )
        trimmed = str(textarea.value)[len(before) :]
        assert trimmed != captured
        ast.parse(str(textarea.value))

        # Undo takes the lines out again.
        user.find(marker="capture-undo").click()
        await asyncio.sleep(0)
        assert str(textarea.value) == before
        assert not capture_review.visible and motion_recorder.capture is None

        # A second span, then a skill run while it is under review: the skill's
        # own motion is WC's, recorded as its call and nothing else, and the
        # recorded call settles the review.
        target[1] -= 6.0
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(lambda: capture_review.visible, timeout_s=20)
        assert motion_recorder.capture is not None
        recording = motion_recorder.capture.recording
        mark = len(str(textarea.value))
        user.find(marker="tab-skills").click()
        user.find(marker="skill-tile-waldo.retract").click()
        await asyncio.sleep(0)
        element("skill-arg-distance_mm").set_value(2)
        user.find(marker="skill-run").click()
        await user.should_see(content="Skill completed", retries=300)
        assert waldoctl.commander.programs.active is program
        await asyncio.sleep(1.0)
        tail = str(textarea.value)[mark:]
        assert tail.count("_skill_waldo_retract(rbt,") == 1, tail
        assert "rbt.move_l(" not in tail and "rbt.move_j(" not in tail, tail
        assert not capture_review.visible

        # A span the planner cannot follow is replayed from a saved copy.
        fallback = span_to_lines(
            recording,
            ui_state.active_robot,
            program="test",
            directory=tmp_path / "fallback",
            tolerance_mm=1e-6,
        )
        assert fallback.replayed
        assert list((tmp_path / "fallback").glob("test-*.json"))
        assert "replay_demonstration(rbt, load_demonstration(" in fallback.source
    finally:
        if is_any_program_recording():
            user.find(marker="tab-program").click()
            await asyncio.sleep(0)
            user.find(marker="editor-record-btn").click()
            await asyncio.sleep(0.1)
    assert not is_any_program_recording()
