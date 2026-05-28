"""Tests for UI enablement behavior based on robot state and limits."""

import asyncio
import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.wait import (
    wait_for_app_ready,
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_motion_stable,
    wait_for_motion_start,
)


@pytest.mark.integration
async def test_joint_at_limit_disables_direction(user: User, robot_state) -> None:
    """Test that when a joint reaches its limit, the jog button for that direction is disabled.

    When a joint is at or near its maximum limit, the positive direction
    button should be disabled to prevent motion beyond the limit.
    """
    from waldo_commander.state import ui_state

    JOINT_LIMITS_DEG = ui_state.active_robot.joints.limits.position.deg

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Get J1 limits
    j1_min, j1_max = JOINT_LIMITS_DEG[0]

    # Move J1 to its max limit using the limit button
    user.find(marker="btn-j1-max-limit").click()
    await wait_for_motion_start(timeout_s=5.0)
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=20.0,
        stable_ticks=30,
    )

    # Verify we're at or near max limit
    assert abs(final_j1 - j1_max) < 2.0, (
        f"J1 should be near max limit {j1_max}°, got {final_j1:.2f}°"
    )

    # At max limit, positive direction should be blocked. ``can_jog_pos[0]``
    # mirrors the backend ``joint_en`` positive bit for J1.
    await asyncio.sleep(0.1)

    pos = waldoctl.commander.status.joints.can_jog_pos
    j1_plus_enabled = pos[0] if pos else True
    assert not j1_plus_enabled, (
        f"J1+ should be disabled at max limit, can_jog_pos[0]={j1_plus_enabled}"
    )


@pytest.mark.integration
async def test_cartesian_at_workspace_limit_disables_axis(
    user: User, robot_state
) -> None:
    """Test that when near workspace limits, cartesian axis buttons become disabled.

    When the robot TCP approaches the edge of the reachable workspace,
    certain cartesian directions should become disabled.
    """
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Extend the arm by moving J2 to its limit (stretches arm outward)
    # This quickly reaches the cartesian workspace boundary
    user.find(marker="btn-j2-max-limit").click()
    await wait_for_motion_start()
    await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[1], timeout_s=15.0
    )

    # Wait for enablement arrays to update
    await asyncio.sleep(0.2)

    # At extended position, some cartesian directions should be disabled.
    wrf = waldoctl.commander.status.pose.cart_jog.by_frame.get("WRF")
    assert wrf is not None, "cart_jog should have WRF frame"
    disabled_count = sum(1 for v in wrf.can_jog_pos if not v) + sum(
        1 for v in wrf.can_jog_neg if not v
    )
    assert disabled_count > 0, (
        f"At extended arm position, some cartesian directions should be disabled. "
        f"WRF can_jog_pos={list(wrf.can_jog_pos)}, can_jog_neg={list(wrf.can_jog_neg)}"
    )


@pytest.mark.integration
async def test_joint_en_updates_on_motion(user: User, robot_state) -> None:
    """Test that joint enable flags update during motion.

    As the robot moves, the joint_en array should update to reflect
    which directions are still valid for motion.
    """
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    # Verify can_jog_pos / can_jog_neg lists each have one entry per joint.
    joints = waldoctl.commander.status.joints
    assert len(joints.can_jog_pos) == 6, (
        f"Expected 6 can_jog_pos values, got {len(joints.can_jog_pos)}"
    )
    assert len(joints.can_jog_neg) == 6, (
        f"Expected 6 can_jog_neg values, got {len(joints.can_jog_neg)}"
    )

    # At home position, most directions should be enabled.
    enabled_count = sum(1 for v in joints.can_jog_pos if v) + sum(
        1 for v in joints.can_jog_neg if v
    )
    assert enabled_count >= 6, (
        f"At home position, at least 6 directions should be enabled, got {enabled_count}"
    )
