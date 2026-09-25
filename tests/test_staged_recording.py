"""What a recording session writes stays marked in the editor until it is kept."""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.test_editor_integration import _set_cursor_line, _set_selection
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.components.simulation_engine import simulation
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state

PROGRAM = (
    "from parol6 import RobotClient\n"
    "with RobotClient() as rbt:\n"
    "    rbt.home()\n"
    "    rbt.move_j([0, -90, 180, 0, 0, 180], speed=0.5)\n"
)


def recorder_anchors(textarea) -> dict[str, int]:
    return {
        key: line
        for key, line in dict(textarea._props.get("line-anchors") or {}).items()
        if key.startswith("__")
    }


@pytest.mark.integration
async def test_a_simulation_pass_keeps_the_lines_the_recorder_tracks(
    user: User, tmp_path, monkeypatch
):
    """Every simulation pass re-declares the editor's line anchors for its
    targets. Doing that by replacing the whole set dropped the anchors that
    say which lines a recording session wrote, so after the next pass the
    recorder rewrote or removed lines by stale numbers."""
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    program = waldoctl.commander.programs.active
    assert program is not None
    textarea = ui_state.active_textarea
    textarea.value = PROGRAM
    program.dry_run.playback.active_cursor_line = 4
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert is_any_program_recording()
    try:
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8.0
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(
            lambda: str(textarea.value).count("rbt.move_") > 1, timeout_s=20
        ), "the uncommanded move was not captured"
        await simulation.run_simulation(program.id)
        await asyncio.sleep(0)
        # Besides the recording cursor, the lines the session wrote are still
        # tracked after a simulation pass.
        tracked = set(recorder_anchors(textarea)) - {"__recording_insert__"}
        assert tracked, (
            "a simulation pass dropped the anchors of the captured lines: "
            f"{recorder_anchors(textarea)}"
        )
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()


def staged_lines(textarea) -> set[int]:
    return {
        spec["line"]
        for spec in textarea.decorations
        if spec.get("class") == "cm-line-staged"
    }


async def _open_program(user: User, source: str):
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    textarea.value = source
    await asyncio.sleep(0)
    return textarea


@pytest.mark.integration
async def test_recorded_lines_are_staged_until_kept_and_undo_takes_them_out(
    user: User,
):
    textarea = await _open_program(user, PROGRAM)
    _set_cursor_line(textarea, 3)
    original = str(textarea.value)

    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    motion_recorder.record_action("io", port=0, state=1)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert not is_any_program_recording()

    # Stopping does not decide: the lines stay marked, the toolbar makes way
    # for Keep and Undo, and the program can be played back meanwhile.
    written = str(textarea.value).split("\n")
    before = original.split("\n")
    marked = staged_lines(textarea)
    assert len(marked) == len(written) - len(before), (marked, textarea.value)
    assert all(written[n - 1] not in before for n in marked), textarea.value
    await user.should_see(marker="staged-keep")
    await user.should_see(marker="staged-undo")
    await user.should_not_see(marker="editor-open-btn")

    user.find(marker="staged-undo").click()
    await asyncio.sleep(0)
    assert str(textarea.value) == original
    assert not staged_lines(textarea)
    assert motion_recorder.session is None
    await user.should_see(marker="editor-open-btn")

    # Keep while still recording ends the take and keeps its lines.
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    kept = str(textarea.value)
    assert kept != original
    user.find(marker="staged-keep").click()
    await asyncio.sleep(0.1)
    assert not is_any_program_recording()
    assert str(textarea.value).startswith(kept.rstrip("\n"))
    assert not staged_lines(textarea)
    editor = ui_state.editor_panel
    assert editor.playback.record_btn._props.get("color") == "negative"
    assert editor.playback._recording_notification is None


@pytest.mark.integration
async def test_recording_over_a_selection_replaces_it_and_undo_puts_it_back(
    user: User,
):
    source = (
        "from parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        "    rbt.home()\n"
        "    rbt.move_j([10, -90, 180, 0, 0, 180], speed=0.5)\n"
        "    rbt.move_j([20, -90, 180, 0, 0, 180], speed=0.5)\n"
        "    rbt.delay(1.0)\n"
    )
    textarea = await _open_program(user, source)

    _set_selection(textarea, 4, 5)
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    # The take starts where the arm is, in place of the selected lines.
    lines = str(textarea.value).split("\n")
    assert "Recording start position" in lines[3], textarea.value
    assert "[10, -90" not in textarea.value and "[20, -90" not in textarea.value
    motion_recorder.record_action("io", port=0, state=1)
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)

    user.find(marker="staged-undo").click()
    await asyncio.sleep(0)
    assert str(textarea.value) == source, (
        "Undo restores the re-recorded lines as they were"
    )

    _set_selection(textarea, 4, 5)
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert ui_state.editor_panel.selection() is None, "Record used the selection"
    motion_recorder.record_action("io", port=0, state=1)
    user.find(marker="staged-keep").click()
    await asyncio.sleep(0.1)
    lines = str(textarea.value).split("\n")
    assert lines[:3] == source.split("\n")[:3]
    assert (
        lines[3].startswith("    rbt.move_j(")
        and "Recording start position" in lines[3]
    )
    assert lines[4] == "    rbt.write_io(0, 1)", textarea.value
    assert lines[5] == "    rbt.delay(1.0)", textarea.value


@pytest.mark.integration
async def test_a_report_of_anchors_from_before_a_write_does_not_move_the_cursor(
    user: User,
):
    """After the recorder writes, the browser can report the anchors as they
    were before the write reaches the server's newer declaration. Taking that
    report as current put the next recorded line above the last one."""
    from tests.test_editor_integration import _fire_editor_event

    textarea = await _open_program(user, PROGRAM)
    _set_cursor_line(textarea, 4)
    waldoctl.commander.programs.active.dry_run.final_joints_rad = None
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    try:
        # The browser confirms the declaration it was sent.
        before = dict(textarea._props["line-anchors"])
        _fire_editor_event(textarea, "anchor-positions", {"anchors": before})
        motion_recorder.record_action("io", port=0, state=1)
        # Its next report is of those same anchors, from before the write.
        _fire_editor_event(textarea, "anchor-positions", {"anchors": before})
        motion_recorder.record_action("io", port=0, state=0)
        lines = str(textarea.value).split("\n")
        first = next(i for i, line in enumerate(lines) if "write_io(0, 1)" in line)
        second = next(i for i, line in enumerate(lines) if "write_io(0, 0)" in line)
        assert second == first + 1 or (
            second > first and "sleep" in lines[first + 1]
        ), lines
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()


@pytest.mark.integration
async def test_anchors_moved_by_the_write_they_were_declared_with_do_not_move_the_take(
    user: User,
):
    """The browser can place the anchors declared with a write on the text
    from before it, report them there, then move them when the write lands.
    Taking the moved positions put the next recorded line a line too low,
    appended below the with block without its indentation."""
    from tests.test_editor_integration import _fire_editor_event

    textarea = await _open_program(user, PROGRAM)
    _set_cursor_line(textarea, 4)
    waldoctl.commander.programs.active.dry_run.final_joints_rad = None
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    try:
        lines = str(textarea.value).split("\n")
        assert "Recording start position" in lines[4], textarea.value
        declared = dict(textarea._props["line-anchors"])
        _fire_editor_event(textarea, "anchor-positions", {"anchors": declared})
        moved = {k: v + 1 if k.startswith("__") else v for k, v in declared.items()}
        _fire_editor_event(textarea, "anchor-positions", {"anchors": moved})
        motion_recorder.record_action("io", port=0, state=1)
        lines = str(textarea.value).split("\n")
        assert lines[5] == "    rbt.write_io(0, 1)", textarea.value
        assert staged_lines(textarea) == {5, 6}, textarea.value
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()


@pytest.mark.integration
async def test_a_capture_that_finishes_converting_after_its_take_ended_is_dropped(
    user: User, tmp_path, monkeypatch
):
    """Converting a captured span runs in a thread, so Keep, Undo or a tab
    switch can land before it returns. A result arriving then went into
    whatever program was in front, as a new take."""
    import threading

    from waldo_commander.services import motion_recorder as recorder_module

    entered, release = threading.Event(), threading.Event()
    real = recorder_module.span_to_lines

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(30), "the test never released the conversion"
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder_module, "span_to_lines", gated)
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    textarea = await _open_program(user, PROGRAM)
    program = waldoctl.commander.programs.active
    assert program is not None
    program.dry_run.playback.active_cursor_line = 4
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert is_any_program_recording()
    try:
        client = waldoctl.commander.client
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8.0
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(entered.is_set, timeout_s=20), (
            "the span never started converting"
        )
        user.find(marker="staged-keep").click()
        await asyncio.sleep(0.1)
        assert not is_any_program_recording()
        kept = str(textarea.value)
        other = waldoctl.commander.programs.new(
            source="print('other')\n", filename="other.py"
        )
        waldoctl.commander.programs.switch(other.id)
        await asyncio.sleep(0.1)
        in_front = ui_state.active_textarea
        in_front_text = str(in_front.value)
        release.set()
        await asyncio.sleep(1.5)
        assert other.source == "print('other')\n", other.source
        assert str(in_front.value) == in_front_text
        assert str(textarea.value) == kept
    finally:
        release.set()
        if is_any_program_recording():
            motion_recorder.toggle_recording()
