// The transform controls of the view: the TCP gizmo (jogging the tool, or
// moving a target while editing), the joint controls while editing, and the
// arrows that move a keep-out.
//
// A drag through any of them is a gesture the app admits. A drag that ends
// any other way than by its release (a cancelled pointer, a hidden handle, a
// lost context) ends aborted: the reason is recorded before the controls are
// told to let go, since they report a release while letting go.

import * as THREE from "three";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import { reducedMotion } from "./fx.js";
import { gizmoLabel, gizmoTicks } from "./handles.js";
import { label } from "./rings.js";

const BALL_RADIUS_M = 0.008;
const LABEL_LIFT_M = 0.03;
const SPRING_MS = 520;
const SPRING_MIN_M = 0.001;

const easeOutBack = (t) => {
  const c1 = 1.70158;
  return 1 + (c1 + 1) * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
};

const matrix3 = (q) => {
  const e = new THREE.Matrix4().makeRotationFromQuaternion(q).elements;
  return [
    [e[0], e[4], e[8]],
    [e[1], e[5], e[9]],
    [e[2], e[6], e[10]],
  ];
};

export class Gizmos {
  constructor(core) {
    this.core = core;
    this.frame = new THREE.Group();
    this.frame.name = "tcp:ball_frame";
    this.ball = new THREE.Mesh(
      new THREE.SphereGeometry(BALL_RADIUS_M, 16, 16),
      new THREE.MeshPhongMaterial({ transparent: true, opacity: 0.9 }),
    );
    this.ball.name = "tcp:ball";
    this.frame.add(this.ball);
    this.frame.visible = false;
    core.scene.add(this.frame);
    this.rev = 0;
    this.placed = null;
    this.miss = false;
    this.tcp = this.controls("tcp", 0.8);
    this.joints = new Map(); // joint node id -> {tc, index, axis}
    this.shape = null; // {tc, node, session}
    this.drag = null; // {kind, g, tc, ...}
    this.marks = null;
    this.spring = null;
  }

  get active() {
    return this.spring !== null;
  }

  // ---- controls ---------------------------------------------------------

  controls(kind, size) {
    const tc = new TransformControls(this.core.camera, this.core.canvas);
    tc.size = size;
    tc.setSpace("local");
    tc.addEventListener("change", () => this.core.requestRender());
    tc.addEventListener("mouseDown", () => this.down(kind, tc));
    tc.addEventListener("objectChange", () => this.changed(kind, tc));
    tc.addEventListener("mouseUp", () => this.up(kind, tc));
    tc.addEventListener("dragging-changed", (e) => {
      this.core.controls.enabled = !e.value && !this.core.pointer.capturing;
    });
    const helper = tc.getHelper();
    helper.visible = false;
    this.core.scene.add(helper);
    tc.wcKind = kind;
    return tc;
  }

  all() {
    const list = [this.tcp, ...[...this.joints.values()].map((j) => j.tc)];
    if (this.shape) list.push(this.shape.tc);
    return list;
  }

  // Attached and shown, so its picker can be grabbed.
  attach(tc, object) {
    if (tc.object !== object) tc.attach(object);
    tc.getHelper().visible = true;
    tc.enabled = true;
  }

  // Taken off, so nothing can grab it; a drag through it ends aborted first.
  detach(tc, reason) {
    if (this.drag && this.drag.tc === tc) this.core.gestures.abort(reason);
    if (tc.object) tc.detach();
    tc.getHelper().visible = false;
    this.core.requestRender();
  }

  setSnap(jointDeg, cartMm) {
    this.tcp.setTranslationSnap(cartMm / 1000);
    this.tcp.setRotationSnap((jointDeg * Math.PI) / 180);
    if (this.drag && this.drag.kind === "tcp") this.drawMarks();
  }

  // ---- TCP gizmo --------------------------------------------------------

  place(rev, position, quaternion) {
    this.rev = rev;
    this.placed = [position, quaternion];
    // A drag measures its delta from where the frame was when it began.
    if (this.drag && this.drag.kind === "tcp") return;
    const world = this.spring ? this.ball.getWorldPosition(new THREE.Vector3()) : null;
    this.frame.position.fromArray(position);
    this.frame.quaternion.fromArray(quaternion);
    if (world) {
      this.frame.updateWorldMatrix(true, false);
      this.spring.from.copy(this.frame.worldToLocal(world));
    }
    this.core.requestRender();
  }

  showGizmo(on) {
    const edit = this.core.ix.edit;
    const mode = edit && edit.active ? "translate" : (this.core.ix.rules || {}).gizmo;
    const visible = on && mode !== "hidden";
    this.frame.visible = visible;
    const colors = this.core.ix.colors || {};
    this.ball.material.color.set(edit && edit.active ? colors.active : colors.ball);
    if (visible) {
      if (mode === "translate" || mode === "rotate") this.tcp.setMode(mode);
      this.attach(this.tcp, this.ball);
    } else this.detach(this.tcp, "hidden");
    this.core.requestRender();
  }

  get gizmoShown() {
    return this.frame.visible && !!this.tcp.object;
  }

  // ---- edit-mode joint controls and the keep-out arrows -----------------

  setJoints(edit) {
    const wanted = new Map();
    if (edit && edit.active) for (const j of edit.joints || []) wanted.set(j.node, j);
    for (const [id, entry] of this.joints) {
      if (!wanted.has(id)) {
        this.detach(entry.tc, "edit");
        entry.tc.getHelper().removeFromParent();
        entry.tc.dispose();
        this.joints.delete(id);
      }
    }
    for (const [id, j] of wanted) {
      const rec = this.core.nodes.get(id);
      if (!rec) continue;
      let entry = this.joints.get(id);
      if (!entry) {
        entry = { tc: this.controls("joint", 0.6), index: j.index, axis: j.axis, node: id };
        entry.tc.setMode("rotate");
        entry.tc.setRotationSnap((5 * Math.PI) / 180);
        this.joints.set(id, entry);
      }
      entry.tc.showX = j.axis === "X";
      entry.tc.showY = j.axis === "Y";
      entry.tc.showZ = j.axis === "Z";
      this.attach(entry.tc, rec.obj);
    }
  }

  setShape(move) {
    const current = this.shape;
    if (current && (!move || move.node !== current.node || move.session !== current.session)) {
      this.detach(current.tc, "shape");
      current.tc.getHelper().removeFromParent();
      current.tc.dispose();
      this.shape = null;
    }
    if (!move) return;
    const rec = this.core.nodes.get(move.node);
    if (!rec) return;
    if (!this.shape) {
      const tc = this.controls("shape", 0.5);
      tc.setMode("translate");
      this.shape = { tc, node: move.node, session: move.session };
    }
    this.attach(this.shape.tc, rec.obj);
  }

  // A node is gone: controls on it come off.
  forget(rec) {
    const entry = this.joints.get(rec.id);
    if (entry) this.detach(entry.tc, "deleted");
    if (this.shape && this.shape.node === rec.id) this.detach(this.shape.tc, "deleted");
  }

  // ---- drags ------------------------------------------------------------

  down(kind, tc) {
    const core = this.core;
    if (core.gestures.blocked) {
      this.letGo(tc, "blocked");
      return;
    }
    let fields;
    let rec = null;
    if (kind === "tcp") {
      this.endSpring();
      fields = { mode: tc.mode, axis: tc.axis, rev: this.rev };
    } else if (kind === "joint") {
      const entry = [...this.joints.values()].find((j) => j.tc === tc);
      rec = entry && core.nodes.get(entry.node);
      if (!rec) return;
      fields = { session: core.ix.edit.session, joint: entry.index, q: rec.q };
    } else {
      rec = core.nodes.get(this.shape.node);
      if (!rec) return;
      fields = { session: this.shape.session, node: this.shape.node };
    }
    const drag = { kind, tc, rec, ending: null };
    const g = core.gestures.begin(kind, fields, core.pointer.lastPointerId, (reason) => this.letGo(tc, reason));
    if (!g) {
      this.letGo(tc, "blocked");
      return;
    }
    drag.g = g;
    this.drag = drag;
    if (rec) rec.dragging = true;
    for (const other of this.all()) if (other !== tc) other.enabled = false;
    if (kind === "tcp") this.beginMarks(tc.mode, tc.axis);
  }

  changed(kind, tc) {
    const drag = this.drag;
    if (!drag || drag.tc !== tc || drag.ending) return;
    const g = drag.g;
    if (kind === "tcp") {
      this.core.gestures.sample(g, this.ballPose());
      this.updateMarks();
    } else if (kind === "joint") {
      this.core.gestures.sample(g, this.jointPose(drag));
    }
  }

  up(kind, tc) {
    const drag = this.drag;
    if (!drag || drag.tc !== tc) return;
    // Letting go on purpose ends the drag aborted, recorded before this ran.
    if (drag.ending) return;
    let fields = {};
    if (kind === "tcp") fields = this.ballPose();
    else if (kind === "joint") fields = this.jointPose(drag);
    else {
      const p = drag.rec.obj.position;
      fields = { session: this.shape.session, node: this.shape.node, x: p.x, y: p.y, z: p.z };
    }
    this.finishDrag(true);
    this.core.gestures.release(drag.g, fields);
    if (kind === "tcp") this.springBack();
    this.core.pointer.settle();
  }

  // The drag ends other than by release: the controls let go of the pointer
  // without it counting as a release.
  letGo(tc, reason) {
    const drag = this.drag;
    if (drag && drag.tc === tc) drag.ending = reason;
    if (tc.dragging) tc.pointerUp(null);
    tc.domElement.removeEventListener("pointermove", tc._onPointerMove);
    const id = this.core.pointer.lastPointerId;
    if (id !== null && tc.domElement.hasPointerCapture && tc.domElement.hasPointerCapture(id)) {
      tc.domElement.releasePointerCapture(id);
    }
    if (drag && drag.tc === tc) {
      this.finishDrag(false);
      if (drag.kind === "tcp") {
        this.ball.position.set(0, 0, 0);
        this.ball.quaternion.identity();
      }
    }
    this.core.controls.enabled = !this.core.pointer.capturing;
    this.core.requestRender();
  }

  // A released drag keeps what it shows, which the app adopts; an aborted
  // one goes back to what the app last sent. The app sends again where it
  // does not take a release.
  finishDrag(released) {
    const drag = this.drag;
    if (!drag) return;
    this.drag = null;
    const rec = drag.rec;
    if (rec) {
      rec.dragging = false;
      if (drag.kind === "shape") {
        if (released) rec.base.p = rec.obj.position.toArray();
        else rec.obj.position.fromArray(rec.base.p);
      }
      if (drag.kind === "joint") {
        if (released) rec.q = this.jointAngle(rec);
        this.core.nodes.applyJoint(rec);
      }
    }
    for (const tc of this.all()) tc.enabled = true;
    if (drag.kind === "tcp") {
      this.endMarks();
      if (this.placed) this.place(this.rev, ...this.placed);
    }
  }

  // The ball's pose in its frame, the drag's delta in the tool frame.
  ballPose() {
    const p = this.ball.position;
    const r = new THREE.Euler().setFromQuaternion(this.ball.quaternion, "XYZ");
    return { x: p.x, y: p.y, z: p.z, rx: r.x, ry: r.y, rz: r.z };
  }

  // A joint node's turn about its own axis, in (-π, π].
  jointAngle(rec) {
    const q = rec.obj.quaternion;
    const value = 2 * Math.atan2(q.x * rec.axis.x + q.y * rec.axis.y + q.z * rec.axis.z, q.w);
    return Math.atan2(Math.sin(value), Math.cos(value));
  }

  jointPose(drag) {
    const entry = this.joints.get(drag.rec.id);
    return { session: this.core.ix.edit.session, joint: entry.index, q: this.jointAngle(drag.rec) };
  }

  // ---- marks and spring -------------------------------------------------

  beginMarks(mode, axis) {
    this.endMarks();
    this.marks = { mode, axis: axis || "", ticks: null, label: label("tcp:label") };
    this.marks.label.position.z = LABEL_LIFT_M;
    this.ball.add(this.marks.label);
    this.drawMarks();
  }

  drawMarks() {
    const m = this.marks;
    if (!m) return;
    if (m.ticks) {
      m.ticks.removeFromParent();
      m.ticks.geometry.dispose();
      m.ticks.material.dispose();
      m.ticks = null;
    }
    const [jointDeg, cartMm] = this.core.snap;
    const { points, axes, size } = gizmoTicks(m.mode, m.axis, cartMm, jointDeg);
    if (points.length) {
      const colors = this.core.ix.colors || {};
      const linear = { X: colors.axisXLinear, Y: colors.axisYLinear, Z: colors.axisZLinear };
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(points.flat(), 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(axes.flatMap((a) => linear[a] || [1, 1, 1]), 3));
      m.ticks = new THREE.Points(geometry, new THREE.PointsMaterial({ size, vertexColors: true, transparent: true }));
      m.ticks.name = "tcp:ticks";
      this.frame.add(m.ticks);
    }
    this.updateMarks();
  }

  updateMarks() {
    const m = this.marks;
    if (!m) return;
    const [jointDeg, cartMm] = this.core.snap;
    const text = gizmoLabel(m.mode, m.axis, this.ball.position.toArray(), matrix3(this.ball.quaternion), cartMm, jointDeg);
    if (m.label.element.textContent !== text) m.label.element.textContent = text;
  }

  endMarks() {
    const m = this.marks;
    if (!m) return;
    this.marks = null;
    m.label.removeFromParent();
    m.label.element.remove();
    if (m.ticks) {
      m.ticks.removeFromParent();
      m.ticks.geometry.dispose();
      m.ticks.material.dispose();
    }
  }

  // Back onto the frame from where the drag let go, tinted when the drag
  // asked for a pose the arm could not reach.
  springBack() {
    const from = this.ball.position.clone();
    this.ball.quaternion.identity();
    if (from.length() <= SPRING_MIN_M || reducedMotion()) {
      this.ball.position.set(0, 0, 0);
      return;
    }
    const colors = this.core.ix.colors || {};
    const own = this.ball.material.emissive.clone();
    const hot = this.miss && colors.miss ? new THREE.Color(colors.miss) : null;
    this.spring = { from, start: performance.now(), own, hot };
    this.core.animate();
  }

  step(now) {
    const s = this.spring;
    if (!s) return false;
    const t = Math.min(1, (now - s.start) / SPRING_MS);
    this.ball.position.copy(s.from).multiplyScalar(1 - easeOutBack(t));
    if (s.hot) {
      const k = Math.max(0, Math.sin(t * Math.PI * 3)) * (1 - t);
      this.ball.material.emissive.copy(s.own).lerp(s.hot, k);
    }
    if (t >= 1) this.endSpring();
    return this.spring !== null;
  }

  endSpring() {
    const s = this.spring;
    if (!s) return;
    this.spring = null;
    this.ball.position.set(0, 0, 0);
    this.ball.material.emissive.copy(s.own);
  }

  dispose() {
    for (const tc of this.all()) {
      tc.detach();
      tc.getHelper().removeFromParent();
      tc.dispose();
    }
    this.endMarks();
    this.frame.removeFromParent();
    this.ball.geometry.dispose();
    this.ball.material.dispose();
  }
}
