"""Integration tests for the program editor via UI."""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.editor_events import (
    _fire_editor_event,
    _set_cursor_line,
    _set_selection,
)
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    simulate_click,
    wait_for_app_ready,
    wait_for_motion_stable,
    wait_for_motion_start,
    wait_until,
)
from waldo_commander.services.programs import (
    is_any_program_running,
)


@pytest.mark.integration
async def test_editor_controls_and_tabs(user: User) -> None:
    """The program tab opens the editor and its controls: the log chevron
    flips, the commands menu lists skills, an edit marks the tab dirty, and
    a tab opens and closes by button. Another tab covers the panel and the
    program tab brings it back."""
    import waldoctl

    from waldo_commander.components.log_panel import log_panel
    from waldo_commander.state import ui_state

    await user.open("/")
    await user.should_see(marker="tab-program")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    for marker in (
        "editor-play-btn",
        "editor-record-btn",
        "editor-log-toggle",
        "editor-new-tab-btn",
        "editor-save-btn",
        "editor-open-btn",
        "editor-commands-btn",
    ):
        await user.should_see(marker=marker)
    editor = ui_state.editor_panel
    assert editor is not None

    # The chevron points down to expand and up to collapse.
    assert log_panel._log_expanded is False
    log_toggle_btn = log_panel.log_toggle_btn
    assert log_toggle_btn is not None
    assert log_toggle_btn._props.get("icon") == "expand_more"
    user.find(marker="editor-log-toggle").click()
    await asyncio.sleep(0.1)
    assert log_panel._log_expanded is True
    assert log_toggle_btn._props.get("icon") == "expand_less"
    user.find(marker="editor-log-toggle").click()
    await asyncio.sleep(0.1)
    assert log_panel._log_expanded is False
    assert log_toggle_btn._props.get("icon") == "expand_more"

    user.find(marker="editor-commands-btn").click()
    await asyncio.sleep(0)
    assert user.find(marker="editor-skill-waldo.retract").elements

    tab = waldoctl.commander.programs.active
    assert tab is not None and tab.is_dirty is False
    dirty_dot = editor._tab_widgets[tab.id]["dirty_dot"]
    assert dirty_dot.visible is False
    textarea = ui_state.active_textarea
    assert textarea is not None
    textarea.value = str(textarea.value) + "\n# Modified"
    assert tab.is_dirty is True
    assert await wait_until(lambda: dirty_dot.visible, timeout_s=2), (
        "an edit must show the dirty dot"
    )

    initial = len(waldoctl.commander.programs.items)
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0)
    assert len(waldoctl.commander.programs.items) == initial + 1
    new_tab = waldoctl.commander.programs.active
    assert new_tab is not None and new_tab.id != tab.id
    user.find(marker=f"editor-tab-close-{new_tab.id}").click()
    # Close is deferred through ui.timer(0).
    assert await wait_until(
        lambda: len(waldoctl.commander.programs.items) == initial, timeout_s=4
    ), "closing the tab did not remove it"
    assert waldoctl.commander.programs.get(new_tab.id) is None

    user.find(marker="tab-io").click()
    await asyncio.sleep(0.1)
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    await user.should_see(marker="editor-play-btn")


@pytest.mark.integration
async def test_run_button_toggles(user: User) -> None:
    """Play runs the program, shows Stop and fires the program's own step
    channel (``Playback.add_step_listener``) on the start edge; a second
    press pauses without ending the run."""
    import waldoctl

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"
    assert is_any_program_running() is False, "Script should not be running initially"
    stop_btn = editor.playback.stop_btn
    assert stop_btn is not None, "Stop button reference should exist"
    assert stop_btn.visible is False, "Stop button should be hidden initially"

    program = waldoctl.commander.programs.active
    assert program is not None
    fired = 0

    def _on_step() -> None:
        nonlocal fired
        fired += 1

    program.dry_run.playback.add_step_listener(_on_step)
    play_btn = user.find(marker="editor-play-btn")
    try:
        play_btn.click()
        assert await wait_until(is_any_program_running, timeout_s=10), (
            "Script should be running after clicking play"
        )
        assert stop_btn.visible is True, "Stop button should be visible when running"
        assert await wait_until(lambda: fired > 0, timeout_s=10), (
            "per-program step channel never fired during the run"
        )

        # A second press pauses: the run goes on, held.
        play_btn.click()
        await asyncio.sleep(0.2)
        assert is_any_program_running() is True, (
            "Script should still be running (paused)"
        )
    finally:
        program.dry_run.playback.remove_step_listener(_on_step)
        if is_any_program_running():
            user.find(marker="editor-stop-btn").click()
            await wait_until(lambda: not is_any_program_running(), timeout_s=10)


_THREE_MOVE_SCRIPT = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
rbt.move_j([95, -95, 185, -5, -5, 185], speed=1.0)
rbt.move_j([90, -90, 180, 0, 0, 180], speed=1.0)
"""

_SLEEP_SCRIPT = """from parol6 import RobotClient
import time
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
time.sleep(0.5)
rbt.move_j([90, -90, 180, 0, 0, 180], speed=1.0)
"""


async def _open_program_tab(user: User):
    """Open the editor on its active program with the simulator ready.
    Returns ``(editor, tab)``."""
    import waldoctl

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None
    tab = waldoctl.commander.programs.active
    assert tab is not None
    assert ui_state.active_textarea is not None
    return editor, tab


async def _simulate(editor, tab, script: str) -> None:
    """Load *script* into the tab and dry-run simulate it, up to a built
    playback timeline."""
    from waldo_commander.components.simulation_engine import simulation as _sim
    from waldo_commander.state import ui_state

    ui_state.active_textarea.value = script
    tab.source = script

    await _sim.run_simulation()
    for _ in range(30):
        if tab.dry_run.path_segments:
            break
        await asyncio.sleep(0.1)
    assert tab.dry_run.path_segments, "simulation produced no path segments"
    # The timeline is lazy — built on the first playback interaction. Build it
    # up front exactly as pressing any step control would.
    assert editor.playback._ensure_timeline() is not None, "timeline build failed"


async def _open_simulated_three_move_program(
    user: User, script: str = _THREE_MOVE_SCRIPT
):
    """Open the editor, load a three-move program, and dry-run simulate it.
    Returns ``(editor, tab)`` once the playback timeline is built."""
    editor, tab = await _open_program_tab(user)
    await _simulate(editor, tab, script)
    return editor, tab


def _move_commands(tab) -> list[int]:
    """The program indices of the plan's moves, in order: what a live run
    reports as ``executing_command`` for the k-th move. Program indices
    count every command the backend queued, the preview's own tool and
    world setup included, so they are read off the record, never assumed."""
    record = tab.dry_run.commanded
    assert record is not None
    return [b.command for b in record.blocks if b.move_type is not None]


async def _wait_j1_near(target: float, timeout_s: float = 3.0) -> None:
    """Wait until joint 1 settles within 1 degree of ``target``."""
    import waldoctl

    interval = 0.05
    j1 = float(waldoctl.commander.status.joints.angles.deg[0])
    for _ in range(int(timeout_s / interval)):
        if abs(j1 - target) < 1.0:
            return
        await asyncio.sleep(interval)
        j1 = float(waldoctl.commander.status.joints.angles.deg[0])
    raise AssertionError(f"J1 never reached {target}: J1={j1}")


@pytest.mark.integration
async def test_step_program_runs_one_command_per_press(user: User) -> None:
    """The Step-program button executes exactly one program command per press.

    From idle, a press launches the subprocess with the stepping IPC left
    paused: the first motion command runs, then the script blocks — running
    but not playing, not finished. A second press advances exactly one more
    command. While the program runs, the sim Previous-step button is hidden
    (live stepping is forward-only); it reappears after the run stops.
    """
    import waldoctl

    editor, tab = await _open_simulated_three_move_program(user)

    prev_btn = editor.playback.prev_btn
    assert prev_btn is not None
    assert prev_btn.visible is True, "prev button should be visible when idle"

    pb = tab.dry_run.playback
    moves = _move_commands(tab)

    async def wait_step_complete(step: int, timeout_s: float) -> None:
        interval = 0.05
        for _ in range(int(timeout_s / interval)):
            if pb.executing_command == step and pb.executing_step_at_end:
                return
            await asyncio.sleep(interval)
        tail = [entry.text for entry in tab.log.entries[-5:]]
        raise TimeoutError(
            f"step {step} never completed: index={pb.executing_command}, "
            f"at_end={pb.executing_step_at_end}, running={is_any_program_running()}, "
            f"log tail={tail}"
        )

    try:
        # First press from idle: subprocess starts paused, runs command #1 only.
        user.find(marker="editor-step-program").click()
        await wait_step_complete(moves[0], timeout_s=30.0)

        assert pb.is_playing is False, "paused start must not enter play mode"
        assert prev_btn.visible is False, "prev button must hide during a live run"
        await _wait_j1_near(85.0)

        # Exactly one command: even given time to continue, the script must
        # still be blocked on command #1.
        await asyncio.sleep(0.5)
        assert pb.executing_command == moves[0], (
            "paused start ran more than one command"
        )
        assert is_any_program_running() is True, "program must be paused, not finished"

        # A preview scrub must not reposition the controller while Python owns it,
        # including between steps when the native queue is idle.
        timeline = editor.playback._timeline
        assert timeline is not None
        slider = next(iter(user.find(marker="editor-scrub-slider").elements))
        with slider.client:
            slider.set_value(timeline.total_duration)
        await asyncio.sleep(0)
        teleport = editor.playback._teleport_task
        if teleport is not None:
            await teleport
        assert not await waldoctl.commander.client.wait_status(
            lambda s: abs(s.angles[0] - 85.0) > 1.0, timeout=1
        ), "preview scrubbing moved the controller during a paused Python run"

        # Second press while running-paused: exactly one more command.
        user.find(marker="editor-step-program").click()
        await wait_step_complete(moves[1], timeout_s=15.0)
        assert pb.is_playing is False
        await _wait_j1_near(95.0)
        assert is_any_program_running() is True, "still paused after the second step"
    finally:
        if is_any_program_running():
            user.find(marker="editor-stop-btn").click()
            for _ in range(50):
                if not is_any_program_running():
                    break
                await asyncio.sleep(0.1)

    assert is_any_program_running() is False
    assert prev_btn.visible is True, "prev button should reappear after the run"


_WITHDRAW_TWICE = """from waldoctl.client import RobotClient as Client
from waldoctl.skills import skill
from waldo_commander.skills import retract

@skill(id="test.withdraw_twice", version="1.0.0")
async def withdraw_twice(rbt: Client):
    await retract.async_call(rbt, distance_mm=2.0)
    await retract.async_call(rbt, distance_mm=2.0)
"""

_WITHDRAW_SYNC = (
    _WITHDRAW_TWICE
    + """
from parol6 import RobotClient
with RobotClient() as rbt:
    rbt.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
    withdraw_twice(rbt)
"""
)

_WITHDRAW_ASYNC = (
    _WITHDRAW_TWICE
    + """
import asyncio
from parol6 import AsyncRobotClient
async def main():
    async with AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        await withdraw_twice.async_call(rbt)
asyncio.run(main())
"""
)


@pytest.mark.integration
async def test_nested_imported_skill_keeps_preview_and_gui_steps(user: User) -> None:
    """A skill a program defines and calls steps one inner motion per press,
    from a sync program (through the wrapper's ``run_skill``) and from an
    async one alike, and its progress and completion reach the program log."""
    import numpy as np
    import waldoctl

    editor, tab = await _open_program_tab(user)
    pb = tab.dry_run.playback

    async def wait_step(step: int) -> None:
        async with asyncio.timeout(30):
            while pb.executing_command != step or not pb.executing_step_at_end:
                await asyncio.sleep(0.05)

    for body in (_WITHDRAW_SYNC, _WITHDRAW_ASYNC):
        await _simulate(editor, tab, body)
        assert tab.dry_run.total_steps == 3
        moves = _move_commands(tab)
        earlier = list(tab.log.entries)

        def logged(text: str) -> bool:
            return any(
                text in entry.text
                for entry in tab.log.entries
                if not any(entry is seen for seen in earlier)
            )

        try:
            user.find(marker="editor-step-program").click()
            # The launch resets the executing command, so the last run's
            # final step cannot be mistaken for this one's first.
            assert await wait_until(is_any_program_running, timeout_s=30)
            await wait_step(moves[0])
            first = await waldoctl.commander.client.pose()
            assert first is not None

            user.find(marker="editor-step-program").click()
            await wait_step(moves[1])
            second = await waldoctl.commander.client.pose()
            assert second is not None
            assert np.linalg.norm(np.array(second[:3]) - first[:3]) == pytest.approx(
                2.0, abs=0.3
            )
            assert is_any_program_running() and not pb.is_playing

            user.find(marker="editor-step-program").click()
            await wait_step(moves[2])
            third = await waldoctl.commander.client.pose()
            assert third is not None
            assert np.linalg.norm(np.array(third[:3]) - second[:3]) == pytest.approx(
                2.0, abs=0.3
            )
            assert is_any_program_running() and not pb.is_playing
            assert logged("waldo.retract progress (0%): Moving along tool Z"), (
                "skill progress must reach the program log through subprocess events"
            )
            user.find(marker="editor-play-btn").click()
            async with asyncio.timeout(15):
                while is_any_program_running():
                    await asyncio.sleep(0.05)
            assert logged("test.withdraw_twice completed"), (
                "terminal skill events must survive subprocess cleanup"
            )
        finally:
            if is_any_program_running():
                user.find(marker="editor-stop-btn").click()
                async with asyncio.timeout(10):
                    while is_any_program_running():
                        await asyncio.sleep(0.05)


_BLENDED_SCRIPT = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0, r=15, wait=False)
rbt.move_j([95, -95, 185, -5, -5, 185], speed=1.0, r=15, wait=False)
rbt.move_j([90, -90, 180, 0, 0, 180], speed=1.0)
rbt.wait_motion()
"""


@pytest.mark.integration
async def test_step_program_blended_moves_run_one_per_press(user: User) -> None:
    """Stepping a blended program executes exactly one exact-stop move per
    press (industrial step-mode semantics: blends only apply in play mode).

    Without the paused-mode member strip, the wrapper's blend branch executes
    r>0 moves with no pause check and one press free-runs the whole program.
    Assertions read the controller directly: during a live run the published
    angles show the preview timeline's pose, not the robot's.
    """
    from waldo_commander.state import ui_state

    editor, tab = await _open_simulated_three_move_program(user, script=_BLENDED_SCRIPT)
    client = ui_state.control_panel.client

    async def wait_controller_j1(target: float, timeout_s: float = 30.0) -> None:
        j1 = None
        for _ in range(int(timeout_s / 0.1)):
            s = await client.status()
            j1 = s.angles[0] if s else None
            if j1 is not None and abs(j1 - target) < 1.0:
                return
            await asyncio.sleep(0.1)
        tail = [entry.text for entry in tab.log.entries[-5:]]
        raise TimeoutError(
            f"controller J1 never reached {target}: J1={j1}, "
            f"running={is_any_program_running()}, log tail={tail}"
        )

    try:
        # Each press executes exactly one group member as an exact stop.
        user.find(marker="editor-step-program").click()
        await wait_controller_j1(85.0)
        await asyncio.sleep(0.5)
        s = await client.status()
        assert abs(s.angles[0] - 85.0) < 1.0, "one press must run one member only"
        assert is_any_program_running() is True, "program must be paused, not finished"

        user.find(marker="editor-step-program").click()
        await wait_controller_j1(95.0)
        assert is_any_program_running() is True, "still paused after the second member"

        # Third press: the non-blended move closes the group, whose events
        # carry the head's command; its own events carry command 2.
        user.find(marker="editor-step-program").click()
        await wait_controller_j1(90.0)
        assert tab.dry_run.playback.executing_command == _move_commands(tab)[2], (
            "the closing move is the program's third move"
        )
        assert is_any_program_running() is True

        # Play resumes normal execution through to completion.
        await editor.playback.toggle_play()
        for _ in range(300):
            if not is_any_program_running():
                break
            await asyncio.sleep(0.1)
        assert is_any_program_running() is False, "play should run to completion"
    finally:
        if is_any_program_running():
            user.find(marker="editor-stop-btn").click()
            for _ in range(50):
                if not is_any_program_running():
                    break
                await asyncio.sleep(0.1)


@pytest.mark.integration
async def test_simulated_program_steps_highlights_and_plays_its_preview(
    user: User,
) -> None:
    """Once a program is simulated, Step shows and is enabled; the cursor's
    move glows in the scene until the cursor leaves it; Previous scrubs back
    one segment and clamps at step 0, disabled there, its enabled state
    following slider scrubs; and Play plays the preview, not the script."""
    from waldo_commander.state import ui_state

    editor, tab = await _open_program_tab(user)
    pbc = editor.playback
    assert pbc.next_btn is not None, "Step button reference should exist"
    assert pbc.next_btn.visible is False, (
        "Step button should be hidden before simulation"
    )
    await _simulate(editor, tab, _THREE_MOVE_SCRIPT)
    assert pbc.next_btn.visible is True, "Step button should show once there are steps"
    assert pbc.next_btn._props.get("disable") is not True, "Step should be enabled"
    assert tab.dry_run.total_steps > 0

    scene = ui_state.urdf_scene
    textarea = ui_state.active_textarea
    assert scene is not None and textarea is not None

    def line_colors(line: int) -> list:
        return [
            obj.color
            for i in scene._line_to_segments.get(line, ())
            if i < len(scene._rendered_segments)
            and scene._rendered_segments[i] is not None
            for obj in scene._rendered_segments[i].objects
        ]

    assert await wait_until(lambda: bool(line_colors(3)), timeout_s=10), (
        "the first move's path was never drawn"
    )
    plain = line_colors(3)
    _set_cursor_line(textarea, 3)
    assert await wait_until(
        lambda: any(a != b for a, b in zip(line_colors(3), plain)), timeout_s=5
    ), "the move under the cursor does not glow"
    _set_cursor_line(textarea, 1)
    assert await wait_until(lambda: line_colors(3) == plain, timeout_s=5), (
        "the glow stays after the cursor leaves the move"
    )

    prev_btn = pbc.prev_btn
    assert prev_btn is not None
    assert prev_btn.visible is True, "prev button should be visible after simulation"
    assert prev_btn._props.get("disable") is True, "prev must be disabled at step 0"
    assert tab.dry_run.playback.current_step == 0

    # Next → step 1; prev becomes enabled.
    user.find(marker="editor-step-next").click()
    await asyncio.sleep(0)
    assert tab.dry_run.playback.current_step == 1
    assert prev_btn._props.get("disable") is not True

    # Prev → back one segment, to the very start.
    user.find(marker="editor-step-prev").click()
    await asyncio.sleep(0)
    assert tab.dry_run.playback.current_step == 0
    assert tab.dry_run.playback.playback_time == 0.0
    assert prev_btn._props.get("disable") is True, "prev must re-disable at step 0"

    # Slider scrubs move current_step without a button press; the enabled
    # state must follow.
    user.find(marker="editor-step-next").click()
    await asyncio.sleep(0)
    assert tab.dry_run.playback.current_step == 1
    scrub_slider = pbc._scrub_slider
    assert scrub_slider is not None
    with scrub_slider.client:
        scrub_slider.value = pbc._timeline.cumulative_times[1] * 0.5
    await asyncio.sleep(0)
    assert tab.dry_run.playback.current_step == 0
    assert prev_btn._props.get("disable") is True, (
        "prev enabled state must track slider scrubs"
    )

    # A double-click race can deliver a second press at step 0 before the
    # disable round-trips to the browser; the handler clamps at the start.
    with prev_btn.client:
        pbc.step_backward()
    await asyncio.sleep(0)
    assert tab.dry_run.playback.current_step == 0
    assert tab.dry_run.playback.playback_time == 0.0

    await pbc.toggle_play()
    await asyncio.sleep(0.1)
    assert tab.dry_run.playback.is_active is True, (
        "Play should start simulation playback when steps exist"
    )
    assert is_any_program_running() is False, (
        "Script should not be running during sim playback"
    )
    await pbc.toggle_play()
    await asyncio.sleep(0)
    assert tab.dry_run.playback.is_active is False


@pytest.mark.integration
async def test_capture_pose_reteaches_replaces_and_inserts(user: User) -> None:
    """The capture-pose button stamps the current robot position into the
    program at the cursor: a bare cursor on a single-pose move re-teaches it
    in place (kwargs kept), a ranged selection is replaced wholesale by one
    fresh move, and anywhere else the pose is inserted as a new line — with
    the tooltip naming the action throughout.
    """
    import re

    import numpy as np
    import waldoctl

    from waldo_commander.components.simulation_engine import simulation as _sim
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None
    tab = waldoctl.commander.programs.active
    assert tab is not None

    move_l_line = (
        "rbt.move_l([0.000, 280.000, 250.000, 90.000, 0.000, 90.000], speed=0.5)"
    )
    move_c_line = (
        "rbt.move_c([15.000, 280.000, 255.000, 90.000, 0.000, 90.000], "
        "[0.000, 300.000, 250.000, 90.000, 0.000, 90.000], speed=0.5)"
    )
    move_rel_line = (
        "rbt.move_l([0.000, 0.000, -20.000, 0.000, 0.000, 0.000], rel=True, speed=0.5)"
    )
    script = (
        "from parol6 import RobotClient\n"
        "rbt = RobotClient()\n"
        "# approach\n"
        "rbt.move_j([85.000, -85.000, 175.000, 5.000, 5.000, 175.000], speed=0.5)\n"
        f"{move_l_line}\n"
        f"{move_c_line}\n"
        f"{move_rel_line}\n"
    )
    textarea = ui_state.active_textarea
    assert textarea is not None
    textarea.value = script
    tab.source = script

    await _sim.run_simulation()
    await asyncio.sleep(0.1)
    targets_by_line = {t.line_number: t for t in tab.dry_run.targets}
    assert {4, 5, 6, 7} <= targets_by_line.keys(), (
        f"Expected targets at lines 4-7, got {sorted(targets_by_line)}"
    )
    assert targets_by_line[6].move_type == "smooth_arc"
    # Targets are tracked beside the source, never marked in it.
    assert all(t.id.startswith("auto_") for t in tab.dry_run.targets)
    assert "# TARGET:" not in str(textarea.value)

    # The browser echoes declared anchors back via "anchor-positions"; the
    # user fixture has no JS, so replay that echo through the real event.
    _fire_editor_event(
        textarea, "anchor-positions", {"anchors": dict(textarea._props["line-anchors"])}
    )
    await asyncio.sleep(0)

    def bracket_floats(line: str) -> list[float]:
        m = re.search(r"\[([^\]]+)\]", line)
        assert m is not None, f"No bracketed list in {line!r}"
        return [float(v) for v in m.group(1).split(",")]

    tooltip = ui_state.capture_pose_tooltip
    assert tooltip is not None

    async def _tip_at(line: int, expected: str) -> None:
        """Place the cursor and wait for the capture tooltip to settle.

        The tooltip is refreshed only by cursor events, while a debounced
        re-simulation can repopulate the dry-run targets afterwards — so the
        cursor event is re-fired until the reteach state it reads is current.
        """
        for _ in range(100):
            _set_cursor_line(textarea, line)
            await asyncio.sleep(0.05)
            if tooltip.text == expected:
                return
        assert tooltip.text == expected, f"line {line}: got {tooltip.text!r}"

    # Move the robot so the current pose differs from the taught values.
    waldoctl.commander.settings.jog.joint_step_deg = 10.0
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()
    await wait_for_motion_stable(lambda: waldoctl.commander.status.joints.angles[0])

    # The jog can re-simulate (position-change checker), re-declaring anchors
    # and dropping the echoed positions; replay the browser echo again. Require
    # a non-empty target set: mid-re-simulation the targets are momentarily
    # cleared, and an empty set is trivially covered by any anchor mapping.
    for _ in range(50):
        target_ids = {t.id for t in tab.dry_run.targets}
        if target_ids and target_ids <= set(dict(textarea._props["line-anchors"])):
            break
        await asyncio.sleep(0.1)
    _fire_editor_event(
        textarea, "anchor-positions", {"anchors": dict(textarea._props["line-anchors"])}
    )
    await asyncio.sleep(0)

    # Bare cursor on the move_j line: capture re-teaches it in place.
    await _tip_at(4, editor._CAPTURE_TIP_RETEACH)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)

    n = ui_state.active_robot.joints.count
    current_angles = list(waldoctl.commander.status.joints.angles.deg[:n])
    lines = textarea.value.splitlines()
    assert lines[3].startswith("rbt.move_j("), (
        "Re-teach must keep move_j lines as move_j"
    )
    assert np.allclose(bracket_floats(lines[3]), current_angles, atol=0.1), (
        f"move_j line should hold current angles {current_angles}, got {lines[3]}"
    )
    assert "speed=0.5" in lines[3], "re-teach must keep the line's kwargs"
    assert lines[4] == move_l_line, (
        "Re-teaching the move_j line must not touch the move_l line"
    )

    # Bare cursor on the move_l line: capture writes the current WRF pose.
    await _tip_at(5, editor._CAPTURE_TIP_RETEACH)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)

    pose = waldoctl.commander.status.pose
    current_pose = [pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz]
    lines = textarea.value.splitlines()
    assert lines[4].startswith("rbt.move_l("), (
        "Re-teach must keep move_l lines as move_l"
    )
    assert np.allclose(bracket_floats(lines[4]), current_pose, atol=0.5), (
        f"move_l line should hold current WRF pose {current_pose}, got {lines[4]}"
    )
    assert lines[5] == move_c_line, (
        "Re-teaching neighbors must not touch the move_c line"
    )

    # A multi-pose arc can't be re-taught from one pose, and a rel= move
    # would be corrupted by an absolute overwrite: both fall back to insert,
    # and the tooltip says which flavor of fallback applies.
    await _tip_at(6, editor._CAPTURE_TIP_INSERT)
    await _tip_at(7, editor._CAPTURE_TIP_BLOCKED)

    # Selecting the move_l + move_c lines replaces both with one fresh move.
    src_before = textarea.value.splitlines()
    _set_selection(textarea, 5, 6)
    await asyncio.sleep(0)
    assert tooltip.text == editor._CAPTURE_TIP_REPLACE
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    lines = textarea.value.splitlines()
    assert len(lines) == len(src_before) - 1, (
        "the selected lines must collapse into one move"
    )
    assert lines[4].startswith("rbt.move_l("), lines[4]
    assert np.allclose(bracket_floats(lines[4])[:3], current_pose[:3], atol=0.5), (
        f"replacement should target the current pose, got {lines[4]}"
    )
    assert lines[5] == move_rel_line, (
        "replacement must not touch the line after the selection"
    )

    # Cursor on a plain line: nothing to re-teach, so capture inserts the pose
    # directly below the cursor and leaves every existing line untouched.
    before_insert = textarea.value.splitlines()
    await _tip_at(3, editor._CAPTURE_TIP_INSERT)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    lines = textarea.value.splitlines()
    assert len(lines) == len(before_insert) + 1, "capture on a plain line must insert"
    assert lines[3].startswith("rbt.move_l("), lines[3]
    assert lines[:3] + lines[4:] == before_insert, (
        "insert must leave existing lines untouched"
    )


@pytest.mark.integration
async def test_manual_inserts_follow_cursor(user: User) -> None:
    """Palette, gizmo, and capture-pose inserts land below the cursor line and
    consecutive inserts stay in order; with the cursor unset or on the last
    line they append at EOF. A selection stays with the tab it was made in."""
    import waldoctl

    from waldo_commander.components.editor_decorations import decorations
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    ui_state.program_panel_visible = True  # flash the lines, not the tab

    editor = ui_state.editor_panel
    tab = waldoctl.commander.programs.active
    textarea = ui_state.active_textarea
    assert editor is not None and tab is not None and textarea is not None

    textarea.value = "# alpha\n# beta\n# gamma\n"

    # Cursor unset -> palette insert appends at EOF. An unfocused
    # selection-change (CodeMirror's echo of a programmatic value update)
    # must not count as cursor placement.
    assert tab.dry_run.playback.active_cursor_line == 0
    _fire_editor_event(
        textarea,
        "selection-change",
        {"line": 1, "column": 1, "from_line": 1, "to_line": 1, "empty": True},
    )
    assert tab.dry_run.playback.active_cursor_line == 0
    with textarea.client:
        editor._insert_command("delay")
    assert textarea.value.splitlines() == [
        "# alpha",
        "# beta",
        "# gamma",
        "time.sleep(1.0)",
    ]

    # Cursor on line 1 -> gizmo target lands on line 2, which is also flashed.
    _set_cursor_line(textarea, 1)
    with textarea.client:
        line_number = editor.add_target_code(
            [100.0, 200.0, 300.0, 0.0, 0.0, 0.0], "cartesian"
        )
    assert line_number == 2
    lines = textarea.value.splitlines()
    assert lines[0] == "# alpha"
    assert lines[1].startswith("rbt.move_l([100.000, 200.000, 300.000")
    assert lines[2] == "# beta"
    assert decorations._active_flashes[-1][1] == {2}

    # A second insert without moving the cursor lands below the first one.
    with textarea.client:
        editor._insert_command("delay")
    lines = textarea.value.splitlines()
    assert lines[2] == "time.sleep(1.0)"
    assert lines[3] == "# beta"

    # Cursor on the last line -> capture pose appends at EOF.
    _set_cursor_line(textarea, len(lines))
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0)
    lines = textarea.value.splitlines()
    assert lines[-1].startswith("rbt.move_l(")
    assert lines[-2] == "time.sleep(1.0)"
    # Indented anchor: the insert matches the body's indentation instead of
    # splitting the suite at column 0.
    textarea.value = "def run():\n    rbt.home()\n"
    _set_cursor_line(textarea, 2)
    with textarea.client:
        editor._insert_command("delay")
    assert textarea.value.splitlines()[2] == "    time.sleep(1.0)"

    # A selection belongs to the tab it was made in: switching tabs leaves
    # the new tab with none, so capture inserts instead of replacing lines.
    textarea.value = "a = 1\nb = 2\nc = 3\n"
    _set_selection(textarea, 2, 3)
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0.1)
    second = waldoctl.commander.programs.active
    assert second is not None and second is not tab
    other = ui_state.active_textarea
    other.value = "x = 1\ny = 2\nz = 3\n"
    await asyncio.sleep(0)
    user.find(marker="editor-capture-pose").click()
    await asyncio.sleep(0.1)
    lines = str(other.value).split("\n")
    assert lines[:3] == ["x = 1", "y = 2", "z = 3"], (
        f"the other tab's selection replaced this tab's lines: {other.value!r}"
    )
    assert any(line.startswith("rbt.move_") for line in lines), other.value


@pytest.mark.integration
async def test_live_run_highlight_follows_program_command(user: User) -> None:
    """A sleep between two moves owns a scrub division of its own. The
    stepping wrapper numbers the queued commands it runs; the host resolves
    that number to the program command the preview drew. The sleep is a
    command on the plan the wrapper never sees, so counting positions would
    put the second move's highlight on the sleep's division and line."""
    import waldoctl

    from waldo_commander.components.editor_decorations import decorations

    editor, tab = await _open_simulated_three_move_program(user, _SLEEP_SCRIPT)
    segments = tab.dry_run.path_segments
    assert [s.move_type for s in segments] == ["joints", "sleep", "joints"]

    # A delay owns time on the commanded record: the scrub bar gives it a
    # division of its own and the timeline holds the arm through it.
    playback = editor.playback
    record = tab.dry_run.commanded
    assert record is not None
    assert [b.line_number for b in record.blocks if b.rows] == [4, 5, 6]
    playback._do_update_scrub_segments()
    tl = playback._ensure_timeline()
    assert tl is not None
    assert [s.move_type for s in tl.segments] == ["joints", "sleep", "joints"]
    assert tl.segments[1].line_number == 5
    assert tl.segment_durations[1] == pytest.approx(0.5, abs=record.row_dt_s)
    assert len(playback._segment_elements) == 3, (
        "the bar must index the record's commands, a sleep included"
    )
    held = tl.sample(tl.cumulative_times[1] + 0.25)
    assert held.segment_index == 1
    assert held.joints == pytest.approx(tl.sample(tl.cumulative_times[1]).joints)

    pb = tab.dry_run.playback
    moves = _move_commands(tab)

    async def wait_complete(command: int) -> None:
        async with asyncio.timeout(30):
            while not (pb.executing_command == command and pb.executing_step_at_end):
                await asyncio.sleep(0.05)

    try:
        user.find(marker="editor-step-program").click()
        await wait_complete(moves[0])
        assert pb.current_step == 0
        assert decorations._executing_line_by_tab.get(tab.id) == 4

        # The sleep runs on its own; the next grant runs the second move.
        user.find(marker="editor-step-program").click()
        await wait_complete(moves[1])
        assert pb.current_step == 2, (
            "the highlight must land on the move, not the sleep"
        )
        assert decorations._executing_line_by_tab.get(tab.id) == 6
        assert editor.playback._exec_step_index == 2
    finally:
        if is_any_program_running():
            await editor.playback.toggle_play()
        async with asyncio.timeout(30):
            while is_any_program_running():
                await asyncio.sleep(0.1)
    assert waldoctl.commander.programs.active is tab


@pytest.mark.integration
async def test_run_selection_brings_the_imports_in_its_scope(
    user: User,
) -> None:
    """A selection runs beside the imports in its scope: the program's own and
    those of the block it sits in, where an inserted skill puts its import.
    One in another function belongs to that function, and a guarded one keeps
    its guard."""
    import waldoctl

    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    editor = ui_state.editor_panel
    textarea = ui_state.active_textarea
    assert editor is not None and textarea is not None
    textarea.value = (
        "import math\n"
        "from typing import TYPE_CHECKING\n"
        "from parol6 import RobotClient\n"
        "if TYPE_CHECKING:\n"
        "    import no_such_typing_module\n"
        "try:\n"
        "    import no_such_fast_json as json\n"
        "except ImportError:\n"
        "    import json\n"
        "\n"
        "\n"
        "def optional():\n"
        "    import no_such_module_in_a_def\n"
        "\n"
        "\n"
        "with RobotClient() as rbt:\n"
        "    from math import tau\n"
        "    assert json.dumps(math.sqrt(4)) == '2.0' and tau > 6\n"
    )
    await asyncio.sleep(0)
    _set_selection(textarea, 18, 18)
    await asyncio.sleep(0)
    user.find(marker="editor-run-selection").click()
    async with asyncio.timeout(30):
        while script_exec.last_exit_code is None:
            await asyncio.sleep(0.05)
    run = waldoctl.commander.programs.get(editor._selection_program_id or "")
    assert run is not None
    assert script_exec.last_exit_code == 0, "\n".join(
        entry.text for entry in run.log.entries
    )
