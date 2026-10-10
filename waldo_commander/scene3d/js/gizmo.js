// The transform controls of the view: the TCP gizmo (jogging the tool, or
// moving a target while editing) and the handles that move the selected
// objects.
//
// A drag through any of them is a gesture the app admits. A drag that ends
// any other way than by its release (a cancelled pointer, a hidden handle, a
// lost context) ends aborted: the reason is recorded before the controls are
// told to let go, since they report a release while letting go.

import * as THREE from "three";
import { SceneTransform } from "./transform.js";
import { reducedMotion } from "./fx.js";
import { Precision } from "./precision.js";
import { TAP_PX } from "./handles.js";

const BALL_RADIUS_M = 0.008;
const SPRING_MS = 520;
const SPRING_MIN_M = 0.001;

const box = new THREE.Box3();
const released = new THREE.Vector3();
// A press on the TCP handles mid-spring: claimed, but it starts no drag.
const SPRINGING = Symbol("springing");
const labelAt = new THREE.Vector3();
const projected = [0, 0, 0];
const riding = new THREE.Vector3();

// A node's materials in `color`, or back in their own.
function paint(rec, color) {
  rec.obj.traverse((o) => {
    const c = o.material?.color;
    if (!c) return;
    if (color) {
      o.userData.wcOwnColor ??= c.getHex();
      c.set(color);
    } else if (o.userData.wcOwnColor !== undefined) {
      c.setHex(o.userData.wcOwnColor);
      delete o.userData.wcOwnColor;
    }
  });
}

const easeOutBack = (t) => {
  const c1 = 1.70158;
  return 1 + (c1 + 1) * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
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
    this.tcp = this.controls("tcp");
    this.shape = null; // {tc, node, session}
    this.selectionFrame = new THREE.Group();
    this.selectionFrame.name = "selection:center";
    core.scene.add(this.selectionFrame);
    this.drag = null; // {kind, g, tc, ...}
    this.pressed = null; // the handle a press landed on
    this.precision = new Precision(this);
    this.spring = null;
    this.tinted = null; // the edit marker drawn in the miss colour
  }

  get active() {
    return this.spring !== null;
  }

  // ---- controls ---------------------------------------------------------

  controls(kind) {
    const tc = new SceneTransform(this.core);
    tc.setSpace("local");
    tc.addEventListener("change", () => this.core.requestRender());
    tc.addEventListener("mouseDown", () => this.down(kind, tc));
    tc.addEventListener("objectChange", () => this.changed(kind, tc));
    tc.addEventListener("mouseUp", () => this.up(kind, tc));
    const helper = tc.getHelper();
    helper.visible = false;
    this.core.scene.add(helper);
    tc.wcKind = kind;
    return tc;
  }

  all() {
    const list = [this.tcp];
    if (this.shape) list.push(this.shape.tc);
    return list;
  }

  // ---- the pointer's tool ---------------------------------------------------

  hit(x, y) {
    if (this.core.gestures.active) return false;
    this.pressed = this.core.pointer.handlesAt(x, y)[0] || (this.springHandle(x, y) ? SPRINGING : null);
    return !!this.pressed;
  }

  // Mid-spring the ball is not yet where the arm is, so its handles take no
  // drag; they are still under the pointer, for the press and the hover.
  springHandle(x, y) {
    if (!this.spring || !this.tcp.object) return false;
    this.tcp.enabled = true;
    const handle = this.tcp.pick(this.core.pointer.ndc(x, y));
    this.tcp.enabled = false;
    return !!handle;
  }

  begin(e) {
    if (this.pressed === SPRINGING) return null;
    const { tc, axis, mode } = this.pressed;
    tc.axis = axis;
    tc.mode = mode;
    if (!tc.begin(e)) return null;
    const x = e.clientX, y = e.clientY;
    const release = (event) => {
      if (Math.hypot(event.clientX - x, event.clientY - y) > TAP_PX) tc.release(event);
      else this.clicked(tc, axis, mode);
    };
    return { g: this.drag.g, move: (event) => tc.drag(event), release, cancel: (reason) => this.core.gestures.abort(reason) };
  }

  // A handle let go where it was pressed moves nothing: its change is typed.
  clicked(tc, axis, mode) {
    this.core.gestures.abort("click");
    this.precision.click(tc, axis, mode);
  }

  // The handle under the pointer, on each control; a change being typed
  // keeps its own handle lit.
  highlight(handles) {
    const typing = this.precision.entry?.tc;
    for (const tc of this.all()) if (tc !== typing) tc.setHover(handles.find((h) => h.tc === tc) || null);
  }

  // Where the selected objects' handles are on screen, for the cards to keep
  // clear of.
  layout() {
    const tc = this.shape?.tc;
    const overlays = this.core.overlays;
    if (!tc?.object || !tc.helper.visible) {
      overlays.mark("handles", null);
      return;
    }
    const handles = overlays.projectBox(box.setFromObject(tc.helper));
    for (const letter of tc.letters) {
      if (!letter.visible) continue;
      const [x, y] = overlays.project(letter.getWorldPosition(labelAt), projected);
      handles.left = Math.min(handles.left, x - 12);
      handles.right = Math.max(handles.right, x + 12);
      handles.top = Math.min(handles.top, y - 12);
      handles.bottom = Math.max(handles.bottom, y + 12);
    }
    overlays.mark("handles", [handles]);
  }

  // Attached and shown, so its picker can be grabbed.
  attach(tc, object) {
    if (tc.object !== object) tc.attach(object);
    const inactive = this.drag && this.drag.tc !== tc;
    tc.getHelper().visible = !inactive;
    tc.enabled = !inactive && !(tc === this.tcp && this.spring);
  }

  // Taken off, so nothing can grab it; a drag through it ends aborted first.
  detach(tc, reason) {
    if (this.drag && this.drag.tc === tc) this.core.gestures.abort(reason);
    if (tc.object) tc.detach();
    tc.getHelper().visible = false;
    this.core.requestRender();
  }

  // ---- TCP gizmo --------------------------------------------------------

  place(rev, position, quaternion) {
    this.rev = rev;
    this.placed = [position, quaternion];
    // A drag measures its delta from where the frame was when it began.
    if (this.drag && this.drag.kind === "tcp") return;
    this.frame.position.fromArray(position);
    this.frame.quaternion.fromArray(quaternion);
    this.core.requestRender();
  }

  showGizmo(on) {
    const edit = this.core.ix.edit;
    const rule = (this.core.ix.rules || {}).gizmo;
    this.tcp.setSpace(edit?.active ? "world" : "local");
    const visible = on && (edit?.active || rule !== "hidden") && !this.core.pointer?.ringDrag;
    this.frame.visible = visible || !!(rule!=="hidden" && this.core.ix.rules?.available && !this.core.ix.rules?.suspended);
    this.paintBall();
    if (visible) {
      this.attach(this.tcp, this.ball);
    } else this.detach(this.tcp, "hidden");
    this.core.requestRender();
  }

  // Editing, the target's own marker is what moves, so the ball is not
  // drawn; the marker takes the miss colour while the arm cannot reach it.
  paintBall() {
    const colors = this.core.ix.colors || {};
    const editing = !!this.core.ix.edit?.active;
    this.ball.visible = !editing;
    this.ball.material.color.set(colors.ball);
    const marker = this.editMarker();
    if (this.tinted && this.tinted !== marker) paint(this.tinted, null);
    this.tinted = editing && this.miss ? marker : null;
    if (marker) paint(marker, this.tinted ? colors.miss : null);
  }

  // The marker of the target being edited.
  editMarker() {
    const id = this.core.ix.edit?.marker;
    return id === undefined || id === null ? null : this.core.nodes.get(id) || null;
  }

  // The edited target's marker rides on the ball while it is dragged.
  follow(rec) {
    const obj = rec.obj;
    this.ball.getWorldPosition(riding);
    obj.position.copy(obj.parent ? obj.parent.worldToLocal(riding) : riding);
  }

  get gizmoShown() {
    return this.frame.visible && !!this.tcp.object;
  }

  // ---- the selected objects' handles ------------------------------------

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
      const tc = this.controls("shape");
      // Along the world's axes, whatever way the keep-out is turned.
      tc.setSpace("world");
      this.shape = { tc, node: move.node, session: move.session };
    }
    this.shape.revision=move.revision;
    this.shape.members=move.members || null;
    if(move.members) {
      if(this.drag?.kind!=="shape") {
        this.selectionFrame.position.fromArray(move.center);
        this.selectionFrame.quaternion.identity();
      }
      this.attach(this.shape.tc, this.selectionFrame);
    } else this.attach(this.shape.tc, rec.obj);
  }

  // A node is gone: controls on it come off.
  forget(rec) {
    if (this.shape && (this.shape.node === rec.id || this.shape.members?.includes(rec.id))) this.detach(this.shape.tc, "deleted");
  }

  // ---- drags ------------------------------------------------------------

  down(kind, tc, exact = false) {
    const core = this.core;
    this.precision.refresh();
    if (core.gestures.blocked) {
      this.letGo(tc, "blocked");
      return;
    }
    let fields;
    let rec = null;
    const extra = {};
    if (kind === "tcp") {
      this.endSpring();
      fields = { mode: tc.mode, axis: tc.axis, rev: this.rev };
      extra.marker = this.editMarker();
    } else {
      rec = core.nodes.get(this.shape.node);
      if (!rec) return;
      fields = { session: this.shape.session, node: this.shape.node, revision:this.shape.revision, mode:tc.mode };
      if(this.shape.members) {
        const members=this.shape.members.map(id=>core.nodes.get(id));
        if(members.some(rec=>!rec))return;
        extra.members=members.map(rec=>({rec,position:rec.obj.position.clone(),quaternion:rec.obj.quaternion.clone()}));
        extra.center=tc.object.position.clone();
      }
    }
    const drag = { kind, tc, rec, ending: null, ...extra };
    const g = core.gestures.begin(kind, fields, core.pointer.lastPointerId, (reason) => this.letGo(tc, reason));
    if (!g) {
      this.letGo(tc, "blocked");
      return;
    }
    drag.g = g;
    this.drag = drag;
    if (rec) for (const moved of new Set([rec, ...(drag.members || []).map((item) => item.rec)])) core.gestures.hold(g, moved, "position", "rotation");
    if (drag.marker) core.gestures.hold(g, drag.marker, "position");
    for (const other of this.all()) if (other !== tc) {
      other.enabled = false;
      other.getHelper().visible = false;
    }
    this.precision.begin(tc, exact);
  }

  changed(kind, tc) {
    const drag = this.drag;
    if (!drag || drag.tc !== tc || drag.ending) return;
    const g = drag.g;
    if (kind === "tcp") {
      this.core.gestures.sample(g, this.ballPose());
      if (drag.marker) this.follow(drag.marker);
      this.precision.update();
    } else {
      if(drag.members) {
        for(const item of drag.members) {
          item.rec.obj.position.copy(item.position).sub(drag.center).applyQuaternion(tc.object.quaternion).add(tc.object.position);
          item.rec.obj.quaternion.copy(tc.object.quaternion).multiply(item.quaternion);
        }
      }
      this.precision.update();
    }
  }

  up(kind, tc) {
    const drag = this.drag;
    if (!drag || drag.tc !== tc) return;
    // Letting go on purpose ends the drag aborted, recorded before this ran.
    if (drag.ending) return;
    let fields = {};
    if (kind === "tcp") fields = this.ballPose();
    else {
      const p = tc.object.position;
      const q=tc.object.quaternion;
      fields = { session: this.shape.session, node: this.shape.node, x: p.x, y: p.y, z: p.z,
        qx:q.x,qy:q.y,qz:q.z,qw:q.w };
    }
    // The spring starts before the frame moves to where the arm is now, so
    // the ball springs back from where it was let go.
    if (kind === "tcp") {
      if(this.core.ix.edit?.active){this.ball.position.set(0,0,0);this.ball.quaternion.identity();}
      else this.springBack();
    }
    this.finishDrag(true);
    if (this.spring) this.tcp.enabled = false;
    // Refused, the handles go back to where the app has the objects, and
    // the change left on them goes.
    const refused = (confirmed) => {
      if (confirmed) return;
      this.precision.settled = null;
      this.setShape(this.core.ix.shapeMove);
    };
    this.core.gestures.release(drag.g, fields, kind === "tcp" ? null : refused);
  }

  // The drag ends other than by release: the controls let go of the pointer
  // without it counting as a release.
  letGo(tc, reason) {
    const drag = this.drag;
    if (drag && drag.tc === tc) drag.ending = reason;
    if (tc.dragging) tc.pointerUp();
    if (drag && drag.tc === tc) {
      this.finishDrag(false);
      if (drag.kind === "tcp") {
        this.ball.position.set(0, 0, 0);
        this.ball.quaternion.identity();
      }
    }
    this.core.pointer.restoreOrbit();
    this.core.requestRender();
  }

  // A released drag keeps what it shows until the app answers; an aborted
  // one goes back to what the app last sent, as the gesture lets go of the
  // objects it held.
  finishDrag(released) {
    const drag = this.drag;
    if (!drag) return;
    this.drag = null;
    if (drag.members && !released) {
      this.selectionFrame.position.copy(drag.center);
      this.selectionFrame.quaternion.identity();
    }
    for (const tc of this.all()) {
      tc.enabled = true;
      tc.getHelper().visible = !!tc.object;
    }
    this.precision.end(released);
    if (drag.kind === "tcp") {
      if (this.placed) this.place(this.rev, ...this.placed);
    }
  }

  // The ball's pose in its frame, the drag's delta in the tool frame.
  ballPose() {
    const p = this.ball.position;
    const r = new THREE.Euler().setFromQuaternion(this.ball.quaternion, "XYZ");
    return { x: p.x, y: p.y, z: p.z, rx: r.x, ry: r.y, rz: r.z };
  }

  // Whether the drag's last pose was out of reach, which tints its spring
  // back, though the answer comes after the spring has begun; a jog that
  // missed leaves no change on the handle. An edited target shows it.
  setMiss(missed) {
    this.miss = missed;
    this.paintBall();
    if (missed && !this.core.ix.edit?.active && this.precision.settled?.tc === this.tcp) this.precision.settled = null;
    const colors = this.core.ix.colors || {};
    if (this.spring && missed && !this.spring.hot && colors.miss) {
      this.spring.hot = new THREE.Color(colors.miss);
    }
    this.core.requestRender();
  }

  // ---- marks and spring -------------------------------------------------

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
    // Where it was let go, in the world: the frame may move under the spring.
    const world = this.ball.getWorldPosition(new THREE.Vector3());
    this.spring = { world, start: performance.now(), own, hot };
    this.core.animate();
  }

  step(now) {
    const s = this.spring;
    if (!s) return false;
    const t = Math.min(1, (now - s.start) / SPRING_MS);
    this.frame.updateWorldMatrix(true, false);
    this.ball.position.copy(this.frame.worldToLocal(released.copy(s.world))).multiplyScalar(1 - easeOutBack(t));
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
    if (!this.drag) this.tcp.enabled = true;
  }

  dispose() {
    for (const tc of this.all()) {
      tc.detach();
      tc.getHelper().removeFromParent();
      tc.dispose();
    }
    this.precision.dispose();
    this.frame.removeFromParent();
    this.selectionFrame.removeFromParent();
    this.ball.geometry.dispose();
    this.ball.material.dispose();
  }
}
