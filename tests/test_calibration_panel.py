"""The Diagnostics tab's Calibration section drives a par6 calibration routine
against the runtime Commander manages and shows its verdict.

Like test_par6_backend.py this boots the app on par6 (``WALDO_PAR6_E2E=1``,
``PAR6D_BIN``) and must run in its own pytest process:

    WALDO_PAR6_E2E=1 PAR6D_BIN=/path/to/par6d pytest tests/test_calibration_panel.py
"""

import os
from pathlib import Path

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready, wait_until
from tests.test_par6_backend import par6_env, requires_par6  # noqa: F401

START_DEG = [0, -90, 170, 0, -20, 180]


@pytest.fixture
def calibration_env(
    par6_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """A private calibration directory: the managed par6d records there and no
    installed config from an earlier run leaks into this app."""
    monkeypatch.setenv("WALDO_PAR6_DIR", str(tmp_path / "par6"))
    monkeypatch.delenv("PAR6_DIAGNOSTICS", raising=False)
    monkeypatch.delenv("PAR6_CONFIG", raising=False)
    return tmp_path / "par6"


@requires_par6
@pytest.mark.integration
async def test_calibration_section_runs_check_against_the_managed_runtime(
    calibration_env: Path, user: User
) -> None:
    import waldoctl

    from waldo_commander.components import calibration
    from waldo_commander.state import robot_state

    await user.open("/")
    await wait_for_app_ready(timeout_s=60.0)
    assert calibration.available()
    # The managed runtime was started recording, into the private directory.
    capture = Path(os.environ["PAR6_DIAGNOSTICS"])
    assert capture.parent == calibration_env and capture.is_file()

    client = waldoctl.commander.client
    assert await client.teleport(START_DEG) >= 0
    assert await wait_until(lambda: robot_state.homed, timeout_s=10.0)

    user.find(marker="tab-diagnostics").click()
    await user.should_see(marker="diag-calibration")
    routine = user.find(marker="calibration-routine")
    routine.elements.pop().set_value("check")
    user.find(marker="calibration-start").click()
    status = user.find(marker="calibration-status").elements.pop()
    assert await wait_until(lambda: "running" in status.text, timeout_s=10.0), (
        status.text
    )
    assert await wait_until(
        lambda: "PASS" in status.text or "FAIL" in status.text, timeout_s=180.0
    ), status.text
    assert "check: PASS" in status.text, status.text
    runs = list((calibration_env / "runs").glob("*-check"))
    assert (
        len(runs) == 1
        and (runs[0] / "check.json").is_file()
        and (runs[0] / "trials.json").is_file()
    )
    log = user.find(marker="calibration-log").elements.pop()
    lines = [child.text for child in log.default_slot.children]
    assert any("check J2" in line for line in lines), lines
    # A check stages nothing: no patch, no apply.
    await user.should_not_see(marker="calibration-patch")
    await user.should_not_see(marker="calibration-apply")
    from waldoctl.robot_status import ActionState

    assert waldoctl.commander.status.action.state == ActionState.IDLE
