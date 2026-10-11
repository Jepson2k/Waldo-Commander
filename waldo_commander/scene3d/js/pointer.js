// The pointer over the view: hovering a link shows its joint's ring and the
// tool shows the gizmo, a target glows under the pointer, a tap by a finger
// or pen pins a handle, and a tap on an object selects it.
//
// A press goes to the first tool that claims it: drawing, a resize handle, a
// gizmo handle, the shown ring, then a path; a second finger's goes to the
// camera. The drag it starts belongs to the pointer that pressed, which is
// captured until the drag ends, and other pointers do not reach the controls
// meanwhile. Escape, a cancelled pointer, a lost capture or a button released
// out of sight end it aborted.
//
// Only what the pointer can act on is hit-tested: the links and the tool,
// the shown ring's grip and the gizmo, and nodes that glow.

import * as THREE from "three";
import { GIZMO, Hover, RingDrag, panelDegrees, tapPx } from "./handles.js";
import { Ring, label } from "./rings.js";
import { niceStep } from "./precision.js";
import { overlap } from "./overlays.js";
import { setStyle } from "./dom.js";

const NONE = [];
const place = new THREE.Vector3();
const at = [0, 0, 0];

export function shown(object) {
  for (let o = object; o; o = o.parent) if (!o.visible) return false;
  return true;
}

// A hit on something drawn and not clipped away; an object isolating faded
// is context, not something to pick.
export function visibleHit(hit) {
  if(!shown(hit.object) || hit.object.userData.faded)return false;
  const list=Array.isArray(hit.object.material)?[hit.object.material[hit.face?.materialIndex||0]]:[hit.object.material];
  return list.every(mat=>!mat || mat.visible!==false && !(mat.clippingPlanes||[]).some(plane=>plane.distanceToPoint(hit.point)<-1e-7));
}

// The plane through `point` that holds `axis` and faces along `ray`, the one
// a drag along the axis moves on; null where the axis points at the eye.
export function facingPlane(ray, axis, point, out, least) {
  const normal = place.copy(ray.direction).addScaledVector(axis, -ray.direction.dot(axis));
  if (normal.length() < least) return null;
  return out.setFromNormalAndCoplanarPoint(normal.normalize(), point);
}

// A turn is read from where the pointer is in the plane it turns in. Within
// DEAD_PX of the plane's centre on screen nothing registers, and a plane seen
// within EDGE_ON (the sine of about 8°) of edge-on is not grabbed, so a pixel
// of jitter cannot become a large turn.
const DEAD_PX = 20;
// An Escape typed into the page's own fields, dialogs and menus is theirs.
const escapeIsTheirs = (target) =>
  target instanceof Element && !!target.closest("input, textarea, select, [contenteditable], .cm-editor, .q-dialog, .q-menu");
const EDGE_ON = 0.14;
// A finger that misses every gizmo handle takes the nearest one within about
// 20 px: the offsets it is tried at, nearest ring first.
const FINGER_OFFSETS = [7, 14, 20].flatMap((r) =>
  Array.from({ length: 8 }, (_, k) => [r * Math.cos((k * Math.PI) / 4), r * Math.sin((k * Math.PI) / 4)]),
);
const onScreen = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];
const onPlane = new THREE.Plane();

export class Pointer {
  constructor(core) {
    this.core = core;
    this.raycaster = new THREE.Raycaster();
    this.point = new THREE.Vector2();
    this.hovered = null; // the hover source under the mouse
    this.glowing = null;
    this.ring = null;
    this.rings = new Map(); // joint -> its ring, once shown
    this.ringDrag = null; // {g, drag: RingDrag, ring, rec, session, step}
    this.drag = null; // {pointerId, g?, move, release, cancel}: the tool a press started
    this.tools = [];
    this.pressed = new Set(); // pointers down on the canvas
    this.lastPointerId = null;
    this.taps = new Map(); // pointerId -> {x, y, shift, suppressed}
    this.graceTimer = 0;
    this.coarse = matchMedia("(pointer:coarse)");
    this.badge = this.makeBadge();
    this.badgeOccluded = false;
    this.groundAt = null;
    this.groundView = new THREE.Matrix4();
    this.groundProjection = new THREE.Matrix4();
    this.groundMarker=new THREE.Mesh(new THREE.RingGeometry(.72,1,40),new THREE.MeshBasicMaterial({transparent:true,opacity:.35,depthWrite:false,side:THREE.DoubleSide,forceSinglePass:true}));
    this.groundMarker.name="placement:ground";this.groundMarker.visible=false;core.scene.add(this.groundMarker);
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
      [window, "keydown", (e) => this.key(e), { capture: true }],
    ];
    for (const [target, type, fn, opts] of this.listeners) target.addEventListener(type, fn, opts);
  }

  // The shown ring as a tool, after the gizmos.
  get ringTool() {
    return { hit: (x, y) => this.ringHit(x, y), begin: (e) => this.ringBegin(e) };
  }

  // ---- rules ------------------------------------------------------------

  get rules() {
    return this.core.ix.rules || {};
  }

  // While a target is edited only its gizmo answers; the dials are the arm's.
  allowed(target) {
    const rules = this.rules;
    if (this.editing) return target === GIZMO;
    if (rules.suspended || !rules.available) return false;
    if (target === GIZMO) return rules.gizmo !== "hidden";
    const u = Number(target.slice(5));
    return !!(this.core.ix.rings || {})[u];
  }

  refresh() {
    if(this.glowing?.source && !this.allowed(this.glowing.source))this.setGlow(null);
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
    const frame = spec && this.core.nodes.get(spec.frame);
    if (!joint || !frame) return;
    let ring = this.rings.get(u);
    if (!ring) this.rings.set(u, (ring = new Ring(this.core, { ...spec, u })));
    ring.show(frame.obj, joint.q, this.jointStep(spec));
    this.ring = ring;
  }

  hideRing() {
    if (this.ringDrag) this.core.gestures.abort("hidden");
    if (this.ring) this.ring.hide();
    this.ring = null;
  }

  // The rings were drawn from specs or colours that have changed.
  forgetRings() {
    this.hideRing();
    for (const ring of this.rings.values()) ring.dispose();
    this.rings.clear();
  }

  // Every frame: the badge follows the tool, the ground mark the camera, and
  // the shown ring its joint while it is not dragged.
  follow() {
    this.layoutBadge();
    this.followGround();
    this.followRing();
  }

  followRing() {
    const ring = this.ring;
    if (!ring || this.ringDrag) return;
    ring.fitScreen();
    ring.setStep(this.jointStep(ring.spec));
    const joint = this.core.nodes.get(this.core.ix.rings[ring.u].joint);
    if (joint && Math.abs(joint.q - ring.q) >= (0.05 * Math.PI) / 180) ring.place(joint.q);
  }

  // ---- picking ----------------------------------------------------------

  ndc(x, y) {
    const r = this.core.overlays.framing ? this.core.overlays.view : this.core.canvas.getBoundingClientRect();
    return this.point.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
  }

  // Where a point in normalised device coordinates is on the page.
  screen(v) {
    const r = this.core.canvas.getBoundingClientRect();
    return [r.left + ((v.x + 1) / 2) * r.width, r.top + ((1 - v.y) / 2) * r.height];
  }

  // Where the pointer at (x, y) meets the plane through `centre` spanned by
  // the perpendicular world vectors `ex` and `ey`: [a, b] for
  // centre + a·ex + b·ey. Null within DEAD_PX of the centre on screen, or
  // when the plane is seen nearly edge-on from there.
  planePoint(centre, ex, ey, x, y) {
    const [c, normal, hit] = onScreen;
    const r = this.core.overlays.framing ? this.core.overlays.view : this.core.canvas.getBoundingClientRect();
    c.copy(centre).project(this.core.camera);
    const p = this.ndc(x, y);
    if (Math.hypot(((p.x - c.x) * r.width) / 2, ((p.y - c.y) * r.height) / 2) < DEAD_PX) return null;
    const ray = this.aim(x, y).ray;
    normal.crossVectors(ex, ey).normalize();
    if (Math.abs(ray.direction.dot(normal)) < EDGE_ON) return null;
    onPlane.setFromNormalAndCoplanarPoint(normal, centre);
    if (!ray.intersectPlane(onPlane, hit)) return null;
    hit.sub(centre);
    return [hit.dot(ex) / ex.lengthSq(), hit.dot(ey) / ey.lengthSq()];
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
    if (this.core.gizmos.frame.visible) list.push(this.core.gizmos.ball);
    if (this.ring) list.push(...this.ring.grips);
    return list;
  }

  // The pointer that owns what is under way: a drag, or an open gesture.
  get owner() {
    if (this.drag) return this.drag.pointerId;
    const g = this.core.gestures.current;
    return g ? g.pointerId : null;
  }

  // The first thing a pointer at (x, y) would act on.
  hit(x, y) {
    const grips = this.ring ? this.ring.grips : NONE;
    for (const h of this.aim(x, y).intersectObjects(this.targets(), true)) {
      if (grips.includes(h.object) || visibleHit(h)) return this.classify(h.object);
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
      const rec = this.core.nodes.get(id);
      if (sources[id] !== undefined) {
        const source=sources[id];
        return {source,object,glow:source.startsWith("ring:") && this.allowed(source)?rec:null};
      }
      if (rec && rec.glow) return { glow: rec, object };
    }
    return null;
  }

  // The gizmo handles under (x, y), from every control attached; for a
  // finger, the nearest ones when none is under it.
  handlesAt(x, y, pointerType = "mouse") {
    const found = this.handlesOn(x, y);
    if (found.length || pointerType !== "touch") return found;
    for (const [dx, dy] of FINGER_OFFSETS) {
      const near = this.handlesOn(x + dx, y + dy);
      if (near.length) return near;
    }
    return found;
  }

  handlesOn(x, y) {
    const p = this.ndc(x, y);
    const found = [];
    for (const tc of this.core.gizmos.all()) {
      const handle = tc.pick(p);
      if (handle) found.push({ tc, axis: handle.axis, mode: handle.mode });
    }
    return found;
  }

  // How far (x, y) is on screen from where `point` is drawn, in pixels.
  pixelsFrom(point, x, y) {
    const r = this.core.overlays.framing ? this.core.overlays.view : this.core.canvas.getBoundingClientRect();
    const c = onScreen[0].copy(point).project(this.core.camera);
    if (c.z > 1) return Infinity;
    const p = this.ndc(x, y);
    return Math.hypot(((p.x - c.x) * r.width) / 2, ((p.y - c.y) * r.height) / 2);
  }

  // Every named node under (x, y), nearest first, for the context menu, and
  // where the pointer ray meets the floor.
  contextAt(x, y) {
    const raycaster = this.aim(x, y);
    const objects = [];
    for (const rec of this.core.nodes.records.values()) if (rec.id !== 0 && rec.name && !rec.deleted) objects.push(rec.obj);
    const hits = [];
    let surface = null;
    for (const h of raycaster.intersectObjects(objects, true)) {
      if (!visibleHit(h)) continue;
      for (let o = h.object; o; o = o.parent) {
        const rec = o.userData.wcId !== undefined ? this.core.nodes.get(o.userData.wcId) : null;
        if (rec && rec.name && !hits.includes(rec.name)) hits.push(rec.name);
        if (!surface && rec && rec.object) {
          surface={id:rec.name,x:h.point.x,y:h.point.y,z:h.point.z};
        }
      }
    }
    const ray = raycaster.ray;
    const k = ray.direction.z !== 0 ? -ray.origin.z / ray.direction.z : -1;
    const ground = k > 0 ? [ray.origin.x + k * ray.direction.x, ray.origin.y + k * ray.direction.y, 0] : null;
    return { hits, ground, surface };
  }

  // ---- presses and drags ------------------------------------------------

  // A drag belongs to the pointer that started it: other pointers' events
  // stop here, before the orbit controls and the tools see them.
  gate(e) {
    if (this.core.gestures.blocked && e.type === "pointerdown") {
      e.stopImmediatePropagation();
      return;
    }
    if (this.owner !== null && e.pointerId !== this.owner) {
      // Another pointer's lift still reaches the orbit and the press
      // bookkeeping, or both would hold it down after the drag; it taps nothing.
      if (e.type === "pointerup" || e.type === "pointercancel") this.taps.delete(e.pointerId);
      else e.stopImmediatePropagation();
      return;
    }
    if (e.type === "pointerdown") {
      this.pressed.add(e.pointerId);
      this.lastPointerId = e.pointerId;
      this.taps.set(e.pointerId, { x: e.clientX, y: e.clientY, shift: e.shiftKey, suppressed: false });
    }
  }

  down(e) {
    if (e.button !== 0 || e.ctrlKey || this.drag || this.core.gestures.blocked) return;
    // A second finger pinches or turns the camera; it grabs nothing.
    if (e.pointerType === "touch" && this.pressed.size > 1) return;
    const x = e.clientX, y = e.clientY;
    // A press anywhere in the view puts away the values riding on the handles.
    this.core.gizmos.precision.dismiss();
    this.core.resizeHandles.dismiss();
    for (const tool of this.tools) {
      if (!tool.hit(x, y, e.pointerType)) continue;
      this.suppressTap(e.pointerId);
      e.stopImmediatePropagation();
      // The drag re-projects against the camera, which must hold still.
      this.core.navigation.halt();
      const drag = tool.begin(e);
      if (drag) this.start(e, drag);
      return;
    }
    this.core.inspection.missed(x, y);
  }

  start(e, drag) {
    drag.pointerId = e.pointerId;
    this.drag = drag;
    this.core.controls.enabled = false;
    this.core.menu.cancel();
    this.setGlow(null);
    try {
      this.core.canvas.setPointerCapture(e.pointerId);
    } catch (err) {
      // A pointer the browser no longer tracks: its release cannot come.
      this.abortDrag("capture");
      return;
    }
    e.preventDefault();
  }

  // The drag is over for the pointer: it is let go and the orbit returns.
  endDrag() {
    const drag = this.drag;
    if (!drag) return null;
    this.drag = null;
    const canvas = this.core.canvas;
    if (canvas.hasPointerCapture(drag.pointerId)) canvas.releasePointerCapture(drag.pointerId);
    this.restoreOrbit();
    return drag;
  }

  abortDrag(reason) {
    const drag = this.endDrag();
    if (drag) drag.cancel(reason);
  }

  // A gesture ended elsewhere (a new epoch, a dialog, a refusal).
  ended(g) {
    if (this.drag && this.drag.g === g) this.endDrag();
  }

  dragMove(e) {
    const drag = this.drag;
    const unseen = e.pointerType === "mouse" && e.buttons === 0;
    if (drag && e.pointerId === drag.pointerId) {
      // The button came up where this page never heard it.
      if (unseen) this.abortDrag("released unseen");
      else drag.move(e);
    }
    if (unseen) this.lift(e);
  }

  up(e) {
    const tap = this.taps.get(e.pointerId);
    this.taps.delete(e.pointerId);
    const drag = this.drag;
    if (drag && e.pointerId === drag.pointerId) {
      this.endDrag();
      drag.release(e);
      this.settle();
    } else if (tap) this.tapped(e, tap);
    this.lift(e);
  }

  tapped(e, tap) {
    if (tap.suppressed || e.button !== 0 || this.core.gestures.active) return;
    if (Math.hypot(e.clientX - tap.x, e.clientY - tap.y) > tapPx(e.pointerType)) return;
    if (e.target !== this.core.canvas) return;
    if (this.handlesAt(e.clientX, e.clientY, e.pointerType).length) return;
    const hit = this.hit(e.clientX, e.clientY);
    // A pointer that may not hover, a pen among them, pins what it taps.
    if (hit && hit.source !== undefined && e.pointerType !== "mouse") this.hover.tap(hit.source);
    else if (!hit) this.hover.miss();
    this.core.selection.tap(e.clientX, e.clientY, tap.shift, hit);
    this.schedule();
  }

  cancel(e) {
    this.taps.delete(e.pointerId);
    if (this.drag && e.pointerId === this.drag.pointerId) this.abortDrag("cancelled");
    this.lift(e);
  }

  lostCapture(e) {
    if (this.drag && e.pointerId === this.drag.pointerId) this.abortDrag("capture");
  }

  key(e) {
    const core = this.core;
    if (e.key !== "Escape") {
      core.inspection.key(e);
      return;
    }
    if (this.drag) this.abortDrag("escape");
    else if (escapeIsTheirs(e.target)) return;
    else {
      if (this.hover.pinned && !this.editing) {
        this.hover.miss();
        core.requestRender();
      }
      if (core.drawingTool.active) core.drawingTool.cancel();
      else if (!core.inspection.escape()) return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }

  // The page lost the user's attention: nothing pressed or dragged outlives it.
  interrupt(reason) {
    this.abortDrag(reason);
    this.core.gestures.abort(reason);
    this.core.drawingTool.cancel();
    this.core.inspection.clear();
    this.lift(null);
    this.leaveCanvas();
  }

  suppressTap(pointerId) {
    const tap = this.taps.get(pointerId);
    if (tap) tap.suppressed = true;
  }

  // ---- the ring ---------------------------------------------------------

  ringHit(x, y) {
    const ring = this.ring;
    if (!ring || this.core.gestures.active) return false;
    const hit = this.hit(x, y);
    return !!hit && hit.ring === ring && !!ring.at(this, x, y);
  }

  ringBegin(e) {
    const ring = this.ring;
    const at = ring.at(this, e.clientX, e.clientY);
    if (!at) return null;
    const spec = this.core.ix.rings[ring.u];
    const drag = new RingDrag(ring.q, at[0], at[1], spec.radius, spec.lo, spec.hi);
    const g = this.core.gestures.begin("ring", {joint: spec.panel[0]}, e.pointerId, () => this.endRing(false));
    if (!g) return null;
    this.ringDrag = { g, drag, ring, step: ring.stepDeg };
    ring.grab();
    return { g, move: (event) => this.trackRing(event), release: (event) => this.releaseRing(event), cancel: (reason) => this.core.gestures.abort(reason) };
  }

  trackRing(e) {
    const rd = this.ringDrag;
    if (!rd) return;
    const at = rd.ring.at(this, e.clientX, e.clientY);
    if (!at) return;
    rd.drag.track(at[0], at[1]);
    const q = rd.drag.value(rd.step * Math.PI / 180);
    if (q === rd.ring.q) return;
    rd.ring.place(q);
    this.core.gestures.sample(rd.g, {delta: this.ringDelta(rd)});
  }

  releaseRing(e) {
    const rd = this.ringDrag;
    if (!rd) return;
    this.trackRing(e);
    const fields = {delta: this.ringDelta(rd)};
    this.endRing(true);
    this.core.gestures.release(rd.g, fields);
  }

  ringDelta(rd) {
    const spec = this.core.ix.rings[rd.ring.u];
    return panelDegrees(spec.panel, rd.ring.q) - panelDegrees(spec.panel, rd.ring.q0);
  }

  endRing(released) {
    const rd = this.ringDrag;
    if (!rd) return;
    this.ringDrag = null;
    rd.ring.letGo();
    rd.ring.setStep(this.jointStep(rd.ring.spec));
    if (!released) this.followRing();
  }

  // ---- hover --------------------------------------------------------------

  move(e) {
    const x = e.clientX, y = e.clientY;
    const live = e.target === this.core.canvas && !this.drag && e.pointerType !== "touch" && !this.core.gestures.active;
    const handles = live ? this.handlesAt(x, y) : NONE;
    if (live) this.core.gizmos.highlight(handles);
    const hit = live && !handles.length ? this.hit(x, y) : null;
    this.placeGround(live && !e.buttons ? [x, y] : null, hit, handles);
    // Over a path a press would scrub, the path is what the pointer is on.
    const scrub = this.core.inspection.hoverAt(live && !handles.length && !hit?.ring ? [x, y] : null);
    if (!live) return;
    if (scrub) {
      this.forgetHover();return;
    }
    const now = performance.now();
    this.hover.gizmo(handles.some((h) => h.tc === this.core.gizmos.tcp) || this.core.gizmos.springHandle(x, y), now);
    const source = hit ? (hit.ring ? `ring:${hit.ring.u}` : (hit.source ?? null)) : null;
    if (source !== this.hovered) {
      if (this.hovered !== null) this.hover.leave(this.hovered, now);
      this.hovered = source;
      if (source !== null) this.hover.enter(source, e.pointerType, now);
    }
    this.setGlow(hit?.glow || null,hit?.glow && hit.source || null);
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
    this.core.controls.enabled = !this.drag && !this.core.drawingTool?.active && this.pressed.size === 0;
  }

  forgetHover() {
    clearTimeout(this.graceTimer);
    this.hideRing();
    this.core.gizmos.showGizmo(this.editing);
    this.hover.clear();
    this.hovered = null;
    this.setGlow(null);
  }

  leaveCanvas() {
    this.placeGround(null, null, NONE);
    this.core.inspection.hoverAt(null);
    this.core.gizmos.highlight(NONE);
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

  setGlow(rec, source=null) {
    if((this.glowing?.source || null)===source && (source || (this.glowing?.rec || null)===rec))return;
    if (this.glowing) {
      const { group } = this.glowing;
      group.removeFromParent();
      group.traverse((o) => o.material && o.material.dispose());
      this.glowing = null;
    }
    if (rec) {
      const glow = this.core.config.glow || {};
      const group = new THREE.Group();
      group.name=source?"joint:hover":"hover:glow";
      const records=source?Object.entries(this.core.ix.sources || {}).filter(([,target])=>target===source).map(([id])=>this.core.nodes.get(Number(id))).filter(Boolean):[rec];
      for(const node of records)node.obj.traverse((o) => {
        if (!o.isMesh || !o.geometry) return;
        const mesh = new THREE.Mesh(
          o.geometry,
          new THREE.MeshBasicMaterial({
            color: source?this.core.ix.colors.action:glow.color,
            transparent: true,
            opacity: source ? .28 : (glow.opacity ?? 0.2),
            side: source?THREE.FrontSide:THREE.BackSide,
            depthWrite: false,
            polygonOffset: !!source,
            polygonOffsetFactor: -1,
            polygonOffsetUnits: -1,
          }),
        );
        mesh.renderOrder = 999;
        mesh.userData.source = o;
        group.add(mesh);
      });
      this.core.scene.add(group);
      this.glowing = { rec, group, records, source };
      this.syncGlow();
    }
    this.core.requestRender();
  }

  // The glow follows its node, a little larger.
  syncGlow() {
    const g = this.glowing;
    if (!g) return;
    const scale = g.source?1:((this.core.config.glow || {}).scale ?? 1.5);
    for (const mesh of g.group.children) {
      const src = mesh.userData.source;
      mesh.visible=shown(src);
      const planes=src.material?.clippingPlanes||null;
      if(mesh.material.clippingPlanes!==planes){mesh.material.clippingPlanes=planes;mesh.material.needsUpdate=true;}
      src.updateWorldMatrix(true, false);
      src.matrixWorld.decompose(mesh.position, mesh.quaternion, mesh.scale);
      mesh.scale.multiplyScalar(scale);
    }
  }

  // ---- snap ---------------------------------------------------------------

  jointStep(spec) {
    const frame=this.core.nodes.get(spec.frame)?.obj;
    if(!frame)return 1;
    const pixel=this.core.metresPerPixel(frame.getWorldPosition(place));
    return Math.min(10,Math.max(.1,niceStep(pixel*2/spec.radius*180/Math.PI)));
  }

  // ---- the TCP badge -------------------------------------------------------

  makeBadge() {
    const badge=label("tcp:affordance");badge.element.className="wc-tcp-affordance";badge.element.style.cssText="";
    const button=document.createElement("button");button.textContent="TCP";
    button.title="Move TCP";button.setAttribute("aria-label","Move TCP");
    button.onpointerdown=e=>e.stopPropagation();
    button.onclick=e=>{e.stopPropagation();this.hover.tap(GIZMO);this.hover.pinned=true;this.core.requestRender();};
    button.onpointerenter=e=>{this.hovered=GIZMO;this.hover.enter(GIZMO,e.pointerType,performance.now());this.schedule();};
    button.onpointerleave=()=>{
      // Hiding the badge beneath a dial label must not dismiss that dial.
      if(this.badgeOccluded)return;
      if(this.hovered===GIZMO)this.hovered=null;
      this.hover.leave(GIZMO,performance.now());this.schedule();
    };
    const leader=document.createElement("span");leader.className="wc-tcp-leader";
    badge.element.append(button,leader);this.core.scene.add(badge);
    badge.userData.coarse=null;
    return badge;
  }

  // Where the badge's button sits from the tool, in pixels.
  get badgeOffset() {
    const gap=this.coarse.matches?46:30;
    return [gap*.5,-gap*.5];
  }

  layoutBadge() {
    const badge=this.badge;
    this.core.gizmos.frame.getWorldPosition(badge.position);
    badge.visible=this.allowed(GIZMO)&&!this.core.gestures.active&&!this.core.inspection.current&&!this.editing;
    const coarse=this.coarse.matches;
    if(badge.userData.coarse===coarse)return;
    badge.userData.coarse=coarse;
    const [dx,dy]=this.badgeOffset;
    badge.element.firstChild.style.transform=`translate(${dx}px,${dy}px)`;
    const leader=badge.element.lastChild;
    leader.style.width=`${Math.max(0,Math.hypot(dx,dy)-(coarse?22:13))}px`;
    leader.style.transform=`rotate(${Math.atan2(dy,dx)}rad)`;
  }

  // The badge hides where it would cover the shown dial's labels.
  occludeBadge() {
    const badge=this.badge,labels=this.core.overlays.marks.get("dial");
    let covered=false;
    if(badge.visible&&labels) {
      const [x,y]=this.core.overlays.project(badge.position,at),[dx,dy]=this.badgeOffset,half=this.coarse.matches?22:13;
      covered=overlap({left:x+dx-half,right:x+dx+half,top:y+dy-half,bottom:y+dy+half},labels,4)>0;
    }
    this.badgeOccluded=covered;
    setStyle(badge.element,"visibility",covered?"hidden":"visible");
  }

  // ---- the ground mark ------------------------------------------------------

  // A faint ring on the floor under the pointer: where a target placed from
  // the context menu would go. `hit` and `handles` are what is under it.
  placeGround(at, hit, handles) {
    this.groundAt=at;
    const camera=this.core.camera;
    this.groundView.copy(camera.matrixWorld);this.groundProjection.copy(camera.projectionMatrix);
    const marker=this.groundMarker,was=marker.visible,x=marker.position.x,y=marker.position.y,s=marker.scale.x;
    marker.visible=false;
    if(at&&!hit&&!handles.length&&!this.editing&&!this.core.gestures.active&&!this.core.drawingTool?.active) {
      const ray=this.aim(at[0],at[1]).ray;
      const k=ray.direction.z<-.001?-ray.origin.z/ray.direction.z:-1;
      if(k>0&&k<(this.core.config.reach||1)*8) {
        marker.position.copy(ray.origin).addScaledVector(ray.direction,k);marker.position.z=.0005;
        marker.scale.setScalar(this.core.metresPerPixel(marker.position)*8);
        marker.visible=true;
      }
    }
    if(marker.visible!==was || marker.visible&&(x!==marker.position.x||y!==marker.position.y||s!==marker.scale.x))this.core.requestRender();
  }

  // The mark goes when a drag or an edit begins, and follows the camera.
  followGround() {
    const marker=this.groundMarker,camera=this.core.camera;
    if(marker.visible&&(this.editing||this.core.gestures.active||this.core.drawingTool.active)){marker.visible=false;this.core.requestRender();}
    if(!this.groundAt||this.groundView.equals(camera.matrixWorld)&&this.groundProjection.equals(camera.projectionMatrix))return;
    const [x,y]=this.groundAt,handles=this.handlesAt(x,y);
    this.placeGround(this.groundAt,handles.length?null:this.hit(x,y),handles);
  }

  recolor(colors) {
    if(colors?.action)this.groundMarker.material.color.set(colors.action);
  }

  // ---- teardown -----------------------------------------------------------

  forget(rec) {
    if (this.glowing?.records.includes(rec)) this.setGlow(null);
    for (const [u, ring] of this.rings) {
      if (ring.spec.frame !== rec.id && ring.spec.joint !== rec.id) continue;
      if (ring === this.ring) this.hideRing();
      ring.dispose();
      this.rings.delete(u);
    }
  }

  dispose() {
    clearTimeout(this.graceTimer);
    this.forgetRings();
    this.setGlow(null);
    this.groundMarker.removeFromParent();this.groundMarker.geometry.dispose();this.groundMarker.material.dispose();
    this.badge.removeFromParent();this.badge.element.remove();
    for (const [target, type, fn, opts] of this.listeners) target.removeEventListener(type, fn, opts);
  }
}
