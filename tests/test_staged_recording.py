"""What a recording session writes stays marked in the editor until it is kept."""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.editor_events import (
    _fire_editor_event,
    _set_cursor_line,
    _set_selection,
)
from tests.helpers.recording import staged_lines
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


async def _toggle_record(user: User, recording: bool) -> None:
    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert is_any_program_recording() is recording


@pytest.mark.integration
async def test_recorded_lines_are_staged_until_kept_and_undo_takes_them_out(
    user: User,
):
    """Recording shows in the button and a notice; the take's steps land
    below the cursor in the order they happened, leave the cursor alone, and
    stay marked until kept: Undo takes them out, Keep (even mid-take) keeps
    them. Recording over a selection replaces it, and Undo puts it back.
    With the program panel covered, a recorded step flashes the program tab."""
    textarea = await _open_program(user, PROGRAM)
    program = waldoctl.commander.programs.active
    assert program is not None
    editor = ui_state.editor_panel
    record_btn = editor.playback.record_btn
    ui_state.program_panel_visible = True
    _set_cursor_line(textarea, 3)
    # No simulation end position to match, so the take opens with one.
    program.dry_run.final_joints_rad = None
    original = str(textarea.value)
    assert not is_any_program_recording()
    assert "recording" not in record_btn.classes
    assert editor.playback._recording_notification is None

    await _toggle_record(user, True)
    # Recording shows in the button's fill, not only in a pulse that
    # reduced motion switches off.
    assert (record_btn.props["color"], record_btn.props["text-color"]) == (
        "wc-record",
        "wc-on-fill",
    )
    assert "recording" in record_btn.classes
    assert editor.playback._recording_notification is not None
    await user.should_see("Recording")

    # Another tab covers the program column: the step lands unseen, so the
    # program tab flashes.
    assert "tab-flash" not in ui_state._program_tab.classes
    ui_state.program_panel_visible = False
    motion_recorder.record_action("io", port=0, state=1)
    assert "tab-flash" in ui_state._program_tab.classes
    ui_state.program_panel_visible = True
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    await _toggle_record(user, False)
    assert (record_btn.props["color"], record_btn.props["text-color"]) == (
        "wc-control",
        "wc-text",
    )
    assert "recording" not in record_btn.classes
    assert editor.playback._recording_notification is None

    written = str(textarea.value).split("\n")
    before = original.split("\n")
    assert written[:3] == before[:3], "lines above the cursor intact"
    tail = written.index(before[3])
    assert written[tail:] == before[3:], "the original tail stays below every step"
    inserted = written[3:tail]
    assert inserted and inserted[0].startswith("    rbt."), (
        "recorded code lands directly below the cursor line"
    )
    anchor = next(
        (i for i, ln in enumerate(inserted) if "Recording start position" in ln), None
    )
    io = next(
        (i for i, ln in enumerate(inserted) if ln == "    rbt.write_io(0, 1)"), None
    )
    move = next(
        (
            i
            for i, ln in enumerate(inserted)
            if "rbt.move_" in ln and "Recording start position" not in ln
        ),
        None,
    )
    assert None not in (anchor, io, move), inserted
    assert anchor < io < move, "steps stay in chronological order"
    assert program.dry_run.playback.active_cursor_line == 3, (
        "recording must not move the user's cursor"
    )

    # Stopping does not decide: the lines stay marked, the toolbar makes way
    # for Keep and Undo, and the program can be played back meanwhile.
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
    await _toggle_record(user, True)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    kept = str(textarea.value)
    assert kept != original
    user.find(marker="staged-keep").click()
    await asyncio.sleep(0.1)
    assert not is_any_program_recording()
    assert str(textarea.value).startswith(kept.rstrip("\n"))
    assert not staged_lines(textarea)
    assert "recording" not in record_btn.classes
    assert editor.playback._recording_notification is None

    # Recording over a selection: the take starts where the arm is, in place
    # of the selected lines.
    source = (
        "from parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        "    rbt.home()\n"
        "    rbt.move_j([10, -90, 180, 0, 0, 180], speed=0.5)\n"
        "    rbt.move_j([20, -90, 180, 0, 0, 180], speed=0.5)\n"
        "    rbt.delay(1.0)\n"
    )
    textarea.value = source
    await asyncio.sleep(0)
    _set_selection(textarea, 4, 5)
    await _toggle_record(user, True)
    lines = str(textarea.value).split("\n")
    assert "Recording start position" in lines[3], textarea.value
    assert "[10, -90" not in textarea.value and "[20, -90" not in textarea.value
    motion_recorder.record_action("io", port=0, state=1)
    await _toggle_record(user, False)

    user.find(marker="staged-undo").click()
    await asyncio.sleep(0)
    assert str(textarea.value) == source, (
        "Undo restores the re-recorded lines as they were"
    )

    _set_selection(textarea, 4, 5)
    await _toggle_record(user, True)
    assert editor.selection() is None, "Record used the selection"
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
async def test_the_take_follows_the_anchors_the_browser_reports(user: User):
    """The recorder reads back the browser's echo of its line anchors. An
    edit above the take moves where the next step lands. A report of the
    anchors from before a write, or of positions that write then moved, does
    not move the take. Undo leaves lines the operator removed or changed."""
    from waldo_commander.services.motion_recorder import _RECORD_ANCHOR_ID

    textarea = await _open_program(user, "# head\n# taught spot\n# tail\n")
    program = waldoctl.commander.programs.active
    assert program is not None

    async def record_at(line: int) -> None:
        _set_cursor_line(textarea, line)
        program.dry_run.final_joints_rad = None
        await _toggle_record(user, True)

    def echo(anchors: dict) -> None:
        _fire_editor_event(textarea, "anchor-positions", {"anchors": anchors})

    async def keep() -> None:
        user.find(marker="staged-keep").click()
        await asyncio.sleep(0.1)
        assert motion_recorder.session is None

    # The user types two lines at the top mid-session; the browser remaps
    # the session's cursor anchor and echoes the shifted position.
    await record_at(2)
    tracked = textarea._props["line-anchors"].get(_RECORD_ANCHOR_ID)
    assert tracked, "session cursor must be declared as a line anchor"
    echo(dict(textarea._props["line-anchors"]))
    textarea.value = "# note 1\n# note 2\n" + str(textarea.value)
    echo({**textarea._props["line-anchors"], _RECORD_ANCHOR_ID: tracked + 2})
    motion_recorder.record_action("io", port=1, state=1)
    await _toggle_record(user, False)
    lines = textarea.value.splitlines()
    io_idx = lines.index("rbt.write_io(1, 1)")
    assert io_idx == tracked + 2, (
        f"recorded step must land below the shifted anchor line: {lines}"
    )
    assert lines.index("# tail") > io_idx, "original tail stays below the step"
    assert _RECORD_ANCHOR_ID not in textarea._props["line-anchors"], (
        "stopping the session must retract its anchor"
    )
    await keep()

    # After the recorder writes, the browser can report the anchors as they
    # were before the write reaches the server's newer declaration. Taking
    # that report as current put the next line above the last one.
    textarea.value = PROGRAM
    await asyncio.sleep(0)
    await record_at(4)
    before = dict(textarea._props["line-anchors"])
    echo(before)
    motion_recorder.record_action("io", port=0, state=1)
    echo(before)
    motion_recorder.record_action("io", port=0, state=0)
    lines = str(textarea.value).split("\n")
    first = next(i for i, line in enumerate(lines) if "write_io(0, 1)" in line)
    second = next(i for i, line in enumerate(lines) if "write_io(0, 0)" in line)
    assert second == first + 1 or (second > first and "sleep" in lines[first + 1]), (
        lines
    )
    await _toggle_record(user, False)
    await keep()

    # The browser can place the anchors declared with a write on the text from
    # before it, report them there, then move them when the write lands.
    # Taking the moved positions put the next line a line too low, below the
    # with block without its indentation.
    textarea.value = PROGRAM
    await asyncio.sleep(0)
    await record_at(4)
    lines = str(textarea.value).split("\n")
    assert "Recording start position" in lines[4], textarea.value
    declared = dict(textarea._props["line-anchors"])
    echo(declared)
    echo({k: v + 1 if k.startswith("__") else v for k, v in declared.items()})
    motion_recorder.record_action("io", port=0, state=1)
    lines = str(textarea.value).split("\n")
    assert lines[5] == "    rbt.write_io(0, 1)", textarea.value
    assert staged_lines(textarea) == {5, 6}, textarea.value
    await _toggle_record(user, False)
    await keep()

    # Undo takes out what the take wrote. A block the operator deleted is not
    # there to take, and one the operator edited is theirs now.
    textarea.value = PROGRAM
    await asyncio.sleep(0)

    async def take() -> list[str]:
        await record_at(3)
        motion_recorder.record_action("io", port=0, state=1)
        await _toggle_record(user, False)
        echo(dict(textarea._props["line-anchors"]))
        return str(textarea.value).split("\n")

    lines = await take()
    marked = sorted(staged_lines(textarea))
    first, last = marked[0], marked[-1]
    block = motion_recorder.session.blocks[0]
    # The browser drops the anchor the deletion spans and slides the one it
    # starts at to the next line.
    removed = lines[: first - 1] + lines[last:]
    textarea.value = "\n".join(removed)
    anchors = dict(textarea._props["line-anchors"])
    anchors.pop(block.last_id)
    anchors[block.first_id] = first
    echo(anchors)
    user.find(marker="staged-undo").click()
    await asyncio.sleep(0)
    assert str(textarea.value).split("\n") == removed, textarea.value

    lines = await take()
    marked = sorted(staged_lines(textarea))
    edited = list(lines)
    edited[marked[-1] - 1] += "  # checked on the bench"
    textarea.value = "\n".join(edited)
    echo(dict(textarea._props["line-anchors"]))
    user.find(marker="staged-undo").click()
    await asyncio.sleep(0)
    assert str(textarea.value).split("\n") == edited, textarea.value


def _captured_moves(textarea, *, kept_only: bool = False) -> list[str]:
    """Move lines a take captured, optionally only those no take has staged."""
    staged = staged_lines(textarea) if kept_only else set()
    return [
        line
        for n, line in enumerate(str(textarea.value).split("\n"), start=1)
        if "rbt.move_" in line
        and "Recording start position" not in line
        and line not in PROGRAM.split("\n")
        and n not in staged
    ]


@pytest.mark.integration
async def test_captured_motion_survives_a_pass_keep_mid_move_and_a_quick_restart(
    user: User, tmp_path, monkeypatch
):
    """A simulation pass keeps the anchors of the lines a take captured; Keep
    with the arm still moving keeps the motion so far; and Stop then Record
    in one tick leaves the motion under way in the take being stopped."""
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    textarea = await _open_program(user, PROGRAM)
    program = waldoctl.commander.programs.active
    assert program is not None
    client = waldoctl.commander.client
    program.dry_run.playback.active_cursor_line = 3
    await _toggle_record(user, True)
    try:
        # Every simulation pass re-declares the editor's anchors for its
        # targets. Replacing the whole set dropped the anchors of the lines
        # the take wrote, so the recorder then edited them by stale numbers.
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8.0
        index = await client.move_j(target, duration=1.0)
        assert await client.wait_command(index, timeout=10)
        assert await wait_until(
            lambda: any(b.kind == "capture" for b in motion_recorder.session.blocks),
            timeout_s=20,
        ), "the uncommanded move was not captured"
        capture = next(b for b in motion_recorder.session.blocks if b.kind == "capture")
        await simulation.run_simulation(program.id)
        await asyncio.sleep(0)
        assert {capture.first_id, capture.last_id} <= set(recorder_anchors(textarea)), (
            "a simulation pass dropped the anchors of the captured lines: "
            f"{recorder_anchors(textarea)}"
        )

        # Keep ends the take with the arm still moving: the motion up to then
        # is part of the take and lands, kept, where it was captured.
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] -= 12.0
        index = await client.move_j(target, duration=2.0)
        assert await client.wait_status(
            lambda s: s.angles[0] < start[0] - 3.0, timeout=10
        )
        landed = len(_captured_moves(textarea))
        user.find(marker="staged-keep").click()
        await asyncio.sleep(0)
        assert not is_any_program_recording()
        assert motion_recorder.session is None
        assert await client.wait_command(index, timeout=15)
        assert await wait_until(
            lambda: len(_captured_moves(textarea)) > landed, timeout_s=30
        ), f"the motion under way at Keep was dropped:\n{textarea.value}"
        assert not staged_lines(textarea)

        # Stopping takes the motion under way into the take being stopped, at
        # the place it had reached. Recording again at once must not hand
        # that span to the new take.
        program.dry_run.playback.active_cursor_line = 3
        await _toggle_record(user, True)
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 12.0
        index = await client.move_j(target, duration=2.0)
        assert await client.wait_status(
            lambda s: s.angles[0] > start[0] + 3.0, timeout=10
        )
        kept = len(_captured_moves(textarea, kept_only=True))
        motion_recorder.toggle_recording()
        motion_recorder.toggle_recording()
        assert is_any_program_recording()
        take = motion_recorder.session
        assert await client.wait_command(index, timeout=15)
        assert await wait_until(
            lambda: len(_captured_moves(textarea, kept_only=True)) > kept,
            timeout_s=30,
        ), f"the first take's motion did not land in it:\n{textarea.value}"
        assert motion_recorder.session is take
        assert not any(
            block.recording is not None
            and block.recording.samples[0].joints_deg == pytest.approx(start, abs=0.5)
            for block in take.blocks
        ), "the first take's motion was staged in the second take"
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()


@pytest.mark.integration
async def test_a_take_selects_the_tool_on_the_arm_and_undo_restores_the_programs(
    user: User,
):
    """Recording with a tool on the arm that the program does not name adds
    its select_tool before the first move, in the move's block. Where the
    program names another tool, the take rewrites that line and Undo puts
    the program's own back."""
    unnamed = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.home()\n"
    )
    named = (
        "from parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        '    rbt.select_tool("NONE")\n'
        "    rbt.home()\n"
    )
    textarea = await _open_program(user, unnamed)
    client = waldoctl.commander.client

    async def put_on_arm(key: str) -> None:
        # The preview applies the program's first tool to the arm; let it do
        # that before another tool goes on.
        assert await wait_until(
            lambda: simulation._simulation_debounce_timer is None, timeout_s=15
        )
        index = await client.select_tool(key)
        assert await client.wait_command(index, timeout=5)
        assert await wait_until(
            lambda: waldoctl.commander.status.tool.key == key, timeout_s=5
        )

    try:
        await put_on_arm("PNEUMATIC")
        _set_cursor_line(textarea, 3)
        await _toggle_record(user, True)
        lines = str(textarea.value).split("\n")
        assert lines[2] == '    rbt.select_tool("PNEUMATIC")', textarea.value
        compile(str(textarea.value), "program.py", "exec")
        user.find(marker="staged-undo").click()
        await asyncio.sleep(0)
        assert motion_recorder.session is None

        textarea.value = named
        await asyncio.sleep(0)
        await put_on_arm("PNEUMATIC")
        _set_cursor_line(textarea, 4)
        await _toggle_record(user, True)
        assert 'rbt.select_tool("PNEUMATIC")' in str(textarea.value), textarea.value
        user.find(marker="staged-undo").click()
        await asyncio.sleep(0)
        assert str(textarea.value) == named
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()
        index = await client.select_tool("NONE")
        assert await client.wait_command(index, timeout=5)


@pytest.mark.integration
async def test_a_take_survives_a_page_reload(user: User):
    """A reload builds new editors while the take goes on staged. Keep and
    Undo act on the editor the program has now, not on the one it had."""
    textarea = await _open_program(user, PROGRAM)
    program = waldoctl.commander.programs.active
    assert program is not None
    original = str(textarea.value)
    _set_cursor_line(textarea, 3)
    await _toggle_record(user, True)
    motion_recorder.record_action("io", port=0, state=1)
    await _toggle_record(user, False)
    assert str(textarea.value) != original

    # User.open leaves the previous simulated page alive. Close it first so
    # its heartbeat cannot schedule a competing reload of the same user.
    previous_page = user.client
    assert previous_page is not None
    for handler in previous_page.disconnect_handlers:
        previous_page.safe_invoke(handler)
    previous_page.delete()
    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    rebuilt = ui_state.textareas_by_tab[program.id]
    assert rebuilt is not textarea
    assert staged_lines(rebuilt), "the reloaded editor does not mark the take"
    user.find(marker="staged-undo").click()
    await asyncio.sleep(0)
    assert str(rebuilt.value) == original, rebuilt.value
    assert program.source == original


@pytest.mark.integration
async def test_a_recorded_pause_runs_in_a_program_that_never_imported_time(
    user: User,
):
    """The time between two recorded steps is written as a sleep. A program
    that never imported ``time`` gets the import with the first one, and only
    once, and the kept take runs to the end."""
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running

    textarea = await _open_program(user, PROGRAM)
    program = waldoctl.commander.programs.active
    assert program is not None
    _set_cursor_line(textarea, 3)
    await _toggle_record(user, True)
    for state in (1, 0, 1):
        motion_recorder.record_action("io", port=0, state=state)
        await asyncio.sleep(0.2)
    user.find(marker="staged-keep").click()
    await asyncio.sleep(0)
    assert not is_any_program_recording()

    lines = [line.strip() for line in str(textarea.value).split("\n")]
    assert sum(line.startswith("time.sleep(") for line in lines) == 2, textarea.value
    assert lines.count("import time") == 1, textarea.value

    await script_exec.start()
    async with asyncio.timeout(60):
        while is_any_program_running():
            await asyncio.sleep(0.05)
    stderr = "\n".join(
        entry.text for entry in program.log.entries if entry.stream == "stderr"
    )
    assert script_exec.last_exit_code == 0, stderr
