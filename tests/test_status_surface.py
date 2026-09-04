"""The v0.8.0 warnings surface: the standing-condition banner and the
warnings/errors log.

Runs on the suite's parol6 fake-serial backend, whose buffer pre-dates the
v0.8.0 fields — which is half the contract under test: the status consumer
must leave the new sub-objects at their defaults instead of resetting them
each tick, so the wiring can be driven through the public
``commander.status`` surface exactly the way a capable backend's consumer
writes it. The full wire path runs against the real par6 runtime in
``test_par6_backend.py``.
"""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import robot_events


@pytest.mark.integration
async def test_the_event_log_keeps_the_whole_structured_error(user: User) -> None:
    """A warning-class condition self-clears; the log is what is left.

    The banner tracks only the standing set, so the log is the only place a
    condition that flickered while nobody was watching can still be read —
    and it has to keep the cause, effect and remedy, not just the title,
    because those are the parts that say what to do about it.

    Driven through ``robot_events`` rather than the wire: this backend's
    buffer carries no warnings at all, so the status consumer would
    overwrite anything staged on ``commander.status``. The wire path runs
    against the real par6 runtime in ``test_par6_backend.py``.
    """
    await user.open("/")
    await wait_for_app_ready()

    robot_events.add(
        code=59,
        title="Control loop degraded",
        cause="p99 over band",
        effect="motion may stutter",
        remedy="reduce background load",
    )

    user.find(marker="tab-diagnostics").click()
    await asyncio.sleep(0)
    await user.should_see(marker="diag-events-log")
    for part in (
        "Control loop degraded",
        "p99 over band",
        "motion may stutter",
        "reduce background load",
    ):
        await user.should_see(part)

    assert robot_events.entries[-1][1] == 59, "the code identifies the condition"
    assert robot_events.entries[-1][5] == "reduce background load"


@pytest.mark.integration
async def test_opening_diagnostics_clears_the_unread_badge(user: User) -> None:
    """The badge is how a condition announces itself while the tab is shut,
    so it has to survive until someone actually looks."""
    await user.open("/")
    await wait_for_app_ready()

    robot_events.add(code=60, title="CAN stale", cause="no frames")
    assert robot_events.unread == 1
    await user.should_see(marker="diag-unread-badge")

    user.find(marker="tab-diagnostics").click()
    for _ in range(50):
        if robot_events.unread == 0:
            break
        await asyncio.sleep(0.1)
    assert robot_events.unread == 0, "opening the tab is what marks it read"
