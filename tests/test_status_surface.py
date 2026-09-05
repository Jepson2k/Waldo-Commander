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
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import robot_events


@pytest.mark.integration
async def test_warning_banner_stands_and_the_log_keeps_history(user: User) -> None:
    """A standing warning raises the persistent banner and lands in the
    warnings/errors log; when the condition self-clears the banner leaves
    but the log entry stays."""
    await user.open("/")
    await wait_for_app_ready()

    st = waldoctl.commander.status
    st.warnings.entries = [
        (-1, 59, "Control loop degraded", "p99 over band", "warning", "reduce load")
    ]
    robot_events.add("warning", "Control loop degraded", "p99 over band", "reduce load")
    await asyncio.sleep(0)
    await user.should_see("Control loop degraded")
    await user.should_see(marker="readout-event-log")
    # The log carries the remedy, not just the symptom.
    await user.should_see("Fix: reduce load")

    # Self-clearing: the banner leaves with the condition, the log does not
    # (the log's copy of the message is why the poll looks for the
    # notification element, not the text).
    from nicegui.elements.notification import Notification

    st.warnings.entries = []
    banner_gone = False
    for _ in range(50):
        try:
            user.find(kind=Notification)
        except AssertionError:
            banner_gone = True
            break
        await asyncio.sleep(0.1)
    assert banner_gone, "the banner must dismiss when the conditions clear"
    assert robot_events.entries, "the log must keep cleared conditions"
    assert robot_events.entries[-1][2] == "Control loop degraded"
