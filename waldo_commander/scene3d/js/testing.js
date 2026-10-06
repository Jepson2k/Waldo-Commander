// What browser tests may see of the view: objects by name, where points
// land on screen, what a press at a pixel would grab, the handles shown,
// the camera, and the frame count. Tests reach the view through this
// surface only.

import * as THREE from "three";
import { GIZMO } from "./handles.js";

export function surface(core) {
  const { canvas } = core;
  const rect = () => canvas.getBoundingClientRect();
  const pixel = (v) => {
    const r = rect();
    return [r.left + ((v.x + 1) / 2) * r.width, r.top + ((1 - v.y) / 2) * r.height];
  };
  // A node, or an object the view itself drew (the gizmo, a ring, a label).
  const byName = (name) => {
    const rec = core.nodes.byName(name);
    if (rec) return rec.obj;
    return core.scene.getObjectByName(name) || null;
  };
  const nodeId = (name) => {
    const rec = core.nodes.byName(name);
    return rec ? rec.id : null;
  };
  const gizmoFor = (name) => {
    const target = byName(name);
    if (!target) return null;
    return core.gizmos.all().find((tc) => tc.object === target) || null;
  };
  const names = (o) => {
    const out = [];
    for (; o; o = o.parent) if (o.name) out.push(o.name);
    return out;
  };

  const S = {
    root: core.root,
    canvas,
    get scene() {
      return core.scene;
    },
    get camera() {
      return core.camera;
    },
    get controls() {
      return core.controls;
    },
    get renderer() {
      return core.renderer;
    },
    get viewHelper() {
      return core.inset.helper;
    },
    get resets() {
      return core.resets;
    },
    byName,
    frames: () => core.frameCount,
    requestRender: () => core.requestRender(),
    idle: () => core.nodes.idle(),
    project(name, points) {
      const o = byName(name);
      if (!o) return null;
      o.updateWorldMatrix(true, false);
      const v = new THREE.Vector3();
      return points.map(([x, y, z]) => pixel(v.set(x, y, z).applyMatrix4(o.matrixWorld).project(core.camera)));
    },
    pixelOf(name) {
      const o = byName(name);
      if (!o) return null;
      const [px, py] = pixel(o.getWorldPosition(new THREE.Vector3()).project(core.camera));
      return document.elementFromPoint(px, py) === canvas ? [px, py] : null;
    },
    // What a press at (px, py) would grab: whether something covers the
    // canvas there, the names from the first object the pointer acts on up
    // to the root, and every gizmo handle under it.
    pick(px, py) {
      const covered = document.elementFromPoint(px, py) !== canvas;
      const hit = core.pointer.hit(px, py);
      const handles = core.pointer.handlesAt(px, py).map(({ tc, axis }) => ({ name: tc.object.name, axis }));
      return { covered, names: hit ? names(hit.object) : [], handles };
    },
    ground(px, py) {
      const ray = core.pointer.aim(px, py).ray;
      const k = -ray.origin.z / ray.direction.z;
      return [ray.origin.x + k * ray.direction.x, ray.origin.y + k * ray.direction.y];
    },
    handles() {
      const shown = [];
      if (core.pointer.ring) shown.push(`ring:${core.pointer.ring.u}`);
      if (core.gizmos.gizmoShown) shown.push(GIZMO);
      return shown;
    },
    snap: () => ({ joint_deg: core.snap[0], cart_mm: core.snap[1] }),
    gizmo(name, mark) {
      const tc = gizmoFor(name);
      if (!tc) return null;
      if (mark) tc.wcProbe = true;
      return { mode: tc.mode, translationSnap: tc.translationSnap, rotationSnap: tc.rotationSnap, probe: !!tc.wcProbe };
    },
    dial(u) {
      const ring = core.pointer.ring;
      return ring && ring.u === u ? { q: ring.q, r: ring.spec.radius, lo: ring.spec.lo, hi: ring.spec.hi } : null;
    },
    label(name) {
      const o = byName(name);
      return o && o.element ? o.element.textContent : null;
    },
    glowing(name) {
      const g = core.pointer.glowing;
      return !!(g && g.rec.name === name && g.group.parent && g.group.children.length);
    },
    framing() {
      const cam = core.camera;
      const view = cam.view;
      return {
        view: view
          ? { enabled: view.enabled, fullWidth: view.fullWidth, fullHeight: view.fullHeight, offsetY: view.offsetY }
          : null,
        aspect: cam.aspect,
        inset: { ...core.layoutInset },
      };
    },
    cameraPose: () => [...core.camera.position.toArray(), ...core.controls.target.toArray()],
    setCameraPose([px, py, pz, tx, ty, tz]) {
      core.camera.position.set(px, py, pz);
      core.controls.target.set(tx, ty, tz);
      core.controls.update();
    },
    zoom(distance) {
      const t = core.controls.target;
      core.camera.position.sub(t).setLength(distance).add(t);
      core.controls.update();
    },
    viewDirection: () => core.camera.position.clone().sub(core.controls.target).normalize().toArray(),
    fx: {
      alarm(names, color) {
        core.fx.run("alarm", [names.map(nodeId).filter((id) => id !== null), color]);
      },
    },
  };

  // Probes: a pixel where a press grabs what is asked for and nothing else.

  S.hoverPixel = (name) => {
    const target = byName(name);
    if (!target) return null;
    target.updateWorldMatrix(true, true);
    const v = new THREE.Vector3();
    const center = new THREE.Vector3();
    let best = null;
    target.traverse((m) => {
      if (best || !m.isMesh || !m.geometry || !m.geometry.attributes.position) return;
      m.geometry.computeBoundingSphere();
      center.copy(m.geometry.boundingSphere.center).applyMatrix4(m.matrixWorld);
      const pos = m.geometry.attributes.position;
      const stride = Math.max(1, Math.floor(pos.count / 400));
      for (let i = 0; i < pos.count && !best; i += stride) {
        v.fromBufferAttribute(pos, i).applyMatrix4(m.matrixWorld).lerp(center, 0.25).project(core.camera);
        if (Math.abs(v.x) > 0.95 || Math.abs(v.y) > 0.95) continue;
        const [px, py] = pixel(v);
        const hit = S.pick(px, py);
        if (!hit.covered && !hit.handles.length && hit.names.includes(name)) best = [px, py];
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
    const p = new THREE.Vector3();
    for (let rad = 0.25; rad <= 0.45; rad += 0.05) {
      for (let a = 0; a < 360; a += 15) {
        const t = (a * Math.PI) / 180;
        const [fx, fy] = pixel(p.set(rad * Math.cos(t), rad * Math.sin(t), 0).project(core.camera));
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
    const tc = gizmoFor(name);
    if (!tc || !tc.object) return null;
    const obj = tc.object;
    obj.updateWorldMatrix(true, false);
    const origin = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
    const axis = new THREE.Vector3(1, 0, 0).applyQuaternion(obj.getWorldQuaternion(new THREE.Quaternion()));
    const toPixel = (d) => {
      const v = origin.clone().addScaledVector(axis, d).project(core.camera);
      return [v.x, v.y, ...pixel(v)];
    };
    const outside = [];
    for (let d = 0.002; d < 0.5; d += 0.002) {
      const [x, y, px, py] = toPixel(d);
      if (Math.abs(x) > 0.95 || Math.abs(y) > 0.95) continue;
      const hit = S.pick(px, py);
      if (hit.covered || hit.names.length) continue;
      if (hit.handles.length !== 1 || hit.handles[0].name !== name || hit.handles[0].axis !== "X") continue;
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
    const tc = core.gizmos.tcp;
    if (!tc.object || tc.mode !== "rotate") return null;
    const [cx, cy] = pixel(tc.object.getWorldPosition(new THREE.Vector3()).project(core.camera));
    for (let rad = 20; rad <= 200; rad += 4) {
      for (let a = 0; a < 360; a += 5) {
        const t = (a * Math.PI) / 180;
        const px = cx + rad * Math.cos(t);
        const py = cy + rad * Math.sin(t);
        const hit = S.pick(px, py);
        if (hit.covered || hit.names.length) continue;
        if (!hit.handles.some((h) => h.name === "tcp:ball" && h.axis === axis)) continue;
        return [px, py, -Math.sin(t), Math.cos(t)];
      }
    }
    return null;
  };

  // A pixel on the rotate control of edit joint `index`, past the arm, and
  // the control's direction on screen there.
  S.jointSpot = (index) => {
    const entry = [...core.gizmos.joints.values()].find((j) => j.index === index);
    if (!entry || !entry.tc.object) return null;
    const [cx, cy] = pixel(entry.tc.object.getWorldPosition(new THREE.Vector3()).project(core.camera));
    for (let rad = 20; rad <= 240; rad += 4) {
      for (let a = 0; a < 360; a += 5) {
        const t = (a * Math.PI) / 180;
        const px = cx + rad * Math.cos(t);
        const py = cy + rad * Math.sin(t);
        if (document.elementFromPoint(px, py) !== canvas) continue;
        const handles = core.pointer.handlesAt(px, py);
        if (handles.length !== 1 || handles[0].tc !== entry.tc) continue;
        if (core.pointer.hit(px, py)) continue;
        return [px, py, -Math.sin(t), Math.cos(t)];
      }
    }
    return null;
  };

  // Pixels where something covers the canvas, each with the angle (radians)
  // at which a pointer there meets the plane of joint u's shown ring.
  S.coveredSpots = (u) => {
    const ring = core.pointer.ring;
    if (!ring || ring.u !== u) return null;
    const out = [];
    for (let y = 8; y < innerHeight - 4; y += 24) {
      for (let x = 8; x < innerWidth - 4; x += 24) {
        if (document.elementFromPoint(x, y) === canvas) continue;
        const at = ring.onPlane(core.pointer.aim(x, y));
        if (at && Math.hypot(at[0], at[1]) > ring.spec.radius) out.push([x, y, Math.atan2(at[1], at[0])]);
      }
    }
    return out;
  };

  // The first angle from joint u's knob, in degrees, where a press grabs its
  // ring; or, with `why`, what stops each angle from being a grip.
  S.ringGrab = (u, why) => {
    const dial = S.dial(u);
    const ring = byName(`jog:dial:${u}`);
    if (!dial || !ring) return why ? "no dial" : null;
    ring.updateWorldMatrix(true, true);
    const v = new THREE.Vector3();
    const seen = {};
    for (let a = 0; a < 360; a += 10) {
      const t = dial.q + (a * Math.PI) / 180;
      const [px, py] = pixel(v.set(dial.r * Math.cos(t), dial.r * Math.sin(t), 0).applyMatrix4(ring.matrixWorld).project(core.camera));
      const hit = S.pick(px, py);
      const grabs = !hit.covered && !hit.handles.length && hit.names.includes(`jog:dial:${u}`);
      if (!why && grabs) return a;
      if (why) {
        const el = document.elementFromPoint(px, py);
        const k = hit.covered
          ? "under " + (el ? String(el.className || el.tagName).slice(0, 40) : "nothing")
          : "hits " + (hit.names[0] || (hit.handles[0] && hit.handles[0].name) || "nothing");
        seen[k] = (seen[k] || 0) + 1;
      }
    }
    if (!why) return null;
    seen.dialog = !!document.querySelector(".q-dialog");
    return seen;
  };

  // A pixel on the orientation inset's sprite for the level axis that faces
  // the viewer most, and that axis.
  S.insetAxis = () => {
    const vh = core.inset.helper;
    if (!vh) return null;
    const r = rect();
    const loc = vh.location;
    const dim = 128;
    const left = r.left + (loc.left !== null ? loc.left : canvas.offsetWidth - dim - loc.right);
    const top = r.top + (loc.top !== null ? loc.top : canvas.offsetHeight - dim - loc.bottom);
    const toInset = core.camera.quaternion.clone().invert();
    let best = null;
    for (const axis of [
      [1, 0, 0],
      [-1, 0, 0],
      [0, 1, 0],
      [0, -1, 0],
    ]) {
      const v = new THREE.Vector3(...axis).applyQuaternion(toInset);
      const x = left + ((v.x / 2 + 1) / 2) * dim;
      const y = top + ((1 - v.y / 2) / 2) * dim;
      if (document.elementFromPoint(x, y) !== canvas) continue;
      if (!best || v.z > best.z) best = { x, y, z: v.z, axis };
    }
    return best && [best.x, best.y, best.axis];
  };

  return S;
}
