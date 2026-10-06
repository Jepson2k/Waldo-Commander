// The pointer over the view: hovering a link shows its joint's ring and the
// tool shows the gizmo, a target glows under the pointer, a ring grabbed is
// dragged with the pointer captured, and a touch tap pins a handle.
//
// Only what the pointer can act on is hit-tested: the links and the tool,
// the shown ring's grip and the gizmo, and nodes that glow. A drag belongs to
// the pointer that started it; other pointers do not reach the controls.

import * as THREE from "three";
import { GIZMO, Hover, RingDrag, panelDegrees, snapFor } from "./handles.js";
import { Ring } from "./rings.js";

const TAP_PX = 6;

export class Pointer {
  constructor(core) {
    this.core = core;
    this.raycaster = new THREE.Raycaster();
    this.hovered = null; // the hover source under the mouse
    this.glowing = null; // {rec, group}
    this.ring = null;
    this.ringDrag = null; // {g, drag: RingDrag, pointerId}
    this.capturing = false;
    this.pressed = new Set(); // pointers down on the canvas
    this.lastPointerId = null;
    this.taps = new Map(); // pointerId -> {x, y, suppressed}
    this.graceTimer = 0;
    this.hover = new Hover({
      allowed: (t) => this.allowed(t),
      show: (t) => this.show(t),
      hide: (t) => this.hide(t),
      dragging: () => this.core.gestures.active,
      graceMs: 200,
    });
    const canvas = core.canvas;
    this.listeners = [
      [canvas, "pointerdown", (e) => this.gate(e), { capture: true }],
      [canvas, "pointermove", (e) => this.gate(e), { capture: true }],
      [canvas, "pointerup", (e) => this.gate(e), { capture: true }],
      [canvas, "pointercancel", (e) => this.gate(e), { capture: true }],
      [canvas, "pointerdown", (e) => this.down(e), { capture: true }],
      [core.root, "pointermove", (e) => this.move(e)],
      [canvas, "pointerleave", () => this.leaveCanvas()],
      [window, "pointerup", (e) => this.up(e)],
      [window, "pointercancel", (e) => this.cancel(e)],
      [window, "pointermove", (e) => this.dragMove(e)],
      [canvas, "lostpointercapture", (e) => this.lostCapture(e)],
      [window, "pointerup", (e) => this.lift(e)],
      [window, "pointercancel", (e) => this.lift(e)],
      [window, "pointermove", (e) => e.pointerType === "mouse" && e.buttons === 0 && this.lift(e)],
      [window, "blur", () => this.lift(null)],
    ];
    for (const [target, type, fn, opts] of this.listeners) target.addEventListener(type, fn, opts);
  }

  // ---- rules ------------------------------------------------------------

  get rules() {
    return this.core.ix.rules || {};
  }

  allowed(target) {
    const rules = this.rules;
    if (rules.suspended || !rules.available) return false;
    if (target === GIZMO) return rules.gizmo !== "hidden";
    const u = Number(target.slice(5));
    return !!(this.core.ix.rings || {})[u];
  }

  refresh() {
    if (this.rules.suspended) {
      this.hover.target = null;
      this.hover.pinned = false;
      this.hover.graceAt = null;
      this.hover.hide();
    }
    this.hover.refresh();
    this.core.gizmos.showGizmo(this.hover.shown === GIZMO || this.editing);
  }

  get editing() {
    const edit = this.core.ix.edit;
    return !!(edit && edit.active);
  }

  show(target) {
    if (target === GIZMO) this.core.gizmos.showGizmo(true);
    else this.showRing(Number(target.slice(5)));
    this.schedule();
  }

  hide(target) {
    if (target === GIZMO) this.core.gizmos.showGizmo(this.editing);
    else this.hideRing();
    this.schedule();
  }

  // ---- rings ------------------------------------------------------------

  showRing(u) {
    this.hideRing();
    const spec = (this.core.ix.rings || {})[u];
    const joint = spec && this.core.nodes.get(spec.joint);
    if (!joint) return;
    this.ring = new Ring(this.core, { ...spec, u }, joint.q, this.core.snap[0]);
  }

  hideRing() {
    if (this.ringDrag) this.core.gestures.abort("hidden");
    if (this.ring) this.ring.dispose();
    this.ring = null;
  }

  // The shown ring's knob follows its joint while it is not dragged.
  follow() {
    const ring = this.ring;
    if (!ring || this.ringDrag) return;
    const joint = this.core.nodes.get(this.core.ix.rings[ring.u].joint);
    if (joint && Math.abs(joint.q - ring.q) >= (0.05 * Math.PI) / 180) ring.place(joint.q);
  }

  // ---- picking ----------------------------------------------------------

  ndc(x, y) {
    const r = this.core.canvas.getBoundingClientRect();
    return new THREE.Vector2(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
  }

  aim(x, y) {
    const threshold = this.core.config.raycast_threshold || 0.005;
    this.raycaster.params.Line.threshold = threshold;
    this.raycaster.params.Points.threshold = threshold;
    this.raycaster.setFromCamera(this.ndc(x, y), this.core.camera);
    return this.raycaster;
  }

  // What can be hovered or grabbed: hover sources, glowing nodes, the gizmo
  // ball and the shown ring's grip. Hidden objects are skipped, except the
  // grip, which is never drawn.
  targets() {
    const list = [];
    const sources = this.core.ix.sources || {};
    for (const id of Object.keys(sources)) {
      const rec = this.core.nodes.get(Number(id));
      if (rec && shown(rec.obj)) list.push(rec.obj);
    }
    for (const rec of this.core.nodes.records.values()) if (rec.glow && !rec.deleted && shown(rec.obj)) list.push(rec.obj);
    if (this.core.gizmos.gizmoShown) list.push(this.core.gizmos.ball);
    if (this.ring) list.push(...this.ring.grips);
    return list;
  }

  // The pointer that started the open gesture.
  get owner() {
    const g = this.core.gestures.current;
    return g ? g.pointerId : null;
  }

  // The first thing a pointer at (x, y) would act on.
  hit(x, y) {
    const grips = this.ring ? this.ring.grips : [];
    for (const h of this.aim(x, y).intersectObjects(this.targets(), true)) {
      if (grips.includes(h.object) || shown(h.object)) return this.classify(h.object);
    }
    return null;
  }

  classify(object) {
    if (this.ring && this.ring.grips.includes(object)) return { ring: this.ring, object };
    if (object === this.core.gizmos.ball) return { source: GIZMO, object };
    const sources = this.core.ix.sources || {};
    for (let o = object; o; o = o.parent) {
      const id = o.userData.wcId;
      if (id === undefined) continue;
      if (sources[id] !== undefined) return { source: sources[id], object };
      const rec = this.core.nodes.get(id);
      if (rec && rec.glow) return { glow: rec, object };
    }
    return null;
  }

  // The gizmo handles under (x, y), from every control attached.
  handlesAt(x, y) {
    const p = this.ndc(x, y);
    const found = [];
    for (const tc of this.core.gizmos.all()) {
      if (!tc.object || !tc.enabled) continue;
      const axis = tc.axis;
      tc.pointerHover({ x: p.x, y: p.y, button: 0 });
      if (tc.axis !== null) found.push({ tc, axis: tc.axis });
      tc.axis = axis;
    }
    return found;
  }

  // Every named node under (x, y), nearest first, for the context menu, and
  // where the pointer ray meets the floor.
  contextAt(x, y) {
    const raycaster = this.aim(x, y);
    const objects = [];
    for (const rec of this.core.nodes.records.values()) if (rec.id !== 0 && rec.name && !rec.deleted) objects.push(rec.obj);
    const hits = [];
    for (const h of raycaster.intersectObjects(objects, true)) {
      if (!shown(h.object)) continue;
      for (let o = h.object; o; o = o.parent) {
        const rec = o.userData.wcId !== undefined ? this.core.nodes.get(o.userData.wcId) : null;
        if (rec && rec.name && !hits.includes(rec.name)) hits.push(rec.name);
      }
    }
    const ray = raycaster.ray;
    const k = ray.direction.z !== 0 ? -ray.origin.z / ray.direction.z : -1;
    const ground = k > 0 ? [ray.origin.x + k * ray.direction.x, ray.origin.y + k * ray.direction.y, 0] : null;
    return { hits, ground };
  }

  // ---- events -----------------------------------------------------------

  // A drag belongs to the pointer that started it: other pointers' events
  // stop here, before the orbit and transform controls see them.
  gate(e) {
    if (this.core.gestures.blocked && e.type === "pointerdown") {
      e.stopImmediatePropagation();
      return;
    }
    if (this.owner !== null && e.pointerId !== this.owner) {
      e.stopImmediatePropagation();
      return;
    }
    if (e.type === "pointerdown") {
      this.pressed.add(e.pointerId);
      this.lastPointerId = e.pointerId;
      this.taps.set(e.pointerId, { x: e.clientX, y: e.clientY, suppressed: false });
    }
  }

  down(e) {
    if (e.button !== 0 || e.ctrlKey || this.core.gestures.active || this.core.gestures.blocked) return;
    const ring = this.ring;
    if (!ring || this.handlesAt(e.clientX, e.clientY).length) return;
    const hit = this.hit(e.clientX, e.clientY);
    if (!hit || hit.ring !== ring) return;
    const at = ring.onPlane(this.aim(e.clientX, e.clientY));
    if (!at) return;
    const spec = this.core.ix.rings[ring.u];
    const drag = new RingDrag(ring.q, at[0], at[1], spec.radius, spec.lo, spec.hi);
    const g = this.core.gestures.begin("ring", { joint: spec.panel[0] }, e.pointerId, () => this.endRing(false));
    if (!g) return;
    this.ringDrag = { g, drag, ring, pointerId: e.pointerId };
    this.capturing = true;
    try {
      this.core.canvas.setPointerCapture(e.pointerId);
    } catch (err) {
      // A pointer the browser no longer tracks: its release cannot come.
      this.core.gestures.abort("capture");
      return;
    }
    this.core.controls.enabled = false;
    this.core.menu.cancel();
    ring.grab();
    e.stopImmediatePropagation();
  }

  dragMove(e) {
    const rd = this.ringDrag;
    if (!rd || e.pointerId !== rd.pointerId) return;
    // The button came up where this page never heard it.
    if (e.pointerType === "mouse" && e.buttons === 0) {
      this.core.gestures.abort("released unseen");
      return;
    }
    this.trackRing(e);
  }

  trackRing(e) {
    const rd = this.ringDrag;
    const at = rd.ring.onPlane(this.aim(e.clientX, e.clientY));
    if (!at) return;
    rd.drag.track(at[0], at[1]);
    const q = rd.drag.value((this.core.snap[0] * Math.PI) / 180);
    if (q === rd.ring.q) return;
    rd.ring.place(q);
    this.core.gestures.sample(rd.g, { delta: this.ringDelta(rd) });
  }

  ringDelta(rd) {
    const spec = this.core.ix.rings[rd.ring.u];
    return panelDegrees(spec.panel, rd.ring.q) - panelDegrees(spec.panel, rd.ring.q0);
  }

  up(e) {
    const rd = this.ringDrag;
    if (rd && e.pointerId === rd.pointerId) {
      this.trackRing(e);
      const delta = this.ringDelta(rd);
      this.endRing(true);
      this.core.gestures.release(rd.g, { delta });
      this.settle();
      return;
    }
    const tap = this.taps.get(e.pointerId);
    this.taps.delete(e.pointerId);
    if (!tap || tap.suppressed || e.button !== 0 || this.core.gestures.active) return;
    if (Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > TAP_PX) return;
    if (e.target !== this.core.canvas) return;
    if (this.handlesAt(e.clientX, e.clientY).length) return;
    const hit = this.hit(e.clientX, e.clientY);
    if (hit && hit.source !== undefined && e.pointerType === "touch") this.hover.tap(hit.source);
    else if (!hit) this.hover.miss();
    this.schedule();
  }

  cancel(e) {
    this.taps.delete(e.pointerId);
    if (this.owner !== null && e.pointerId === this.owner) this.core.gestures.abort("cancelled");
  }

  lostCapture(e) {
    if (this.owner === null || e.pointerId !== this.owner) return;
    if (this.core.gizmos.drag || this.ringDrag) this.core.gestures.abort("capture");
  }

  endRing(released) {
    const rd = this.ringDrag;
    if (!rd) return;
    this.ringDrag = null;
    this.capturing = false;
    this.restoreOrbit();
    rd.ring.letGo();
    if (!released) this.follow();
  }

  suppressTap(pointerId) {
    const tap = this.taps.get(pointerId);
    if (tap) tap.suppressed = true;
  }

  move(e) {
    if (e.pointerType === "touch" || this.ringDrag || this.core.gestures.active) return;
    if (e.target !== this.core.canvas) return;
    const now = performance.now();
    const handles = this.handlesAt(e.clientX, e.clientY);
    const onGizmo = handles.some((h) => h.tc === this.core.gizmos.tcp);
    this.hover.gizmo(onGizmo, now);
    const hit = handles.length ? null : this.hit(e.clientX, e.clientY);
    const source = hit ? (hit.ring ? `ring:${hit.ring.u}` : (hit.source ?? null)) : null;
    if (source !== this.hovered) {
      if (this.hovered !== null) this.hover.leave(this.hovered, now);
      this.hovered = source;
      if (source !== null) this.hover.enter(source, e.pointerType, now);
    }
    this.setGlow(hit && hit.glow ? hit.glow : null);
    this.schedule();
  }

  // A pointer is up (null: all of them).
  lift(e) {
    if (e) this.pressed.delete(e.pointerId);
    else this.pressed.clear();
    this.restoreOrbit();
  }

  // The camera orbits again once no drag holds it and no press is still
  // down: a press the orbit took before a drag let go of it would turn the
  // camera by everything since.
  restoreOrbit() {
    this.core.controls.enabled = !this.core.gizmos.drag && !this.capturing && this.pressed.size === 0;
  }

  forgetHover() {
    clearTimeout(this.graceTimer);
    this.hover.clear();
    this.hovered = null;
    this.setGlow(null);
  }

  leaveCanvas() {
    const now = performance.now();
    this.hover.gizmo(false, now);
    if (this.hovered !== null) this.hover.leave(this.hovered, now);
    this.hovered = null;
    this.setGlow(null);
    this.schedule();
  }

  // After a drag: go to whatever the pointer is over now, after the grace.
  settle() {
    this.hover.settle(performance.now());
    this.schedule();
  }

  schedule() {
    clearTimeout(this.graceTimer);
    const at = this.hover.graceAt;
    if (at === null) return;
    this.graceTimer = setTimeout(() => {
      this.hover.tick(performance.now());
      this.schedule();
    }, Math.max(0, at - performance.now()) + 1);
  }

  // ---- glow ---------------------------------------------------------------

  setGlow(rec) {
    if ((this.glowing ? this.glowing.rec : null) === rec) return;
    if (this.glowing) {
      const { group } = this.glowing;
      group.removeFromParent();
      group.traverse((o) => o.material && o.material.dispose());
      this.glowing = null;
    }
    if (rec) {
      const glow = this.core.config.glow || {};
      const group = new THREE.Group();
      rec.obj.traverse((o) => {
        if (!o.isMesh || !o.geometry) return;
        const mesh = new THREE.Mesh(
          o.geometry,
          new THREE.MeshBasicMaterial({
            color: glow.color,
            transparent: true,
            opacity: glow.opacity ?? 0.2,
            side: THREE.BackSide,
            depthWrite: false,
          }),
        );
        mesh.renderOrder = 999;
        mesh.userData.source = o;
        group.add(mesh);
      });
      this.core.scene.add(group);
      this.glowing = { rec, group };
      this.syncGlow();
    }
    this.core.requestRender();
  }

  // The glow follows its node, a little larger.
  syncGlow() {
    const g = this.glowing;
    if (!g) return;
    const scale = (this.core.config.glow || {}).scale ?? 1.5;
    for (const mesh of g.group.children) {
      const src = mesh.userData.source;
      src.updateWorldMatrix(true, false);
      src.matrixWorld.decompose(mesh.position, mesh.quaternion, mesh.scale);
      mesh.scale.multiplyScalar(scale);
    }
  }

  // ---- snap ---------------------------------------------------------------

  // The step for the camera's distance from its target; the handles hear of
  // a change in band.
  updateSnap() {
    const bands = this.core.ix.bands;
    if (!bands) return;
    const d = this.core.camera.position.distanceTo(this.core.controls.target);
    const [joint, cart] = snapFor(d, bands);
    const [j0, c0] = this.core.snap;
    if (joint === j0 && cart === c0) return;
    this.core.snap = [joint, cart];
    if (this.ring) this.ring.setStep(joint);
    this.core.gizmos.setSnap(joint, cart);
    if (this.ringDrag) this.trackRingAgain();
  }

  // A drag re-snaps where the pointer is when the step changes.
  trackRingAgain() {
    const rd = this.ringDrag;
    const q = rd.drag.value((this.core.snap[0] * Math.PI) / 180);
    if (q === rd.ring.q) return;
    rd.ring.place(q);
    this.core.gestures.sample(rd.g, { delta: this.ringDelta(rd) });
  }

  // ---- teardown -----------------------------------------------------------

  forget(rec) {
    if (this.glowing && this.glowing.rec === rec) this.setGlow(null);
  }

  dispose() {
    clearTimeout(this.graceTimer);
    this.hideRing();
    this.setGlow(null);
    for (const [target, type, fn, opts] of this.listeners) target.removeEventListener(type, fn, opts);
  }
}

function shown(object) {
  for (let o = object; o; o = o.parent) if (!o.visible) return false;
  return true;
}
