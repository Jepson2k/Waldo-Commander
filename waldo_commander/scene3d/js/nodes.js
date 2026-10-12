// The scene's nodes as the browser holds them: each record keeps the three.js
// object and the base state Python last sent. An effect or a drag holding a
// property owns it, and ops then change only the base, which the property
// returns to when the last hold lets go.
//
// Isolating objects fades the others without touching their base: it is
// applied here, where the base is written, so an edit does not undo it.

import * as THREE from "three";
import { build, clip, dispose, loadStl, materials, paint } from "./kinds.js";

const DRAWN = new Set(["box", "sphere", "cylinder", "capsule", "lathe", "line", "polyline"]);
// The workcell's objects: what isolating fades.
const OBJECT = /^(shape|install):/;
// What is left of an object isolating leaves out: enough to keep it in place
// as context, too little to read as something to pick.
const FADED = 0.12;

export class Nodes {
  constructor(core) {
    this.core = core;
    this.generation = 0;
    this.records = new Map();
    this.records.set(0, this.record(0, "root", core.scene, null));
    this.joints = [];
    this.isolated = null; // names of the objects isolating leaves unfaded
    this.objectsChanged = false;
  }

  record(id, kind, obj, parent) {
    return {
      id,
      kind,
      obj,
      parent,
      children: new Set(),
      name: "",
      object: false,
      drawable: null,
      base: { p: [0, 0, 0], r: [0, 0, 0, 1], s: [1, 1, 1], m: null, v: 1, l: [] },
      holds: { scale: 0, position: 0, rotation: 0, opacity: 0 },
      held: null, // the drag holding it, until the app answers that drag
      glow: false,
      q: 0,
      axis: null,
      prismatic: false,
      ready: Promise.resolve(true),
      gen: this.generation,
      deleted: false,
    };
  }

  get(id) {
    const rec = this.records.get(id);
    return rec && !rec.deleted ? rec : null;
  }

  byName(name) {
    for (const rec of this.records.values()) if (rec.name === name && !rec.deleted) return rec;
    return null;
  }

  create(id, parentId, kind, args, state) {
    const parent = this.get(parentId);
    if (!parent) {
      console.warn(`scene: no parent ${parentId} for ${kind} ${id}`);
      return;
    }
    const obj = build(kind, args, !!state.w);
    const rec = this.record(id, kind, obj, parent);
    obj.userData.wcId = id;
    if (DRAWN.has(kind)) rec.drawable = obj;
    if (kind === "joint") {
      rec.axis = new THREE.Vector3(...args[0]);
      rec.prismatic = args[1] === "prismatic";
    }
    this.records.set(id, rec);
    parent.children.add(rec);
    this.update(rec, state);
    if (!("v" in state)) this.show(rec);
    if (kind === "joint") this.setJoint(rec, state.q || 0);
    parent.obj.add(obj);
    if (kind === "stl") rec.ready = this.load(rec, args[0], !!state.w);
  }

  // The mesh is built from the record's state when it arrives, not when it
  // was asked for: material, clipping and visibility may have changed since.
  async load(rec, url, wireframe) {
    rec.url = url;
    let drawable;
    try {
      drawable = await loadStl(url, wireframe);
    } catch (error) {
      console.warn(`scene: could not load ${url}`, error);
      return false;
    }
    if (rec.deleted || rec.gen !== this.generation) {
      dispose(drawable);
      return false;
    }
    rec.drawable = drawable;
    this.paint(rec);
    this.clip(rec);
    rec.obj.add(drawable);
    this.core.requestRender();
    return true;
  }

  update(rec, state) {
    const obj = rec.obj;
    if ("n" in state) this.rename(rec, state.n);
    if ("p" in state) {
      rec.base.p = state.p;
      if (!rec.holds.position) obj.position.fromArray(state.p);
    }
    if ("r" in state) {
      rec.base.r = state.r;
      if (!rec.holds.rotation) obj.quaternion.fromArray(state.r);
    }
    if ("s" in state) {
      rec.base.s = state.s;
      if (!rec.holds.scale) obj.scale.fromArray(state.s);
    }
    if ("m" in state) {
      rec.base.m = state.m;
      this.paint(rec);
    }
    if ("v" in state) {
      rec.base.v = state.v;
      this.show(rec);
    }
    if ("g" in state) rec.glow = !!state.g;
    if ("l" in state) {
      rec.base.l = state.l;
      this.clip(rec);
    }
    if (rec.held !== null && ("p" in state || "r" in state || "s" in state)) this.core.gestures.confirm(rec);
    this.core.requestRender();
  }

  rename(rec, name) {
    rec.obj.name = rec.name = name;
    const object = OBJECT.test(name);
    if (object !== rec.object) this.objectsChanged = true;
    rec.object = object;
  }

  // ---- holds --------------------------------------------------------------

  hold(rec, ...properties) {
    for (const property of properties) rec.holds[property]++;
  }

  release(rec, ...properties) {
    for (const property of properties) {
      if (--rec.holds[property] > 0 || rec.deleted) continue;
      if (property === "position") rec.obj.position.fromArray(rec.base.p);
      else if (property === "rotation") rec.obj.quaternion.fromArray(rec.base.r);
      else if (property === "scale") rec.obj.scale.fromArray(rec.base.s);
    }
    this.core.requestRender();
  }

  // ---- what the view changes ----------------------------------------------

  // The object the record is, or is drawn under.
  objectOf(rec) {
    for (let r = rec; r; r = r.parent) if (r.object) return r;
    return null;
  }

  show(rec) {
    const visible = !!rec.base.v;
    if (rec.obj.visible === visible) return;
    rec.obj.visible = visible;
    this.core.casters = NaN;
  }

  paint(rec) {
    if (!rec.drawable || !rec.base.m) return;
    const [color, opacity, side] = rec.base.m;
    const object = this.objectOf(rec);
    const faded = !!object && this.isolated !== null && !this.isolated.has(object.name);
    const shown = rec.holds.opacity ? materials(rec.drawable)[0].opacity : faded ? Math.min(opacity, FADED) : opacity;
    paint(rec.drawable, [color, shown, side]);
    if (!object) return;
    const drawable = rec.drawable;
    drawable.userData.casts ??= drawable.castShadow;
    drawable.userData.faded = faded;
    drawable.castShadow = !faded && drawable.userData.casts;
    for (const mat of materials(drawable)) mat.depthWrite = !faded;
  }

  clip(rec) {
    if (!rec.drawable) return;
    if (!rec.base.l.length && !materials(rec.drawable)[0].clippingPlanes?.length) return;
    clip(rec.drawable, rec.base.l);
    if (rec.base.l.length) this.core.renderer.localClippingEnabled = true;
  }

  setIsolated(names) {
    this.isolated = names;
    for (const rec of this.records.values()) if (rec.drawable && !rec.deleted) this.paint(rec);
    this.core.requestRender();
  }

  delete(id) {
    const rec = this.get(id);
    if (!rec) return;
    rec.parent.children.delete(rec);
    rec.obj.removeFromParent();
    const forget = (r) => {
      r.deleted = true;
      if (r.object) this.objectsChanged = true;
      this.records.delete(r.id);
      this.core.forget(r);
      for (const child of r.children) forget(child);
    };
    forget(rec);
    dispose(rec.obj);
    this.core.requestRender();
  }

  defineJoints(ids) {
    this.joints = ids.map((id) => this.get(id));
  }

  setJoints(values) {
    values.forEach((value, i) => {
      const rec = this.joints[i];
      if (value !== null && rec) this.setJoint(rec, value);
    });
    this.core.requestRender();
  }

  // The app's value for a joint.
  setJoint(rec, value) {
    rec.q = value;
    this.applyJoint(rec);
  }

  applyJoint(rec, node = rec.obj, q = rec.q) {
    if (rec.kind !== "joint") return;
    if (rec.prismatic) node.position.copy(rec.axis).multiplyScalar(q);
    else node.quaternion.setFromAxisAngle(rec.axis, q);
    this.core.requestRender();
  }

  reset() {
    this.generation++;
    const root = this.records.get(0);
    for (const rec of [...root.children]) this.delete(rec.id);
    root.children.clear();
    this.joints = [];
    this.objectsChanged = false;
  }

  idle() {
    return Promise.all([...this.records.values()].map((rec) => rec.ready));
  }
}
