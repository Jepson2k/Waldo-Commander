// The jog handles' logic, apart from the DOM and three.js: which handle
// shows as the pointer moves over the arm, a ring drag's angle, the snap step
// at a camera distance, and the drag marks' text. Times are passed in, so a
// test can drive it with its own clock.

export const GIZMO = "gizmo";

export const minus = (s) => s.replace(/-/g, "−");
export const signed = (v, digits = 1) => minus(`${v >= 0 && !Object.is(v, -0) ? "+" : ""}${v.toFixed(digits)}`);
export const unsigned = (v, digits = 1) => minus(v.toFixed(digits));
const step = (v) => String(Number(v.toPrecision(6)));

// (joint step °, cartesian step mm) for a camera-to-target distance, from
// bands of [distance above which a band applies, joint °, cartesian mm],
// coarse to fine. A band's lower bound belongs to it, except the first
// band's. Before any distance, the second band applies.
export function snapFor(distance, bands) {
  if (distance === null || !Number.isFinite(distance) || distance < 0) return [bands[1][1], bands[1][2]];
  if (distance > bands[0][0]) return [bands[0][1], bands[0][2]];
  for (const [lower, joint, cart] of bands.slice(1, -1)) if (distance >= lower) return [joint, cart];
  const last = bands[bands.length - 1];
  return [last[1], last[2]];
}

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

// A ring drag: the pointer's angle around the joint axis, accumulated across
// the ±π seam, snapped to the step and clamped to the joint's travel. A
// pointer too near the axis has no meaningful angle and is ignored.
const DEAD_ZONE = 0.15;

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
    if (x * x + y * y < (DEAD_ZONE * this.radius) ** 2) return;
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

// The gizmo's drag marks: dots at whole steps along the dragged axis (or
// around it, rotating), and the delta in the tool frame.
const GIZMO_TICKS_EACH_SIDE = 10;
export const GIZMO_TICK_RING_M = 0.06;

export function gizmoTicks(mode, axis, cartMm, jointDeg) {
  const n = GIZMO_TICKS_EACH_SIDE;
  const points = [];
  const axes = [];
  let size = 0.003;
  if (mode === "translate") {
    const s = cartMm / 1000;
    size = Math.max(0.0008, Math.min(0.003, 0.4 * s));
    for (const letter of axis) {
      const i = "XYZ".indexOf(letter);
      if (i < 0) continue;
      for (let k = -n; k <= n; k++) {
        const p = [0, 0, 0];
        p[i] = k * s;
        points.push(p);
        axes.push(letter);
      }
    }
  } else if (mode === "rotate" && "XYZ".includes(axis) && axis.length === 1) {
    const s = (jointDeg * Math.PI) / 180;
    const r = GIZMO_TICK_RING_M;
    size = Math.max(0.0008, Math.min(0.003, 0.4 * r * s));
    const i = "XYZ".indexOf(axis);
    const a = (i + 1) % 3;
    const b = (i + 2) % 3;
    for (let k = -n; k <= n; k++) {
      const p = [0, 0, 0];
      p[a] = r * Math.cos(k * s);
      p[b] = r * Math.sin(k * s);
      points.push(p);
      axes.push(axis);
    }
  }
  return { points, axes, size };
}

// `local` is the ball's pose in its frame: position [m] and rotation matrix
// (row-major 3×3).
export function gizmoLabel(mode, axis, position, R, cartMm, jointDeg) {
  if (mode === "translate") {
    const letters = [...axis].filter((c) => "XYZ".includes(c)).join("");
    const deltas = [...letters].map((c) => signed(position["XYZ".indexOf(c)] * 1000)).join(" ");
    return `Tool ${letters}  Δ${deltas} mm  step ${step(cartMm)} mm`;
  }
  if (axis.length === 1 && "XYZ".includes(axis)) {
    const angle = localAngle(R, axis);
    const tenths = Math.round(((angle * 180) / Math.PI) * 10);
    return `Tool R${axis}  Δ${signed(tenths / 10)}°  step ${step(jointDeg)}°`;
  }
  const trace = R[0][0] + R[1][1] + R[2][2];
  const angle = Math.acos(Math.min(1, Math.max(-1, (trace - 1) / 2)));
  return `Tool  Δ${((angle * 180) / Math.PI).toFixed(1)}°  step ${step(jointDeg)}°`;
}

// The signed angle of a rotation about one of its own axes.
export function localAngle(R, axis) {
  if (axis === "X") return Math.atan2(R[2][1], R[1][1]);
  if (axis === "Y") return Math.atan2(R[0][2], R[2][2]);
  return Math.atan2(R[1][0], R[0][0]);
}
