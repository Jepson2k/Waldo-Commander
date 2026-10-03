"""While recording, motion nobody in WC commanded is staged in the program."""

import ast
import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.recording import staged_lines
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.programs import is_any_program_recording
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
    """A move nobody in WC commanded lands in the take as moves that plan to
    where the arm ended, badged with their count; switched to raw it replays
    the saved points instead, and Undo takes the whole take out."""
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
        target[0] += 12.0
        target[2] -= 8.0
        # A move no UI hook sees: what another client, MCP or a hand on the
        # arm looks like to the recorder.
        index = await client.move_j(target, duration=1.2)
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
        moves = captured.count("rbt.move_")
        # The badge counts the move lines it sits on.
        assert badges and badges[0].startswith(
            f"captured · {moves} move{'s' if moves != 1 else ''}"
        ), (badges, captured)
        ast.parse(str(textarea.value))

        # The lines plan to where the arm actually ended up.
        preview = _run_simulation_isolated(
            PROGRAM.rsplit("    rbt.home()", 1)[0] + captured + "\n",
            np.radians(start),
        )
        assert preview["error"] is None, preview["error"]
        final = np.degrees(preview["final_joints_rad"])
        # J5 is near zero, where only J4 + J6 sets the pose.
        assert final[[0, 1, 2, 4]] == pytest.approx(
            np.asarray(target)[[0, 1, 2, 4]], abs=0.5
        )
        assert final[3] + final[5] == pytest.approx(target[3] + target[5], abs=0.5)

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
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()
    assert not is_any_program_recording()


async def _open(user: User, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()


async def _record(user: User, source: str = PROGRAM):
    """The program tab showing *source*, recording below its third line."""
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    program = waldoctl.commander.programs.active
    assert program is not None
    textarea = ui_state.active_textarea
    textarea.value = source
    program.dry_run.playback.active_cursor_line = 3
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert is_any_program_recording()
    return program, textarea


def _captures(session) -> list:
    return [b for b in session.blocks if b.kind == "capture"] if session else []


def _block_text(textarea, block) -> str:
    lines = str(textarea.value).split("\n")
    return "\n".join(lines[block.first_line - 1 : block.last_line])


async def _move(client, target, duration: float) -> None:
    index = await client.move_j(target, duration=duration)
    assert await client.wait_command(index, timeout=15)


def _inserted(before: list[str], after: list[str]) -> tuple[int, list[str]]:
    """Where *after* has lines *before* lacks, as one insertion."""
    at = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), len(before))
    count = len(after) - len(before)
    assert after[:at] == before[:at] and after[at + count :] == before[at:], after
    return at, after[at : at + count]


@pytest.mark.integration
async def test_a_capture_holds_its_place_while_it_converts(
    user: User, tmp_path, monkeypatch
):
    """Conversion runs beside the observer, not in its way: an action recorded
    while a capture converts goes after it, and a second move made meanwhile
    is watched from its start. Keep and a tab switch landing mid-conversion
    still put the lines where the span closed, kept, in the program it was
    recorded in; the program in front gets nothing."""
    import threading

    from waldo_commander.services import motion_recorder as recorder_module

    entered, release = threading.Event(), threading.Event()
    real = recorder_module.span_to_lines

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(60), "the test never released the conversion"
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder_module, "span_to_lines", held)
    await _open(user, monkeypatch, tmp_path)
    program, textarea = await _record(user)
    client = waldoctl.commander.client
    try:
        start = await client.angles()
        assert start is not None
        first = list(start)
        first[0] += 8.0
        await _move(client, first, 1.0)
        assert await wait_until(entered.is_set, timeout_s=20), (
            "the first span never started converting"
        )
        motion_recorder.record_action("io", port=0, state=1)
        second = list(first)
        second[2] -= 6.0
        await _move(client, second, 1.0)
        assert await wait_until(
            lambda: len(motion_recorder.pending_captures(program.id)) == 2,
            timeout_s=20,
        ), "the second move was not watched while the first one converted"
        release.set()
        assert await wait_until(
            lambda: len(_captures(motion_recorder.session)) == 2, timeout_s=60
        ), textarea.value
        one, two = sorted(
            _captures(motion_recorder.session), key=lambda b: b.first_line
        )
        lines = str(textarea.value).split("\n")
        io = next(n for n, line in enumerate(lines, 1) if "rbt.write_io(0, 1)" in line)
        assert one.last_line < io < two.first_line, textarea.value
        assert "rbt.move_" in _block_text(textarea, one)
        assert "rbt.move_" in _block_text(textarea, two)
        # Watched from where it began, not picked up once the observer was
        # free again.
        recorded = two.recording
        assert recorded is not None
        assert recorded.samples[0].joints_deg == pytest.approx(first, abs=0.2)
        assert recorded.samples[-1].joints_deg == pytest.approx(second, abs=0.2)
        assert len(recorded.samples) >= 10, len(recorded.samples)
        ast.parse(str(textarea.value))

        closes_at = two.last_line
        entered.clear()
        release.clear()
        third = list(second)
        third[0] -= 8.0
        await _move(client, third, 1.0)
        assert await wait_until(entered.is_set, timeout_s=20), (
            "the third span never started converting"
        )
        user.find(marker="staged-keep").click()
        await asyncio.sleep(0.1)
        assert not is_any_program_recording()
        assert motion_recorder.session is None
        kept = str(textarea.value).split("\n")
        other = waldoctl.commander.programs.new(
            source="print('other')\n", filename="other.py"
        )
        waldoctl.commander.programs.switch(other.id)
        await asyncio.sleep(0.1)
        in_front = ui_state.active_textarea
        in_front_text = str(in_front.value)
        release.set()
        assert await wait_until(
            lambda: len(str(textarea.value).split("\n")) > len(kept), timeout_s=30
        ), f"the kept take's capture was dropped:\n{textarea.value}"
        assert other.source == "print('other')\n", other.source
        assert str(in_front.value) == in_front_text
        at, inserted = _inserted(kept, str(textarea.value).split("\n"))
        assert at == closes_at, (
            f"the capture did not land where it closed:\n{textarea.value}"
        )
        assert any("rbt.move_" in line for line in inserted), inserted
        assert not staged_lines(textarea)
    finally:
        release.set()
        if is_any_program_recording():
            motion_recorder.toggle_recording()


@pytest.mark.integration
async def test_what_one_captured_span_is(user: User, tmp_path, monkeypatch, caplog):
    """A dial move the controller refuses or never acknowledges records
    nothing and leaves no motion owned. Stillness is distance from where the
    arm came to rest, so a move slower than the per-publication threshold is
    still a move. Commander's own move is left alone, and a move nobody
    commanded that starts as soon as it has come to rest is captured whole.
    The time the arm stood still between two captures is a delay before the
    second, as it is before a recorded action."""
    from parol6.utils.error_catalog import ErrorCode, make_error
    from parol6.utils.errors import MotionError

    await _open(user, monkeypatch, tmp_path)
    _, textarea = await _record(user)
    panel = ui_state.control_panel
    assert panel is not None
    client = panel.client
    real = client.move_j

    async def refused(*args, **kwargs):
        raise MotionError(
            make_error(ErrorCode.SYS_SELF_COLLISION, sample=1, total=1, pairs="L4/L6")
        )

    async def unacknowledged(*args, **kwargs):
        return -1

    def captures() -> list:
        return _captures(motion_recorder.session)

    try:
        start = await client.angles()
        assert start is not None
        written = str(textarea.value)
        monkeypatch.setattr(client, "move_j", refused)
        await panel.move_joint_to_angle(0, start[0] + 5.0)
        assert not motion_recorder.owns_motion, "a refused dial move left its jog open"
        monkeypatch.setattr(client, "move_j", unacknowledged)
        await panel.move_joint_to_angle(0, start[0] + 5.0)
        assert not motion_recorder.owns_motion
        waiting = panel._jog_end_wait_task
        if waiting is not None:
            await waiting
        assert str(textarea.value) == written, (
            "a move the controller never took was recorded"
        )
        monkeypatch.setattr(client, "move_j", real)
        refusals = [
            r
            for r in caplog.get_records("call")
            if r.getMessage().startswith("Go to joint angle failed")
        ]
        assert refusals
        records = caplog.get_records("call")
        records[:] = [r for r in records if r not in refusals]

        # What moves the arm next is captured again, slow as it is.
        slow = list(start)
        slow[0] += 1.0
        await _move(client, slow, 2.0)
        assert await wait_until(lambda: len(captures()) == 1, timeout_s=30), (
            "a 1° move over 2 s was not captured"
        )
        block = captures()[0]
        assert "rbt.move_" in _block_text(textarea, block), textarea.value
        assert block.recording.samples[0].joints_deg == pytest.approx(start, abs=0.1)
        assert block.recording.samples[-1].joints_deg == pytest.approx(slow, abs=0.1)

        owned = list(slow)
        owned[0] += 6.0
        with motion_recorder.owned():
            await _move(client, owned, 0.8)
            # The owned move has come to rest before its window closes.
            await asyncio.sleep(0.2)
        external = list(owned)
        external[0] += 6.0
        await _move(client, external, 0.6)
        assert await wait_until(lambda: len(captures()) >= 2, timeout_s=30), (
            "the move right after the owned one was not captured"
        )
        assert len(captures()) == 2, textarea.value
        recorded = captures()[1].recording
        assert recorded.samples[0].joints_deg == pytest.approx(owned, abs=0.1), (
            "the capture began partway through the move"
        )
        assert recorded.samples[-1].joints_deg == pytest.approx(external, abs=0.1)

        await asyncio.sleep(1.0)  # the operator waits before the next move
        back = list(external)
        back[0] -= 6.0
        await _move(client, back, 0.6)
        assert await wait_until(lambda: len(captures()) == 3, timeout_s=30)
        last = captures()[2]
        above = str(textarea.value).split("\n")[last.first_line - 2].strip()
        assert above.startswith("rbt.delay("), textarea.value
        assert float(above.removeprefix("rbt.delay(").rstrip(")")) >= 1.0, above
    finally:
        monkeypatch.setattr(client, "move_j", real)
        if is_any_program_recording():
            motion_recorder.toggle_recording()


@pytest.mark.integration
async def test_gripper_changes_are_captured_where_they_happen(
    user: User, tmp_path, monkeypatch
):
    """A gripper that closes between two moves, with no hold for the arm to
    stand still in, splits the captured motion there; one that opens while
    the arm stands still is captured on its own."""
    await _open(user, monkeypatch, tmp_path)
    client = waldoctl.commander.client
    index = await client.select_tool("PNEUMATIC")
    assert await client.wait_command(index, timeout=5)
    try:
        index = await client.tool.open()
        assert await client.wait_command(index, timeout=5)
        assert await wait_until(
            lambda: waldoctl.commander.status.tool.key == "PNEUMATIC", timeout_s=5
        )
        _, textarea = await _record(user)
        start = await client.angles()
        assert start is not None
        there = list(start)
        there[0] += 10.0
        back = list(there)
        back[2] -= 8.0
        # The gripper closes in the instant between the two moves, far too
        # short a stop for a hold of its own.
        index = await client.move_j(there, duration=1.2)
        assert await client.wait_command(index, timeout=15)
        assert await client.tool.close() >= 0
        index = await client.move_j(back, duration=1.2)
        assert await client.wait_command(index, timeout=15)
        assert await wait_until(
            lambda: bool(_captures(motion_recorder.session)), timeout_s=30
        )
        moving = _block_text(textarea, _captures(motion_recorder.session)[0])
        lines = moving.split("\n")
        closed = next(
            n for n, line in enumerate(lines) if "rbt.tool.set_position(1.000)" in line
        )
        assert any("rbt.move_" in line for line in lines[:closed]), moving
        assert any("rbt.move_" in line for line in lines[closed + 1 :]), moving

        opening = await client.tool.open()
        assert await client.wait_command(opening, timeout=5)
        assert await wait_until(
            lambda: len(_captures(motion_recorder.session)) == 2, timeout_s=30
        ), "a gripper opened with the arm still was not captured"
        still = _block_text(textarea, _captures(motion_recorder.session)[1])
        assert "rbt.tool.set_position(0.000)" in still, still
        assert "rbt.move_" not in still, still
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()
        index = await client.select_tool("NONE")
        assert await client.wait_command(index, timeout=5)


@pytest.mark.integration
async def test_capture_survives_a_stalled_stream_and_says_when_it_stops(
    user: User, tmp_path, monkeypatch
):
    """A stall ends the status stream it happened on; the observer watches on
    with a new one. When it cannot watch at all the take says so."""
    await _open(user, monkeypatch, tmp_path)
    client = waldoctl.commander.client
    live = client.stream_status
    streams = 0

    async def stalls_once():
        nonlocal streams
        streams += 1
        stall = streams == 1
        seen = 0
        async for status in live():
            yield status
            seen += 1
            if stall and seen >= 10:
                await asyncio.sleep(60)  # the wire goes quiet

    monkeypatch.setattr(client, "stream_status", stalls_once)
    await _record(user)
    try:
        assert await wait_until(lambda: streams >= 2, timeout_s=15), (
            "the observer never opened a new stream after the stall"
        )
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8.0
        await _move(client, target, 1.0)
        assert await wait_until(
            lambda: bool(_captures(motion_recorder.session)), timeout_s=30
        ), "motion after a stall was not captured"
        await user.should_not_see(marker="staged-capture-stopped")
        motion_recorder.keep()

        async def no_rate():
            return None

        monkeypatch.setattr(client, "status_rate", no_rate)
        user.find(marker="editor-record-btn").click()
        await asyncio.sleep(0.1)
        assert is_any_program_recording()
        await user.should_see(marker="staged-capture-stopped", retries=100)
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()
