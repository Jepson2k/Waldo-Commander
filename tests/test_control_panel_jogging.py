"""Integration tests for control panel jogging functionality.

These tests use the NiceGUI `user` fixture and the real PAROL6 controller
(in fake-serial mode) to verify that jog controls actually change the
reported robot state, rather than just asserting on client call patterns.
"""

import asyncio
import time

import pytest
import waldoctl
from nicegui import binding
from nicegui.testing import User
from waldoctl import ActionState

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    simulate_click,
    teleport_to_jog_pose,
    wait_for_motion_stable,
    wait_for_motion_start,
    wait_for_app_ready,
)

#: How long to wait for a held axis to open its jog stream [s].
#:
#: A polling wait, so a healthy run leaves as soon as the first datagram
#: goes out and pays nothing for the ceiling. It is generous because the
#: ceiling is only ever reached on the slowest CI runner, where the first
#: jog_l has to wait on the controller — five seconds was enough on Linux
#: and not on Windows, which reads as "the stream never opened".
_JOG_STREAM_BUDGET_S = 20.0


async def _wait_issued(issued: list, count: int, timeout_s: float = 10.0) -> None:
    """Wait until `issued` holds `count` entries.

    Lets a click-per-command loop pace itself off the commands instead of a
    clock: it moves on the moment the press became a move, and says which
    click went missing when one does not.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(issued) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"only {len(issued)} of {count} commands were issued")


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

    # Regression: the J1 readout widget is bound to commander.status.joints
    # .angles, which the status loop mutates in place via set_deg(). If 'angles'
    # were a BindableProperty it would propagate only on reassignment and the
    # readout would freeze; left non-bindable, the binding is a polled active
    # link. Force one refresh step and assert the widget tracks the live angle.
    binding._refresh_step()
    await asyncio.sleep(0)
    readout = next(iter(user.find(marker="joint-readout-0").elements))
    assert readout.value is not None and abs(readout.value - final_j1) < 0.5, (
        f"J1 readout widget froze: shows {readout.value}, live angle is "
        f"{final_j1:.2f}° (angles binding is not tracking in-place set_deg)"
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


@pytest.mark.integration
async def test_joint_jog_rapid_clicks(user: User) -> None:
    """Rapid clicking drops no click, and the arm lands on the last target.

    What the panel owes is one command per click: a press arriving while the
    previous move is still running must still be seen. What it does NOT owe
    is a sum. Each click targets `measured + step`, read at the moment it is
    handled, so clicks that overlap motion deliberately do not accumulate —
    the old form of this test asserted a total and then widened the band to
    40-110% to survive that, which is why it was skipped on CI rather than
    fixed.

    Both halves are read off the commands themselves, so nothing here waits
    on a clock.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel
    await cp.client.select_profile("TOPPRA")

    waldoctl.commander.settings.jog.joint_step_deg = 1.0
    num_clicks = 5

    targets: list = []
    orig_move_j = cp.client.move_j

    async def move_j_spy(angles, *args, **kwargs):
        targets.append(list(angles))
        return await orig_move_j(angles, *args, **kwargs)

    cp.client.move_j = move_j_spy
    try:
        await wait_for_motion_stable(
            lambda: waldoctl.commander.status.joints.angles[0],
            timeout_s=3.0,
            stable_ticks=20,
        )
        # One click per issued command: the next press goes in as soon as the
        # previous one has been turned into a move, which is as fast as a
        # person can click and far faster than the motion completes.
        for i in range(num_clicks):
            await simulate_click(user, "btn-j1-plus", hold_ms=30)
            await _wait_issued(targets, i + 1)
    finally:
        cp.client.move_j = orig_move_j

    assert len(targets) == num_clicks, (
        f"the panel dropped a click: {len(targets)} move_j for {num_clicks} presses"
    )
    # Never backwards: a click read a stale pose at worst, never an older one.
    assert all(b[0] >= a[0] for a, b in zip(targets, targets[1:])), (
        f"a click targeted behind its predecessor: {[t[0] for t in targets]}"
    )
    assert targets[-1][0] > targets[0][0] - 1e-9, "no click moved the target at all"
    # A joint step is ABSOLUTE — `measured + step`, read when the click is
    # handled — so clicks that overlap the motion resolve to the same target
    # and the arm ends on the last one rather than five steps along. That is
    # the behaviour; asserting a sum here is what made the old test flaky.
    final_j1 = await wait_for_motion_stable(
        lambda: waldoctl.commander.status.joints.angles[0],
        timeout_s=20.0,
        stable_ticks=20,
    )
    assert abs(final_j1 - targets[-1][0]) < 0.5, (
        f"the arm settled at {final_j1:.3f}°, not the last commanded "
        f"{targets[-1][0]:.3f}°"
    )


@pytest.mark.integration
async def test_cartesian_jog_rapid_clicks(user: User) -> None:
    """The cartesian half of :func:`test_joint_jog_rapid_clicks`.

    Same contract, same reason it is read off the commands: one move per
    click, each aimed further along +Z, and the arm settling on the last of
    them. A cartesian step is an IK solve per press, so it is the case most
    likely to have a press arrive mid-solve.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel
    await cp.client.select_profile("TOPPRA")

    user.find("Cartesian Jog").click()
    await asyncio.sleep(0)
    await teleport_to_jog_pose(cp.client)

    waldoctl.commander.settings.jog.joint_step_deg = 2.0
    num_clicks = 5

    targets: list = []
    orig_move_l = cp.client.move_l

    async def move_l_spy(pose, *args, **kwargs):
        targets.append(list(pose))
        return await orig_move_l(pose, *args, **kwargs)

    cp.client.move_l = move_l_spy
    try:
        initial_z = await wait_for_motion_stable(
            lambda: float(waldoctl.commander.status.pose.z),
            timeout_s=3.0,
            stable_ticks=20,
        )
        for i in range(num_clicks):
            await simulate_click(user, "axis-zplus", hold_ms=30)
            await _wait_issued(targets, i + 1)
    finally:
        cp.client.move_l = orig_move_l

    assert len(targets) == num_clicks, (
        f"the panel dropped a click: {len(targets)} move_l for {num_clicks} presses"
    )
    step = float(waldoctl.commander.settings.jog.joint_step_deg)
    assert all(abs(t[2] - step) < 1e-9 for t in targets), (
        f"every cartesian click is the same relative +Z step: {[t[2] for t in targets]}"
    )
    # A cartesian step is RELATIVE, so unlike the joint one it does not
    # resolve to a single target — but a press landing mid-move supersedes
    # the move in flight rather than queueing behind it, so the total is not
    # a sum either. What holds either way: the arm goes up, and it cannot go
    # further than every click asked for. A band between those two is the
    # runtime's supersede timing, and pinning it is what made this flaky.
    final_z = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.z),
        timeout_s=20.0,
        stable_ticks=20,
    )
    ceiling = initial_z + num_clicks * step
    assert final_z > initial_z + step / 2, (
        f"clicking +Z left the arm at {final_z:.3f} mm, from {initial_z:.3f} mm"
    )
    assert final_z <= ceiling + 1.0, (
        f"the arm reached Z {final_z:.3f} mm, past the {ceiling:.3f} mm every "
        f"click together asked for"
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


@pytest.mark.integration
async def test_translation_frame_toggle_changes_jog_frame(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Translation RF setting must drive the frame argument of both
    cartesian jog paths: incremental clicks (move_l) and streamed holds
    (jog_l, whose memoized axis lookup must be invalidated on toggle).
    Rotation clicks stay in TRF regardless of the selection.
    """
    import time

    from nicegui import app as ng_app

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel
    moves: list[str] = []
    jogs: list[str] = []
    real_move_l = cp.client.move_l
    real_jog_l = cp.client.jog_l

    async def move_l_spy(pose, **kwargs):
        moves.append(kwargs.get("frame", "WRF"))
        return await real_move_l(pose, **kwargs)

    async def jog_l_spy(frame, *args, **kwargs):
        jogs.append(frame)
        return await real_jog_l(frame, *args, **kwargs)

    monkeypatch.setattr(cp.client, "move_l", move_l_spy)
    monkeypatch.setattr(cp.client, "jog_l", jog_l_spy)

    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    frame_select = next(iter(user.find(marker="select-translation-frame").elements))
    user.find(marker="tab-cartesian").click()
    await asyncio.sleep(0)

    async def select_frame(value: str) -> None:
        frame_select.set_value(value)
        for _ in range(20):
            await asyncio.sleep(0.1)
            if cp.translation_frame == value:
                return
        assert cp.translation_frame == value, (
            f"translation_frame should hydrate to {value}"
        )

    async def hold_axis(marker: str) -> None:
        """Hold an axis button past the click threshold until a jog_l streams."""
        n_before = len(jogs)
        user.find(marker=marker).trigger("mousedown")
        try:
            deadline = time.monotonic() + _JOG_STREAM_BUDGET_S
            while time.monotonic() < deadline and len(jogs) == n_before:
                await asyncio.sleep(0.05)
        finally:
            user.find(marker=marker).trigger("mouseup")
        assert len(jogs) > n_before, "expected a streamed jog_l while holding"

    async def click_axis(marker: str) -> None:
        """Click an axis and wait until its move_l has been issued."""
        n_before = len(moves)
        await simulate_click(user, marker)
        for _ in range(50):
            if len(moves) > n_before:
                break
            await asyncio.sleep(0.1)
        assert len(moves) > n_before, f"expected a move_l after clicking {marker}"

    waldoctl.commander.settings.jog.joint_step_deg = 5.0
    try:
        # Tool frame: incremental click sends move_l(frame="TRF")
        await select_frame("TRF")
        assert ng_app.storage.general["translation_frame"] == "TRF"
        await wait_for_motion_stable(lambda: float(waldoctl.commander.status.pose.z))
        await click_axis("axis-zplus")
        assert moves[-1] == "TRF", f"expected TRF move_l, got {moves}"
        await wait_for_motion_start()
        await wait_for_motion_stable(lambda: float(waldoctl.commander.status.pose.z))

        # Tool frame: streamed hold sends jog_l("TRF", ...)
        await hold_axis("axis-zminus")
        assert jogs[-1] == "TRF", f"expected TRF jog_l, got {jogs}"
        await wait_for_motion_stable(lambda: float(waldoctl.commander.status.pose.z))

        # Back to world frame: the memoized lookup must not keep serving TRF
        await select_frame("WRF")
        await hold_axis("axis-zplus")
        assert jogs[-1] == "WRF", f"expected WRF jog_l after toggle back, got {jogs}"
        await wait_for_motion_stable(lambda: float(waldoctl.commander.status.pose.z))

        # Rotation clicks stay in TRF even with WRF translation selected
        await click_axis("axis-rzplus")
        assert moves[-1] == "TRF", f"rotation click must stay TRF, got {moves}"
    finally:
        frame_select.set_value("WRF")
        await asyncio.sleep(0)


@pytest.mark.integration
async def test_jog_arrow_inversion_flips_button_direction_and_label(user: User) -> None:
    """The Invert X Jog switch must flip what the physical X arrow slot
    commands AND its label/marker in lockstep: the left arrow sends X+ by
    default (camera-matched inversion) and X- when inverted, so the glyph,
    the marker, and the actual motion never disagree.
    """
    from nicegui import app as ng_app

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel
    user.find(marker="tab-cartesian").click()
    await asyncio.sleep(0)

    # The homed standby pose is a wrist singularity (J5 = 0) where the X-step
    # move_l can fail IK partway; jog from the singularity-free pose instead.
    await teleport_to_jog_pose(cp.client)

    waldoctl.commander.settings.jog.joint_step_deg = 5.0

    # The physical left-arrow slot commands X+ by default
    slot = next(iter(user.find(marker="axis-xplus").elements))
    assert slot is cp._cart_slot_elems["lr_neg"], (
        "axis-xplus should be the left-arrow slot by default"
    )

    initial_x = await wait_for_motion_stable(
        lambda: float(waldoctl.commander.status.pose.x), tolerance=0.05, stable_ticks=30
    )
    await simulate_click(user, "axis-xplus")
    await wait_for_motion_start()
    # The 5mm move_l has a sub-tolerance creep phase before its main ramp, so
    # value-stability would trigger early — wait for the action to finish.
    for _ in range(100):
        if waldoctl.commander.status.action.state == ActionState.IDLE:
            break
        await asyncio.sleep(0.1)
    final_x = float(waldoctl.commander.status.pose.x)
    assert 4.9 <= final_x - initial_x <= 5.1, (
        f"left arrow should move X +5.0mm by default, moved {final_x - initial_x:.2f}mm"
    )

    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    invert_switch = next(iter(user.find(marker="switch-invert-x").elements))
    try:
        invert_switch.set_value(True)
        for _ in range(20):
            await asyncio.sleep(0.1)
            if cp.invert_x:
                break
        assert cp.invert_x, "invert_x should hydrate from the switch"
        assert ng_app.storage.general["jog_invert_x"] is True

        # Same physical slot now advertises and commands X-
        assert slot._markers == ["axis-xminus"], (
            f"left arrow should re-mark to axis-xminus, got {slot._markers}"
        )
        label = cp._cart_slot_meta["lr_neg"]["label"]
        assert label.text == "X-", "left arrow label should read X-"

        user.find(marker="tab-cartesian").click()
        await asyncio.sleep(0)
        assert next(iter(user.find(marker="axis-xminus").elements)) is slot

        initial_x = await wait_for_motion_stable(
            lambda: float(waldoctl.commander.status.pose.x),
            tolerance=0.05,
            stable_ticks=30,
        )
        await simulate_click(user, "axis-xminus")
        await wait_for_motion_start()
        for _ in range(100):
            if waldoctl.commander.status.action.state == ActionState.IDLE:
                break
            await asyncio.sleep(0.1)
        final_x = float(waldoctl.commander.status.pose.x)
        assert 4.9 <= initial_x - final_x <= 5.1, (
            f"inverted left arrow should move X -5.0mm, "
            f"moved {final_x - initial_x:.2f}mm"
        )
    finally:
        invert_switch.set_value(False)
        await asyncio.sleep(0)


@pytest.mark.integration
async def test_inversion_mid_hold_releases_captured_axis(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flipping Invert X while an arrow is held must still stop the stream on
    release: the axis is captured at press, so the release targets what is
    actually streaming instead of the re-resolved (flipped) axis."""
    import time

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel
    user.find(marker="tab-cartesian").click()
    await asyncio.sleep(0)

    # Stream from the singularity-free pose: jog_l steps from the homed
    # standby pose (J5 = 0) can fail IK and wedge the shared controller.
    await teleport_to_jog_pose(cp.client)

    jogs: list = []
    orig_jog_l = cp.client.jog_l

    async def jog_l_spy(*args, **kwargs):
        jogs.append(kwargs)
        return await orig_jog_l(*args, **kwargs)

    monkeypatch.setattr(cp.client, "jog_l", jog_l_spy)

    user.find(marker="axis-xplus").trigger("mousedown")
    try:
        n = len(jogs)
        deadline = time.monotonic() + _JOG_STREAM_BUDGET_S
        while time.monotonic() < deadline and len(jogs) == n:
            await asyncio.sleep(0.05)
        assert len(jogs) > n, "expected a streamed jog_l while holding"

        cp.set_jog_inversion(invert_x=True)  # what the settings switch does
        await asyncio.sleep(0)
    finally:
        # The flip re-marked the held slot from axis-xplus to axis-xminus.
        # The release handler is async: let it run while inversion is still
        # flipped before restoring, or the test wouldn't exercise the race.
        user.find(marker="axis-xminus").trigger("mouseup")
        await asyncio.sleep(0.3)
        cp.set_jog_inversion(invert_x=False)

    await asyncio.sleep(0.3)
    settled = len(jogs)
    await asyncio.sleep(0.5)
    assert len(jogs) == settled, "release must stop the captured axis's stream"
    assert not any(cp._cart_pressed_axes.values()), (
        f"no axis may stay pressed after release: {cp._cart_pressed_axes}"
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


@pytest.mark.integration
async def test_held_jog_self_releases_when_button_disables_at_limit(
    user: User,
) -> None:
    """A held jog whose button disables at the joint limit must self-release.

    NiceGUI drops events from disabled elements, so the mouseup for a button
    that disables mid-hold never reaches the server, and touch input has no
    mouseleave fallback. Without a server-side release the jog stream renews
    against the limit indefinitely.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel

    # Park J1 just above the point where one more step no longer fits, so a
    # short hold crosses the enabled-binding boundary.
    hi = float(ui_state.active_robot.joints.limits.position.deg[0, 1])
    step = abs(float(waldoctl.commander.settings.jog.joint_step_deg))
    pose = [float(a) for a in waldoctl.commander.status.joints.angles.deg]
    pose[0] = hi - step - 1.0
    await cp.client.teleport(pose)
    for _ in range(50):
        if abs(float(waldoctl.commander.status.joints.angles.deg[0]) - pose[0]) < 0.5:
            break
        await asyncio.sleep(0.1)

    # Hold without ever releasing: once the button disables, a browser mouseup
    # would be dropped anyway, so the test never sends one.
    user.find(marker="btn-j1-plus").trigger("mousedown")
    for _ in range(30):
        await asyncio.sleep(0.1)
        if ui_state.joint_jog_timer.active:
            break
    assert ui_state.joint_jog_timer.active, "hold never started streaming"

    for _ in range(100):
        await asyncio.sleep(0.1)
        if not ui_state.joint_jog_timer.active:
            break
    assert not ui_state.joint_jog_timer.active, (
        "jog stream must stop when the button disables at the limit"
    )
    assert float(waldoctl.commander.status.joints.angles.deg[0]) <= hi + 0.5


@pytest.mark.integration
async def test_held_cart_jog_self_releases_when_pad_disables(user: User) -> None:
    """A held cart jog whose pad strong-disables mid-hold must self-release.

    The strong-disable class sets pointer-events:none, so the browser drops
    the mouseup for a pad that disables mid-hold (touch has no mouseleave
    fallback); cart_jog_tick must release the jog itself.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    cp = ui_state.control_panel
    user.find(marker="tab-cartesian").click()
    await asyncio.sleep(0)
    await teleport_to_jog_pose(cp.client)

    user.find(marker="axis-xplus").trigger("mousedown")
    for _ in range(30):
        await asyncio.sleep(0.1)
        if ui_state.cart_jog_timer.active:
            break
    assert ui_state.cart_jog_timer.active, "hold never started streaming"

    # Availability flips the way the status consumer applies it: a wholesale
    # list swap on the held direction's frame.
    frame = cp._translation_frame_name()
    frame_av = waldoctl.commander.status.pose.cart_jog.by_frame[frame]
    frame_av.can_jog_pos = [False, True, True, True, True, True]

    for _ in range(50):
        await asyncio.sleep(0.1)
        if not ui_state.cart_jog_timer.active:
            break
    assert not ui_state.cart_jog_timer.active, (
        "cart jog stream must stop when the pad disables mid-hold"
    )
