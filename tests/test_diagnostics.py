"""Diagnostics shows what this backend reports, and omits the rest.

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
import contextlib

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import poll_until, wait_for_app_ready, wait_until
from waldo_commander.state import robot_events, ui_state


def _text(user: User, marker: str) -> str:
    return next(iter(user.find(marker=marker).elements)).text


async def _settle(user: User, marker: str, predicate) -> str:
    return await poll_until(
        lambda: _text(user, marker), predicate, timeout_s=8.0, what=marker
    )


def _classes(user: User, marker: str) -> list[str]:
    return next(iter(user.find(marker=marker).elements)).classes


@pytest.mark.integration
async def test_diagnostics_reports_only_what_the_backend_reports(
    user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """One app start walks the panel from a shut footer button to a status
    stream that has stopped; the stream is cancelled last because nothing
    after it would update.
    """
    await user.open("/")
    await wait_for_app_ready()
    panel = ui_state.bottom_panel
    assert not panel.visible, "the bottom panel starts hidden"

    # -- The event log announces itself and keeps the whole error. --
    # A one-line strip could only ever show the title, which is the half that
    # does not say what to do about the condition; the panel has room for the
    # cause, the effect and the remedy. And nobody opens a panel they have no
    # reason to open, so an entry that lands behind a shut one has to say so,
    # at the worst severity still unread.
    # The log is process-global and nothing resets it between tests.
    robot_events.clear()
    robot_events.add(
        code=60,
        title="CAN stale",
        cause="no frames for 200 ms",
        effect="motion refused",
        remedy="check the bus wiring",
    )
    assert robot_events.unread_severity == "warning"
    await poll_until(
        lambda: _text(user, "footer-warnings"),
        lambda t: t == "1",
        timeout_s=3.0,
        what="the footer's warning count",
    )
    assert _text(user, "footer-errors") == "0"
    assert await wait_until(
        lambda: "unread-warning" in _classes(user, "footer-events")
    ), "an unseen entry tints the footer button"

    robot_events.add(code=61, title="Bus off", severity="error")
    assert await wait_until(
        lambda: "unread-error" in _classes(user, "footer-events")
    ), "an unseen error tints it red"
    robot_events.add(code=62, title="CAN stale again")
    # The counts and the tint refresh in the same binding pass.
    await poll_until(
        lambda: _text(user, "footer-warnings"),
        lambda t: t == "2",
        timeout_s=3.0,
        what="the second warning",
    )
    classes = _classes(user, "footer-events")
    assert "unread-error" in classes and "unread-warning" not in classes, (
        "a warning after an unseen error must not hide the error",
        classes,
    )

    user.find(marker="footer-events").click()
    await asyncio.sleep(0)
    assert panel.visible and panel.tabs.value == "diagnostics"
    await user.should_see(marker="diagnostics-panel")
    for part in (
        "CAN stale",
        "no frames for 200 ms",
        "motion refused",
        "check the bus wiring",
    ):
        await user.should_see(part)
    assert await wait_until(lambda: robot_events.unread_severity == ""), (
        "rendering the log to an open panel is what marks it read"
    )
    assert await wait_until(
        lambda: not {"unread-warning", "unread-error"}
        & set(_classes(user, "footer-events"))
    ), "and the tint goes with it"

    user.find(marker="diag-clear-events").click()
    await asyncio.sleep(0)
    assert not robot_events.entries
    await user.should_not_see("CAN stale")

    # -- Only the sections this backend can fill are shown. --
    # Absence is the message: a Motor bus section reading "not reported" or a
    # torque chart drawing six flat zero lines both claim a measurement that
    # was never taken.
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

    # -- Drive faults appear without analog readings. --
    # That combination is the one a temperatures-only availability check gets
    # wrong: the section has to appear on the strength of the faults alone,
    # with the readings it does not have left unknown rather than zeroed.
    page = ui_state.diagnostics_page
    health = waldoctl.commander.status.drive_health
    joints = ui_state.active_robot.joints.count
    await wait_until(lambda: bool(health.faults), timeout_s=8.0)
    assert health.faults, "the backend reports per-drive faults"
    assert not health.temperatures_c, "and no analog registers"
    assert not health.currents_ma
    assert health.bus_voltage_v is None

    await user.should_see(marker="diag-section-drives")
    # Healthy drives with nothing analog to show are one line, not a table of
    # dashes implying broken sensors.
    await user.should_see(marker="diag-drives-summary")
    assert _text(user, "diag-drives-summary") == "6 drives · no faults"
    await user.should_not_see(marker="diag-drive-fault-1")
    await user.should_not_see(marker="diag-drive-temp-1")
    await user.should_not_see(marker="diag-drive-supply")

    # A fault opens the table: a fault column and nothing else. The status
    # loop rewrites the faults on its next tick, so the injected one is read
    # in the same tick it is drawn; the table stays open once shown.
    health.faults = [("overtemp",)] + [()] * (len(health.faults) - 1)
    page.update()
    assert _text(user, "diag-drive-fault-1") == "overtemp"
    await user.should_see(marker="diag-drive-fault-1")
    await user.should_see(marker="diag-drives-head-fault")
    await user.should_not_see(marker="diag-drives-summary")
    await user.should_not_see(marker="diag-drives-head-temp")
    await user.should_not_see(marker="diag-drives-head-current")

    # A backend that stops reporting its drives leaves nothing to show, and
    # the last temperature and fault it sent are no longer true. Written and
    # read without yielding: the next status frame republishes this
    # backend's own drive health.
    health.temperatures_c = [41.0] * joints
    health.faults = [("overcurrent",)] + [()] * (joints - 1)
    page.update()
    assert _text(user, "diag-drive-temp-1") == "41"
    assert _text(user, "diag-drive-fault-1") == "overcurrent"

    health.temperatures_c = []
    health.currents_ma = []
    health.faults = []
    page.update()
    assert _text(user, "diag-drive-temp-1") == "—"
    assert _text(user, "diag-drive-fault-1") == "—"

    # -- The verdict names what is wrong. --
    # A busy operator reads one line, not a column of numbers, and only what
    # is outside its normal range takes any colour.
    await _settle(user, "diag-verdict", lambda t: t.startswith("Running"))
    assert _text(user, "diag-estop") == "clear"
    estop = next(iter(user.find(marker="diag-estop").elements))
    assert "diag-fault" not in estop.classes, "a healthy reading carries no colour"

    # estop == 0 is the chain broken, matching the controller wire format.
    # Read synchronous rendering before yielding to the live status consumer,
    # which replaces these injected fields with the simulator's next frame.
    waldoctl.commander.status.io.estop = 0
    page.update()

    assert _text(user, "diag-verdict") == "Stopped — e-stop pressed", (
        "the headline has to say what is wrong, not just that something is"
    )
    assert _text(user, "diag-estop") == "pressed"
    assert "diag-fault" in estop.classes, "and the row that caused it is the one lit"

    waldoctl.commander.status.io.estop = 1
    page.update()
    assert _text(user, "diag-verdict").startswith("Running"), "and it clears again"

    # The backend's own warnings come first: they name the condition, where
    # an inferred reading only names a symptom. Read back before yielding,
    # since the next status frame republishes this backend's empty list.
    status = waldoctl.commander.status
    status.warnings.entries = [
        waldoctl.RobotError(-1, 60, "Gripper slow to answer", "", "", "")
    ]
    page.update()
    assert _text(user, "diag-verdict") == "Running degraded — Gripper slow to answer"
    status.warnings.entries = []

    # A CAN bus that is error-passive still carries traffic: degraded, not
    # stopped, however the backend spells the state.
    status.link_health.state = "ERROR_PASSIVE"
    page.update()
    assert _text(user, "diag-verdict").startswith("Running degraded")
    link = next(iter(user.find(marker="diag-link-state").elements))
    assert "diag-warn" in link.classes
    status.link_health.state = ""
    page.update()
    assert _text(user, "diag-verdict").startswith("Running")

    # -- A condition this backend reports reaches the log. --
    # waldoctl routes self-clearing conditions to ``warnings`` and hard
    # latches to the standing error, and a backend is free to use only one of
    # them. This one only ever sets the standing error, so a log wired to
    # ``warnings`` alone stays empty however badly the move goes — which reads
    # as a healthy machine rather than an unasked question.
    robot_events.clear()
    client = ui_state.control_panel.client
    # Metres out, against a reach of about half a metre: the controller
    # cannot plan it and answers with a standing error.
    await client.move_l([5000.0, 5000.0, 5000.0, 180.0, 0.0, 0.0], speed=0.5)

    assert await wait_until(lambda: bool(robot_events.entries), timeout_s=8.0), (
        "the backend reported a condition and the log never heard about it"
    )
    _, code, title, _cause, _effect, remedy, severity = robot_events.entries[0]
    assert code, "an entry with no code cannot be traced back to the backend"
    assert title, "an entry with no title says nothing to the operator"
    assert remedy, "the remedy is the half that says what to do about it"
    assert severity == "error", "a standing error is counted as one"
    assert robot_events.errors == 1 and robot_events.warnings == 0

    # A latched error has stopped the backend, and the headline says so in
    # the backend's own words rather than "Running normally" above the log.
    await _settle(user, "diag-verdict", lambda t: t == f"Stopped — {title}")

    # -- The verdict goes stale when status stops. --
    # Every reading is the last one heard once status stops arriving, and a
    # green "Running normally" over them claims a robot nobody can see.
    await client.reset_state()
    await client.reset()
    await _settle(user, "diag-verdict", lambda t: t.startswith("Running"))
    verdict = next(iter(user.find(marker="diag-verdict").elements))
    assert "diag-fault" not in verdict.classes

    consumer = next(
        t
        for t in asyncio.all_tasks()
        if getattr(t.get_coro(), "__qualname__", "") == "_status_consumer"
    )
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer

    await _settle(user, "diag-verdict", lambda t: t.startswith("No status for"))
    assert "diag-fault" in verdict.classes

    # The footer's other button opens the same panel on the log tab, and the
    # panel closes.
    user.find(marker="footer-log").click()
    await asyncio.sleep(0)
    assert panel.visible and panel.tabs.value == "log"
    await user.should_see(marker="response-log")

    user.find(marker="bottom-panel-close").click()
    await asyncio.sleep(0)
    assert not panel.visible
    await user.should_not_see(marker="response-log")

    # The refused move is part of the test, and the controller logs it at
    # ERROR. Drop just that record so the fixture's blanket ERROR check still
    # guards everything else.
    caplog.get_records("call")[:] = [
        r
        for r in caplog.get_records("call")
        if "IK: partial path" not in r.getMessage()
    ]


def test_the_overrun_rate_counts_only_what_this_page_watched() -> None:
    """The controller's count runs from its own boot; a page that opens an
    hour later and divides that total by its own age reports a rate that
    never happened."""
    from waldo_commander.components.diagnostics import _OverrunRate

    rate = _OverrunRate()
    assert rate.per_minute(500, now=100.0) is None, "one sample is no rate"
    assert rate.per_minute(500, now=160.0) == 0.0
    assert rate.per_minute(503, now=220.0) == pytest.approx(1.5)
    # A controller restart starts its count again, and the rate with it.
    assert rate.per_minute(2, now=230.0) is None
    assert rate.per_minute(4, now=290.0) == pytest.approx(2.0)


@pytest.mark.integration
async def test_a_backend_that_answers_nothing_is_not_queried_at_the_status_rate(
    user: User,
) -> None:
    """The boot constants are one query, and a failed one must not become a
    query per status tick.

    ``_ask_constants`` cleared its own latch on failure, so the very next
    tick started it again: against a backend that was reachable but
    answering nothing, the tab queried it for as long as it stayed open, at
    whatever rate status arrives. The retry backs off now.
    """
    from waldo_commander.components.diagnostics import DiagnosticsPage

    await user.open("/")
    await wait_for_app_ready()

    calls = 0

    class Mute:
        """Reachable, and answers nothing — the case the latch mishandled."""

        async def loop_stats(self):
            nonlocal calls
            calls += 1
            raise ConnectionError("no answer")

    with user:
        page = DiagnosticsPage(Mute(), lambda: True)
        page.build()
    for _ in range(40):
        page.update()
        await asyncio.sleep(0)

    assert calls == 1, (
        f"a mute backend was queried {calls} times across 40 status ticks; "
        "the boot-constants query must back off, not re-fire every tick"
    )
