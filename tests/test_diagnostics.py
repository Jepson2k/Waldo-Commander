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

from tests.helpers.wait import wait_for_app_ready, wait_until
from waldo_commander.state import robot_events, ui_state


def _text(user: User, marker: str) -> str:
    return next(iter(user.find(marker=marker).elements)).text


async def _settle(user: User, marker: str, predicate, timeout_s: float = 8.0) -> str:
    if not await wait_until(lambda: predicate(_text(user, marker)), timeout_s):
        raise AssertionError(
            f"{marker} never satisfied the check; last text {_text(user, marker)!r}"
        )
    return _text(user, marker)


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

    health = waldoctl.commander.status.drive_health
    await wait_until(lambda: bool(health.faults), timeout_s=8.0)
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
async def test_the_event_log_announces_itself_and_keeps_the_whole_error(
    user: User,
) -> None:
    """One warning, from the badge on a shut tab to a cleared log.

    A one-line strip could only ever show the title, which is the half that
    does not say what to do about the condition; the tab has room for the
    cause, the effect and the remedy. And nobody opens a tab they have no
    reason to open, so an entry that lands behind a shut one has to say so.
    """
    await user.open("/")
    await wait_for_app_ready()

    # The log is process-global and nothing resets it between tests, so the
    # count is read against whatever earlier warnings left behind.
    unread_before = robot_events.unread
    robot_events.add(
        code=60,
        title="CAN stale",
        cause="no frames for 200 ms",
        effect="motion refused",
        remedy="check the bus wiring",
    )
    assert robot_events.unread == unread_before + 1
    await user.should_see(marker="diag-unread-badge", retries=30)

    user.find(marker="tab-diagnostics").click()
    await asyncio.sleep(0)
    await user.should_see(marker="diagnostics-panel")
    for part in (
        "CAN stale",
        "no frames for 200 ms",
        "motion refused",
        "check the bus wiring",
    ):
        await user.should_see(part)
    assert await wait_until(lambda: robot_events.unread == 0), (
        "rendering the log to an open tab is what marks it read"
    )

    user.find(marker="diag-clear-events").click()
    await asyncio.sleep(0)
    assert not robot_events.entries
    await user.should_not_see("CAN stale")
