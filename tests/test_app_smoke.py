"""Smoke test for Waldo Commander app startup and basic UI presence."""

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready


@pytest.mark.integration
async def test_app_starts_and_builds_its_core_ui(user: User) -> None:
    """The page loads (the user fixture asserts HTTP 200), the status
    consumer receives data from the controller, and the core controls,
    tabs and readouts are built.

    Regression: the server readiness check used loop.sock_sendto(), which
    uvloop (NiceGUI's event loop) does not implement. start_controller()
    failed silently, leaving the status consumer uncreated and the UI frozen
    with stale position data.
    """
    from waldo_commander.state import readiness_state

    await user.open("/")
    await wait_for_app_ready(timeout_s=15.0)
    assert readiness_state._backend_done, (
        "status consumer never received a STATUS update"
    )

    for marker in (
        "btn-home",
        "btn-robot-toggle",
        "btn-estop",
        "tab-program",
        "tab-io",
        "tab-settings",
        "tab-gripper",
        "readout-x",
        "btn-j1-plus",
        "btn-j1-minus",
        "btn-j6-plus",
        "btn-j6-minus",
    ):
        await user.should_see(marker=marker)
