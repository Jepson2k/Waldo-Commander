"""Integration tests for control panel jogging functionality.

These tests use the NiceGUI `user` fixture and the real PAROL6 controller
(in fake-serial mode) to verify that jog controls actually change the
reported robot state, rather than just asserting on client call patterns.
"""

import asyncio
import math
import re
import time

import numpy as np
import pytest
import waldoctl
from nicegui import binding
from nicegui.testing import User

from tests.helpers.motion import idle, settled, wait_moved
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    poll_until,
    simulate_click,
    teleport_to_jog_pose,
    wait_for_app_ready,
    wait_for_motion_start,
    wait_until,
)
from waldo_commander.components.joint_dial import DIAL_RADIUS, dial_angle

#: How long to wait for a held axis to open its jog stream [s].
#:
#: A polling wait, so a healthy run leaves as soon as the first datagram
#: goes out and pays nothing for the ceiling. It is generous because the
#: ceiling is only ever reached on the slowest CI runner, where the first
#: jog_l has to wait on the controller — five seconds was enough on Linux
#: and not on Windows, which reads as "the stream never opened".
_JOG_STREAM_BUDGET_S = 20.0


def _j1() -> float:
    return float(waldoctl.commander.status.joints.angles.deg[0])


def _z() -> float:
    return float(waldoctl.commander.status.pose.z)


def _arc_end(path: str) -> tuple[float, float]:
    """Where the one arc of ``M x0 y0 A r r 0 large sweep x1 y1`` ends."""
    n = [float(t) for t in re.findall(r"-?\d+(?:\.\d+)?", path)]
    assert len(n) == 9, f"not a single arc: {path!r}"
    return n[7], n[8]


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
async def test_joint_jog_steps_dials_and_editing(user: User) -> None:
    """A brief click on a joint jog button moves the joint by the step in
    either direction, also at 1° under TOPPRA. Only the moved joint's dial
    redraws, and the readout tracks the angle. Editing mode ignores a press."""
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    cp = ui_state.control_panel

    assert len(cp._dials) == ui_state.active_robot.joints.count
    assert await wait_until(
        lambda: all(math.isfinite(d.last_deg) for d in cp._dials)
    ), "the dials never took their first angles from the status loop"
    dial_j1, dial_j2 = cp._dials[0], cp._dials[1]

    def props(dial) -> tuple[str, list[float]]:
        return dial.props["fill"], list(dial.props["knob"])

    # +5°: J1 and its dial move, J2's dial stays as it was.
    waldoctl.commander.settings.jog.joint_step_deg = 5.0
    start = await settled(_j1)
    j1_before, j2_before = props(dial_j1), props(dial_j2)
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()
    assert await wait_until(lambda: props(dial_j1) != j1_before, timeout_s=10.0), (
        "J1 moved but its dial did not redraw"
    )
    await wait_moved(_j1, start, lambda d: 4.9 <= d <= 5.1, what="J1 moving +5.0°±0.1°")
    assert props(dial_j2) == j2_before, "J2 did not move, so its dial must not redraw"

    # The redrawn dial shows the live angle: its knob is where the geometry
    # puts the current J1 angle, within the redraw threshold.
    live = _j1()
    expected = dial_angle(dial_j1.lo, dial_j1.hi, live, DIAL_RADIUS)[1]
    knob = tuple(dial_j1.props["knob"])
    assert math.dist(knob, expected) < 0.3, f"knob {knob} vs live angle {live:.2f}°"
    assert math.dist(_arc_end(dial_j1.props["fill"]), expected) < 0.3, (
        "the fill ends under the knob"
    )

    # Regression: the J1 readout widget is bound to commander.status.joints
    # .angles, which the status loop mutates in place via set_deg(). If 'angles'
    # were a BindableProperty it would propagate only on reassignment and the
    # readout would freeze; left non-bindable, the binding is a polled active
    # link. Force one refresh step and assert the widget tracks the live angle.
    binding._refresh_step()
    await asyncio.sleep(0)
    readout = next(iter(user.find(marker="joint-readout-0").elements))
    assert readout.value is not None and abs(readout.value - _j1()) < 0.5, (
        f"J1 readout widget froze: shows {readout.value}, live angle is "
        f"{_j1():.2f}° (angles binding is not tracking in-place set_deg)"
    )

    # The readout shows the angle to a tenth, so clicking into it and out
    # again without an edit is not a move: J1 sits off a tenth and stays.
    client = waldoctl.commander.client
    q = list(await client.angles())
    q[0] = round(q[0], 1) + 0.04
    assert await client.wait_command(await client.move_j(q, duration=0.5), timeout=10)
    off_tenth = await settled(_j1, tolerance=0.005)
    user.find(marker="joint-readout-0").trigger("focus").trigger("blur")
    assert not await wait_until(lambda: abs(_j1() - off_tenth) > 0.02, timeout_s=2.0), (
        f"leaving the J1 readout unedited moved J1 from {off_tenth:.3f}° to "
        f"{_j1():.3f}°"
    )
    # An edit is a target: J1 goes where it was typed.
    readout_field = user.find(marker="joint-readout-0").trigger("focus")
    typed = round(off_tenth, 1) + 2.0
    for element in readout_field.elements:
        element.value = typed
    readout_field.trigger("blur")
    await wait_moved(
        _j1,
        off_tenth,
        lambda d: abs(off_tenth + d - typed) < 0.1,
        what=f"J1 moving to the typed {typed:.1f}°",
    )

    # -3° with the minus button (mousedown/mouseup — jog buttons don't
    # listen for a raw click).
    waldoctl.commander.settings.jog.joint_step_deg = 3.0
    start = await settled(_j1)
    await simulate_click(user, "btn-j1-minus")
    await wait_for_motion_start()
    await wait_moved(
        _j1, start, lambda d: -3.1 <= d <= -2.9, what="J1 moving -3.0°±0.1°"
    )

    # Editing mode: the target editor controls the robot and
    # ``set_joint_pressed`` early-returns on ``commander.status.editing_mode``.
    initial = _j1()
    waldoctl.commander.status.editing_mode = True
    try:
        await asyncio.sleep(0.1)
        await simulate_click(user, "btn-j1-plus")
        await asyncio.sleep(0.3)
        assert abs(_j1() - initial) < 0.1, (
            f"J1 should not move in editing mode. "
            f"Initial: {initial:.2f}°, Current: {_j1():.2f}°"
        )
    finally:
        waldoctl.commander.status.editing_mode = False

    # Step precision with a small step under the TOPPRA profile (the app's
    # own client, not session_client).
    await cp.client.select_profile("TOPPRA")
    waldoctl.commander.settings.jog.joint_step_deg = 1.0
    start = await settled(_j1)
    await simulate_click(user, "btn-j1-plus")
    await wait_for_motion_start()
    await wait_moved(
        _j1, start, lambda d: 0.9 <= d <= 1.1, what="J1 moving +1.0°±0.1° (TOPPRA)"
    )


@pytest.mark.integration
async def test_cartesian_jog_steps(user: User) -> None:
    """A brief click on a cartesian jog button moves the TCP by the step:
    Z+ and Z- in translation, RZ+ in rotation, and 1 mm under TOPPRA."""
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    user.find("Cartesian Jog").click()
    await asyncio.sleep(0)

    def rz() -> float:
        return float(waldoctl.commander.status.pose.rz)

    waldoctl.commander.settings.jog.joint_step_deg = 10.0
    start = await settled(_z)
    await simulate_click(user, "axis-zplus")
    await wait_for_motion_start()
    await wait_moved(
        _z, start, lambda d: 9.9 <= d <= 10.1, what="Z moving +10.0mm±0.1mm"
    )

    waldoctl.commander.settings.jog.joint_step_deg = 5.0
    start = await settled(_z)
    await simulate_click(user, "axis-zminus")
    await wait_for_motion_start()
    await wait_moved(
        _z, start, lambda d: -5.1 <= d <= -4.9, what="Z moving -5.0mm±0.1mm"
    )

    waldoctl.commander.settings.jog.joint_step_deg = 2.0
    start = await settled(rz)
    await simulate_click(user, "axis-rzplus")
    await wait_for_motion_start()
    await wait_moved(
        rz, start, lambda d: 1.9 <= abs(d) <= 2.1, what="RZ changing 2.0°±0.1°"
    )

    # Step precision with a small step under the TOPPRA profile.
    await ui_state.control_panel.client.select_profile("TOPPRA")
    waldoctl.commander.settings.jog.joint_step_deg = 1.0
    start = await settled(_z)
    await simulate_click(user, "axis-zplus")
    await wait_for_motion_start()
    await wait_moved(
        _z, start, lambda d: 0.9 <= d <= 1.1, what="Z moving +1.0mm±0.1mm (TOPPRA)"
    )


@pytest.mark.integration
async def test_rapid_clicks_drop_no_press(user: User) -> None:
    """Rapid clicking drops no click, joint or cartesian.

    What the panel owes is one command per click: a press arriving while the
    previous move is still running must still be seen. What it does NOT owe
    is a sum. Each joint click targets `measured + step`, read at the moment
    it is handled, so clicks that overlap motion deliberately do not
    accumulate — the old form of this test asserted a total and then widened
    the band to 40-110% to survive that, which is why it was skipped on CI
    rather than fixed. A cartesian step is an IK solve per press, so it is
    the case most likely to have a press arrive mid-solve.

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
    num_clicks = 5

    waldoctl.commander.settings.jog.joint_step_deg = 1.0
    joint_targets: list = []
    orig_move_j = cp.client.move_j

    async def move_j_spy(angles, *args, **kwargs):
        joint_targets.append(list(angles))
        return await orig_move_j(angles, *args, **kwargs)

    cp.client.move_j = move_j_spy
    try:
        await settled(_j1)
        # One click per issued command: the next press goes in as soon as the
        # previous one has been turned into a move, which is as fast as a
        # person can click and far faster than the motion completes.
        for i in range(num_clicks):
            await simulate_click(user, "btn-j1-plus", hold_ms=30)
            await _wait_issued(joint_targets, i + 1)
        j1_targets = [t[0] for t in joint_targets]
        assert len(joint_targets) == num_clicks, (
            f"the panel dropped a click: {len(joint_targets)} move_j "
            f"for {num_clicks} presses"
        )
        # Never backwards: a click read a stale pose at worst, never an older one.
        assert all(b >= a for a, b in zip(j1_targets, j1_targets[1:])), (
            f"a click targeted behind its predecessor: {j1_targets}"
        )
        # A joint step is ABSOLUTE — `measured + step`, read when the click is
        # handled — so clicks that overlap the motion resolve to the same
        # target and the arm ends on the last one rather than five steps
        # along. That is the behaviour; asserting a sum here is what made the
        # old test flaky.
        await poll_until(
            _j1,
            lambda v: abs(v - j1_targets[-1]) < 0.5 and idle(),
            timeout_s=20.0,
            interval=0.05,
            what=f"J1 settling on the last commanded {j1_targets[-1]:.3f}°",
        )
        # The rapid clicks can all land before a status shows the arm moving,
        # so they may share a target. A click once a status has shown the arm
        # partway along a step must step from there, not from where it began.
        waldoctl.commander.settings.jog.joint_step_deg = 10.0
        began = await settled(_j1)
        await simulate_click(user, "btn-j1-minus", hold_ms=30)
        await _wait_issued(joint_targets, num_clicks + 1)
        assert await wait_until(lambda: began - _j1() >= 1.0, timeout_s=10.0), (
            f"J1 never started its 10° step down from {began:.3f}°"
        )
        await simulate_click(user, "btn-j1-minus", hold_ms=30)
        await _wait_issued(joint_targets, num_clicks + 2)
    finally:
        cp.client.move_j = orig_move_j
    first, second = joint_targets[-2][0], joint_targets[-1][0]
    assert first == pytest.approx(began - 10.0, abs=0.1), (
        f"a click with J1 at rest on {began:.3f}° did not step from it: {first:.3f}°"
    )
    assert second <= first - 0.9, (
        f"a click with J1 a degree or more along its step targeted {second:.3f}°, "
        f"no further than the step's own {first:.3f}°: it read a stale pose"
    )
    await poll_until(
        _j1,
        lambda v: abs(v - joint_targets[-1][0]) < 0.5 and idle(),
        timeout_s=20.0,
        interval=0.05,
        what=f"J1 settling on {joint_targets[-1][0]:.3f}°",
    )

    user.find("Cartesian Jog").click()
    await asyncio.sleep(0)
    await teleport_to_jog_pose(cp.client)

    waldoctl.commander.settings.jog.joint_step_deg = 2.0
    cart_targets: list = []
    orig_move_l = cp.client.move_l

    async def move_l_spy(pose, *args, **kwargs):
        cart_targets.append(list(pose))
        return await orig_move_l(pose, *args, **kwargs)

    cp.client.move_l = move_l_spy
    try:
        initial_z = await settled(_z)
        for i in range(num_clicks):
            await simulate_click(user, "axis-zplus", hold_ms=30)
            await _wait_issued(cart_targets, i + 1)
    finally:
        cp.client.move_l = orig_move_l

    assert len(cart_targets) == num_clicks, (
        f"the panel dropped a click: {len(cart_targets)} move_l for {num_clicks} presses"
    )
    step = float(waldoctl.commander.settings.jog.joint_step_deg)
    assert all(abs(t[2] - step) < 1e-9 for t in cart_targets), (
        f"every cartesian click is the same relative +Z step: {[t[2] for t in cart_targets]}"
    )
    # A cartesian step is RELATIVE, so unlike the joint one it does not
    # resolve to a single target — but a press landing mid-move supersedes
    # the move in flight rather than queueing behind it, so the total is not
    # a sum either. What holds either way: the arm goes up, and it cannot go
    # further than every click asked for. A band between those two is the
    # runtime's supersede timing, and pinning it is what made this flaky.
    final_z = await settled(_z, timeout_s=20.0)
    ceiling = initial_z + num_clicks * step
    assert final_z > initial_z + step / 2, (
        f"clicking +Z left the arm at {final_z:.3f} mm, from {initial_z:.3f} mm"
    )
    assert final_z <= ceiling + 1.0, (
        f"the arm reached Z {final_z:.3f} mm, past the {ceiling:.3f} mm every "
        f"click together asked for"
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
        await settled(_z)
        await click_axis("axis-zplus")
        assert moves[-1] == "TRF", f"expected TRF move_l, got {moves}"
        await wait_for_motion_start()
        await settled(_z)

        # Tool frame: streamed hold sends jog_l("TRF", ...)
        await hold_axis("axis-zminus")
        assert jogs[-1] == "TRF", f"expected TRF jog_l, got {jogs}"
        await settled(_z)

        # Back to world frame: the memoized lookup must not keep serving TRF
        await select_frame("WRF")
        await hold_axis("axis-zplus")
        assert jogs[-1] == "WRF", f"expected WRF jog_l after toggle back, got {jogs}"
        await settled(_z)

        # Rotation clicks stay in TRF even with WRF translation selected
        await click_axis("axis-rzplus")
        assert moves[-1] == "TRF", f"rotation click must stay TRF, got {moves}"
    finally:
        frame_select.set_value("WRF")
        await asyncio.sleep(0)


@pytest.mark.integration
async def test_jog_arrow_inversion(user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Invert X Jog switch must flip what the physical X arrow slot
    commands AND its label/marker in lockstep: the left arrow sends X+ by
    default (camera-matched inversion) and X- when inverted, so the glyph,
    the marker, and the actual motion never disagree. Flipping it while an
    arrow is held must still stop the stream on release: the axis is
    captured at press, so the release targets what is actually streaming
    instead of the re-resolved (flipped) axis.
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

    def x() -> float:
        return float(waldoctl.commander.status.pose.x)

    # The physical left-arrow slot commands X+ by default
    slot = next(iter(user.find(marker="axis-xplus").elements))
    assert slot is cp._cart_slot_elems["lr_neg"], (
        "axis-xplus should be the left-arrow slot by default"
    )

    # The 5mm move_l has a sub-tolerance creep phase before its main ramp, so
    # value-stability alone would end the wait early.
    start = await settled(x)
    await simulate_click(user, "axis-xplus")
    await wait_for_motion_start()
    await wait_moved(
        x, start, lambda d: 4.9 <= d <= 5.1, what="the left arrow moving X +5.0mm"
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

        start = await settled(x)
        await simulate_click(user, "axis-xminus")
        await wait_for_motion_start()
        await wait_moved(
            x,
            start,
            lambda d: -5.1 <= d <= -4.9,
            what="the inverted left arrow moving X -5.0mm",
        )
    finally:
        invert_switch.set_value(False)
        await asyncio.sleep(0)
    assert await wait_until(lambda: not cp.invert_x, timeout_s=2.0), (
        "invert_x should hydrate back from the switch"
    )

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
        await wait_until(lambda: not any(cp._cart_pressed_axes.values()), 2.0)
        cp.set_jog_inversion(invert_x=False)

    await wait_until(lambda: not ui_state.cart_jog_timer.active, 2.0)
    streamed = len(jogs)
    await asyncio.sleep(0.3)  # six ticks of the 20 Hz jog stream
    assert len(jogs) == streamed, "release must stop the captured axis's stream"
    assert not any(cp._cart_pressed_axes.values()), (
        f"no axis may stay pressed after release: {cp._cart_pressed_axes}"
    )


@pytest.mark.integration
async def test_held_jogs_self_release_and_a_joint_press_cancels_a_cartesian_one(
    user: User,
) -> None:
    """A held jog whose button disables at the joint limit, or whose
    cartesian pad strong-disables mid-hold, must self-release: NiceGUI drops
    events from disabled elements and the strong-disable class sets
    pointer-events:none, so the mouseup never reaches the server, and touch
    input has no mouseleave fallback. Without a server-side release the jog
    stream renews against the limit indefinitely.

    A joint press made while a cartesian press is still waiting for its hold
    timer cancels it: the joint steps, and the cartesian press never turns
    into a stream.
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
    assert await wait_until(lambda: abs(_j1() - pose[0]) < 0.5, 5.0)

    # Hold without ever releasing: once the button disables, a browser mouseup
    # would be dropped anyway, so the test never sends one.
    user.find(marker="btn-j1-plus").trigger("mousedown")
    assert await wait_until(lambda: ui_state.joint_jog_timer.active, 3.0), (
        "hold never started streaming"
    )
    assert await wait_until(lambda: not ui_state.joint_jog_timer.active, 10.0), (
        "jog stream must stop when the button disables at the limit"
    )
    assert _j1() <= hi + 0.5

    user.find(marker="tab-cartesian").click()
    await asyncio.sleep(0)
    await teleport_to_jog_pose(cp.client)

    user.find(marker="axis-xplus").trigger("mousedown")
    assert await wait_until(lambda: ui_state.cart_jog_timer.active, 3.0), (
        "hold never started streaming"
    )
    # Availability flips the way the status consumer applies it: a wholesale
    # list swap on the held direction's frame.
    frame = cp._translation_frame_name()
    frame_av = waldoctl.commander.status.pose.cart_jog.by_frame[frame]
    available = list(frame_av.can_jog_pos)
    frame_av.can_jog_pos = [False, True, True, True, True, True]
    assert await wait_until(lambda: not ui_state.cart_jog_timer.active, 5.0), (
        "cart jog stream must stop when the pad disables mid-hold"
    )
    frame_av.can_jog_pos = available

    await teleport_to_jog_pose(cp.client)
    start = np.array(await cp.client.angles())
    user.find(marker="axis-xplus").trigger("mousedown")
    await simulate_click(user, "btn-j1-plus")
    # Well past the cartesian press's hold threshold: had it survived, it
    # would be streaming X by now.
    await asyncio.sleep(cp.CLICK_HOLD_THRESHOLD_S * 4)
    user.find(marker="axis-xplus").trigger("mouseup")
    assert await cp.client.wait_motion(timeout=10, settle_window=0.3)
    end = np.array(await cp.client.angles())
    assert end[0] > start[0] + 0.1
    assert end[1:] == pytest.approx(start[1:], abs=0.1)
