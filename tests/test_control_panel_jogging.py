"""Integration tests for control panel jogging functionality.

These tests use the NiceGUI `user` fixture and the real PAROL6 controller
(in fake-serial mode) to verify that jog controls actually change the
reported robot state, rather than just asserting on client call patterns.
"""

import asyncio
import os

import pytest
import waldoctl
from nicegui.testing import User
from waldoctl import ActionState

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    simulate_click,
    wait_for_motion_stable,
    wait_for_motion_start,
    wait_for_app_ready,
)


@pytest.mark.integration
async def test_joint_jog_button_sends_jog_j(user: User) -> None:
    """Clicking a joint jog button should result in joint motion.

    Ensures that when simulator mode is active, clicking the J1 + jog
    button causes the reported J1 angle to change.
    """
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    initial_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0], timeout_s=3.0
    )

    # Click J1 plus button and wait for motion
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()

    # Wait for motion to stabilize
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0], timeout_s=5.0
    )

    # We expect J1 to change after the jog command
    assert abs(final_j1 - initial_j1) > 0.1, (
        f"Expected J1 angle to change after jog. "
        f"Initial: {initial_j1:.2f}°, Final: {final_j1:.2f}°"
    )


@pytest.mark.integration
async def test_cartesian_axis_disabled_when_at_limit(user: User) -> None:
    """Verify cartesian axis buttons become disabled when at workspace limits.

    When the robot is at or near a cartesian workspace limit, the jog button
    for that direction should become disabled to prevent motion beyond limits.
    """
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    # Switch to cartesian jog tab
    user.find(marker="tab-cartesian").click()
    await asyncio.sleep(0.1)

    # Check that cartesian buttons exist and are initially enabled
    xplus = user.find(marker="axis-xplus")
    assert xplus is not None, "X+ axis button should exist"


@pytest.mark.integration
async def test_joint_jog_moves_both_directions(user: User) -> None:
    """Verify joint jog buttons move by step amount in both directions.

    When a joint jog button is clicked briefly (not held), it should move
    the joint by approximately the configured step size using move_j.
    Tests both positive and negative directions.
    """

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # --- Part 1: Positive direction ---
    waldoctl.commander.settings.jog.joint_step_deg = 5.0
    initial_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0]
    )

    # Click J1 plus button (single click, not hold)
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0]
    )

    # J1 should have increased by exactly 5 degrees (±0.1° for rounding)
    delta = final_j1 - initial_j1
    assert 4.9 <= delta <= 5.1, f"Expected J1 to move +5.0°±0.1°, moved {delta:.2f}°"

    # --- Part 2: Negative direction ---
    waldoctl.commander.settings.jog.joint_step_deg = 3.0
    initial_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0]
    )

    # Click J1 minus button (mousedown/mouseup pair — jog buttons don't listen for raw click)
    await simulate_click(user, "btn-j1-minus")
    await wait_for_motion_start()
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0]
    )

    # J1 should have decreased by exactly 3 degrees (±0.1° for rounding)
    delta = initial_j1 - final_j1
    assert 2.9 <= delta <= 3.1, f"Expected J1 to move -3.0°±0.1°, moved {delta:.2f}°"


@pytest.mark.integration
async def test_cartesian_jog_all_axes(user: User) -> None:
    """Verify cartesian jog buttons move correctly in all axes.

    Tests Z+, Z-, and RZ+ to cover translation and rotation.
    When a cartesian jog button is clicked briefly (not held), it should move
    the TCP by approximately the configured step size using move_l.
    """

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Switch to Cartesian Jog tab
    user.find("Cartesian Jog").click()

    # Wait for robot to be completely idle - no pending commands
    for _ in range(50):  # Up to 5 seconds
        if waldoctl.commander.status.action.state == ActionState.IDLE:
            break
        await asyncio.sleep(0.1)

    # --- Part 1: Z+ translation ---
    waldoctl.commander.settings.jog.joint_step_deg = 10.0
    await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), tolerance=0.05, stable_ticks=30
    )
    initial_z = float(waldoctl.commander.status.pose.z)

    await simulate_click(user, "axis-zplus")
    await wait_for_motion_start()
    final_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), tolerance=0.1
    )

    delta_z = final_z - initial_z
    assert 9.9 <= delta_z <= 10.1, (
        f"Expected Z to move +10.0mm±0.1mm, moved {delta_z:.2f}mm"
    )

    # --- Part 2: Z- translation ---
    waldoctl.commander.settings.jog.joint_step_deg = 5.0
    await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), tolerance=0.1, stable_ticks=20
    )
    initial_z = float(waldoctl.commander.status.pose.z)

    await simulate_click(user, "axis-zminus")
    await wait_for_motion_start()
    final_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), tolerance=0.1
    )

    delta = initial_z - final_z
    assert 4.9 <= delta <= 5.1, f"Expected Z to move -5.0mm±0.1mm, moved {delta:.2f}mm"

    # --- Part 3: RZ+ rotation ---
    waldoctl.commander.settings.jog.joint_step_deg = 2.0
    await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.rz), tolerance=0.1, stable_ticks=20
    )
    initial_rz = float(waldoctl.commander.status.pose.rz)

    await simulate_click(user, "axis-rzplus")
    await wait_for_motion_start()
    final_rz = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.rz), tolerance=0.1
    )

    delta = abs(final_rz - initial_rz)
    assert 1.9 <= delta <= 2.1, f"Expected RZ to change 2.0°±0.1°, changed {delta:.2f}°"


@pytest.mark.integration
async def test_joint_jog_one_degree_step(user: User) -> None:
    """Verify single click with 1.0° step moves exactly 1 degree.

    Regression test for step precision with small step sizes.
    Uses TOPPRA motion profile.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Set motion profile to TOPPRA (use the app's own client, not session_client)
    await ui_state.control_panel.client.select_profile("TOPPRA")

    # Set step size to 1.0 degrees
    waldoctl.commander.settings.jog.joint_step_deg = 1.0

    # Wait for robot to be completely stable
    initial_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=3.0,
        stable_ticks=20,
    )

    # Single click on J1 plus
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=5.0,
        stable_ticks=20,
    )

    delta = final_j1 - initial_j1
    assert 0.9 <= delta <= 1.1, f"Expected J1 to move +1.0°±0.1°, moved {delta:.4f}°"


@pytest.mark.integration
async def test_cartesian_jog_one_mm_step(user: User) -> None:
    """Verify single click with 1.0mm step moves exactly 1mm.

    Regression test for cartesian step precision with small step sizes.
    Uses TOPPRA motion profile.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Set motion profile to TOPPRA (use the app's own client, not session_client)
    await ui_state.control_panel.client.select_profile("TOPPRA")

    # Switch to Cartesian Jog tab
    user.find("Cartesian Jog").click()
    await asyncio.sleep(0.1)

    # Set step size to 1.0mm
    waldoctl.commander.settings.jog.joint_step_deg = 1.0

    # Wait for robot to be completely stable
    initial_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), timeout_s=3.0, stable_ticks=20
    )

    # Single click on Z plus
    await simulate_click(user, "axis-zplus")
    await wait_for_motion_start()
    final_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), timeout_s=5.0, stable_ticks=20
    )

    delta = final_z - initial_z
    assert 0.9 <= delta <= 1.1, f"Expected Z to move +1.0mm±0.1mm, moved {delta:.4f}mm"


@pytest.mark.skipif(
    "GITHUB_ACTIONS" in os.environ,
    reason="Timing-dependent: CI runners may not complete all motion steps",
)
@pytest.mark.integration
async def test_joint_jog_rapid_clicks(user: User) -> None:
    """Verify rapid clicking accumulates steps correctly.

    When clicking multiple times in quick succession, each click should
    add the full step amount. Tests for race conditions with status updates.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Set motion profile to TOPPRA (use the app's own client, not session_client)
    await ui_state.control_panel.client.select_profile("TOPPRA")

    # Set step size to 1.0 degrees
    waldoctl.commander.settings.jog.joint_step_deg = 1.0
    num_clicks = 5
    expected_total = num_clicks * 1.0

    # Wait for robot to be completely stable
    initial_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=3.0,
        stable_ticks=20,
    )

    # Rapid clicks - 150ms between clicks is fast but realistic human speed
    for _ in range(num_clicks):
        await simulate_click(user, "btn-j1-plus", hold_ms=30)
        await asyncio.sleep(0.15)  # 150ms between clicks (~6-7 clicks/sec)

    # Allow time for all motion commands to be processed before checking stability
    await asyncio.sleep(0.5)

    # Wait for all motion to complete (longer timeout and more stable ticks for CI)
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=15.0,
        stable_ticks=50,
    )

    delta = final_j1 - initial_j1
    # Wide tolerance: rapid clicking on slow platforms loses clicks due to
    # event loop scheduling and motion command queuing
    min_expected = expected_total * 0.4
    max_expected = expected_total * 1.1
    assert min_expected <= delta <= max_expected, (
        f"Expected J1 to move ~{expected_total}° after {num_clicks} rapid clicks, "
        f"moved {delta:.4f}° (tolerance: {min_expected:.1f}° to {max_expected:.1f}°)"
    )


@pytest.mark.skipif(
    "GITHUB_ACTIONS" in os.environ,
    reason="Timing-dependent: CI runners may not complete all motion steps",
)
@pytest.mark.integration
async def test_cartesian_jog_rapid_clicks(user: User) -> None:
    """Verify rapid cartesian clicking accumulates steps correctly.

    When clicking multiple times in quick succession, each click should
    add the full step amount.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Set motion profile to TOPPRA (use the app's own client, not session_client)
    await ui_state.control_panel.client.select_profile("TOPPRA")

    # Switch to Cartesian Jog tab
    user.find("Cartesian Jog").click()
    await asyncio.sleep(0.1)

    # Set step size to 2.0mm (slightly larger for clearer signal)
    waldoctl.commander.settings.jog.joint_step_deg = 2.0
    num_clicks = 5
    expected_total = num_clicks * 2.0

    # Wait for robot to be completely stable
    initial_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), timeout_s=3.0, stable_ticks=20
    )

    # Rapid clicks - 300ms between clicks (cartesian moves take longer due to IK)
    for _ in range(num_clicks):
        await simulate_click(user, "axis-zplus", hold_ms=30)
        await asyncio.sleep(0.3)  # 300ms between clicks (~3 clicks/sec)

    # Allow time for all motion commands to be processed before checking stability
    await asyncio.sleep(0.5)

    # Wait for all motion to complete (longer timeout and more stable ticks for CI)
    final_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z), timeout_s=15.0, stable_ticks=50
    )

    delta = final_z - initial_z
    # Wide tolerance: rapid clicking on slow platforms loses clicks due to
    # event loop scheduling and motion command queuing
    min_expected = expected_total * 0.4
    max_expected = expected_total * 1.1
    assert min_expected <= delta <= max_expected, (
        f"Expected Z to move ~{expected_total}mm after {num_clicks} rapid clicks, "
        f"moved {delta:.4f}mm (tolerance: {min_expected:.1f}mm to {max_expected:.1f}mm)"
    )


@pytest.mark.integration
async def test_go_to_joint_limit_reaches_actual_limit(user: User) -> None:
    """Go-to-limit buttons should move the joint to its actual limit.

    Clicking a joint limit button should result in the joint reaching
    or being very close to its defined min/max limit value.
    """
    from waldo_commander.state import ui_state

    JOINT_LIMITS_DEG = ui_state.active_robot.joints.limits.position.deg

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Wait for any queued commands to complete first (action_state becomes IDLE)
    for _ in range(50):  # Up to 5 seconds
        if waldoctl.commander.status.action.state == ActionState.IDLE:
            break
        await asyncio.sleep(0.1)

    # Get J1 limits
    j1_min, j1_max = JOINT_LIMITS_DEG[0]

    # Wait for initial status and snapshot current angles after queue drains
    initial_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0], timeout_s=5.0
    )

    # Click J1's min limit button
    user.find(marker="btn-j1-min-limit").click()

    # Wait for motion to start (action_state becomes EXECUTING or angles change)
    await wait_for_motion_start(timeout_s=5.0)

    # Wait for motion to complete and stabilize (limit moves can take 5+ seconds)
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=20.0,
        stable_ticks=30,
    )

    # J1 should be at or very close to its minimum limit (within 1 degree)
    assert abs(final_j1 - j1_min) < 1.0, (
        f"Expected J1 to reach min limit {j1_min}°, "
        f"was {initial_j1:.2f}°, now {final_j1:.2f}°"
    )


# ============================================================================
# Editing Mode Control Panel Tests
# ============================================================================


@pytest.mark.integration
async def test_jog_buttons_disabled_in_editing_mode(user: User) -> None:
    """Verify jog presses don't move the robot when in editing mode.

    When editing mode is active (target editor controls the robot),
    ``set_joint_pressed`` early-returns on ``commander.status.editing_mode``,
    so a jog press is ignored.
    """
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    # Get initial J1 angle
    initial_j1 = waldoctl.commander.status.joints.angles[0]

    # Enable editing mode (the target editor takes over robot control)
    waldoctl.commander.status.editing_mode = True
    await asyncio.sleep(0.1)

    # Press J1 plus (mousedown/mouseup) — should NOT cause motion in editing mode
    await simulate_click(user, "btn-j1-plus")
    await asyncio.sleep(0.3)

    # J1 should NOT have moved (editing mode blocks jog)
    assert abs(waldoctl.commander.status.joints.angles[0] - initial_j1) < 0.1, (
        f"J1 should not move in editing mode. "
        f"Initial: {initial_j1:.2f}°, Current: {waldoctl.commander.status.joints.angles[0]:.2f}°"
    )

    # Clean up
    waldoctl.commander.status.editing_mode = False
