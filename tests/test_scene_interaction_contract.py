"""What a gesture from the 3D view may do.

The browser proposes and the app decides. These tests send what a browser
sends, but late, from a page that has lost control, from a drag that went
quiet, or against a view that has fallen behind, and check that the robot and
the world only ever do what the app allowed. The robot is the fake-serial
controller.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import httpx
import numpy as np
import pytest
import waldoctl
from nicegui import core
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction
from waldoctl import Box

from tests.helpers.scene_events import Drag, ball, held_until, right_click, ring
from tests.helpers.wait import (
    JOG_SAFE_POSE_DEG,
    ensure_robot_ready_for_motion,
    poll_until,
    reload_page,
    teleport_to_jog_pose,
    wait_for_tool_key,
    wait_until,
)
from tests.test_scene_jog_handles import (
    _frame_on_tcp,
    _open,
    _shown_frame,
    _start_recording,
)
from waldo_commander.services.control_lease import BROWSER, control_lease
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state


def _ops(calls: Any) -> list[list[Any]]:
    """The ops of every message a patched ``run_javascript`` was asked to send."""
    ops: list[list[Any]] = []
    for call in calls.call_args_list:
        code = call.args[0]
        head = code.index(",'apply',[") + len(",'apply',[")
        ops += json.loads(code[head:-2])
    return ops


async def _mcp(*calls: tuple[str, dict[str, Any]]) -> None:
    """Make *calls* as one AI session."""
    from fastmcp import Client as McpClient

    from waldo_commander.mcp.server import get_mcp

    async with McpClient(get_mcp()) as mcp:
        for tool, args in calls:
            await mcp.call_tool(tool, args)


@pytest.mark.integration
async def test_gestures_begun_before_a_stop_a_switch_or_a_takeover_move_nothing(
    user: User,
) -> None:
    """A ring or gizmo drag the browser began before a mode switch, a
    takeover by another driver or a Stop, arriving after it, is refused: the
    arm stays put and the lease stays where the takeover put it. A tab that
    lost the session to another tab — its open drag, a new one, its view
    remounting — leaves the new tab's drag alone."""
    urdf = await _open(user)
    scene = urdf.scene
    panel = ui_state.control_panel

    async def switch() -> None:
        # A simulator request from stale UI status, without ever opening a
        # hardware serial transport in this suite.
        waldoctl.commander.status.simulator_active = False
        with user.client:
            await panel.on_toggle_sim()
        await ensure_robot_ready_for_motion()

    async def takeover() -> None:
        control_lease.seize("mcp", "other", "Other driver")

    async def stop() -> None:
        await _mcp(("motion.stop", {}))

    for interrupt in (switch, takeover, stop):
        await teleport_to_jog_pose(panel.client)
        await _frame_on_tcp(urdf)
        start = np.array(await panel.client.angles())
        # Begun before the interrupt; on the wire until after it.
        late_ring = Drag(user, scene, "ring")
        late_gizmo = Drag(user, scene, "tcp")
        rev = scene._tcp_rev
        await interrupt()
        late_ring.begin(joint=0).move(delta=10.0).release(delta=10.0)
        late_gizmo.begin(mode="translate", axis="X", rev=rev)
        late_gizmo.move(**ball(x=0.02)).release(**ball(x=0.02))
        assert not scene.gestures.open, interrupt.__name__
        if interrupt is takeover:
            assert control_lease.held_by("mcp", "other"), (
                "a late grab took control back"
            )
        control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")
        await panel.jog_tick()
        await panel.cart_jog_tick()
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1), (
            interrupt.__name__
        )

    # Tab A has a ring in hand when tab B takes the session over.
    await teleport_to_jog_pose(panel.client)
    start = np.array(await panel.client.angles())
    a_drag = ring(user, scene, 0)
    # Its target is still pending in the UI when the session changes hands.
    ui_state.joint_jog_timer.active = False
    a_drag.move(delta=5.0)
    # A second tab on the same app.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(core.app), base_url="http://test"
    ) as http:
        tab_b = User(http)
        await tab_b.open("/")
        # Its "Take over": the session is freed and the tab loads again.
        ui_state.active_client_id = None
        await reload_page(tab_b)
        assert await wait_until(
            lambda: ui_state.urdf_scene not in (None, urdf), 20.0
        ), "tab B never drew its own scene"
        await ensure_robot_ready_for_motion()
        b_urdf = ui_state.urdf_scene
        assert b_urdf is not None
        assert await wait_until(lambda: b_urdf._handles_available, 5.0)
        b_drag = ring(tab_b, b_urdf.scene, 1).move(delta=5.0)
        assert b_urdf.scene.gestures.open, "tab B's drag was refused"

        # Everything tab A sends now lands nowhere, and its drag going quiet
        # ends only its own.
        a_drag.move(delta=20.0).release(delta=20.0)
        ring(user, scene, 0).move(delta=20.0).release(delta=20.0)
        UserInteraction(user, {scene}, None).trigger("init")
        assert await held_until(
            b_drag,
            lambda: abs(waldoctl.commander.status.joints.angles.deg[1] - start[1] - 5)
            < 0.1,
            20.0,
        ), "tab B's drag was cut off"
        await held_until(b_drag, lambda: False, 0.8)
        b_drag.release(delta=5.0)
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        end = np.array(await panel.client.angles())
        assert end[0] == pytest.approx(start[0], abs=0.1), "tab A moved J1"
        assert end[1] == pytest.approx(start[1] + 5.0, abs=0.1)


@pytest.mark.integration
async def test_a_drag_gone_quiet_ends_and_a_frame_the_arm_has_left_is_refused(
    user: User,
) -> None:
    """A ring drag whose browser stops reporting ends within half a second:
    the arm stops short of its target, nothing is recorded, and a late
    release revives nothing. A gizmo grabbed on a frame drawn where the arm
    no longer is would aim the drag the wrong way: it is refused, and the
    frame is placed again."""
    urdf = await _open(user)
    scene = urdf.scene
    panel = ui_state.control_panel
    angles = waldoctl.commander.status.joints.angles
    await teleport_to_jog_pose(panel.client)
    textarea = await _start_recording(user)
    before = str(textarea.value).count("move_j(")
    speed = waldoctl.commander.settings.jog.speed
    waldoctl.commander.settings.jog.speed = 10
    try:
        start = np.array(await panel.client.angles())
        quiet = ring(user, scene, 0).move(delta=30.0)
        went_quiet = time.monotonic()
        assert await wait_until(lambda: not scene.gestures.open, 2.0)
        assert time.monotonic() - went_quiet < 0.8
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        stopped = np.array(await panel.client.angles())
        assert stopped[0] < start[0] + 20.0, "the arm went on to the target"
        quiet.move(delta=30.0).release(delta=30.0)
        await panel.jog_tick()
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        assert np.array(await panel.client.angles()) == pytest.approx(stopped, abs=0.1)
        assert str(textarea.value).count("move_j(") == before, textarea.value
    finally:
        waldoctl.commander.settings.jog.speed = speed
        if is_any_program_recording():
            motion_recorder.toggle_recording()

    await teleport_to_jog_pose(panel.client)
    shown, _ = await _frame_on_tcp(urdf)
    rev = scene._tcp_rev
    moved = list(JOG_SAFE_POSE_DEG)
    moved[0] += 10.0
    await panel.client.teleport(moved)
    assert await wait_until(
        lambda: np.linalg.norm(_shown_frame(urdf)[0] - shown) > 30.0, 10.0
    ), "the frame never followed the arm"
    start = np.array(await panel.client.angles())
    placed = scene._tcp_rev
    behind = Drag(user, scene, "tcp").begin(mode="translate", axis="X", rev=rev)
    assert not scene.gestures.open, "a drag on a stale frame was admitted"
    assert scene._tcp_rev > placed, "the frame was not placed again"
    behind.move(**ball(x=0.02)).release(**ball(x=0.02))
    await panel.cart_jog_tick()
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1)
    assert angles.deg[0] == pytest.approx(moved[0], abs=0.1)


@pytest.mark.integration
async def test_edits_from_an_ended_session_change_nothing(user: User) -> None:
    """A keep-out let go after the AI or a program replaced it stays as they
    left it, and one let go of other than by its release never moves.
    A joint turned for a target edit that has since ended leaves the next
    edit alone, and when a joint turn ends the browser is sent every joint
    as the edit has it, over anything older still on its way."""
    urdf = await _open(user)
    scene = urdf.scene
    handle = waldoctl.commander.scene
    assert handle is not None
    bench = Box(name="bench", x=0.1, y=0.1, z=0.1, pose=(0.4, 0.1, 0.05, 0, 0, 0))
    try:
        handle.shapes = [bench]
        assert await wait_until(lambda: "shape:bench" in urdf._shape_objects, 5.0)
        right_click(user, scene, ["shape:bench"])
        user.find(marker="shape-menu-move").click()
        arrows = scene.interaction["shapeMove"]
        moving = Drag(user, scene, "shape").begin(**arrows)
        replaced = Box(
            name="bench", x=0.1, y=0.1, z=0.1, pose=(0.3, -0.2, 0.05, 0, 0, 0)
        )
        await _mcp(
            ("control.take_control", {}),
            (
                "world.update_shape",
                {"name": "bench", "shape": list(replaced.to_wire())},
            ),
        )
        assert await wait_until(
            lambda: handle.shapes and tuple(handle.shapes[0].pose[:2]) == (0.3, -0.2),
            5.0,
        )
        moving.release(**arrows, x=0.25, y=0.25, z=0.05)
        assert tuple(handle.shapes[0].pose[:3]) == pytest.approx((0.3, -0.2, 0.05))
        control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")

        def move_bench() -> dict[str, Any]:
            if scene.interaction["shapeMove"] is None:
                right_click(user, scene, ["shape:bench"])
                user.find(marker="shape-menu-move").click()
            return scene.interaction["shapeMove"]

        # A program sets the world while the browser, still in control,
        # drags the keep-out.
        arrows = move_bench()
        moving = Drag(user, scene, "shape").begin(**arrows)
        handle.shapes = [
            Box(name="bench", x=0.1, y=0.1, z=0.1, pose=(0.2, 0.2, 0.05, 0, 0, 0))
        ]
        moving.release(**arrows, x=0.25, y=0.25, z=0.05)
        assert tuple(handle.shapes[0].pose[:3]) == pytest.approx((0.2, 0.2, 0.05))

        arrows = move_bench()
        dropped = Drag(user, scene, "shape").begin(**arrows).abort()
        dropped.release(**arrows, x=0.25, y=0.25, z=0.05)
        assert tuple(handle.shapes[0].pose[:3]) == pytest.approx((0.2, 0.2, 0.05))
    finally:
        handle.shapes = []

    def edit_target() -> int:
        right_click(user, scene, [], [0.3, 0.1, 0.0])
        user.find(marker="scene-target-at-robot").click()
        return scene.interaction["edit"]["session"]

    first = edit_target()
    assert await wait_until(lambda: waldoctl.commander.status.editing_mode, 5)
    q0 = urdf._editing_angles[0]
    old = Drag(user, scene, "joint").begin(session=first, joint=0, q=q0)
    old.move(session=first, joint=0, q=q0 + 0.2)
    user.find(marker="edit-bar-cancel").click()
    session = edit_target()
    assert session != first
    fresh = urdf._editing_angles[0]
    old.move(session=first, joint=0, q=q0 + 0.5)
    old.release(session=first, joint=0, q=q0 + 0.5)
    assert urdf._editing_angles[0] == pytest.approx(fresh)

    UserInteraction(user, {scene}, None).trigger("init")
    await asyncio.sleep(0)
    turn = Drag(user, scene, "joint").begin(session=session, joint=0, q=fresh)
    with patch.object(scene.client, "run_javascript") as sent:
        turn.move(session=session, joint=0, q=fresh + 0.2)
        turn.release(session=session, joint=0, q=fresh + 0.3)
        await asyncio.sleep(0)
    vectors = [op[1] for op in _ops(sent) if op[0] == "q"]
    assert vectors, "the edit's joints were never sent"
    assert len(vectors[-1]) == len(urdf.joint_names), "only part of the arm was sent"
    assert vectors[-1][0] == pytest.approx(fresh + 0.3, abs=1e-6)
    assert urdf._editing_angles[0] == pytest.approx(fresh + 0.3)
    user.find(marker="edit-bar-cancel").click()


@pytest.mark.integration
async def test_a_remounted_view_gets_the_whole_scene_and_shapes_from_any_thread(
    user: User,
) -> None:
    """A view that mounts again is sent the gizmo's frame where the tool's
    calibrated TCP is, though nothing moved since. Keep-outs set from a worker
    thread are drawn by the UI loop, each created where it belongs."""
    urdf = await _open(user)
    scene = urdf.scene
    handle = waldoctl.commander.scene
    assert handle is not None
    await teleport_to_jog_pose(ui_state.control_panel.client)

    # A tool on the flange and its TCP calibrated 60 mm further out.
    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    offset_z = next(iter(user.find(marker="tcp-offset-z").elements), None)
    next(iter(user.find(marker="select-tool").elements)).set_value("PNEUMATIC")
    await wait_for_tool_key("PNEUMATIC", timeout_s=5.0)
    assert await wait_until(
        lambda: next(iter(user.find(marker="tcp-offset-z").elements)) is not offset_z,
        5.0,
    ), "the offset inputs were never rebuilt for the tool"
    user.find(marker="tcp-offset-z").trigger("update:modelValue", 60.0)
    client = ui_state.control_panel.client
    await poll_until(
        client.tcp_offset,
        lambda got: float(got[2]) == 60.0,
        timeout_s=5.0,
        what="the controller's TCP calibrated 60 mm out",
    )
    try:
        assert await wait_until(
            lambda: np.linalg.norm(_shown_frame(urdf)[0] - _tcp_now()) < 0.5, 10.0
        ), "the gizmo's frame never moved to the calibrated TCP"

        trigger = UserInteraction(user, {scene}, None)
        trigger.trigger("init")
        await asyncio.sleep(0)
        with patch.object(scene.client, "run_javascript") as sent:
            trigger.trigger("init")
            await asyncio.sleep(0)
        placed = [op for op in _ops(sent) if op[0] == "tcp"]
        assert placed, "the remounted view was not told where the gizmo is"
        assert np.array(placed[-1][2]) * 1000.0 == pytest.approx(_tcp_now(), abs=0.5)

        keepout = Box(name="wall", x=0.1, y=0.1, z=0.1, pose=(0.4, -0.1, 0.05, 0, 0, 0))
        with patch.object(scene.client, "run_javascript") as sent:
            await asyncio.to_thread(setattr, handle, "shapes", [keepout])
            assert await wait_until(lambda: "shape:wall" in urdf._shape_objects, 5.0)
            await asyncio.sleep(0)
        creates = [
            op for op in _ops(sent) if op[0] == "c" and op[5].get("n") == "shape:wall"
        ]
        assert len(creates) == 1, creates
        assert creates[0][5]["p"] == pytest.approx([0.4, -0.1, 0.05])
    finally:
        handle.shapes = []


def _tcp_now() -> np.ndarray:
    pose = waldoctl.commander.status.pose
    return np.array([pose.x, pose.y, pose.z], dtype=float)
