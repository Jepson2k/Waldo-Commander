// The jog handles' logic, apart from the DOM and three.js: which handle
// shows as the pointer moves over the arm, a ring drag's angle, and its text. Times are passed in, so a
// test can drive it with its own clock.

export const GIZMO = "gizmo";
// How far a press may wander and still be a click, in pixels: a finger
// wanders further than a mouse or a pen.
const TAP_PX = { mouse: 6, pen: 6, touch: 10 };
export const tapPx = (pointerType) => TAP_PX[pointerType] ?? TAP_PX.mouse;
// How far from a small handle a finger may land and still take it, in pixels.
export const FINGER_PX = 24;

export const minus = (s) => s.replace(/-/g, "−");
export const signed = (v, digits = 1) => minus(`${v >= 0 && !Object.is(v, -0) ? "+" : ""}${v.toFixed(digits)}`);
export const unsigned = (v, digits = 1) => minus(v.toFixed(digits));
// Metres as millimetres the way a number input shows them: no trailing zero.
export const mm = (v) => String(Number((v * 1000).toFixed(1)));
export const finite = (v) => v !== null && Number.isFinite(v);
// A recorded value, or a dash where the record has none.
export const reading = (v, digits = 1) => (finite(v) ? unsigned(v, digits) : "—");
const step = (v) => String(Number(v.toPrecision(6)));

// One handle at a time: the ring of the link under the pointer, or the
// gizmo. A handle goes, or gives way to the next one, `graceMs` after the
// pointer leaves it; a touch tap pins one until a tap on nothing.
export class Hover {
  constructor({ allowed, show, hide, dragging, graceMs }) {
    this.allowed = allowed;
    this.onShow = show;
    this.onHide = hide;
    this.dragging = dragging;
    this.graceMs = graceMs;
    this.target = null;
    this.shown = null;
    this.pinned = false;
    this.gizmoHovered = false;
    this.graceAt = null;
  }

  enter(target, pointerType, now) {
    if (pointerType === "mouse") this.pinned = false;
    this.target = target;
    if (this.shown === target) this.graceAt = null;
    else if (this.shown === null) {
      if (this.allowed(target)) this.show(target);
    } else this.graceAt = now + this.graceMs;
  }

  leave(target, now) {
    if (target === GIZMO && this.gizmoHovered) return;
    if (this.target === target) this.target = null;
    if (this.shown !== null) this.graceAt = now + this.graceMs;
  }

  // The pointer is on (or off) one of the gizmo's handles, which reach past
  // the arm: it is still on the gizmo.
  gizmo(hovered, now) {
    if (hovered === this.gizmoHovered) return;
    this.gizmoHovered = hovered;
    if (hovered) this.enter(GIZMO, "mouse", now);
    else this.leave(GIZMO, now);
  }

  tap(target) {
    if (!this.allowed(target)) return;
    this.target = target;
    this.pinned = true;
    this.graceAt = null;
    if (this.shown !== target && !this.dragging()) {
      this.hide();
      this.show(target);
    }
  }

  miss() {
    if (!this.pinned || this.dragging()) return;
    this.pinned = false;
    this.target = null;
    this.hide();
  }

  // When the grace runs out, the handle under the pointer takes over.
  tick(now) {
    if (this.graceAt === null || now < this.graceAt) return;
    this.graceAt = null;
    if (this.pinned || this.dragging()) return;
    if (this.gizmoHovered && this.shown === GIZMO) return;
    if (this.target === this.shown) return;
    this.hide();
    if (this.target !== null && this.allowed(this.target)) this.show(this.target);
  }

  // After a drag: go to whatever the pointer is over now, after the grace.
  settle(now) {
    if (this.target !== this.shown && !this.pinned) this.graceAt = now + this.graceMs;
  }

  // The rules changed: a handle no longer allowed goes, and the one under
  // the pointer shows if it now may.
  refresh() {
    if (this.shown !== null && !this.allowed(this.shown)) {
      this.pinned = false;
      this.graceAt = null;
      this.hide();
    }
    if (this.shown === null && this.target !== null && this.allowed(this.target)) this.show(this.target);
  }

  // What was shown is gone (the scene was sent again): start from nothing.
  clear() {
    this.target = null;
    this.shown = null;
    this.pinned = false;
    this.gizmoHovered = false;
    this.graceAt = null;
  }

  show(target) {
    this.shown = target;
    this.onShow(target);
  }

  hide() {
    const shown = this.shown;
    if (shown === null) return;
    this.shown = null;
    this.onHide(shown);
  }
}

// A snapped value holds until the raw one is HYSTERESIS of a step past the
// halfway mark, so a pointer resting on a step boundary cannot flip it. A
// value off the current step's grid (the step changed) does not hold.
const HYSTERESIS = 0.15;

export function stickySnap(raw, step, last) {
  if (!step) return raw;
  const onGrid = last !== null && Math.abs(last / step - Math.round(last / step)) < 1e-6;
  if (onGrid && Math.abs(raw - last) < step * (0.5 + HYSTERESIS)) return last;
  return Math.round(raw / step) * step;
}

// A ring drag: the pointer's angle around the joint axis, accumulated across
// the ±π seam, snapped to the step and clamped to the joint's travel.
export class RingDrag {
  constructor(q0, x, y, radius, lo, hi) {
    this.q0 = q0;
    this.theta = Math.atan2(y, x);
    this.acc = 0;
    this.radius = radius;
    this.lo = lo;
    this.hi = hi;
  }

  track(x, y) {
    if (x === null || y === null || !Number.isFinite(x) || !Number.isFinite(y)) return;
    const theta = Math.atan2(y, x);
    let d = theta - this.theta;
    if (d > Math.PI) d -= 2 * Math.PI;
    else if (d < -Math.PI) d += 2 * Math.PI;
    this.theta = theta;
    this.acc += d;
  }

  value(stepRad) {
    let q = this.q0 + Math.round(this.acc / stepRad) * stepRad;
    if (this.lo !== null && this.hi !== null) q = Math.min(this.hi, Math.max(this.lo, q));
    return q;
  }
}

// A joint value in the control panel's degrees: [index, sign, offset °].
export const panelDegrees = ([, sign, offset], q) => ((q * 180) / Math.PI - offset) * sign;

export function ringLabel(name, panel, q, q0, stepDeg) {
  const deg = panelDegrees(panel, q);
  let text = `${name}  ${unsigned(deg)}°`;
  if (q0 !== null) text += `  Δ${signed(deg - panelDegrees(panel, q0))}°`;
  return `${text}  step ${step(stepDeg)}°`;
}
