"""The Diagnostics tab shows what this backend reports, and omits the rest.

Backends differ enormously in what they can say about themselves. A fixed
layout serves the richest one and leaves everything else showing a column
of dashes, which reads as "all zero" rather than "nobody asked this robot".
So a section appears only once its backend has actually reported something.

This suite runs on the parol6 fake-serial backend, which times its control
loop and reports per-joint drive faults but has no fieldbus, no analog
drive registers and no torque sensing. The richer half of the contract runs
against the real par6 runtime in ``test_par6_backend.py``.
"""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.state import robot_events, ui_state


def _text(user: User, marker: str) -> str:
    return next(iter(user.find(marker=marker).elements)).text


async def _settle(user: User, marker: str, predicate, timeout_s: float = 8.0) -> str:
    text = ""
    for _ in range(int(timeout_s / 0.1)):
        text = _text(user, marker)
        if predicate(text):
            return text
        await asyncio.sleep(0.1)
    raise AssertionError(f"{marker} never satisfied the check; last text {text!r}")


async def _open_diagnostics(user: User) -> None:
    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-diagnostics").click()
    await asyncio.sleep(0)
    await user.should_see(marker="diagnostics-panel")


@pytest.mark.integration
async def test_only_the_sections_this_backend_can_fill_are_shown(user: User) -> None:
    """The adaptive contract, on a backend with plenty it cannot report.

    Absence is the message: a Motor bus section reading "not reported" or a
    torque chart drawing six flat zero lines both claim a measurement that
    was never taken.
    """
    await _open_diagnostics(user)

    # Reported: this backend times its loop, so the tail is real and is
    # quoted against the budget its target rate implies — a bare
    # millisecond figure means nothing on its own. The section reveals on
    # the first frame that carries loop health, so wait for it rather than
    # reading at the instant the tab opens.
    await user.should_see(marker="diag-section-loop")
    await _settle(user, "diag-loop-rate", lambda t: "Hz target" in t)
    await _settle(user, "diag-loop-p99", lambda t: "budget" in t)
    assert waldoctl.commander.status.loop_health.measured

    # Not reported: no fieldbus and no torque sensing on this backend.
    assert not ui_state.active_robot.has_force_torque
    await user.should_not_see(marker="diag-section-link")
    await user.should_not_see(marker="diag-section-torques")
    await user.should_not_see(marker="diag-torque-chart")
    # And no CAN drives: par6's Drives tab stays out of a parol6 session
    # even when the par6 package is installed alongside.
    await user.should_not_see(marker="tab-par6-drives")


@pytest.mark.integration
async def test_drive_faults_appear_without_analog_readings(user: User) -> None:
    """This backend's drivers report fault flags and no analog registers.

    That combination is the one a temperatures-only availability check gets
    wrong: the section has to appear on the strength of the faults alone,
    with the readings it does not have left unknown rather than zeroed.
    """
    await _open_diagnostics(user)

    for _ in range(80):
        if waldoctl.commander.status.drive_health.faults:
            break
        await asyncio.sleep(0.1)
    health = waldoctl.commander.status.drive_health
    assert health.faults, "the backend reports per-drive faults"
    assert not health.temperatures_c, "and no analog registers"
    assert not health.currents_ma
    assert health.bus_voltage_v is None

    await user.should_see(marker="diag-section-drives")
    # Fault bits and no analog registers: a fault column and nothing else,
    # rather than °C and mA columns of dashes implying broken sensors.
    await user.should_see(marker="diag-drives-head-fault")
    await user.should_not_see(marker="diag-drives-head-temp")
    await user.should_not_see(marker="diag-drives-head-current")
    await user.should_not_see(marker="diag-drive-temp-1")
    await user.should_not_see(marker="diag-drive-supply")
    assert _text(user, "diag-drive-fault-1") == "—", "a healthy drive lists no faults"


@pytest.mark.integration
async def test_the_event_log_carries_the_whole_error_and_clears(user: User) -> None:
    """A one-line strip could only ever show the title, which is the half
    that does not say what to do. The tab has room for the rest."""
    await _open_diagnostics(user)

    robot_events.add(
        code=60,
        title="CAN stale",
        cause="no frames for 200 ms",
        effect="motion refused",
        remedy="check the bus wiring",
    )
    for part in (
        "CAN stale",
        "no frames for 200 ms",
        "motion refused",
        "check the bus",
    ):
        await user.should_see(part)

    user.find(marker="diag-clear-events").click()
    await asyncio.sleep(0)
    assert not robot_events.entries
    await user.should_not_see("CAN stale")
