"""Integration tests for simulator toggle, mode switching, and E-STOP behavior.

These tests use the NiceGUI `user` fixture and the real PAROL6 controller
(in fake-serial mode) to verify that mode toggles, HOME, and digital
E-STOP behavior work as expected at the UI and state level.
"""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready


@pytest.mark.integration
async def test_home_command_behavior(
    user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """HOME command should be blocked without connection, allowed with simulator.

    Tests both:
    1. HOME is blocked when neither simulator nor hardware is active
    2. HOME is allowed and sends command when simulator is active
    """
    await user.open("/")
    await wait_for_app_ready()

    # --- Part 1: HOME blocked without connection ---
    # Override state after page load so HOME guard sees both flags as False
    import waldoctl as _wctl

    _wctl.commander.status.simulator_active = False
    _wctl.commander.status.connected = False

    user.find(marker="btn-home").click()
    await asyncio.sleep(0)

    # Should see an error notification
    await user.should_see("Robot mode requires a hardware connection")
    # And we should not see the success message
    assert not any("Sent HOME" in m for m in user.notify.messages)

    # --- Part 2: HOME allowed with simulator ---
    # Enable simulator mode and try again
    _wctl.commander.status.simulator_active = True

    user.find(marker="btn-home").click()

    # Wait for the async HOME command to complete and log
    for _ in range(20):
        await asyncio.sleep(0.1)
        if any("HOME sent" in r.message for r in caplog.get_records("call")):
            break
    assert any("HOME sent" in r.message for r in caplog.get_records("call"))

    # --- Part 3: holding HOME past the threshold calibrates ---
    from waldo_commander.constants import HOME_LONG_PRESS_S
    from waldo_commander.state import robot_state

    for _ in range(100):
        if robot_state.homed:
            break
        await asyncio.sleep(0.1)
    assert robot_state.homed

    btn = user.find(marker="btn-home")
    btn.trigger("pointerdown")
    # The firmware drops the homed flag while it seeks the end stops — a
    # planned return move never does — so watch for it during the hold.
    deadline = asyncio.get_running_loop().time() + HOME_LONG_PRESS_S + 3.0
    while robot_state.homed and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert not robot_state.homed, "calibration never dropped the homed flag"
    btn.trigger("pointerup")
    btn.trigger("click")
    for _ in range(300):
        if robot_state.homed:
            break
        await asyncio.sleep(0.1)
    assert robot_state.homed


@pytest.mark.integration
async def test_digital_estop_dialog_behavior(user: User) -> None:
    """Digital E-STOP dialog should appear with Resume button."""
    await user.open("/")
    await wait_for_app_ready()

    # Click E-STOP button to trigger digital estop
    user.find(marker="btn-estop").click()
    await asyncio.sleep(0.1)

    # Dialog should appear with correct content
    await user.should_see("Digital E-STOP Active")
    await user.should_see("Robot motion has been stopped.")

    # Resume button should be present (marked for testability)
    resume_btn = user.find(marker="btn-estop-resume")
    assert resume_btn is not None, "Resume button should be present"

    # Verify dialog has the overlay-card styling (frosted glass effect)
    dialog_card = user.find(marker="estop-dialog")
    assert dialog_card is not None, "E-STOP dialog card should have marker"

    # Clean up: dismiss the dialog by clicking Resume
    resume_btn.click()
    await asyncio.sleep(0.1)


@pytest.mark.integration
async def test_freedrive_is_disabled_on_a_robot_without_the_capability(
    user: User,
) -> None:
    """parol6 reports ``has_freedrive`` False, so the control renders
    unavailable rather than inviting a click that can only be refused.

    The enabled path is covered against a capable backend in
    ``test_par6_backend.py``, where the button round-trips on the wire.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    assert not ui_state.active_robot.has_freedrive, (
        "this test needs a backend that reports no freedrive"
    )
    button = next(iter(user.find(marker="btn-freedrive").elements))
    assert not button.enabled, "freedrive must be disabled without the capability"


@pytest.mark.unit
async def test_mode_switch_stops_running_script(tmp_path) -> None:
    """Switching between simulator and robot modes should stop any running user script.

    This is a safety feature: when changing modes, any running script is
    automatically stopped to prevent unexpected robot behavior.

    This unit test verifies the behavior without going through the full UI
    mode toggle flow, which would cause serial port errors in test environments.
    """
    from waldo_commander.services.script_runner import (
        run_script,
        stop_script,
        create_default_config,
    )

    # Create a long-running script
    script_content = """import time
while True:
    print("running")
    time.sleep(0.1)
"""
    script_path = tmp_path / "test_long_running.py"
    script_path.write_text(script_content, encoding="utf-8")

    # Start the script
    config = create_default_config(str(script_path))
    handle = await run_script(
        config,
        on_stdout=lambda line: None,
        on_stderr=lambda line: None,
    )

    # Yield to handler
    await asyncio.sleep(0)

    # Verify script is running
    assert handle["proc"].returncode is None, "Expected script to still be running"

    # Now simulate what on_toggle_sim does: stop the script
    # This tests the core safety behavior
    await stop_script(handle, timeout=2.0)

    # Verify script was stopped
    assert handle["proc"].returncode is not None, (
        "Expected script to be stopped after mode switch"
    )
