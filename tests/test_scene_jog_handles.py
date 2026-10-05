"""The 3D view's hover-revealed jog handles: a ring per joint and the TCP gizmo.

Pointer and transform events are dispatched to the scene element the way the
browser sends them; the robot is the fake-serial controller.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any

import numpy as np
import pytest
import waldoctl
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction
from pinokin import so3_from_rpy

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

_DIAL = re.compile(r"^jog:dial:\d+$")
_POINTER_R = 0.05


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


def _objects(urdf: Any, name: str) -> list[Any]:
    return [o for o in urdf.scene.objects.values() if o.name == name]


def _one(urdf: Any, name: str) -> Any:
    found = _objects(urdf, name)
    assert len(found) == 1, f"{len(found)} objects named {name!r}"
    return found[0]


def _dials(urdf: Any) -> list[str]:
    return sorted(
        o.name for o in urdf.scene.objects.values() if _DIAL.match(o.name or "")
    )


def _scene(user: User, urdf: Any) -> UserInteraction:
    return UserInteraction(user, {urdf.scene}, None)


def _pointer(
    user: User,
    urdf: Any,
    obj: Any,
    type_: str,
    x: float | None = 0.0,
    y: float | None = 0.0,
    *,
    pointer_type: str = "mouse",
) -> None:
    _scene(user, urdf).trigger(
        "pointerevent",
        {
            "type": type_,
            "object_id": obj.id,
            "object_name": obj.name or "",
            "pointer_type": pointer_type,
            "button": 0,
            "alt_key": False,
            "ctrl_key": False,
            "meta_key": False,
            "shift_key": False,
            "x": x,
            "y": y,
            "z": 0.0,
            "wx": 0.0,
            "wy": 0.0,
            "wz": 0.0,
        },
    )


def _at(user: User, urdf: Any, dial: Any, type_: str, deg: float) -> None:
    """A captured pointer event where the ray meets the ring's plane at ``deg``."""
    a = math.radians(deg)
    _pointer(
        user, urdf, dial, type_, _POINTER_R * math.cos(a), _POINTER_R * math.sin(a)
    )


def _camera_at(user: User, distance_m: float) -> None:
    """The scene's camera-distance report, as scene-framing.js emits it."""
    assert user.client is not None
    UserInteraction(user, {user.client.layout}, None).trigger(
        "wc_camera_distance", {"distance": distance_m}
    )


def _transform(
    user: User, urdf: Any, type_: str, mode: str, axis: str, **pose: float
) -> None:
    ball = urdf._tcp_ball
    args = {
        "type": type_,
        "mode": mode,
        "axis": axis,
        "object_id": ball.id,
        "object_name": "tcp:ball",
    }
    for key in ("x", "y", "z", "rx", "ry", "rz", "wx", "wy", "wz"):
        args[key] = pose.get(key, 0.0)
    # The browser also sends the rotation matrix of three.js' XYZ Euler angles, and the scale.
    rx, ry, rz = args["rx"], args["ry"], args["rz"]
    cx, sx, cy, sy, cz, sz = (
        math.cos(rx),
        math.sin(rx),
        math.cos(ry),
        math.sin(ry),
        math.cos(rz),
        math.sin(rz),
    )
    args["R"] = [
        [cy * cz, -cy * sz, sy],
        [cx * sz + sx * sy * cz, cx * cz - sx * sy * sz, -sx * cy],
        [sx * sz - cx * sy * cz, sx * cz + cx * sy * sz, cx * cy],
    ]
    args["sx"] = args["sy"] = args["sz"] = 1.0
    _scene(user, urdf).trigger(type_, args)


def _hover(user: User, urdf: Any, link: str) -> Any:
    mesh = _objects(urdf, f"link:{link}")[0]
    _pointer(user, urdf, mesh, "pointerover", None, None)
    return mesh


def _unhover(user: User, urdf: Any, mesh: Any) -> None:
    _pointer(user, urdf, mesh, "pointerout", None, None)


@pytest.mark.integration
async def test_hover_reveals_one_handle_at_a_time_and_the_grace_hides_it(
    user: User,
) -> None:
    urdf = await _open(user)
    assert _dials(urdf) == [] and not _objects(urdf, "tcp:ball"), "nothing at rest"

    l2 = _hover(user, urdf, "L2")
    assert _dials(urdf) == ["jog:dial:1"]

    # Moving on to the next link: the shown ring stays through the grace,
    # then gives way; never two at once.
    _unhover(user, urdf, l2)
    l3 = _hover(user, urdf, "L3")
    assert _dials(urdf) == ["jog:dial:1"]
    assert await wait_until(lambda: _dials(urdf) == ["jog:dial:2"], timeout_s=2.0)

    # Leaving to nothing hides it after the grace, and deletes it.
    _unhover(user, urdf, l3)
    assert _dials(urdf) == ["jog:dial:2"]
    assert await wait_until(lambda: _dials(urdf) == [], timeout_s=2.0)

    # The last link shows the gizmo, not a ring.
    l6 = _hover(user, urdf, "L6")
    assert _dials(urdf) == [] and len(_objects(urdf, "tcp:ball")) == 1
    _unhover(user, urdf, l6)
    assert await wait_until(lambda: not _objects(urdf, "tcp:ball"), timeout_s=2.0)

    # Hidden keeps the gizmo away; switching back reveals it under the pointer.
    user.find(marker="gizmo-mode-hidden").click()
    try:
        l6 = _hover(user, urdf, "L6")
        assert not _objects(urdf, "tcp:ball")
    finally:
        user.find(marker="gizmo-mode-move").click()
    assert len(_objects(urdf, "tcp:ball")) == 1
    _unhover(user, urdf, l6)
    assert await wait_until(lambda: not _objects(urdf, "tcp:ball"), timeout_s=2.0)

    # Target editing suspends the handles and takes its ball away when done.
    scene = _scene(user, urdf)
    click = {
        "button": 2,
        "alt_key": False,
        "ctrl_key": False,
        "meta_key": False,
        "shift_key": False,
        "hits": [],
        "intersections": {},
    }
    # What the browser sends for a right-click: the press, the scene's hits,
    # and a release that has not moved.
    page = UserInteraction(user, {urdf.scene.client.layout}, None)
    page.trigger("wc_right_press", {})
    scene.trigger("click3d", {**click, "click_type": "contextmenu"})
    page.trigger("wc_right_release", {"moved": 0.0})
    user.find(marker="scene-target-at-robot").click()
    assert await wait_until(lambda: waldoctl.commander.status.editing_mode, 5)
    _hover(user, urdf, "L2")
    assert _dials(urdf) == []
    user.find(marker="edit-bar-cancel").click()
    assert not waldoctl.commander.status.editing_mode
    assert not _objects(urdf, "tcp:ball")


@pytest.mark.integration
async def test_ring_drag_snaps_clamps_and_moves_the_joint(user: User) -> None:
    urdf = await _open(user)
    await teleport_to_jog_pose(ui_state.control_panel.client)
    start = float(waldoctl.commander.status.joints.angles.deg[1])
    hi_deg = math.degrees(urdf.joint_pos_limits["L2"]["max"])

    _camera_at(user, 2.0)
    assert urdf.snap.joint_deg == 5.0
    _hover(user, urdf, "L2")
    dial = _one(urdf, "jog:dial:1")
    label = _one(urdf, "jog:dial:1:label")

    def text() -> str:
        return label.args[0]

    assert "step 5°" in text() and "Δ" not in text()

    # Across the ±180° seam: +12° of pointer travel is two 5° steps.
    _at(user, urdf, dial, "pointerdown", 170.0)
    _at(user, urdf, dial, "pointermove", -178.0)
    assert "Δ+10.0°" in text(), text()
    _at(user, urdf, dial, "pointermove", -173.0)
    assert "Δ+15.0°" in text(), text()

    # A ray that misses the ring's plane, or a non-number, changes nothing.
    _pointer(user, urdf, dial, "pointermove", None, None)
    _pointer(user, urdf, dial, "pointermove", float("nan"), float("nan"))
    assert "Δ+15.0°" in text(), text()

    # Zooming in mid-drag re-snaps the same pointer travel to the finer step.
    _camera_at(user, 0.4)
    assert "Δ+17.0°" in text() and "step 0.5°" in text(), text()
    _camera_at(user, 2.0)
    assert "Δ+15.0°" in text() and "step 5°" in text(), text()

    # Far past the joint's upper limit, the ring stops at the limit.
    _at(user, urdf, dial, "pointermove", -90.0)
    limit = f"{hi_deg:.1f}°".replace("-", "\u2212")
    assert f"  {limit}  " in text(), text()

    # Back to +22° of travel, released there: +20°.
    _at(user, urdf, dial, "pointermove", -168.0)
    assert "Δ+20.0°" in text(), text()
    _at(user, urdf, dial, "pointerup", -168.0)

    def joint() -> float:
        return float(waldoctl.commander.status.joints.angles.deg[1])

    assert await wait_until(lambda: abs(joint() - (start + 20.0)) <= 0.05, 20.0), (
        f"J2 {joint():.3f}, expected {start + 20.0:.3f}"
    )
    assert await wait_until(lambda: not ui_state.joint_jog_timer.active, 5.0)


@pytest.mark.integration
async def test_ring_drags_while_recording_write_one_move_j_each(user: User) -> None:
    """A ring drag while recording writes one move_j at its target. A ring
    grabbed again owns its own settle wait, so the previous release cannot
    close it while it is held."""
    urdf = await _open(user)
    await teleport_to_jog_pose(ui_state.control_panel.client)
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    textarea.value = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    )
    from tests.test_editor_integration import _set_cursor_line

    _set_cursor_line(textarea, 3)
    await asyncio.sleep(0)
    start = float(waldoctl.commander.status.joints.angles.deg[0])

    user.find(marker="editor-record-btn").click()
    assert await wait_until(is_any_program_recording, 2.0)
    before = str(textarea.value).count("move_j(")
    try:
        _camera_at(user, 2.0)
        _hover(user, urdf, "L1")
        dial = _one(urdf, "jog:dial:0")
        _at(user, urdf, dial, "pointerdown", 0.0)
        _at(user, urdf, dial, "pointermove", 6.0)
        _at(user, urdf, dial, "pointerup", 6.0)

        assert await wait_until(
            lambda: str(textarea.value).count("move_j(") == before + 1, 30.0
        ), textarea.value
        recorded = re.findall(r"move_j\(\[([^\]]*)\]", str(textarea.value))
        recorded_j1 = float(recorded[-1].split(",")[0])
        assert abs(recorded_j1 - (start + 5.0)) <= 0.1, (recorded_j1, start)
        assert not ui_state.joint_jog_timer.active
        # Nothing else turns up: the drag was one owned move, not a capture.
        await asyncio.sleep(1.0)
        assert str(textarea.value).count("move_j(") == before + 1, textarea.value
        panel = ui_state.control_panel
        # A new grab owns its own settle wait. The previous release must
        # not close this second jog while the pointer is still held.
        assert panel.ring_drag_begin(0)
        panel.ring_drag_target(0, start + 10)
        await asyncio.sleep(0.15)
        await panel.ring_drag_end()
        second_start = float(waldoctl.commander.status.joints.angles.deg[1])
        assert panel.ring_drag_begin(1)
        panel.ring_drag_target(1, second_start + 5)
        await asyncio.sleep(0.8)
        assert str(textarea.value).count("move_j(") == before + 2, textarea.value
        await panel.ring_drag_end()
        assert await wait_until(
            lambda: str(textarea.value).count("move_j(") == before + 3, 20
        )
    finally:
        if is_any_program_recording():
            motion_recorder.toggle_recording()


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


@pytest.mark.integration
async def test_gizmo_drag_shows_ticks_and_label_and_moves_in_the_tool_frame(
    user: User,
) -> None:
    urdf = await _open(user)
    await teleport_to_jog_pose(ui_state.control_panel.client)
    _camera_at(user, 2.0)
    _hover(user, urdf, "L6")
    frame = _one(urdf, "tcp:ball_frame")
    assert await wait_until(
        lambda: np.linalg.norm(np.array([frame.x, frame.y, frame.z]) * 1000 - _tcp_mm())
        < 0.5,
        5.0,
    ), "the gizmo's frame sits on the TCP"
    origin_mm = np.array([frame.x, frame.y, frame.z]) * 1000.0
    R_frame = np.array(frame.R)

    # Translate along the tool's X: ticks at 10 mm steps on that axis, and a
    # label with the delta.
    _transform(user, urdf, "transform_start", "translate", "X")
    ticks = _one(urdf, "tcp:ticks")
    points = np.array(ticks.args[0])
    assert len(points) == 21
    assert np.allclose(points[:, 1:], 0.0)
    assert np.allclose(np.diff(points[:, 0]), 0.010)
    label = _one(urdf, "tcp:label")
    assert label.args[0] == "Tool X  Δ+0.0 mm  step 10 mm"

    _transform(user, urdf, "transform", "translate", "X", x=0.020)
    assert label.args[0] == "Tool X  Δ+20.0 mm  step 10 mm"
    target = origin_mm + R_frame[:, 0] * 20.0
    assert await wait_until(lambda: np.linalg.norm(_tcp_mm() - target) < 0.5, 20.0), (
        f"TCP {_tcp_mm()}, expected {target}"
    )
    _transform(user, urdf, "transform_end", "translate", "X", x=0.020)
    assert not _objects(urdf, "tcp:ticks") and not _objects(urdf, "tcp:label")
    ball = _one(urdf, "tcp:ball")
    assert (ball.x, ball.y, ball.z) == (0.0, 0.0, 0.0), "the ball is back on its frame"

    # Rotate about the tool's Z: the result is the frame's orientation turned
    # about its own Z, not the drag's angles taken as a world orientation.
    user.find(marker="gizmo-mode-rotate").click()
    assert await wait_until(
        lambda: np.linalg.norm(np.array([frame.x, frame.y, frame.z]) * 1000 - _tcp_mm())
        < 0.5,
        10.0,
    ), "the frame follows the TCP once the drag is over"
    R_frame = np.array(frame.R)
    _transform(user, urdf, "transform_start", "rotate", "Z")
    ring = np.array(_one(urdf, "tcp:ticks").args[0])
    assert np.allclose(ring[:, 2], 0.0), "ticks lie in the plane the ball turns in"
    radii = np.linalg.norm(ring[:, :2], axis=1)
    assert np.allclose(radii, radii[0])
    assert np.allclose(np.diff(np.arctan2(ring[:, 1], ring[:, 0])), math.radians(5.0))
    _transform(user, urdf, "transform", "rotate", "Z", rz=math.radians(10.0))
    assert _one(urdf, "tcp:label").args[0] == "Tool RZ  Δ+10.0°  step 5°"
    expected = R_frame @ _rz(10.0)

    def error_deg() -> float:
        c = (np.trace(expected.T @ _tcp_rotation()) - 1.0) / 2.0
        return math.degrees(math.acos(min(1.0, max(-1.0, c))))

    assert await wait_until(lambda: error_deg() < 0.3, 20.0), f"{error_deg():.2f}° off"
    _transform(user, urdf, "transform_end", "rotate", "Z", rz=math.radians(10.0))


@pytest.mark.integration
async def test_ring_drag_handoffs_and_interruptions(user: User) -> None:
    """Grabbing the next ring before the first joint arrives keeps the first
    joint's commanded target. A ring drag interrupted from elsewhere — a
    Stop, a simulator request, target editing, a lease takeover — never
    sends its last target on release, and a takeover after a release does
    not refresh the servo the release left moving. A page reload mid-drag
    still leaves the next joint press working."""
    from fastmcp import Client as McpClient

    from waldo_commander.mcp.server import get_mcp
    from waldo_commander.services.control_lease import BROWSER, control_lease

    urdf = await _open(user)
    panel = ui_state.control_panel
    angles = waldoctl.commander.status.joints.angles
    _camera_at(user, 2.0)

    def take_over() -> None:
        control_lease.seize("mcp", "other", "Other driver")
        control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")

    # Grab the next ring before the first joint has reached its target: the
    # first joint's commanded target is kept, and both arrive.
    await teleport_to_jog_pose(panel.client)
    start = np.array(await panel.client.angles())
    assert panel.ring_drag_begin(0)
    panel.ring_drag_target(0, float(start[0] + 10))
    await panel.ring_drag_end()
    assert panel.ring_drag_begin(1)
    panel.ring_drag_target(1, float(start[1] + 5))
    await panel.ring_drag_end()
    assert await wait_until(
        lambda: abs(angles.deg[0] - start[0] - 10) < 0.1
        and abs(angles.deg[1] - start[1] - 5) < 0.1,
        20,
    ), list(angles.deg)

    # Release commits an angle, but it must not refresh that servo after a
    # takeover, even if this browser immediately regains the lease. Started
    # from where the handoff left the arm, so the commanded pose it kept is
    # the one the next grab builds on.
    start = np.array(await panel.client.angles())
    speed = waldoctl.commander.settings.jog.speed
    waldoctl.commander.settings.jog.speed = 10
    try:
        l1 = _hover(user, urdf, "L1")
        dial = _one(urdf, "jog:dial:0")
        _at(user, urdf, dial, "pointerdown", 0.0)
        _at(user, urdf, dial, "pointermove", 26.0)
        _at(user, urdf, dial, "pointerup", 26.0)
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
    _unhover(user, urdf, l1)
    assert await wait_until(lambda: _dials(urdf) == [], 2.0)

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
        urdf.enter_editing_mode(np.radians(start).tolist())
        urdf.exit_editing_mode()

    for interrupt in (lease, editing, simulator, stop):
        await teleport_to_jog_pose(panel.client)
        start = np.array(await panel.client.angles())
        assert await wait_until(lambda: urdf._handles_available, 5.0)
        l2 = _hover(user, urdf, "L2")
        dial = _one(urdf, "jog:dial:1")
        _at(user, urdf, dial, "pointerdown", 0)
        # Hold the periodic sender so the interrupt reaches a target still
        # pending in the UI.
        ui_state.joint_jog_timer.active = False
        _at(user, urdf, dial, "pointermove", 10)
        await interrupt()
        await panel.jog_tick()
        _at(user, urdf, dial, "pointerup", 10)
        assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
        assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1), (
            interrupt.__name__
        )
        _unhover(user, urdf, l2)
        assert await wait_until(lambda: _dials(urdf) == [], 2.0)

    await teleport_to_jog_pose(panel.client)
    assert await wait_until(lambda: urdf._handles_available, 5.0)
    _hover(user, urdf, "L2")
    dial = _one(urdf, "jog:dial:1")
    _at(user, urdf, dial, "pointerdown", 0)
    ui_state.joint_jog_timer.active = False
    _at(user, urdf, dial, "pointermove", 10)
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
async def test_gizmo_lifecycle_never_moves_the_arm_on_its_own(user: User) -> None:
    """A touch-pinned gizmo survives a miss from an arrow press and goes on
    the next plain miss. Events from a deleted gizmo do not move its
    replacement, and a grab after other motion does not replay an old pose."""
    urdf = await _open(user)
    panel = ui_state.control_panel

    mesh = _objects(urdf, "link:L6")[0]
    _pointer(user, urdf, mesh, "click", pointer_type="touch")
    ball = _one(urdf, "tcp:ball")
    miss = {
        "button": 0,
        "alt_key": False,
        "ctrl_key": False,
        "meta_key": False,
        "shift_key": False,
    }
    _scene(user, urdf).trigger("pointermissed", {"type": "pointerdown", **miss})
    assert _one(urdf, "tcp:ball").id == ball.id
    _transform(user, urdf, "transform_start", "translate", "X")
    _transform(user, urdf, "transform_end", "translate", "X")
    _scene(user, urdf).trigger("pointermissed", {"type": "click", **miss})
    assert not _objects(urdf, "tcp:ball")

    await teleport_to_jog_pose(panel.client)
    mesh = _hover(user, urdf, "L6")
    old = _one(urdf, "tcp:ball")
    _unhover(user, urdf, mesh)
    assert await wait_until(lambda: not _objects(urdf, "tcp:ball"), 2)
    mesh = _hover(user, urdf, "L6")
    assert _one(urdf, "tcp:ball").id != old.id
    start = np.array(await panel.client.angles())
    args = {
        "object_id": old.id,
        "object_name": "tcp:ball",
        "mode": "translate",
        "axis": "X",
        "x": 0.02,
        "y": 0,
        "z": 0,
        "rx": 0,
        "ry": 0,
        "rz": 0,
        "wx": 0,
        "wy": 0,
        "wz": 0,
    }
    for event in ("transform_start", "transform", "transform_end"):
        _scene(user, urdf).trigger(event, {**args, "type": event})
    await panel.cart_jog_tick()
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1)
    _unhover(user, urdf, mesh)
    assert await wait_until(lambda: not _objects(urdf, "tcp:ball"), 2)

    mesh = _hover(user, urdf, "L6")
    _transform(user, urdf, "transform_start", "translate", "X")
    _transform(user, urdf, "transform", "translate", "X", x=0.005)
    await panel.cart_jog_tick()
    _transform(user, urdf, "transform_end", "translate", "X", x=0.005)
    _unhover(user, urdf, mesh)
    assert await wait_until(lambda: not _objects(urdf, "tcp:ball"), 2)
    await teleport_to_jog_pose(panel.client)
    start = np.array(await panel.client.angles())
    _hover(user, urdf, "L6")
    _transform(user, urdf, "transform_start", "translate", "X")
    await panel.cart_jog_tick()
    _transform(user, urdf, "transform_end", "translate", "X")
    assert await panel.client.wait_motion(timeout=10, settle_window=0.5)
    assert np.array(await panel.client.angles()) == pytest.approx(start, abs=0.1)
