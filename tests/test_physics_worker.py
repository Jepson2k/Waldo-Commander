"""A cancelled physics job must leave the app and the next job usable."""

import asyncio
from contextlib import suppress
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from waldo_commander.services.path_visualizer import _PhysicsPool


def _blocked_job(paths: tuple[str, str]) -> int:
    marker, release = map(Path, paths)
    marker.write_text(str(os.getpid()))
    while not release.exists():
        time.sleep(0.01)
    return os.getpid()


def _worker_pid(_args: tuple) -> int:
    return os.getpid()


async def test_physics_cancellation_terminates_its_worker_and_allows_another_job(
    tmp_path: Path,
) -> None:
    pool = _PhysicsPool()
    for cancel_via in ("pool", "caller"):
        marker, release = (
            tmp_path / f"started-{cancel_via}",
            tmp_path / f"release-{cancel_via}",
        )
        task = asyncio.create_task(pool.run(_blocked_job, (str(marker), str(release))))
        try:
            async with asyncio.timeout(20):
                while not marker.exists() or not marker.read_text():
                    await asyncio.sleep(0.01)
            worker_pid = int(marker.read_text())
            assert worker_pid != os.getpid(), "physics must not execute inside the app"

            if cancel_via == "pool":
                pool.cancel()
            else:
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(10):
                while any(
                    p.pid == worker_pid for p in multiprocessing.active_children()
                ):
                    await asyncio.sleep(0.01)

            async with asyncio.timeout(20):
                successor = await pool.run(_worker_pid, ())
            assert successor not in (worker_pid, os.getpid())
        finally:
            release.touch()
            pool.shutdown()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.integration
async def test_delayed_preview_retains_the_submitted_joint_pose(user):
    import pickle
    import numpy as np
    import waldoctl
    from nicegui import run
    from tests.helpers.wait import (
        enable_sim,
        ensure_robot_ready_for_motion,
        wait_for_app_ready,
        wait_until,
    )
    from waldo_commander.services.path_visualizer import (
        path_visualizer,
        _run_simulation_packed,
    )

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    program = waldoctl.commander.programs.active
    assert program is not None
    source = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.move_j([0.5, 0, 0, 0, 0, 0], rel=True, duration=0.2)\n"
    assert (
        await path_visualizer.update_path_visualization(source, tab_id=program.id)
        is None
    )
    submitted = path_visualizer._planned_args[program.id]
    frozen = pickle.dumps(submitted)
    initial = submitted[1].copy()
    assert program.dry_run.final_joints_rad is not None
    planned_final = np.array(program.dry_run.final_joints_rad, copy=True)
    client = waldoctl.commander.client
    target = np.degrees(initial).tolist()
    target[0] += 2
    try:
        index = await client.move_j(target, duration=0.5)
        assert index >= 0 and await client.wait_command(index, timeout=10)
        assert await wait_until(
            lambda: (
                abs(waldoctl.commander.status.joints.angles.rad[0] - initial[0]) > 0.01
            )
        )
        assert pickle.dumps(submitted) == frozen, (
            "live status rewrote the already displayed plan's starting pose"
        )
        delayed = await run.cpu_bound(_run_simulation_packed, submitted)
        assert delayed["error"] is None
        assert delayed["final_joints_rad"] == pytest.approx(planned_final)
    finally:
        await client.stop()
