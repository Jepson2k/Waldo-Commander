"""The 3D scene in a real browser: the jog handles under real pointer events,
the render loop, the fog, the context menu, a held object's world pose and
the physics overlay of a predicted run.

The tests share one page through ``class_screen``. Each puts back the camera,
shapes, appearance mode and window size it changes. The gizmo test checks the
gizmo is absent until L6 is hovered, so it runs first; the physics overlay
leaves the program column open, so it runs last.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import numpy as np
import pytest
import waldoctl
from nicegui import core
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.mouse_button import MouseButton
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl import Box

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    defocus_editor,
    focus_editor,
    hover_scene_object,
    js,
    pointer_to,
    project_local,
    run_in_app,
    scene_canvas,
    scene_object_pixel,
    send_global_key,
    tap,
)
from tests.helpers.browser_session import no_visible, wait, window_size
from tests.helpers.wait import (
    JOG_SAFE_POSE_DEG,
    screen_get_scene_object,
    screen_wait_for_scene_ready,
    screen_wait_for_tcp_ball,
)
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.constants import DEFAULT_CAMERA
from waldo_commander.services.keybindings import keybindings_manager
from waldo_commander.services.motion_guard import motion_guard
from waldo_commander.services.urdf_scene.jog_handles_mixin import GIZMO
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

_SCENE = "getElement(document.querySelector('.nicegui-scene'))"

_ZOOM = f"""
const c = {_SCENE};
const t = c.controls.target;
c.camera.position.sub(t).setLength(arguments[0]).add(t);
"""

_CAMERA = f"""
const c = {_SCENE};
return [...c.camera.position.toArray(), ...c.controls.target.toArray()];
"""

_SET_CAMERA = f"""
const c = {_SCENE};
const [px, py, pz, tx, ty, tz] = arguments[0];
c.camera.position.set(px, py, pz);
c.controls.target.set(tx, ty, tz);
c.controls.update();
"""

_DIAL = f"""
const c = {_SCENE};
let knob = null;
for (const o of c.objects.values()) if (o.mesh && o.mesh.name === arguments[0]) knob = o.mesh;
if (!knob) return null;
const sphere = knob.children.find((m) => m.isMesh);
return sphere ? {{q: knob.rotation.z, r: sphere.position.x}} : null;
"""

# The first angle (from the knob, in degrees) where the ring is the first thing
# the scene's pointer ray hits, so a press there grabs the ring.
_GRAB = f"""
const [name, q, r] = arguments;
const c = {_SCENE};
let dial = null;
for (const o of c.objects.values()) if (o.mesh && o.mesh.name === name) dial = o.mesh;
if (!dial) return null;
dial.updateWorldMatrix(true, true);
const canvas = c.renderer.domElement;
const rect = canvas.getBoundingClientRect();
const v = dial.position.clone();
for (let a = 0; a < 360; a += 10) {{
  const t = q + a * Math.PI / 180;
  v.set(r * Math.cos(t), r * Math.sin(t), 0).applyMatrix4(dial.matrixWorld).project(c.camera);
  const px = rect.left + (v.x + 1) / 2 * rect.width;
  const py = rect.top + (1 - v.y) / 2 * rect.height;
  if (document.elementFromPoint(px, py) !== canvas) continue;
  c._raycaster.setFromCamera({{ x: v.x, y: v.y }}, c.camera);
  const hits = c._raycaster.intersectObjects(c.interactiveObjects, true);
  let o = hits.length ? hits[0].object : null;
  while (o && o !== dial) o = o.parent;
  if (o === dial) return a;
}}
return null;
"""

_GIZMO_SNAP = f"""
const c = {_SCENE};
let id = null;
for (const [oid, o] of c.objects) if (o.mesh && o.mesh.name === 'tcp:ball') id = oid;
const tc = id === null ? null : c.transform_controls.get(id);
if (!tc) return null;
if (arguments[0]) tc.wcProbe = true;
return {{t: tc.translationSnap, r: tc.rotationSnap, probe: !!tc.wcProbe}};
"""

# The middle of the stretch of the X arrow of the named object's gizmo that
# sticks out past the object, where a press grabs the arrow and nothing else,
# and the arrow's direction on screen.
_ARROW_TIP = f"""
const c = {_SCENE};
let id = null;
for (const [oid, o] of c.objects) if (o.mesh && o.mesh.name === arguments[0]) id = oid;
const tc = id === null ? null : c.transform_controls.get(id);
if (!tc || !tc.object) return null;
const ball = tc.object;
ball.updateWorldMatrix(true, false);
const origin = ball.position.clone().setFromMatrixPosition(ball.matrixWorld);
const axis = ball.position.clone().set(1, 0, 0).applyQuaternion(ball.getWorldQuaternion(ball.quaternion.clone()));
const canvas = c.renderer.domElement;
const rect = canvas.getBoundingClientRect();
const toPixel = (d) => {{
  const v = origin.clone().addScaledVector(axis, d).project(c.camera);
  return [v.x, v.y, rect.left + (v.x + 1) / 2 * rect.width, rect.top + (1 - v.y) / 2 * rect.height];
}};
// Other gizmos (the joint rings while editing) must not have a handle there.
const others = [...c.transform_controls.values()].filter((o) => o !== tc);
const outside = [];
for (let d = 0.002; d < 0.5; d += 0.002) {{
  const [x, y, px, py] = toPixel(d);
  if (Math.abs(x) > 0.95 || Math.abs(y) > 0.95 || document.elementFromPoint(px, py) !== canvas) continue;
  tc.pointerHover({{ x, y, button: 0 }});
  if (tc.axis !== 'X') continue;
  c._raycaster.setFromCamera({{ x, y }}, c.camera);
  if (c._raycaster.intersectObjects(c.interactiveObjects, true).length) continue;
  if (others.some((o) => (o.pointerHover({{ x, y, button: 0 }}), o.axis !== null))) continue;
  outside.push(d);
}}
for (const t of [tc, ...others]) t.pointerHover({{ x: 2, y: 2, button: 0 }});
if (!outside.length) return null;
const d = outside[Math.floor(outside.length / 2)];
const [, , px, py] = toPixel(d);
const [, , qx, qy] = toPixel(d + 0.01);
const n = Math.hypot(qx - px, qy - py);
return [px, py, (qx - px) / n, (qy - py) / n];
"""

# A pixel away from the canvas edges where a press hits neither an
# interactive object nor a gizmo.
_EMPTY_SPOT = f"""
const c = {_SCENE};
const canvas = c.renderer.domElement;
const rect = canvas.getBoundingClientRect();
const gizmos = [...c.transform_controls.values()];
let spot = null;
for (let fy = 0.3; fy <= 0.7 && !spot; fy += 0.1) {{
  for (let fx = 0.2; fx <= 0.8 && !spot; fx += 0.1) {{
    const px = rect.left + fx * rect.width;
    const py = rect.top + fy * rect.height;
    if (document.elementFromPoint(px, py) !== canvas) continue;
    const pointer = {{ x: fx * 2 - 1, y: 1 - fy * 2, button: 0 }};
    c._raycaster.setFromCamera(pointer, c.camera);
    if (c._raycaster.intersectObjects(c.interactiveObjects, true).length) continue;
    if (gizmos.some((tc) => (tc.pointerHover(pointer), tc.axis !== null))) continue;
    spot = [px, py];
  }}
}}
for (const tc of gizmos) tc.pointerHover({{ x: 2, y: 2, button: 0 }});
return spot;
"""

# A pixel on the given ring of the rotate gizmo, past the tool, and the
# ring's direction on screen there.
_RING_SPOT = f"""
const c = {_SCENE};
let id = null;
for (const [oid, o] of c.objects) if (o.mesh && o.mesh.name === 'tcp:ball') id = oid;
const tc = id === null ? null : c.transform_controls.get(id);
if (!tc || !tc.object || tc.mode !== 'rotate') return null;
const center = tc.object.getWorldPosition(tc.object.position.clone()).project(c.camera);
const canvas = c.renderer.domElement;
const rect = canvas.getBoundingClientRect();
const cx = rect.left + (center.x + 1) / 2 * rect.width;
const cy = rect.top + (1 - center.y) / 2 * rect.height;
let spot = null;
for (let r = 20; r <= 200 && !spot; r += 4) {{
  for (let a = 0; a < 360 && !spot; a += 5) {{
    const t = a * Math.PI / 180;
    const px = cx + r * Math.cos(t);
    const py = cy + r * Math.sin(t);
    if (document.elementFromPoint(px, py) !== canvas) continue;
    const pointer = {{ x: (px - rect.left) / rect.width * 2 - 1, y: 1 - (py - rect.top) / rect.height * 2, button: 0 }};
    tc.pointerHover(pointer);
    if (tc.axis !== arguments[0]) continue;
    c._raycaster.setFromCamera(pointer, c.camera);
    if (c._raycaster.intersectObjects(c.interactiveObjects, true).length) continue;
    spot = [px, py, -Math.sin(t), Math.cos(t)];
  }}
}}
tc.pointerHover({{ x: 2, y: 2, button: 0 }});
return spot;
"""

# The pixel at the named object's origin, if the canvas is there.
_CENTRE_PIXEL = f"""
const c = {_SCENE};
let root = null;
for (const o of c.objects.values()) if (o.mesh && o.mesh.name === arguments[0]) root = o.mesh;
if (!root) return null;
const v = root.getWorldPosition(root.position.clone()).project(c.camera);
const rect = c.renderer.domElement.getBoundingClientRect();
const px = rect.left + (v.x + 1) / 2 * rect.width;
const py = rect.top + (1 - v.y) / 2 * rect.height;
return document.elementFromPoint(px, py) === c.renderer.domElement ? [px, py] : null;
"""

# A whole viewport pixel on the floor near the arm where a press hits neither
# an interactive object nor a gizmo, and the floor point under it.
_FLOOR_SPOT = f"""
const c = {_SCENE};
const canvas = c.renderer.domElement;
const rect = canvas.getBoundingClientRect();
const gizmos = [...c.transform_controls.values()];
const p = c.camera.position.clone();
const ray = c._raycaster.ray;
let spot = null;
for (let r = 0.25; r <= 0.45 && !spot; r += 0.05) {{
  for (let a = 0; a < 360 && !spot; a += 15) {{
    const t = a * Math.PI / 180;
    p.set(r * Math.cos(t), r * Math.sin(t), 0).project(c.camera);
    const px = Math.round(rect.left + (p.x + 1) / 2 * rect.width);
    const py = Math.round(rect.top + (1 - p.y) / 2 * rect.height);
    if (document.elementFromPoint(px, py) !== canvas) continue;
    const pointer = {{ x: (px - rect.left) / rect.width * 2 - 1, y: 1 - (py - rect.top) / rect.height * 2, button: 0 }};
    c._raycaster.setFromCamera(pointer, c.camera);
    if (c._raycaster.intersectObjects(c.interactiveObjects, true).length) continue;
    if (gizmos.some((tc) => (tc.pointerHover(pointer), tc.axis !== null))) continue;
    c._raycaster.setFromCamera(pointer, c.camera);
    const k = -ray.origin.z / ray.direction.z;
    spot = [px, py, ray.origin.x + k * ray.direction.x, ray.origin.y + k * ray.direction.y];
  }}
}}
for (const tc of gizmos) tc.pointerHover({{ x: 2, y: 2, button: 0 }});
return spot;
"""

_DIALS_SHOWN = f"""
const c = {_SCENE};
const shown = [];
for (const o of c.objects.values()) {{
  if (o.mesh && /^jog:dial:\\d+$/.test(o.mesh.name) && o.mesh.parent && o.mesh.visible) shown.push(o.mesh.name);
}}
return shown;
"""

_GLOWING = f"""
const c = {_SCENE};
for (const [id, o] of c.objects) {{
  if (!o.mesh || o.mesh.name !== arguments[0]) continue;
  const a = c.effectArtifacts.get(id);
  return !!(a && a.effect === 'glow' && a.group.parent && a.group.children.length);
}}
return false;
"""

_MENU_ITEMS = """
return [...document.querySelectorAll('.q-menu')]
  .filter((m) => m.getClientRects().length > 0)
  .flatMap((m) => [...m.querySelectorAll('.q-item')].map((i) => i.textContent.trim()));
"""

# A program with two targets, the last of them a move_l in front of the arm.
_TARGET_PROGRAM = (
    "from parol6 import RobotClient\n"
    "rbt = RobotClient()\n"
    "rbt.move_j([85.000, -85.000, 175.000, 5.000, 5.000, 175.000], speed=0.5)\n"
    "rbt.move_l([0.000, 280.000, 250.000, 90.000, 0.000, 90.000], speed=0.5)\n"
)

_HELD_MATRIX = """
const el = document.querySelector('.nicegui-scene');
const c = el && getElement(el);
if (!c || !c.objects) return null;
for (const o of c.objects.values()) {
  if (o.mesh && o.mesh.name === 'shape:held-part') {
    o.mesh.updateWorldMatrix(true, false);
    return o.mesh.matrixWorld.elements;
  }
}
return null;
"""

# Walks the three.js scene for the physics overlay group and reports what is
# in it: the predicted polyline (vertex-coloured, so the following-error
# gradient is real geometry and not a uniform), the contact arrows and the COM.
_OVERLAY_JS = """
const canvas = document.querySelector('canvas');
const host = canvas && canvas.closest('[id^="c"]');
const comp = host && getElement(host.id.slice(1));
const scene = comp && comp.scene;
if (!scene) return null;
let group = null;
scene.traverse((o) => { if (o.name === 'simulation:physics') group = o; });
if (!group) return {found: false};
let lines = 0, meshes = 0, shown = 0, vertexColored = 0, points = 0;
group.traverse((o) => {
  if (o === group) return;
  if (o.isLine || o.isLineSegments) {
    lines += 1;
    if (o.geometry && o.geometry.getAttribute('color')) {
      vertexColored += 1;
      points = Math.max(points, o.geometry.getAttribute('position').count);
    }
  } else if (o.isMesh) {
    meshes += 1;
    if (o.visible) shown += 1;
  }
});
return {found: true, lines, meshes, shown, vertexColored, points};
"""

_VIEW_FLAGS = (
    "paths_visible",
    "predicted_visible",
    "contacts_visible",
    "com_visible",
)


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def _joint(i: int) -> float:
    return float(waldoctl.commander.status.joints.angles.deg[i])


def _settled(i: int, timeout: float = 20.0) -> float:
    """The joint's value once it has stopped moving."""
    deadline = time.monotonic() + timeout
    last = _joint(i)
    while time.monotonic() < deadline:
        time.sleep(0.3)
        now = _joint(i)
        if abs(now - last) < 0.005:
            return now
        last = now
    raise AssertionError(f"J{i + 1} never settled")


def _teleport_to_jog_pose() -> None:
    assert core.loop is not None
    client = ui_state.control_panel.client
    asyncio.run_coroutine_threadsafe(
        client.teleport(JOG_SAFE_POSE_DEG), core.loop
    ).result(10)
    _wait(lambda: abs(_joint(1) - JOG_SAFE_POSE_DEG[1]) < 0.5, 10.0, "teleport")


@contextmanager
def _camera_kept(screen: Screen) -> Iterator[None]:
    """Give the next test the camera this one found, and take the pointer off
    the arm so no handle stays revealed."""
    saved = js(screen, _CAMERA)
    try:
        yield
    finally:
        js(screen, _SET_CAMERA, saved)
        screen.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
        )


@contextmanager
def _program_previewed(source: str) -> Iterator[list[str]]:
    """Preview ``source`` in the scene and yield its target ids; the program
    that was there comes back afterwards, each through the editor as an edit
    would come."""
    from waldo_commander.components.simulation_engine import simulation

    def edit(text: str) -> None:
        ui_state.active_textarea.value = text  # schedules the preview

    def previewing() -> bool:
        return (
            simulation._simulation_debounce_timer is not None
            or simulation._physics_timer is not None
        )

    def saved() -> tuple[str, object]:
        tab = waldoctl.commander.programs.active
        assert tab is not None
        return ui_state.active_textarea.value, tab.dry_run.last_sim_joints_deg

    text, sim_joints = run_in_app(saved)
    run_in_app(lambda: edit(source))
    _wait(lambda: not run_in_app(previewing), 30.0, "the program's preview")
    try:
        yield run_in_app(
            lambda: [t.id for t in waldoctl.commander.programs.active.dry_run.targets]
        )
    finally:
        run_in_app(lambda: edit(text))
        # A preview still running when the next test injects its records
        # would overwrite them.
        _wait(lambda: not run_in_app(previewing), 30.0, "the restored preview")
        # A previewed program re-previews whenever the arm moves; the program
        # that was there had not been previewed.
        run_in_app(
            lambda: setattr(
                waldoctl.commander.programs.active.dry_run,
                "last_sim_joints_deg",
                sim_joints,
            )
        )


def _caught_up() -> None:
    """Return once every event the browser has sent so far has reached the
    app, and every update the app sent in answer has reached the browser:
    each round trip's reply comes back behind what was sent before it."""
    client = ui_state.urdf_scene.scene.client

    async def round_trips() -> None:
        await client.run_javascript("0", timeout=5.0)
        await client.run_javascript("0", timeout=5.0)

    assert core.loop is not None
    asyncio.run_coroutine_threadsafe(round_trips(), core.loop).result(15)


def _tcp_mm() -> np.ndarray:
    pose = waldoctl.commander.status.pose
    return np.array([pose.x, pose.y, pose.z], dtype=float)


def _drag_along(
    screen: Screen, x: float, y: float, dx: float, dy: float, length: float = 40
) -> None:
    """A left-button drag from (x, y) along the unit direction (dx, dy)."""
    actions = ActionChains(screen.selenium, duration=20)
    pointer_to(screen, x, y, actions)
    actions.click_and_hold()
    for step in range(1, 9):
        pointer_to(
            screen, x + dx * length * step / 8, y + dy * length * step / 8, actions
        )
    actions.release()
    actions.perform()


def _choose(screen: Screen, x: float, y: float, item: str) -> list[str]:
    """Right-click at (x, y), pick ``item`` from the scene's menu and return
    the items the menu offered."""
    actions = ActionChains(screen.selenium, duration=0)
    pointer_to(screen, x, y, actions)
    actions.context_click()
    actions.perform()

    offered: list[str] = []

    def shown_item(driver):
        shown = [
            e
            for e in driver.find_elements(By.CSS_SELECTOR, ".q-menu .q-item")
            if e.is_displayed()
        ]
        texts = [e.text.strip() for e in shown]
        # The menu fills in after it opens; pick from it once it stops changing.
        settled, offered[:] = texts == offered, texts
        if not settled or item not in texts:
            return False
        shown[texts.index(item)].click()
        return True

    # The menu is filled in after it opens, so its items can go stale while read.
    WebDriverWait(
        screen.selenium,
        5,
        poll_frequency=0.05,
        ignored_exceptions=(StaleElementReferenceException,),
    ).until(shown_item, message=f"no {item!r} in the menu")
    # A closing menu still covers the canvas and would swallow the next press.
    wait(screen, 5).until(lambda _: no_visible(screen, ".q-menu"))
    return offered


def _records(
    q_rad: np.ndarray, rows: int = 40
) -> tuple[waldoctl.TickIndex, waldoctl.TickIndex]:
    """A commanded/predicted pair the way a backend reports them: a TCP
    path that sweeps forward, a lag that grows along it, and contact for
    the middle third.

    The predicted joints hold at *q_rad* on every row on purpose. Playback
    teleports the simulated arm to whatever the record says, so a record
    that moved it would leave it moved for every test after this one — and
    a pose the planner will not accept turns into a self-collision refusal
    three tests later. What this checks is the drawing, not the arm.
    """
    t = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    joints = np.tile(np.asarray(q_rad, dtype=np.float32)[:6], (rows, 1))
    commanded = joints + t[:, None] * 0.02
    tcp = np.zeros((rows, 6), dtype=np.float32)
    tcp[:, 0] = 0.25 + t * 0.12
    tcp[:, 2] = 0.30 - t * 0.06

    lo, hi = rows // 3, 2 * rows // 3
    starts = np.zeros(rows + 1, dtype=np.uint32)
    pos, force = [], []
    for r in range(rows):
        if lo <= r < hi:
            pos.append([tcp[r, 0], 0.02, tcp[r, 2] - 0.03])
            force.append([0.0, -6.0, 14.0])
        starts[r + 1] = len(pos)
    com = np.zeros((rows, 3), dtype=np.float32)
    com[:, 0] = 0.10 + t * 0.02
    com[:, 2] = 0.22

    blocks = (waldoctl.TickBlock(command=0, start_row=0, rows=rows, line_number=1),)
    tool_closed = np.linspace(0.0, 1.0, rows, dtype=np.float32)
    return (
        waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=commanded.astype(np.float32),
            tcp=tcp,
            tool_closed=tool_closed,
            tool_gripping=np.zeros(rows, dtype=np.bool_),
            blocks=blocks,
            digest=b"screen-commanded",
        ),
        waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=joints.astype(np.float32),
            tcp=tcp,
            tool_closed=tool_closed,
            tool_gripping=np.zeros(rows, dtype=np.bool_),
            blocks=blocks,
            objects=(),
            digest=b"screen-predicted",
            channels={
                "com": com,
                "contact_pos": np.asarray(pos, dtype=np.float32).reshape(-1, 3),
                "contact_force": np.asarray(force, dtype=np.float32).reshape(-1, 3),
                "contact_starts": starts,
            },
        ),
    )


@pytest.mark.browser
@skip_webgl_macos_ci
class TestScene:
    def test_gizmo_snap_follows_the_zoom_without_reattaching(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()
        assert screen_get_scene_object(screen, "tcp:ball") is None, (
            "the gizmo waits for a hover"
        )
        urdf = ui_state.urdf_scene
        assert urdf is not None
        with _camera_kept(screen):
            js(screen, _ZOOM, 1.3)
            _wait(lambda: urdf.snap.joint_deg == 5.0, 1.0, "the 5° band")
            # A hover aimed while the teleported pose is still being drawn
            # can land beside the link, so aim again until the gizmo comes.
            deadline = time.monotonic() + 10.0
            while True:
                hover_scene_object(screen, "link:L6")
                try:
                    snap = _wait(lambda: js(screen, _GIZMO_SNAP, True), 1.5, "")
                    break
                except AssertionError:
                    if time.monotonic() > deadline:
                        raise AssertionError("the gizmo never attached on hover")
            assert snap["t"] == pytest.approx(0.010) and snap["r"] == pytest.approx(
                math.radians(5.0)
            )
            ball = screen_wait_for_tcp_ball(screen, timeout_s=5.0)
            assert ball is not None and ball["type"] == "Mesh" and ball["visible"], ball

            js(screen, _ZOOM, 0.4)
            # Translation and rotation snaps can land a frame apart.
            snap = _wait(
                lambda: (s := js(screen, _GIZMO_SNAP, False))
                and s["t"] == pytest.approx(0.001)
                and s["r"] == pytest.approx(math.radians(0.5))
                and s,
                2.0,
                "the 1 mm and 0.5° bands on the gizmo",
            )
            assert snap["probe"], "the gizmo was re-attached instead of updated"

    def test_ring_drag_moves_the_joint_by_whole_steps_and_leaves_the_camera(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()

        with _camera_kept(screen):
            # Pulled out past 1.2 m, the scene reports its distance and the
            # handles snap in 5° steps within a second.
            js(screen, _ZOOM, 1.3)
            urdf = ui_state.urdf_scene
            assert urdf is not None
            _wait(lambda: urdf.snap.joint_deg == 5.0, 1.0, "the 5° band")

            hover_scene_object(screen, "link:L2")
            dial = _wait(lambda: js(screen, _DIAL, "jog:dial:1:knob"), 5.0, "J2's ring")
            start = _settled(1)
            camera = js(screen, _CAMERA)

            # Grab the ring where nothing covers it and sweep 21° around it in
            # 3° moves; a drag turns the joint by how far it goes, not where it
            # starts.
            grab = js(screen, _GRAB, "jog:dial:1", dial["q"], dial["r"])
            assert grab is not None, "no part of J2's ring is uncovered"
            path = project_local(
                screen,
                "jog:dial:1",
                [
                    [
                        dial["r"] * math.cos(dial["q"] + math.radians(grab + a)),
                        dial["r"] * math.sin(dial["q"] + math.radians(grab + a)),
                        0.0,
                    ]
                    for a in range(0, 22, 3)
                ],
            )
            assert path is not None
            actions = ActionChains(screen.selenium, duration=20)
            pointer_to(screen, path[0][0], path[0][1], actions)
            actions.click_and_hold()
            for x, y in path[1:]:
                pointer_to(screen, x, y, actions)
            actions.release()
            actions.perform()

            _wait(lambda: abs(_joint(1) - start) > 4.0, 15.0, "J2 to move")
            moved = _settled(1) - start
            steps = moved / 5.0
            assert round(steps) >= 1 and abs(steps - round(steps)) < 0.02, (
                f"J2 moved {moved:.3f}°, not a whole number of 5° steps"
            )
            assert js(screen, _CAMERA) == pytest.approx(camera, abs=1e-9), (
                "the drag orbited the camera"
            )

    def test_axes_inset_preserves_main_scene_render(self, class_screen: Screen) -> None:
        """``viewHelper.render()`` clears the framebuffer when
        ``renderer.autoClear`` is true; without the scene-loop guard, WC's URDF
        scene (which always enables ``set_axes_inset``) gets wiped each frame
        and the user sees a blank canvas.
        """
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        js(
            screen,
            'const div = document.querySelector(".nicegui-scene");'
            'if (!div) throw new Error("scene element not mounted");'
            "const comp = getElement(div);"
            'if (!comp || !comp.viewHelper) throw new Error("axes inset not active");'
            "window.__viewHelperRender = comp.viewHelper.render;"
            "const orig = comp.viewHelper.render.bind(comp.viewHelper);"
            "window.__autoClearLog = [];"
            "comp.viewHelper.render = function (renderer) {"
            "  window.__autoClearLog.push(renderer.autoClear);"
            "  return orig(renderer);"
            "};",
        )
        try:
            # A few frames of the render loop.
            wait(screen, 5).until(
                lambda _: js(screen, "return window.__autoClearLog.length") >= 2
            )
            log = js(screen, "return window.__autoClearLog")
            assert all(v is False for v in log), (
                f"renderer.autoClear must be false during viewHelper.render; got {log}"
            )
        finally:
            js(
                screen,
                "getElement(document.querySelector('.nicegui-scene')).viewHelper.render"
                " = window.__viewHelperRender;",
            )

    def test_zoomed_out_the_fog_starts_beyond_the_robot(
        self, class_screen: Screen
    ) -> None:
        """A fog fixed to the reach swallowed the arm, its paths and targets
        once the camera pulled back past a couple of metres."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        reach = run_in_app(lambda: ui_state.urdf_scene._chain_reach())
        read = (
            "const view = getElement(document.querySelector('.nicegui-scene'));"
            "if (!view.scene.fog) return null;"
            "view.camera.position.set(0, -6, 6); view.controls.update();"
            "return {near: view.scene.fog.near, d: view.camera.position.length()};"
        )
        with _camera_kept(screen):
            try:
                fog = wait(screen, 5).until(
                    lambda _: (m := js(screen, read))
                    and m["near"] > m["d"] + reach
                    and m
                )
            except TimeoutException:
                fog = js(screen, read)
            assert fog and fog["near"] > fog["d"] + reach, (fog, reach)

    def test_right_click_opens_a_context_menu_that_closes_on_an_outside_click(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        canvas = screen.selenium.find_element(By.CSS_SELECTOR, ".nicegui-scene canvas")
        visible_items = (
            "return [...document.querySelectorAll('.q-menu')]"
            ".filter(m => m.getClientRects().length > 0)"
            ".map(m => m.querySelectorAll('.q-item').length);"
        )
        # A right-click can land before the scene's raycast is wired up.
        for _attempt in range(5):
            ActionChains(screen.selenium).context_click(canvas).perform()
            try:
                items = wait(screen, 2).until(
                    lambda _: (shown := js(screen, visible_items))
                    and shown[0]
                    and shown
                )
                break
            except TimeoutException:
                continue
        else:
            raise AssertionError(
                "Context menu not visible after 5 right-click attempts"
            )
        assert items[0] > 0, "Context menu should have options"

        ActionChains(screen.selenium).move_to_element(canvas).click().perform()
        wait(screen, 5).until(
            lambda _: no_visible(screen, ".q-menu"),
            message="Context menu should close when clicking outside",
        )

    def test_a_target_glows_under_the_pointer_and_right_clicks_to_its_menu(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        with _camera_kept(screen), _program_previewed(_TARGET_PROGRAM) as targets:
            assert targets, "the program shows no targets"
            group = f"targetgroup:{targets[-1]}"
            _wait(lambda: screen_get_scene_object(screen, group), 15.0, "the target")
            x, y = scene_object_pixel(screen, group)
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, x, y, actions)
            actions.perform()
            _wait(lambda: js(screen, _GLOWING, group), 5.0, "the target to glow")

            ActionChains(screen.selenium).context_click().perform()
            items = _wait(lambda: js(screen, _MENU_ITEMS), 5.0, "the target's menu")
            assert "Edit Target..." in items and "Delete Target" in items, items
            canvas = screen.selenium.find_element(
                By.CSS_SELECTOR, ".nicegui-scene canvas"
            )
            ActionChains(screen.selenium).move_to_element(canvas).click().perform()
            wait(screen, 5).until(lambda _: no_visible(screen, ".q-menu"))

            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            _wait(lambda: not js(screen, _GLOWING, group), 5.0, "the glow to go")

    def test_the_gizmo_arrow_jogs_on_a_drag_and_a_tap_keeps_it_pinned(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()
        urdf = ui_state.urdf_scene
        assert urdf is not None

        def shown() -> object:
            return run_in_app(lambda: urdf._shown_handle)

        with _camera_kept(screen):
            # A mouse drag along the arrow, from where it sticks out past the
            # tool, jogs the tool.
            hover_scene_object(screen, "link:L6")
            _wait(lambda: js(screen, _GIZMO_SNAP, False), 5.0, "the gizmo")
            x, y, dx, dy = _wait(
                lambda: js(screen, _ARROW_TIP, "tcp:ball"), 5.0, "the X arrow"
            )
            start = _tcp_mm()
            camera = js(screen, _CAMERA)
            _drag_along(screen, x, y, dx, dy)
            _wait(
                lambda: np.linalg.norm(_tcp_mm() - start) > 2.0,
                15.0,
                "the tool to move",
            )
            assert shown() == GIZMO, "the drag hid the gizmo"
            assert js(screen, _CAMERA) == pytest.approx(camera, abs=1e-6), (
                "the drag orbited the camera"
            )

            # Touch has no hover: a tap on the arm pins the gizmo, and a tap
            # on its arrow grabs the arrow, so it must not count as a tap on
            # empty space that unpins it.
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            _wait(lambda: shown() is None, 5.0, "the gizmo to hide")
            tap(screen, *scene_object_pixel(screen, "link:L6"))
            _wait(lambda: js(screen, _GIZMO_SNAP, False), 5.0, "the pinned gizmo")
            x, y, _, _ = _wait(
                lambda: js(screen, _ARROW_TIP, "tcp:ball"), 5.0, "the X arrow"
            )
            tap(screen, x, y)
            _caught_up()
            assert shown() == GIZMO, "a tap on the gizmo's arrow unpinned it"

            tap(screen, *_wait(lambda: js(screen, _EMPTY_SPOT), 5.0, "empty space"))
            _wait(lambda: shown() is None, 5.0, "a tap on empty space to unpin")

    def test_a_right_drag_pans_without_opening_the_menu(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)

        def right_drag(x: float, y: float, offsets: list[int]) -> None:
            actions = ActionBuilder(screen.selenium)
            actions.pointer_action.move_to_location(round(x), round(y))
            actions.pointer_action.pointer_down(button=MouseButton.RIGHT)
            for dx in offsets:
                actions.pointer_action.move_to_location(round(x) + dx, round(y))
            actions.pointer_action.pointer_up(button=MouseButton.RIGHT)
            actions.perform()

        with _camera_kept(screen):
            x, y = _wait(lambda: js(screen, _EMPTY_SPOT), 5.0, "empty space")
            # A menu left open by a right-click must not come back with a pan.
            # A press may show the menu for a moment; its release closes it.
            actions = ActionChains(screen.selenium, duration=0)
            pointer_to(screen, x, y, actions)
            actions.context_click()
            actions.perform()
            _wait(lambda: js(screen, _MENU_ITEMS), 5.0, "the menu")
            camera = js(screen, _CAMERA)
            right_drag(x, y, [10, 20, 30, 40, 50, 60])
            _wait(lambda: js(screen, _CAMERA) != camera, 5.0, "the camera to pan")
            _caught_up()
            assert not js(screen, _MENU_ITEMS), "a right-drag brought back the menu"
            wait(screen, 5).until(
                lambda _: no_visible(screen, ".q-menu"),
                message="a right-drag left a menu open",
            )

            # A pan that comes back to where it started is still a pan.
            x, y = _wait(lambda: js(screen, _EMPTY_SPOT), 5.0, "empty space")
            right_drag(x, y, [20, 40, 60, 40, 20, 0])
            _caught_up()
            assert not js(screen, _MENU_ITEMS), (
                "a pan back to its start opened the menu"
            )

            # The menu key or a long press opens the menu with no right press
            # before it; headless Chrome makes no contextmenu from a synthetic
            # long press, so the event comes straight to the canvas.
            x, y = _wait(lambda: js(screen, _EMPTY_SPOT), 5.0, "empty space")
            js(
                screen,
                "document.querySelector('.nicegui-scene canvas').dispatchEvent("
                "new MouseEvent('contextmenu', {clientX: arguments[0],"
                " clientY: arguments[1], bubbles: true, cancelable: true}));",
                x,
                y,
            )
            _wait(
                lambda: js(screen, _MENU_ITEMS), 5.0, "the menu without a right press"
            )
            ActionChains(screen.selenium).move_to_element(
                scene_canvas(screen)
            ).click().perform()
            wait(screen, 5).until(lambda _: no_visible(screen, ".q-menu"))

    def test_moving_between_links_shows_one_handle_at_a_time(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()
        with _camera_kept(screen):
            hover_scene_object(screen, "link:L2")
            _wait(lambda: js(screen, _DIALS_SHOWN) == ["jog:dial:1"], 5.0, "J2's ring")
            hover_scene_object(screen, "link:L3")
            _wait(
                lambda: js(screen, _DIALS_SHOWN) == ["jog:dial:2"],
                5.0,
                "J3's ring, and J2's gone",
            )

    def test_the_rotate_gizmo_turns_the_tool_and_leaves_the_camera(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()

        def rpy() -> np.ndarray:
            pose = waldoctl.commander.status.pose
            return np.array([pose.rx, pose.ry, pose.rz], dtype=float)

        click_marked(screen, "gizmo-mode-rotate")
        try:
            with _camera_kept(screen):
                hover_scene_object(screen, "link:L6")
                deadline = time.monotonic() + 5.0
                while not (
                    spot := next(
                        (s for a in "ZXY" if (s := js(screen, _RING_SPOT, a))), None
                    )
                ):
                    assert time.monotonic() < deadline, "no ring of the gizmo is free"
                    time.sleep(0.1)
                x, y, dx, dy = spot
                start = rpy()
                camera = js(screen, _CAMERA)
                _drag_along(screen, x, y, dx, dy)
                _wait(
                    lambda: np.abs(rpy() - start).max() > 2.0,
                    15.0,
                    "the tool to turn",
                )
                assert js(screen, _CAMERA) == pytest.approx(camera, abs=1e-6), (
                    "the drag orbited the camera"
                )
        finally:
            click_marked(screen, "gizmo-mode-move")

    def test_orbit_wheel_and_reset_move_the_camera_and_its_snap(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        urdf = ui_state.urdf_scene
        assert urdf is not None
        default = [
            DEFAULT_CAMERA["x"],
            DEFAULT_CAMERA["y"],
            DEFAULT_CAMERA["z"],
            0.0,
            0.0,
            DEFAULT_CAMERA["look_at_z"],
        ]
        with _camera_kept(screen):
            click_marked(screen, "reset-camera")
            _wait(
                lambda: js(screen, _CAMERA) == pytest.approx(default, abs=1e-6),
                5.0,
                "the default view",
            )
            _wait(lambda: urdf.snap.joint_deg == 1.0, 5.0, "the 1° band")

            x, y = _wait(lambda: js(screen, _EMPTY_SPOT), 5.0, "empty space")
            _drag_along(screen, x, y, 1, 0, 80)
            orbited = js(screen, _CAMERA)
            assert orbited[:3] != pytest.approx(default[:3], abs=1e-3), "no orbit"
            assert orbited[3:] == pytest.approx(default[3:], abs=1e-6), (
                "orbiting moved the target"
            )

            canvas = scene_canvas(screen)
            deadline = time.monotonic() + 10.0
            while urdf.snap.joint_deg != 5.0:
                assert time.monotonic() < deadline, "zooming out never reached 5°"
                ActionChains(screen.selenium).scroll_from_origin(
                    ScrollOrigin.from_element(canvas), 0, 200
                ).perform()
                time.sleep(0.2)

            click_marked(screen, "reset-camera")
            _wait(
                lambda: js(screen, _CAMERA) == pytest.approx(default, abs=1e-6),
                5.0,
                "the default view again",
            )
            _wait(lambda: urdf.snap.joint_deg == 1.0, 5.0, "the 1° band again")

    def test_a_ring_drag_keeps_turning_the_joint_off_the_ring(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()
        urdf = ui_state.urdf_scene
        assert urdf is not None
        with _camera_kept(screen):
            js(screen, _ZOOM, 1.3)
            _wait(lambda: urdf.snap.joint_deg == 5.0, 1.0, "the 5° band")
            hover_scene_object(screen, "link:L2")
            dial = _wait(lambda: js(screen, _DIAL, "jog:dial:1:knob"), 5.0, "J2's ring")
            start = _settled(1)
            grab = js(screen, _GRAB, "jog:dial:1", dial["q"], dial["r"])
            assert grab is not None, "no part of J2's ring is uncovered"
            # Grab the ring, then sweep 30° around it at 1.6 times its radius,
            # well off the ring: the captured pointer keeps turning the joint.
            angles = [dial["q"] + math.radians(grab + a) for a in range(0, 31, 3)]
            points = [
                [dial["r"] * math.cos(angles[0]), dial["r"] * math.sin(angles[0]), 0.0]
            ]
            points += [
                [1.6 * dial["r"] * math.cos(t), 1.6 * dial["r"] * math.sin(t), 0.0]
                for t in angles[1:]
            ]
            path = project_local(screen, "jog:dial:1", points)
            assert path is not None
            actions = ActionChains(screen.selenium, duration=20)
            pointer_to(screen, path[0][0], path[0][1], actions)
            actions.click_and_hold()
            for px, py in path[1:]:
                pointer_to(screen, px, py, actions)
            actions.release()
            actions.perform()
            _wait(lambda: abs(_joint(1) - start) > 4.0, 15.0, "J2 to move")
            moved = abs(_settled(1) - start)
            assert moved >= 15.0, f"J2 stopped at {moved:.1f}° off the ring"

    def test_a_target_is_placed_edited_and_deleted_from_the_scene(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        _teleport_to_jog_pose()
        urdf = ui_state.urdf_scene
        assert urdf is not None

        def lines() -> list[str]:
            return run_in_app(lambda: ui_state.active_textarea.value).splitlines()

        def editing_angles() -> list[float]:
            return run_in_app(lambda: list(urdf._editing_angles))

        def drag_the_gizmo_and_confirm() -> None:
            _wait(lambda: js(screen, _GIZMO_SNAP, False), 10.0, "the editing gizmo")
            start = editing_angles()
            x, y, dx, dy = _wait(
                lambda: js(screen, _ARROW_TIP, "tcp:ball"), 5.0, "the X arrow"
            )
            _drag_along(screen, x, y, dx, dy)
            _wait(lambda: editing_angles() != start, 10.0, "the arm to follow")
            click_marked(screen, "edit-bar-confirm")
            _wait(
                lambda: not run_in_app(lambda: urdf._editing_unified_target),
                5.0,
                "the edit to end",
            )

        def target_on(line_number: int) -> str:
            def find() -> str | None:
                tab = waldoctl.commander.programs.active
                assert tab is not None
                for target in tab.dry_run.targets:
                    if target.line_number == line_number and not target.id.startswith(
                        "pending_"
                    ):
                        return f"targetgroup:{target.id}"
                return None

            group = _wait(
                lambda: run_in_app(find), 15.0, f"a target on line {line_number}"
            )
            _wait(lambda: screen_get_scene_object(screen, group), 15.0, group)
            return group

        with _camera_kept(screen), _program_previewed(_TARGET_PROGRAM):
            try:
                before = lines()

                # Place a target at the arm, drag it away, and keep it.
                x, y = _wait(lambda: js(screen, _EMPTY_SPOT), 5.0, "empty space")
                _choose(screen, x, y, "Place Target at Robot Position...")
                drag_the_gizmo_and_confirm()
                after = _wait(
                    lambda: len(now := lines()) == len(before) + 1 and now,
                    10.0,
                    "a new line",
                )
                (added,) = [i for i, line in enumerate(after) if line not in before]
                assert "move_l(" in after[added], after[added]

                # Edit it: dragging its gizmo rewrites its line.
                group = target_on(added + 1)
                _choose(screen, *scene_object_pixel(screen, group), "Edit Target...")
                drag_the_gizmo_and_confirm()
                _wait(
                    lambda: lines()[added] != after[added], 10.0, "the line to change"
                )
                assert "move_l(" in lines()[added], lines()[added]

                # Escape in the code editor is the editor's own; anywhere else
                # it cancels a new placement, and neither stops the arm.
                _choose(screen, x, y, "Place Target at Robot Position...")
                _wait(lambda: js(screen, _GIZMO_SNAP, False), 10.0, "the editing gizmo")
                stops = motion_guard.stop_generation
                click_tab(screen, "program")
                try:
                    editor = focus_editor(screen)
                    _wait(
                        lambda: keybindings_manager.editor_focused,
                        5.0,
                        "the editor to have the keyboard",
                    )
                    editor.send_keys(Keys.ESCAPE)
                    _caught_up()
                    assert run_in_app(lambda: urdf._editing_unified_target), (
                        "Escape in the editor cancelled the placement"
                    )
                finally:
                    close_panel(screen, "program-panel")
                defocus_editor(screen)
                send_global_key(screen, Keys.ESCAPE)
                _wait(
                    lambda: not run_in_app(lambda: urdf._editing_unified_target),
                    5.0,
                    "Escape to cancel",
                )
                _caught_up()
                assert motion_guard.stop_generation == stops, "Escape stopped the arm"
                assert len(lines()) == len(after), "the cancelled target was kept"

                # Delete it, straight from the menu.
                group = target_on(added + 1)
                _choose(screen, *scene_object_pixel(screen, group), "Delete Target")
                _wait(lambda: lines() == before, 10.0, "the line to go")
                # Its marker goes with it, not a second later with the next
                # preview, so it cannot be deleted twice.
                _wait(
                    lambda: screen_get_scene_object(screen, group) is None,
                    0.3,
                    "the marker to go",
                )
            finally:
                if run_in_app(lambda: urdf._editing_unified_target):
                    click_marked(screen, "edit-bar-cancel")

    def test_a_keep_out_is_placed_moved_and_deleted_from_the_scene(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        handle = waldoctl.commander.scene

        def shapes() -> dict[str, tuple[float, ...]]:
            return run_in_app(lambda: {s.name: tuple(s.pose) for s in handle.shapes})

        def gizmo_on(name: str) -> bool:
            return js(
                screen,
                f"const c = {_SCENE};"
                "for (const [id, o] of c.objects)"
                "  if (o.mesh && o.mesh.name === arguments[0]) return c.transform_controls.has(id);"
                "return false;",
                name,
            )

        before = shapes()
        with _camera_kept(screen):
            try:
                x, y, *floor = _wait(
                    lambda: js(screen, _FLOOR_SPOT), 5.0, "floor near the arm"
                )
                offered = _choose(screen, x, y, "Box Here...")
                assert any(i.startswith("Place Target Here") for i in offered), offered
                click_marked(screen, "shape-dialog-save")
                (name,) = _wait(
                    lambda: set(shapes()) - set(before), 5.0, "the new keep-out"
                )
                assert shapes()[name][:2] == pytest.approx(floor, abs=0.003), (
                    "the keep-out is not where the floor was clicked"
                )
                obj = f"shape:{name}"
                centre = _wait(lambda: js(screen, _CENTRE_PIXEL, obj), 10.0, obj)

                # Move it by its gizmo's arrow; the drop is its new pose.
                _choose(screen, *centre, "Move (drag arrows)")
                _wait(lambda: gizmo_on(obj), 5.0, "the keep-out's gizmo")
                start = shapes()[name]
                x, y, dx, dy = _wait(
                    lambda: js(screen, _ARROW_TIP, obj), 5.0, "the X arrow"
                )
                _drag_along(screen, x, y, dx, dy)
                _wait(
                    lambda: shapes()[name][0] != start[0], 10.0, "the keep-out to move"
                )
                assert shapes()[name][1:] == pytest.approx(start[1:], abs=1e-6)
                centre = _wait(lambda: js(screen, _CENTRE_PIXEL, obj), 10.0, obj)
                _choose(screen, *centre, "Stop Moving")
                _wait(lambda: not gizmo_on(obj), 5.0, "the gizmo to go")

                _choose(screen, *centre, "Delete Keep-out")
                click_marked(screen, "shape-delete-confirm")
                _wait(lambda: name not in shapes(), 5.0, "the keep-out to go")
            finally:
                run_in_app(
                    lambda: setattr(
                        handle, "shapes", [s for s in handle.shapes if s.name in before]
                    )
                )

    def test_held_object_follows_the_flange(self, class_screen: Screen) -> None:
        """A shape attached to the flange is parented to the last actuated
        link, so its world matrix in three.js is the Python FK of the pose
        times its flange offset, at every pose."""
        import parol6.PAROL6_ROBOT as model

        screen = class_screen
        screen_wait_for_scene_ready(screen)
        assert core.loop is not None
        local = (0.03, 0.02, 0.25, 0.2, -0.3, 0.4)

        async def prepare():
            client = waldoctl.commander.client
            assert await client.simulator(True) == 1
            assert await client.reset() == 1
            index = await client.home()
            assert await client.wait_command(index, timeout=15)
            index = await client.select_tool("NONE")
            assert await client.wait_command(index, timeout=5)
            world = await client.shapes()
            assert world is not None
            part = Box(name="held-part", x=0.045, y=0.07, z=0.1).attach(
                flange_pose=local,
                epoch=world.attachment_epoch,
            )
            assert await client.set_shapes([part]) == 1
            await waldoctl.commander.scene.refresh_from_backend()
            return np.radians(await client.angles())

        joints = asyncio.run_coroutine_threadsafe(prepare(), core.loop).result(30)
        scene = ui_state.urdf_scene
        assert scene is not None

        def at_pose(q):
            scene.set_appearance_mode(RobotAppearanceMode.EDITING)
            scene.set_editing_angles(q.tolist())

        try:
            for turn in (0, 0.3):
                q = joints.copy()
                q[0] += turn
                expected = model.robot.fkine(q) @ model._pose_to_matrix(local)
                run_in_app(lambda: at_pose(q))

                def matches(_driver):
                    actual = js(screen, _HELD_MATRIX)
                    return actual is not None and np.allclose(
                        np.asarray(actual).reshape((4, 4), order="F"),
                        expected,
                        atol=1e-6,
                    )

                wait(screen, 20).until(matches)
        finally:
            asyncio.run_coroutine_threadsafe(
                waldoctl.commander.client.set_shapes([]), core.loop
            ).result(10)
            run_in_app(lambda: scene.set_appearance_mode(RobotAppearanceMode.LIVE))

    def test_a_simulated_run_paints_its_path_contacts_and_com(
        self, class_screen: Screen
    ) -> None:
        """The predicted path, the contact arrows and the centre-of-mass marker
        are three.js geometry: whether they exist in Python says nothing about
        whether anything reaches the canvas. The installed backend plans and
        does not simulate, so the commanded/predicted pair is injected, shaped
        as a backend produces it; everything downstream is the shipping code."""
        screen = class_screen
        screen_wait_for_scene_ready(screen)
        saved_view: dict[str, bool] = {}

        def populate() -> waldoctl.TickIndex:
            from waldo_commander.services.preview_segments import segments_from_record
            from waldo_commander.state import simulation_state

            view = waldoctl.commander.settings.view
            saved_view.update({f: getattr(view, f) for f in _VIEW_FLAGS})
            for flag in _VIEW_FLAGS:
                setattr(view, flag, True)
            program = waldoctl.commander.programs.active
            assert program is not None
            # A predicted pass always answers a plan, so a predicted record on
            # screen always has the commanded one and its segments beside it.
            commanded, predicted = _records(waldoctl.commander.status.joints.angles.rad)
            program.dry_run.commanded = commanded
            program.dry_run.commanded_revision = 1
            program.dry_run.predicted = predicted
            program.dry_run.predicted_revision = 1
            program.dry_run.path_segments = segments_from_record(commanded, [])
            program.dry_run.total_steps = 1
            simulation_state.notify_changed()
            return predicted

        def seek(ticks: waldoctl.TickIndex) -> None:
            from waldo_commander.components.playback import playback

            # The frame annotations ride the playback batch, so put playback
            # inside the contact window through the real seek path.
            playback.invalidate_timeline()
            playback.update_scrub_segments()
            assert playback._ensure_timeline() is not None, "no timeline to seek in"
            playback._apply_time(ticks.duration_s * 0.5)

        def restore() -> None:
            """No record, no segments, no timeline, and the view settings as
            they were: a record left behind would be replayed by the next test
            that touches playback, and the view flags are process-wide."""
            from waldo_commander.components.playback import playback
            from waldo_commander.state import simulation_state

            view = waldoctl.commander.settings.view
            for flag, value in saved_view.items():
                setattr(view, flag, value)
            program = waldoctl.commander.programs.active
            if program is not None:
                program.dry_run.commanded = None
                program.dry_run.commanded_revision = -1
                program.dry_run.predicted = None
                program.dry_run.predicted_revision = -1
                program.dry_run.path_segments = []
                program.dry_run.total_steps = 0
                program.dry_run.playback.playback_time = 0.0
            playback.invalidate_timeline()
            simulation_state.notify_changed()

        def overlay() -> dict | None:
            return js(screen, _OVERLAY_JS)

        with window_size(screen, 1280, 900):
            # The scrub bar and its playback controls live on the program tab,
            # and seeking is what drives the per-frame annotations.
            click_tab(screen, "program")
            ticks = run_in_app(populate)
            try:
                try:
                    wait(screen, 15).until(
                        lambda _: (i := overlay())
                        and i.get("found")
                        and i.get("vertexColored")
                    )
                except TimeoutException:
                    pass
                run_in_app(lambda: seek(ticks))
                try:
                    wait(screen).until(
                        lambda _: (i := overlay()) and i.get("shown", 0) >= 2
                    )
                except TimeoutException:
                    pass
                info = overlay()

                assert info is not None, "no three.js scene on the page"
                assert info.get("found"), (
                    "the physics overlay group never reached the scene"
                )
                assert info["vertexColored"] >= 1, (
                    f"the predicted path must carry per-vertex colours — a uniform "
                    f"colour shows no following error at all: {info}"
                )
                assert info["points"] >= 40, f"the path is missing rows: {info}"
                assert info["meshes"] >= 2, (
                    f"the per-frame pool was never created: {info}"
                )
                assert info["shown"] >= 2, (
                    f"expected contact arrows and the centre-of-mass marker to be "
                    f"shown at a frame that has contacts: {info}"
                )
            finally:
                run_in_app(restore)
