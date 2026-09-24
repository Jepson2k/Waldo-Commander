"""While recording, motion nobody in WC commanded is staged in the program."""

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
from tests.test_editor_integration import _set_selection
from waldo_commander.components.script_execution import script_exec
from waldo_commander.demonstrations import span_to_lines
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.state import ui_state

PROGRAM = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.home()\n"


def _capture(session):
    return (
        next((b for b in session.blocks if b.kind == "capture"), None)
        if session
        else None
    )


@pytest.mark.integration
async def test_uncommanded_motion_is_staged_as_moves_or_as_recorded(
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
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8.0
        # A move no UI hook sees: what another client, MCP or a hand on the
        # arm looks like to the recorder.
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(
            lambda: _capture(motion_recorder.session) is not None, timeout_s=20
        ), "the captured span never reached the program"
        block = _capture(motion_recorder.session)
        lines = str(textarea.value).split("\n")
        captured = "\n".join(lines[block.first_line - 1 : block.last_line])
        assert "rbt.move_" in captured, captured
        badges = [
            spec["text"]
            for spec in textarea.decorations
            if spec.get("class") == "cm-staged-badge"
        ]
        assert badges and badges[0].startswith("captured · "), badges
        ast.parse(str(textarea.value))

        # The lines plan to where the arm actually ended up.
        preview = await run.cpu_bound(
            _run_simulation_isolated,
            PROGRAM.rsplit("    rbt.home()", 1)[0] + captured + "\n",
            np.radians(start),
        )
        assert preview["error"] is None, preview["error"]
        final = np.degrees(preview["final_joints_rad"])
        assert final == pytest.approx(target, abs=0.5)

        # Raw, the same lines replay the recorded points instead.
        element("staged-capture-mode").set_value("raw")
        assert await wait_until(
            lambda: block.mode == "raw"
            and "replay_demonstration(rbt, load_demonstration(" in str(textarea.value),
            timeout_s=20,
        ), textarea.value
        saved = list(tmp_path.glob("*.json"))
        assert len(saved) == 1, saved
        ast.parse(str(textarea.value))
        element("staged-capture-mode").set_value("moves")
        assert await wait_until(
            lambda: block.mode == "moves"
            and "replay_demonstration" not in str(textarea.value),
            timeout_s=20,
        ), textarea.value
        assert list(tmp_path.glob("*.json")) == saved, "switching saves nothing new"

        # Undo takes the whole take out again.
        user.find(marker="staged-undo").click()
        await asyncio.sleep(0.1)
        assert str(textarea.value) == PROGRAM
        assert motion_recorder.session is None and not is_any_program_recording()

        # A new take: a span, then a skill inserted and run live while the
        # span is staged. The skill's motion is WC's own: the program gets its
        # call once and no captured moves, and the span stays staged.
        program.dry_run.playback.active_cursor_line = 3
        user.find(marker="editor-record-btn").click()
        await asyncio.sleep(0.1)
        target[1] -= 6.0
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(
            lambda: _capture(motion_recorder.session) is not None, timeout_s=20
        )
        recording = _capture(motion_recorder.session).recording
        mark = len(str(textarea.value).rstrip("\n"))
        user.find(marker="editor-commands-btn").click()
        user.find(marker="editor-skill-waldo.retract").click()
        await asyncio.sleep(0)
        element("skill-arg-distance_mm").set_value(2)
        user.find(marker="skill-insert").click()
        await asyncio.sleep(0)
        call = next(
            number
            for number, line in enumerate(str(textarea.value).split("\n"), start=1)
            if "_skill_waldo_retract(rbt," in line
        )
        _set_selection(textarea, call, call)
        await asyncio.sleep(0)
        user.find(marker="editor-run-selection").click()
        editor = ui_state.editor_panel
        async with asyncio.timeout(30):
            await asyncio.sleep(0.1)
            while editor._running_selection or is_any_program_running():
                await asyncio.sleep(0.05)
        assert waldoctl.commander.programs.active is program
        await asyncio.sleep(1.0)
        tail = str(textarea.value)[mark:]
        assert tail.count("_skill_waldo_retract(rbt,") == 1, tail
        assert "rbt.move_l(" not in tail and "rbt.move_j(" not in tail, tail
        kinds = [b.kind for b in motion_recorder.session.blocks]
        assert "capture" in kinds and "action" in kinds, kinds
        user.find(marker="staged-keep").click()
        await asyncio.sleep(0.1)
        assert motion_recorder.session is None

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
        if is_any_program_running():
            await script_exec.stop()
        if is_any_program_recording():
            motion_recorder.toggle_recording()
    assert not is_any_program_recording()
