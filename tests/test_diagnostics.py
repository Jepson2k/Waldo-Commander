"""The diagnostics tab: loop health off the backend's ``loop_stats`` query,
motor-bus link health off ``commander.status``, and the drives section
saying plainly when a backend streams no telemetry.

Runs on the suite's parol6 fake-serial backend; the telemetry-fed drives
table and the live torque series are exercised against the real par6
runtime in ``test_par6_backend.py``.
"""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import ui_state


def _text(user: User, marker: str) -> str:
    return next(iter(user.find(marker=marker).elements)).text


async def _wait_text(user: User, marker: str, predicate, timeout_s: float = 5.0):
    text = ""
    for _ in range(int(timeout_s / 0.1)):
        text = _text(user, marker)
        if predicate(text):
            return text
        await asyncio.sleep(0.1)
    raise AssertionError(f"{marker} never satisfied predicate; last text {text!r}")


@pytest.mark.integration
async def test_diagnostics_tab_reports_loop_link_and_drive_availability(
    user: User,
) -> None:
    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-diagnostics").click()
    await asyncio.sleep(0)
    await user.should_see(marker="diagnostics-panel")

    # Loop health is the backend's own answer, not a placeholder: the rate
    # line must quote the target the controller reports over the wire.
    stats = await ui_state.control_panel.client.loop_stats()
    assert stats is not None
    rate = await _wait_text(user, "diag-loop-rate", lambda t: "Hz" in t)
    assert f"of {stats.target_hz:.0f} Hz" in rate
    overruns = _text(user, "diag-loop-overruns")
    assert overruns.endswith("ticks") and int(overruns.split()[0]) >= 0

    # Link health follows the public status surface the consumer writes.
    lh = waldoctl.commander.status.link_health
    lh.state = "BusOff"
    lh.restarts = 3
    lh.tx_errors = 12
    await _wait_text(user, "diag-link-state", lambda t: t == "BusOff")
    assert _text(user, "diag-link-restarts") == "3"
    assert _text(user, "diag-link-tx-errors") == "12"

    # parol6 streams no telemetry; the drives section says so rather than
    # sitting on dashes forever.
    await user.should_see("Drive telemetry is not available on this backend")
    await user.should_see(marker="diag-torque-chart")
