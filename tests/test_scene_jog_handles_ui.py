"""The 3D view's jog handles in a real browser: real pointer events, real camera."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

import pytest
import waldoctl
from nicegui import core
from selenium.webdriver.common.action_chains import ActionChains

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import (
    hover_scene_object,
    js,
    pointer_to,
    project_local,
)
from tests.helpers.wait import JOG_SAFE_POSE_DEG, screen_wait_for_scene_ready
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


@pytest.mark.browser
@skip_webgl_macos_ci
class TestSceneJogHandles:
    """Ring and gizmo in one browser session."""

    def test_ring_drag_moves_the_joint_by_whole_steps_and_leaves_the_camera(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        assert core.loop is not None
        client = ui_state.control_panel.client
        asyncio.run_coroutine_threadsafe(
            client.teleport(JOG_SAFE_POSE_DEG), core.loop
        ).result(10)
        _wait(lambda: abs(_joint(1) - JOG_SAFE_POSE_DEG[1]) < 0.5, 10.0, "teleport")

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

        # Grab the ring where nothing covers it and sweep 21° around it in 3°
        # moves; a drag turns the joint by how far it goes, not where it starts.
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

    def test_gizmo_snap_follows_the_zoom_without_reattaching(
        self, class_screen: Screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        urdf = ui_state.urdf_scene
        assert urdf is not None
        js(screen, _ZOOM, 1.3)
        _wait(lambda: urdf.snap.joint_deg == 5.0, 1.0, "the 5° band")
        hover_scene_object(screen, "link:L6")
        snap = _wait(lambda: js(screen, _GIZMO_SNAP, True), 10.0, "the gizmo")
        assert snap["t"] == pytest.approx(0.010) and snap["r"] == pytest.approx(
            math.radians(5.0)
        )

        js(screen, _ZOOM, 0.4)
        snap = _wait(
            lambda: (s := js(screen, _GIZMO_SNAP, False))
            and s["t"] == pytest.approx(0.001)
            and s,
            2.0,
            "the 1 mm band on the gizmo",
        )
        assert snap["r"] == pytest.approx(math.radians(0.5))
        assert snap["probe"], "the gizmo was re-attached instead of updated"
