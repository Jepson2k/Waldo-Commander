"""Waits on a move's outcome rather than on a stretch of unchanging readings.

``wait_for_motion_stable(stable_ticks=N)`` costs at least N × 0.1 s even when
the arm is long done; these return as soon as the controller says the move is
over and the value is where the test needs it.
"""

import time
from typing import Callable

import waldoctl
from waldoctl import ActionState

from tests.helpers.wait import poll_until, wait_for_motion_stable


def idle() -> bool:
    return waldoctl.commander.status.action.state == ActionState.IDLE


async def wait_idle(timeout_s: float = 15.0) -> None:
    await poll_until(
        lambda: waldoctl.commander.status.action.state,
        lambda state: state == ActionState.IDLE,
        timeout_s=timeout_s,
        interval=0.05,
        what="the action state reaching IDLE",
    )


async def settled(
    read: Callable[[], float], timeout_s: float = 15.0, tolerance: float = 0.05
) -> float:
    """``read()`` once three status reads agree with the controller idle at
    both ends: an idle flicker between a move's phases does not count."""
    deadline = time.monotonic() + timeout_s
    while True:
        await wait_idle(max(deadline - time.monotonic(), 0.1))
        value = await wait_for_motion_stable(
            read,
            timeout_s=max(deadline - time.monotonic(), 0.3),
            tolerance=tolerance,
            stable_ticks=3,
        )
        if idle():
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"the arm never settled within {timeout_s}s")


async def wait_moved(
    read: Callable[[], float],
    start: float,
    accept: Callable[[float], bool],
    *,
    what: str,
    timeout_s: float = 20.0,
) -> float:
    """The change ``read() - start`` once the controller is idle with it accepted.

    Both at once: a value passing through the accepted band mid-move is not
    the end of the move, and an idle flicker between a move's phases is not
    either.
    """
    return await poll_until(
        lambda: read() - start,
        lambda delta: accept(delta) and idle(),
        timeout_s=timeout_s,
        interval=0.05,
        what=what,
    )
