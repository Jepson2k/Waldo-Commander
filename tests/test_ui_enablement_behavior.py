"""Tests for UI enablement behavior based on robot state and limits."""

import pytest
import waldoctl
from nicegui.testing import User

from tests.helpers.motion import idle, settled
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    poll_until,
    teleport_to_jog_pose,
    wait_for_app_ready,
    wait_for_motion_start,
    wait_until,
)


@pytest.mark.integration
async def test_limits_disable_the_directions_that_would_pass_them(user: User) -> None:
    """Joint enable flags cover every joint and, at home, most directions.
    With a joint at its maximum limit its positive direction is disabled;
    with the arm stretched to the workspace edge some cartesian directions
    are."""
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    joints = waldoctl.commander.status.joints
    assert len(joints.can_jog_pos) == 6, (
        f"Expected 6 can_jog_pos values, got {len(joints.can_jog_pos)}"
    )
    assert len(joints.can_jog_neg) == 6, (
        f"Expected 6 can_jog_neg values, got {len(joints.can_jog_neg)}"
    )
    enabled_count = sum(1 for v in joints.can_jog_pos if v) + sum(
        1 for v in joints.can_jog_neg if v
    )
    assert enabled_count >= 6, (
        f"At home position, at least 6 directions should be enabled, got {enabled_count}"
    )

    # A prior test can leave J1 parked at its max limit; the limit-move is
    # then a no-op and wait_for_motion_start times out. Start from a known
    # pose so the move is real.
    client = ui_state.control_panel.client
    await teleport_to_jog_pose(client)
    j1_max = float(ui_state.active_robot.joints.limits.position.deg[0][1])
    user.find(marker="btn-j1-max-limit").click()
    await wait_for_motion_start(timeout_s=5.0)
    await poll_until(
        lambda: float(waldoctl.commander.status.joints.angles.deg[0]),
        lambda v: abs(v - j1_max) < 2.0 and idle(),
        timeout_s=20.0,
        interval=0.05,
        what=f"J1 reaching its max limit {j1_max}°",
    )
    # ``can_jog_pos[0]`` mirrors the backend ``joint_en`` positive bit for J1,
    # which arrives on a status frame.
    assert await wait_until(lambda: not joints.can_jog_pos[0], timeout_s=2.0), (
        f"J1+ should be disabled at max limit, can_jog_pos={list(joints.can_jog_pos)}"
    )

    # Extend the arm by moving J2 to its limit (stretches arm outward)
    # This quickly reaches the cartesian workspace boundary
    await teleport_to_jog_pose(client)
    user.find(marker="btn-j2-max-limit").click()
    await wait_for_motion_start()
    await settled(
        lambda: float(waldoctl.commander.status.joints.angles.deg[1]), timeout_s=15.0
    )

    # Enablement is computed by the IK worker subprocess and arrives on a
    # later status frame, so poll for it: a fixed wait passes on an idle
    # machine and fails whenever the worker is competing for CPU.
    def _disabled_count() -> int:
        frame = waldoctl.commander.status.pose.cart_jog.by_frame.get("WRF")
        if frame is None:
            return 0
        return sum(1 for v in frame.can_jog_pos if not v) + sum(
            1 for v in frame.can_jog_neg if not v
        )

    await poll_until(
        _disabled_count,
        lambda n: n > 0,
        timeout_s=10.0,
        what="a disabled cartesian jog direction",
    )

    wrf = waldoctl.commander.status.pose.cart_jog.by_frame.get("WRF")
    assert wrf is not None, "cart_jog should have WRF frame"
    assert _disabled_count() > 0, (
        f"At extended arm position, some cartesian directions should be disabled. "
        f"WRF can_jog_pos={list(wrf.can_jog_pos)}, can_jog_neg={list(wrf.can_jog_neg)}"
    )
