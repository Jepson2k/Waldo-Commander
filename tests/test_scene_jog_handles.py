"""The 3D view's jog handles: a ring per joint and the TCP gizmo.

The browser decides what the pointer grabs and sends the app gesture events;
these tests send those events the way the browser does. The robot is the
fake-serial controller.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any

import numpy as np
import pytest
import waldoctl
from nicegui.testing import User
from pinokin import so3_from_rpy

from tests.helpers.scene_events import (
    Drag,
    ball,
    gizmo,
    held_until,
    right_click,
    ring,
)
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    reload_page,
    simulate_click,
    teleport_to_jog_pose,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state


async def _open(user: User) -> Any:
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    assert await wait_until(lambda: ui_state.urdf_scene is not None, timeout_s=10)
    urdf = ui_state.urdf_scene
    assert urdf is not None and urdf.scene is not None
    # The status loop reports jogging as possible within a tick of sim mode.
    assert await wait_until(lambda: urdf._handles_available, timeout_s=5.0)
    return urdf


async def _start_recording(user: User) -> Any:
    """An open program with the cursor in its ``with`` block, recording."""
    from tests.test_editor_integration import _set_cursor_line

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    textarea.value = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    )
    _set_cursor_line(textarea, 3)
    await asyncio.sleep(0)
    user.find(marker="editor-record-btn").click()
    assert await wait_until(is_any_program_recording, 2.0)
    return textarea


async def _released(drag: Drag, **fields: Any) -> None:
    """Let go of a ring and wait until the app has sent its last target."""
    drag.release(**fields)
    assert await wait_until(lambda: not ui_state.joint_jog_timer.active, 5.0)


def _rz(deg: float) -> np.ndarray:
    a = math.radians(deg)
    return np.array(
        [[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0, 0, 1]]
    )


def _tcp_rotation() -> np.ndarray:
    pose = waldoctl.commander.status.pose
    R = np.zeros((3, 3))
    so3_from_rpy(math.radians(pose.rx), math.radians(pose.ry), math.radians(pose.rz), R)
    return R


def _tcp_mm() -> np.ndarray:
    pose = waldoctl.commander.status.pose
    return np.array([pose.x, pose.y, pose.z], dtype=float)


def _shown_frame(urdf: Any) -> tuple[np.ndarray, np.ndarray]:
    """Where the browser was last told to draw the gizmo: position (mm) and axes."""
    p, R = urdf.scene.tcp_at(urdf.scene._tcp_rev)
    return np.array(p) * 1000.0, np.array(R)


async def _frame_on_tcp(urdf: Any) -> tuple[np.ndarray, np.ndarray]:
    assert await wait_until(
        lambda: np.linalg.norm(_shown_frame(urdf)[0] - _tcp_mm()) < 0.5, 10.0
    ), "the gizmo's frame sits on the TCP"
    return _shown_frame(urdf)


@pytest.mark.integration
async def test_ring_drags_while_recording_write_one_move_j_each(user: User) -> None:
    """A ring drag while recording writes one move_j at its target; one let
    go of other than by its release writes nothing and moves nothing. A ring
    grabbed again owns its own settle wait, so the previous release cannot
    close it while it is held."""
    urdf = await _open(user)
    scene = urdf.scene
    await teleport_to_jog_pose(ui_state.control_panel.client)
    textarea = await _start_recording(user)
    start = np.array(waldoctl.commander.status.joints.angles.deg, dtype=float)
    before = str(textarea.value).count("move_j(")
    try:
        await _released(ring(user, scene, 0).move(delta=5.0), delta=5.0)
        assert await wait_until(
            lambda: str(textarea.value).count("move_j(") == before + 1, 30.0
        ), textarea.value
        recorded = re.findall(r"move_j\(\[([^\]]*)\]", str(textarea.value))
        recorded_j1 = float(recorded[-1].split(",")[0])
        assert abs(recorded_j1 - (start[0] + 5.0)) <= 0.1, (recorded_j1, start)
        assert not ui_state.joint_jog_timer.active

        # A drag that ends aborted (the browser lost the pointer) sends
        # nothing more and records nothing: the drag was one owned move.
        dropped = ring(user, scene, 0)
        ui_state.joint_jog_timer.active = False
        dropped.move(delta=5.0).abort()
        dropped.release(delta=5.0)
        await asyncio.sleep(1.0)
        assert str(textarea.value).count("move_j(") == before + 1, textarea.value
        assert waldoctl.commander.status.joints.angles.deg[0] == pytest.approx(
            start[0] + 5.0, abs=0.1
        )

        # A new grab owns its own settle wait. The previous release must
        # not close this second jog while the pointer is still held.
        first = ring(user, scene, 0).move(delta=5.0)
        await held_until(first, lambda: False, 0.15)
        await _released(first, delta=5.0)
        second = ring(user, scene, 1).move(delta=5.0)
        assert await held_until(
            second, lambda: str(textarea.value).count("move_j(") == before + 2, 10.0
        ), textarea.value
        await held_until(second, lambda: False, 0.3)
        assert str(textarea.value).count("move_j(") == before + 2, textarea.value
        second.release(delta=5.0)
        assert await wait_until(
            lambda: str(textarea.value).count("move_j(") == before + 3, 20
        )
        recorded = re.findall(r"move_j\(\[([^\]]*)\]", str(textarea.value))
        last = [float(v) for v in recorded[-1].split(",")]
        assert last[0] == pytest.approx(start[0] + 10.0, abs=0.1)
        assert last[1] == pytest.approx(start[1] + 5.0, abs=0.1)
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()


@pytest.mark.integration
async def test_ring_drag_handoffs_and_interruptions(user: User) -> None:
    """Grabbing the next ring before the first joint arrives keeps the first
    joint's commanded target. A ring drag interrupted from elsewhere — a
    Stop, a simulator request, target editing from the scene's menu, a lease
    takeover — never sends its last target on release, and a takeover after
    a release does not refresh the servo the release left moving. A target
    that is not a number moves nothing. A page reload mid-drag still leaves
    the next joint press working."""
    from fastmcp import Client as McpClient

    from waldo_commander.mcp.server import get_mcp
    from waldo_commander.services.control_lease import BROWSER, control_lease

    urdf = await _open(user)
    scene = urdf.scene
    panel = ui_state.control_panel
    angles = waldoctl.commander.status.joints.angles

    def take_over() -> None:
        control_lease.seize("mcp", "other", "Other driver")
        control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")

    # A target that is not a number is no target: the release sends the
    # joint where it was.
    await teleport_to_jog_pose(panel.client)
    start = np.array(await panel.client.angles())
    await _released(ring(user, scene, 1).move(delta=float("nan")), delta=float("inf"))
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1)

    # Grab the next ring before the first joint has reached its target: the
    # first joint's commanded target is kept, and both arrive.
    start = np.array(await panel.client.angles())
    await _released(ring(user, scene, 0).move(delta=10.0), delta=10.0)
    await _released(ring(user, scene, 1).move(delta=5.0), delta=5.0)
    assert await wait_until(
        lambda: abs(angles.deg[0] - start[0] - 10) < 0.1
        and abs(angles.deg[1] - start[1] - 5) < 0.1,
        20,
    ), list(angles.deg)

    speed = waldoctl.commander.settings.jog.speed
    waldoctl.commander.settings.jog.speed = 10
    try:
        # A grab while another ring is still held ends that one first: the
        # target it sent is dropped, not carried into the new drag.
        start = np.array(await panel.client.angles())
        held = ring(user, scene, 0)
        ui_state.joint_jog_timer.active = False
        held.move(delta=10.0)
        await panel.jog_tick()
        await _released(ring(user, scene, 1).move(delta=5.0), delta=5.0)
        assert await wait_until(lambda: abs(angles.deg[1] - start[1] - 5) < 0.1, 20)
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        assert angles.deg[0] < start[0] + 3, "the held ring's target was carried"

        # Release commits an angle, but it must not refresh that servo after
        # a takeover, even if this browser immediately regains the lease.
        start = np.array(await panel.client.angles())
        ring(user, scene, 0).move(delta=25.0).release(delta=25.0)
        assert await wait_until(lambda: angles.deg[0] > start[0] + 1, 5)
        take_over()
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        stopped = np.array(await panel.client.angles())
        assert stopped[0] < start[0] + 24
        await panel.jog_tick()
        await asyncio.sleep(0.4)
        assert np.array(await panel.client.angles()) == pytest.approx(stopped, abs=0.1)
    finally:
        waldoctl.commander.settings.jog.speed = speed

    async def stop() -> None:
        async with McpClient(get_mcp()) as mcp:
            await mcp.call_tool("motion.stop")

    async def lease() -> None:
        take_over()

    async def simulator() -> None:
        # Exercise a simulator request from stale UI status without ever
        # opening a hardware serial transport in this regression suite.
        waldoctl.commander.status.simulator_active = False
        with user.client:
            await panel.on_toggle_sim()
        await ensure_robot_ready_for_motion()

    async def editing() -> None:
        # Right-click the floor and place a target at the robot, then leave.
        right_click(user, scene, [], [0.3, 0.1, 0.0])
        user.find(marker="scene-target-at-robot").click()
        assert await wait_until(lambda: waldoctl.commander.status.editing_mode, 5)
        user.find(marker="edit-bar-cancel").click()
        assert not waldoctl.commander.status.editing_mode

    for interrupt in (lease, editing, simulator, stop):
        await teleport_to_jog_pose(panel.client)
        start = np.array(await panel.client.angles())
        assert await wait_until(lambda: urdf._handles_available, 5.0)
        drag = ring(user, scene, 1)
        # Hold the periodic sender so the interrupt reaches a target still
        # pending in the UI.
        ui_state.joint_jog_timer.active = False
        drag.move(delta=10.0)
        await interrupt()
        await panel.jog_tick()
        drag.release(delta=10.0)
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1), (
            interrupt.__name__
        )

    await teleport_to_jog_pose(panel.client)
    assert await wait_until(lambda: urdf._handles_available, 5.0)
    drag = ring(user, scene, 1)
    ui_state.joint_jog_timer.active = False
    drag.move(delta=10.0)
    await reload_page(user)
    await ensure_robot_ready_for_motion()
    panel = ui_state.control_panel
    start = np.array(await panel.client.angles())
    await simulate_click(user, "btn-j1-plus")
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    end = np.array(await panel.client.angles())
    assert end[0] > start[0] + 0.1
    assert end[1] == pytest.approx(start[1], abs=0.1)


@pytest.mark.integration
async def test_gizmo_drags_move_the_tool_in_its_own_frame_and_nothing_else(
    user: User,
) -> None:
    """A gizmo drag moves the TCP in the frame the app placed the gizmo in:
    along the tool's X, and turned about the tool's own Z. A grab and a let
    go with no move replays no old pose; samples from a drag the app never
    started, or after a drag ended, move nothing. Editing a target, the
    release solves the pose it was let go at, and a burst of samples costs a
    bounded number of solves."""
    urdf = await _open(user)
    scene = urdf.scene
    panel = ui_state.control_panel
    await teleport_to_jog_pose(panel.client)
    origin, R_frame = await _frame_on_tcp(urdf)

    drag = gizmo(user, scene, "translate", "X").move(**ball(x=0.020))
    target = origin + R_frame[:, 0] * 20.0
    assert await held_until(
        drag, lambda: np.linalg.norm(_tcp_mm() - target) < 0.5, 20.0
    ), f"TCP {_tcp_mm()}, expected {target}"
    drag.release(**ball(x=0.020))
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)

    # Rotate about the tool's Z: the result is the frame's orientation turned
    # about its own Z, not the drag's angles taken as a world orientation.
    user.find(marker="gizmo-mode-rotate").click()
    _, R_frame = await _frame_on_tcp(urdf)
    drag = gizmo(user, scene, "rotate", "Z").move(**ball(rz=math.radians(10.0)))
    expected = R_frame @ _rz(10.0)

    def error_deg() -> float:
        c = (np.trace(expected.T @ _tcp_rotation()) - 1.0) / 2.0
        return math.degrees(math.acos(min(1.0, max(-1.0, c))))

    assert await held_until(drag, lambda: error_deg() < 0.3, 20.0), (
        f"{error_deg():.2f}° off"
    )
    drag.release(**ball(rz=math.radians(10.0)))
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    user.find(marker="gizmo-mode-move").click()

    # Nothing here may move the arm: a grab let go where it was taken, a
    # drag the app never heard begin, and samples after a drag ended.
    await teleport_to_jog_pose(panel.client)
    await _frame_on_tcp(urdf)
    start = np.array(await panel.client.angles())
    drag = gizmo(user, scene)
    await panel.cart_jog_tick()
    drag.release(**ball())
    drag.move(**ball(x=0.02)).release(**ball(x=0.02))
    Drag(user, scene, "tcp").move(**ball(x=0.02)).release(**ball(x=0.02))
    await panel.cart_jog_tick()
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1)

    # Editing a target: samples are solved at most 30 times a second, and
    # the release solves the pose it was let go at.
    origin, R_frame = await _frame_on_tcp(urdf)
    right_click(user, scene, [], [0.3, 0.1, 0.0])
    user.find(marker="scene-target-at-robot").click()
    assert await wait_until(lambda: waldoctl.commander.status.editing_mode, 5)
    try:
        solver = urdf._ik_solver
        solves = 0
        solve = solver.solve

        def counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal solves
            solves += 1
            return solve(*args, **kwargs)

        solver.solve = counted
        try:
            drag = gizmo(user, scene)
            began = time.monotonic()
            for i in range(1, 21):
                drag.move(**ball(x=0.001 * i))
                await asyncio.sleep(0.005)
            drag.release(**ball(x=0.030))
            took = time.monotonic() - began
        finally:
            solver.solve = solve
        # Twenty samples, however long the platform's timer made them take.
        assert 1 <= solves <= 30 * took + 2 and solves < 20, (solves, took)
        landed = origin + R_frame[:, 0] * 30.0
        assert np.linalg.norm(_shown_frame(urdf)[0] - landed) < 0.5, (
            _shown_frame(urdf)[0],
            landed,
        )
    finally:
        user.find(marker="edit-bar-cancel").click()


@pytest.mark.integration
async def test_a_released_gizmo_drag_reaches_where_it_was_let_go(user: User) -> None:
    """A gizmo let go before the jog timer sent its last pose still drives the
    tool there, though the move outlasts a servo target's life, and the
    recording names that pose."""
    urdf = await _open(user)
    await teleport_to_jog_pose(ui_state.control_panel.client)
    textarea = await _start_recording(user)
    try:
        origin, R_frame = await _frame_on_tcp(urdf)
        target = origin + R_frame[:, 0] * 40.0

        # The last move and the release arrive together, between two ticks.
        gizmo(user, urdf.scene).move(**ball(x=0.040)).release(**ball(x=0.040))

        assert await wait_until(
            lambda: np.linalg.norm(_tcp_mm() - target) < 0.5, 20.0
        ), f"TCP {_tcp_mm()}, expected {target}"
        assert await wait_until(lambda: "move_l(" in str(textarea.value), 30.0), (
            textarea.value
        )
        recorded = re.findall(r"move_l\(\[([^\]]*)\]", str(textarea.value))
        xyz = np.array([float(v) for v in recorded[-1].split(",")[:3]])
        assert np.linalg.norm(xyz - target) < 0.5, (xyz, target)
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()
