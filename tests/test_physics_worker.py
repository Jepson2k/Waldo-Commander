"""A cancelled or crashed physics job must leave the app and the next job usable."""

import asyncio
from concurrent.futures.process import BrokenProcessPool
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


def _dying_job(_args: tuple) -> int:
    os._exit(1)


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


async def test_a_worker_that_dies_mid_job_leaves_the_next_job_a_fresh_one() -> None:
    pool = _PhysicsPool()
    try:
        async with asyncio.timeout(40):
            with pytest.raises(BrokenProcessPool):
                await pool.run(_dying_job, ())
            successor = await pool.run(_worker_pid, ())
        assert successor != os.getpid()
    finally:
        pool.shutdown()
