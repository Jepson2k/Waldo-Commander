"""Run a tray loop, edit advisory progress, and stop an unfinished transfer."""

import asyncio

import numpy as np
import pytest
import waldoctl
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
)
from tests.test_skill_library import START
from waldo_commander.patterns import (
    PatternProgress,
    grid_poses,
    load_progress,
    save_progress,
)
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.setup import SetupStore
from waldo_commander.skills import (
    SignalFixture,
    gripper_open,
    transfer,
    transfer_with_signal,
)


@pytest.mark.integration
async def test_tray_loop_progress_cancellation_and_generated_signal_transfer(
    user, tmp_path, monkeypatch
):
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.state import ui_state

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path / "setups"))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    rbt = waldoctl.commander.client
    index = await rbt.move_j(START, speed=1)
    assert await rbt.wait_command(index, timeout=20)
    index = await rbt.select_tool("PNEUMATIC")
    assert await rbt.wait_command(index, timeout=5)
    await gripper_open.async_call(rbt)
    pick = Pose(tuple(await rbt.pose()))
    targets = grid_poses(pick, rows=1, columns=2, pitch_x_mm=2, pitch_y_mm=0)
    progress = PatternProgress.for_poses(targets)
    path = tmp_path / "progress.json"
    for index in progress.pending():
        await transfer.async_call(
            rbt, pick=pick, place=targets[index], clearance_mm=2, speed=0.5
        )
        progress = progress.mark(index)
        assert save_progress(path, progress, client=rbt)
    assert load_progress(path, targets).pending() == ()
    progress = progress.mark(1, completed=False)
    assert save_progress(path, progress, client=rbt)
    assert load_progress(path, targets).pending() == (1,)
    expected = targets[-1].matrix()
    expected[:3, 3] += 2 * expected[:3, 2]
    np.testing.assert_allclose(
        Pose(tuple(await rbt.pose())).matrix(), expected, atol=0.1
    )
    run = asyncio.create_task(
        transfer.async_call(
            rbt, pick=pick, place=targets[1], clearance_mm=20, speed=0.001
        )
    )
    try:
        assert await rbt.wait_status(
            lambda s: s.action_state == waldoctl.ActionState.EXECUTING, timeout=10
        )
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert await rbt.wait_status(
            lambda s: s.action_state == waldoctl.ActionState.IDLE, timeout=3
        )
        # The cancelled transfer owns no progress file, so what matters is
        # that the program's own record still says the cell is pending.
        assert load_progress(path, targets).pending() == (1,)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    signal = DigitalSignal("parol6", "output", 0, 2, 2)
    before = await rbt.pose()
    with pytest.raises(ValueError, match="preview client"):
        await transfer_with_signal.async_call(
            rbt, pick=pick, place=pick, grip=signal, closed_fixture=SignalFixture(True)
        )
    assert await rbt.pose() == pytest.approx(before)

    # A grip output left closed by an earlier cancelled run is refused before
    # any motion: descending onto the pickup cell with the tool still closed on
    # a part is a collision.
    assert await rbt.write_io(signal.index, signal.encode(True)) >= 0
    assert await rbt.wait_status(lambda s: s.io[2] == 1, timeout=3)
    with pytest.raises(ValueError, match="already at its closed level"):
        await transfer_with_signal.async_call(
            rbt, pick=pick, place=targets[1], grip=signal, clearance_mm=2
        )
    assert await rbt.pose() == pytest.approx(before)
    assert await rbt.write_io(signal.index, signal.encode(False)) >= 0
    assert await rbt.wait_status(lambda s: s.io[2] == 0, timeout=3)

    SetupStore().save(
        "bench",
        SetupSnapshot(
            poses={"pick": pick, "place": targets[1]}, signals={"grip": signal}
        ),
    )

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    ui_state.active_textarea.value = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    )
    user.find(marker="tab-skills").click()
    element("skill-choice").set_value("waldo.transfer_with_signal")
    element("skill-place-pose").set_value("place")
    element("skill-arg-clearance_mm").set_value(2)
    user.find(marker="skill-insert").click()
    await user.should_see("Inserted Python skill call")
    assert (
        "_skill_waldo_transfer_with_signal("
        in waldoctl.commander.programs.active.source
    )
    user.find(marker="skill-run").click()
    try:
        await user.should_see("Skill completed", retries=300)
        assert script_exec.last_exit_code == 0
        assert (await rbt.io())[2] == 0
    finally:
        if is_any_program_running():
            await script_exec.stop()
