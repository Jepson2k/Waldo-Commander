"""The status chip's robot buddy wears the robot's state.

Driven through the real paths on the fake-serial controller — the E-STOP
and Reset buttons, a jog button, the editor's play button — and read back
off the buddy element the page renders. How the browser draws each mood
and reaction is covered in ``test_robot_buddy_screen.py``.
"""

import asyncio
import time
from collections.abc import Callable

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    simulate_click,
    wait_for_app_ready,
)
from waldo_commander.components.robot_buddy import Mood, Reaction, RobotBuddy
from waldo_commander.components.script_execution import script_exec
from waldo_commander.state import robot_events, ui_state


def _buddy(user: User, marker: str) -> RobotBuddy:
    element = next(iter(user.find(marker=marker).elements))
    assert isinstance(element, RobotBuddy)
    return element


async def _wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.integration
async def test_chip_buddy_follows_estop_and_watches_the_arm_move(user: User) -> None:
    """Grey in the simulator, alarmed for as long as an E-STOP is latched
    (with its own alarmed buddy in the dialog), and focused on the arm
    while it moves."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    chip = _buddy(user, "readout-robot-buddy")
    assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL)
    assert await _wait_for(lambda: not chip.busy), "idle arm, idle buddy"

    waldoctl.commander.settings.jog.joint_step_deg = 10.0
    await simulate_click(user, "btn-j1-plus")
    assert await _wait_for(lambda: chip.busy, timeout=5.0), (
        "the buddy should focus on the arm while the jog moves it"
    )
    assert await _wait_for(lambda: not chip.busy, timeout=10.0), (
        "the buddy should relax once the arm has settled"
    )

    user.find(marker="btn-estop").click()
    assert await _wait_for(lambda: chip.mood == Mood.ALARMED)
    assert _buddy(user, "estop-buddy").mood == Mood.ALARMED

    user.find(marker="btn-estop-resume").click()
    assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL), (
        "Reset clears the E-STOP, so the buddy calms back down"
    )


@pytest.mark.integration
async def test_chip_buddy_reacts_to_how_programs_end_and_to_new_warnings(
    user: User,
) -> None:
    """A clean exit is celebrated, a crash is winced at, the buddy works
    along while the program runs, and a new warning startles it."""
    await user.open("/")
    await wait_for_app_ready()

    chip = _buddy(user, "readout-robot-buddy")
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    program = waldoctl.commander.programs.active
    assert program is not None

    async def run(source: str) -> int:
        ui_state.active_textarea.value = source
        program.source = source
        script_exec.last_exit_code = None
        user.find(marker="editor-play-btn").click()
        assert await _wait_for(lambda: script_exec.last_exit_code is not None, 15.0)
        assert script_exec.last_exit_code is not None
        return script_exec.last_exit_code

    busy_seen = False

    async def watch_busy() -> None:
        nonlocal busy_seen
        while not busy_seen:
            busy_seen = chip.busy
            await asyncio.sleep(0.02)

    watcher = asyncio.create_task(watch_busy())
    assert await run("import time\ntime.sleep(1.0)\n") == 0
    watcher.cancel()
    assert busy_seen, "the buddy should be busy while a program runs"
    assert chip.last_reaction == Reaction.CELEBRATE
    assert await _wait_for(lambda: not chip.busy)

    # Guarded so only the real run crashes: the editor's path preview
    # executes the same source under ``__name__ == "__simulation__"``.
    crash = "if __name__ == '__main__':\n    raise RuntimeError('boom')\n"
    assert await run(crash) != 0
    assert chip.last_reaction == Reaction.OOPS

    # The fake-serial backend reports no warnings of its own; add one the
    # way the status consumer does when a new condition arrives.
    robot_events.add("warning", "Control loop degraded", "p99 over band")
    assert await _wait_for(lambda: chip.last_reaction == Reaction.STARTLE)
