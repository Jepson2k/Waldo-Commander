"""The diagnostics tab reads the status broadcast, and nothing else.

The loop's tail, the drives' readings and the bus link all arrive on
``commander.status``; the only query is a single ``loop_stats()`` for the
boot constants. Both tests run against the suite's live fake-serial
backend, so what they assert is what a real status consumer produced. The
drives table is exercised where drives actually report readings, against
the par6 runtime, in ``test_par6_backend.py``.
"""

import asyncio

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import poll_until, wait_for_app_ready
from waldo_commander.state import ui_state


def _text(user: User, marker: str) -> str:
    return next(iter(user.find(marker=marker).elements)).text


async def _settle(user: User, marker: str, predicate) -> str:
    return await poll_until(
        lambda: _text(user, marker), predicate, timeout_s=8.0, what=marker
    )


@pytest.mark.integration
async def test_loop_health_comes_off_the_broadcast(user: User) -> None:
    """The tail the tab shows is the loop's own, checked against what the
    query says about the same loop, and it is quoted against the budget the
    target rate implies — a bare millisecond figure means nothing alone."""
    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-diagnostics").click()
    await asyncio.sleep(0)
    await user.should_see(marker="diagnostics-panel")

    stats = await ui_state.control_panel.client.loop_stats()
    assert stats is not None
    await _settle(user, "diag-loop-rate", lambda t: "Hz target" in t)
    assert _text(user, "diag-loop-rate") == f"{stats.target_hz:.0f} Hz target"

    await _settle(user, "diag-loop-p99", lambda t: "budget" in t)
    health = waldoctl.commander.status.loop_health
    assert health.measured, "the backend reports a loop tail; the surface must say so"
    assert f"{1.0 / stats.target_hz * 1000.0:.2f} ms budget" in _text(
        user, "diag-loop-p99"
    )
    assert _text(user, "diag-loop-overruns") == str(health.overruns)

    # Link health, already on the status surface.
    st = waldoctl.commander.status
    st.link_health.state = "BusOff"
    st.link_health.restarts = 3
    st.link_health.tx_errors = 12
    await _settle(user, "diag-link-state", lambda t: t == "BusOff")
    assert _text(user, "diag-link-restarts") == "3"
    assert _text(user, "diag-link-tx-errors") == "12"


@pytest.mark.integration
async def test_a_backend_that_reports_no_drive_readings_says_so(user: User) -> None:
    """This backend's drivers report fault flags, not analog registers.
    Silence about the drives has to read as "not reported", never as a
    healthy-looking row of dashes or a plausible zero."""
    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-diagnostics").click()
    await asyncio.sleep(0)
    await user.should_see(marker="diagnostics-panel")

    assert not waldoctl.commander.status.drive_health.temperatures_c
    await user.should_see("This backend's drives report no readings")
    assert _text(user, "diag-drive-supply") == "—"
