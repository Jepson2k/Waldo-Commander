"""The 3D scene as browser tests see it.

Every test reaches the scene through ``S``, a small surface over the scene
component: its three.js objects by name, where they land on screen, what a
pointer at a pixel would grab, the camera, and probes that find a pixel where
a press grabs a given handle. Tests never touch the component's internals.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

from tests.helpers.browser_helpers import js

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

SCENE_ROOT = ".nicegui-scene"
SCENE_CANVAS = f"{SCENE_ROOT} canvas"

_SURFACE = r"""
if (!window.__wcSurface) window.__wcSurface = function () {
  const root = document.querySelector('.nicegui-scene');
  const c = root && getElement(root);
  if (!c || !c.renderer || !c.objects) return null;
  if (c.__wcSurface) return c.__wcSurface;
  const canvas = c.renderer.domElement;
  const byName = (name) => {
    for (const o of c.objects.values()) if (o.mesh && o.mesh.name === name) return o.mesh;
    return null;
  };
  const idOf = (name) => {
    for (const [id, o] of c.objects) if (o.mesh && o.mesh.name === name) return id;
    return null;
  };
  const gizmos = () => [...c.transform_controls.values()];
  const gizmoOf = (name) => {
    const id = idOf(name);
    return id === null ? null : c.transform_controls.get(id) || null;
  };
  const rect = () => canvas.getBoundingClientRect();
  const pixel = (v) => {
    const r = rect();
    return [r.left + (v.x + 1) / 2 * r.width, r.top + (1 - v.y) / 2 * r.height];
  };
  const ndc = (px, py) => {
    const r = rect();
    return { x: (px - r.left) / r.width * 2 - 1, y: 1 - (py - r.top) / r.height * 2 };
  };
  const names = (o) => {
    const out = [];
    for (; o; o = o.parent) if (o.name) out.push(o.name);
    return out;
  };
  const S = {
    root,
    canvas,
    get scene() { return c.scene; },
    get camera() { return c.camera; },
    get controls() { return c.controls; },
    get renderer() { return c.renderer; },
    get viewHelper() { return c.viewHelper; },
    byName,
    frames: () => c.renderer.info.render.frame,
    requestRender: () => c.request_render(),
    idle: () => Promise.all([...c.objects.values()].map((o) => o.ready_promise).filter(Boolean)),
    project(name, points) {
      const o = byName(name);
      if (!o) return null;
      o.updateWorldMatrix(true, false);
      const v = o.position.clone();
      return points.map(([x, y, z]) => pixel(v.set(x, y, z).applyMatrix4(o.matrixWorld).project(c.camera)));
    },
    pixelOf(name) {
      const o = byName(name);
      if (!o) return null;
      const [px, py] = pixel(o.getWorldPosition(o.position.clone()).project(c.camera));
      return document.elementFromPoint(px, py) === canvas ? [px, py] : null;
    },
    // What a press at (px, py) would grab: whether something covers the
    // canvas there, the names from the first interactive object hit up to the
    // root, and every gizmo handle under the pointer.
    pick(px, py) {
      const covered = document.elementFromPoint(px, py) !== canvas;
      const p = ndc(px, py);
      c._raycaster.setFromCamera(p, c.camera);
      const hits = c._raycaster.intersectObjects(c.interactiveObjects, true);
      const handles = [];
      for (const tc of gizmos()) {
        if (!tc.object) continue;
        tc.pointerHover({ x: p.x, y: p.y, button: 0 });
        if (tc.axis !== null) handles.push({ name: tc.object.name, axis: tc.axis });
        tc.pointerHover({ x: 2, y: 2, button: 0 });
      }
      return { covered, names: hits.length ? names(hits[0].object) : [], handles };
    },
    ground(px, py) {
      c._raycaster.setFromCamera(ndc(px, py), c.camera);
      const ray = c._raycaster.ray;
      const k = -ray.origin.z / ray.direction.z;
      return [ray.origin.x + k * ray.direction.x, ray.origin.y + k * ray.direction.y];
    },
    handles() {
      const shown = [];
      for (const o of c.objects.values()) {
        const m = o.mesh && /^jog:dial:(\d+)$/.exec(o.mesh.name);
        if (m && o.mesh.parent && o.mesh.visible) shown.push('ring:' + m[1]);
      }
      if (gizmoOf('tcp:ball')) shown.push('gizmo');
      return shown;
    },
    gizmo(name, mark) {
      const tc = gizmoOf(name);
      if (!tc) return null;
      if (mark) tc.wcProbe = true;
      return { mode: tc.mode, translationSnap: tc.translationSnap, rotationSnap: tc.rotationSnap, probe: !!tc.wcProbe };
    },
    dial(u) {
      const knob = byName(`jog:dial:${u}:knob`);
      const sphere = knob && knob.children.find((m) => m.isMesh);
      return sphere ? { q: knob.rotation.z, r: sphere.position.x } : null;
    },
    glowing(name) {
      const id = idOf(name);
      const a = id === null ? null : c.effectArtifacts.get(id);
      return !!(a && a.effect === 'glow' && a.group.parent && a.group.children.length);
    },
    framing() {
      const cam = window.SceneFraming && SceneFraming.camera();
      if (!cam) return null;
      const view = cam.view;
      return {
        view: view ? { enabled: view.enabled, fullWidth: view.fullWidth, fullHeight: view.fullHeight, offsetY: view.offsetY } : null,
        aspect: cam.aspect,
        inset: SceneFraming.getInset(),
      };
    },
    cameraPose: () => [...c.camera.position.toArray(), ...c.controls.target.toArray()],
    setCameraPose([px, py, pz, tx, ty, tz]) {
      c.camera.position.set(px, py, pz);
      c.controls.target.set(tx, ty, tz);
      c.controls.update();
    },
    zoom(distance) {
      const t = c.controls.target;
      c.camera.position.sub(t).setLength(distance).add(t);
    },
    viewDirection: () => c.camera.position.clone().sub(c.controls.target).normalize().toArray(),
    fx: {
      alarm(names, color) {
        SceneFx.alarm(Number(root.id.slice(1)), names.map((n) => byName(n).object_id), color);
      },
    },
  };

  // Probes: a pixel where a press grabs what is asked for and nothing else.

  S.hoverPixel = (name) => {
    const target = byName(name);
    if (!target) return null;
    target.updateWorldMatrix(true, true);
    const v = target.position.clone();
    const center = target.position.clone();
    let best = null;
    target.traverse((m) => {
      if (best || !m.isMesh || !m.geometry || !m.geometry.attributes.position) return;
      m.geometry.computeBoundingSphere();
      center.copy(m.geometry.boundingSphere.center).applyMatrix4(m.matrixWorld);
      const pos = m.geometry.attributes.position;
      const stride = Math.max(1, Math.floor(pos.count / 400));
      for (let i = 0; i < pos.count && !best; i += stride) {
        v.fromBufferAttribute(pos, i).applyMatrix4(m.matrixWorld).lerp(center, 0.25).project(c.camera);
        if (Math.abs(v.x) > 0.95 || Math.abs(v.y) > 0.95) continue;
        const [px, py] = pixel(v);
        const hit = S.pick(px, py);
        if (!hit.covered && hit.names.includes(name)) best = [px, py];
      }
    });
    return best;
  };

  S.emptySpot = () => {
    const r = rect();
    for (let fy = 0.3; fy <= 0.7; fy += 0.1) {
      for (let fx = 0.2; fx <= 0.8; fx += 0.1) {
        const px = r.left + fx * r.width;
        const py = r.top + fy * r.height;
        const hit = S.pick(px, py);
        if (!hit.covered && !hit.names.length && !hit.handles.length) return [px, py];
      }
    }
    return null;
  };

  S.floorSpot = () => {
    const p = c.camera.position.clone();
    for (let rad = 0.25; rad <= 0.45; rad += 0.05) {
      for (let a = 0; a < 360; a += 15) {
        const t = a * Math.PI / 180;
        const [fx, fy] = pixel(p.set(rad * Math.cos(t), rad * Math.sin(t), 0).project(c.camera));
        const px = Math.round(fx);
        const py = Math.round(fy);
        const hit = S.pick(px, py);
        if (hit.covered || hit.names.length || hit.handles.length) continue;
        return [px, py, ...S.ground(px, py)];
      }
    }
    return null;
  };

  // The middle of the stretch of the named object's X arrow that sticks out
  // past every object, and the arrow's direction on screen.
  S.arrowTip = (name) => {
    const tc = gizmoOf(name);
    if (!tc || !tc.object) return null;
    const obj = tc.object;
    obj.updateWorldMatrix(true, false);
    const origin = obj.position.clone().setFromMatrixPosition(obj.matrixWorld);
    const axis = obj.position.clone().set(1, 0, 0).applyQuaternion(obj.getWorldQuaternion(obj.quaternion.clone()));
    const toPixel = (d) => {
      const v = origin.clone().addScaledVector(axis, d).project(c.camera);
      return [v.x, v.y, ...pixel(v)];
    };
    const outside = [];
    for (let d = 0.002; d < 0.5; d += 0.002) {
      const [x, y, px, py] = toPixel(d);
      if (Math.abs(x) > 0.95 || Math.abs(y) > 0.95) continue;
      const hit = S.pick(px, py);
      if (hit.covered || hit.names.length) continue;
      if (hit.handles.length !== 1 || hit.handles[0].name !== name || hit.handles[0].axis !== 'X') continue;
      outside.push(d);
    }
    if (!outside.length) return null;
    const d = outside[Math.floor(outside.length / 2)];
    const [, , px, py] = toPixel(d);
    const [, , qx, qy] = toPixel(d + 0.01);
    const n = Math.hypot(qx - px, qy - py);
    return [px, py, (qx - px) / n, (qy - py) / n];
  };

  // A pixel on the rotate gizmo's ring about `axis`, past the tool, and the
  // ring's direction on screen there.
  S.ringSpot = (axis) => {
    const tc = gizmoOf('tcp:ball');
    if (!tc || !tc.object || tc.mode !== 'rotate') return null;
    const [cx, cy] = pixel(tc.object.getWorldPosition(tc.object.position.clone()).project(c.camera));
    for (let rad = 20; rad <= 200; rad += 4) {
      for (let a = 0; a < 360; a += 5) {
        const t = a * Math.PI / 180;
        const px = cx + rad * Math.cos(t);
        const py = cy + rad * Math.sin(t);
        const hit = S.pick(px, py);
        if (hit.covered || hit.names.length) continue;
        if (!hit.handles.some((h) => h.name === 'tcp:ball' && h.axis === axis)) continue;
        return [px, py, -Math.sin(t), Math.cos(t)];
      }
    }
    return null;
  };

  // The first angle from joint u's knob, in degrees, where a press grabs its
  // ring; or, with `why`, what stops each angle from being a grip.
  S.ringGrab = (u, why) => {
    const dial = S.dial(u);
    const ring = byName(`jog:dial:${u}`);
    if (!dial || !ring) return why ? 'no dial' : null;
    ring.updateWorldMatrix(true, true);
    const v = ring.position.clone();
    const seen = {};
    for (let a = 0; a < 360; a += 10) {
      const t = dial.q + a * Math.PI / 180;
      const [px, py] = pixel(v.set(dial.r * Math.cos(t), dial.r * Math.sin(t), 0).applyMatrix4(ring.matrixWorld).project(c.camera));
      const hit = S.pick(px, py);
      if (!why && !hit.covered && hit.names.includes(`jog:dial:${u}`)) return a;
      if (why) {
        const el = document.elementFromPoint(px, py);
        const k = hit.covered ? 'under ' + (el ? String(el.className || el.tagName).slice(0, 40) : 'nothing') : 'hits ' + (hit.names[0] || 'nothing');
        seen[k] = (seen[k] || 0) + 1;
      }
    }
    if (!why) return null;
    seen.dialog = !!document.querySelector('.q-dialog');
    return seen;
  };

  // A pixel on the orientation inset's sprite for the level axis that faces
  // the viewer most, and that axis.
  S.insetAxis = () => {
    const vh = c.viewHelper;
    if (!vh) return null;
    const r = rect();
    const loc = vh.location;
    const dim = 128;
    const left = r.left + (loc.left !== null ? loc.left : canvas.offsetWidth - dim - loc.right);
    const top = r.top + (loc.top !== null ? loc.top : canvas.offsetHeight - dim - loc.bottom);
    const toInset = c.camera.quaternion.clone().invert();
    let best = null;
    for (const axis of [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]]) {
      const v = c.camera.position.clone().set(...axis).applyQuaternion(toInset);
      const x = left + (v.x / 2 + 1) / 2 * dim;
      const y = top + (1 - v.y / 2) / 2 * dim;
      if (document.elementFromPoint(x, y) !== canvas) continue;
      if (!best || v.z > best.z) best = { x, y, z: v.z, axis };
    }
    return best && [best.x, best.y, best.axis];
  };

  return (c.__wcSurface = S);
};
"""

_PRELUDE = _SURFACE + "const S = window.__wcSurface();\nif (!S) return null;\n"


def with_surface(body: str) -> str:
    """``body`` as a page script with the scene's surface bound to ``S``."""
    return _PRELUDE + body


def scene_js(screen: Screen, body: str, *args: Any) -> Any:
    """Run ``body`` in the page with the scene's surface bound to ``S``; null
    while no scene is mounted."""
    return js(screen, with_surface(body), *args)


def scene_js_async(screen: Screen, body: str, *args: Any) -> Any:
    """``scene_js`` for a body that ends by calling ``done(value)``."""
    return screen.selenium.execute_async_script(
        "const done = arguments[arguments.length - 1];\n"
        + _SURFACE
        + "const S = window.__wcSurface();\nif (!S) { done(null); return; }\n"
        + body,
        *args,
    )


def scene_canvas(screen: Screen) -> WebElement:
    return screen.selenium.find_element(By.CSS_SELECTOR, SCENE_CANVAS)


def scene_object(screen: Screen, name: str) -> dict | None:
    """The named three.js object's type, visibility and position, or None."""
    return scene_js(
        screen,
        "const o = S.byName(arguments[0]);"
        "return o && {name: o.name, type: o.type, visible: o.visible,"
        " position: {x: o.position.x, y: o.position.y, z: o.position.z}};",
        name,
    )


def wait_for_scene_idle(screen: Screen) -> None:
    """Return once every object the scene has been sent is loaded and drawn."""
    scene_js_async(screen, "S.idle().then(() => done(true));")


def frames(screen: Screen) -> int:
    return scene_js(screen, "return S.frames()")


def frames_at_rest(screen: Screen, window_s: float, timeout: float = 15.0) -> int:
    """The scene's frame count once it has held for *window_s*."""
    deadline = time.monotonic() + timeout
    count = frames(screen)
    since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        now = frames(screen)
        if now != count:
            count, since = now, time.monotonic()
        elif time.monotonic() - since >= window_s:
            return count
    raise AssertionError(f"the scene kept drawing at rest ({count} frames)")


def pointer_to(screen: Screen, x: float, y: float, actions: ActionChains) -> None:
    """Queue a real pointer move to viewport point (x, y) on ``actions``."""
    canvas = scene_canvas(screen)
    rect = canvas.rect
    actions.move_to_element_with_offset(
        canvas,
        round(x - (rect["x"] + rect["width"] / 2)),
        round(y - (rect["y"] + rect["height"] / 2)),
    )


def project_local(
    screen: Screen, name: str, points: list[list[float]]
) -> list[list[float]] | None:
    """Viewport pixels of ``points`` given in the frame of the scene object
    ``name``, wherever they land."""
    return scene_js(
        screen, "return S.project(arguments[0], arguments[1])", name, points
    )


def scene_object_pixel(
    screen: Screen, name: str, timeout: float = 20.0
) -> tuple[float, float]:
    """A viewport pixel where ``name`` is the first thing a press would hit."""
    deadline = time.monotonic() + timeout
    while True:
        pixel = scene_js(screen, "return S.hoverPixel(arguments[0])", name)
        if pixel is not None:
            return pixel[0], pixel[1]
        if time.monotonic() > deadline:
            raise AssertionError(f"no pixel of {name!r} is hit first on the canvas")
        time.sleep(0.2)


def hover_scene_object(screen: Screen, name: str, timeout: float = 20.0) -> None:
    """Rest the real mouse on ``name``, so the scene reports it as hovered."""
    x, y = scene_object_pixel(screen, name, timeout)
    actions = ActionChains(screen.selenium, duration=0)
    pointer_to(screen, x, y, actions)
    actions.perform()


def shown_handles(screen: Screen) -> list[str]:
    """The jog handles on screen: ``ring:<joint>`` and ``gizmo``."""
    return scene_js(screen, "return S.handles()") or []


def snap_deg(screen: Screen) -> float:
    """How far a joint ring snaps, in degrees, at the camera's distance."""
    from tests.helpers.browser_helpers import run_in_app
    from waldo_commander.state import ui_state

    return run_in_app(lambda: ui_state.urdf_scene.snap.joint_deg)
