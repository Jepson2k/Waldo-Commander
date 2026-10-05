// The scene's nodes as the browser holds them: each record keeps the three.js
// object and the base state Python last sent. An effect running on a
// property holds it, and ops then change only the base, which the effect
// returns to when it ends.

import * as THREE from "three";
import { build, clip, dispose, loadStl, paint } from "./kinds.js";

const DRAWN = new Set(["box", "sphere", "cylinder", "capsule", "lathe", "line", "polyline"]);

export class Nodes {
  constructor(core) {
    this.core = core;
    this.generation = 0;
    this.records = new Map();
    this.records.set(0, this.record(0, "root", core.scene, null));
    this.joints = [];
  }

  record(id, kind, obj, parent) {
    return {
      id,
      kind,
      obj,
      parent,
      children: new Set(),
      name: "",
      drawable: null,
      base: { p: [0, 0, 0], r: [0, 0, 0, 1], s: [1, 1, 1], m: null, v: 1, l: [] },
      holds: { scale: 0, position: 0, opacity: 0 },
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
    if (kind === "joint") this.setJoint(rec, state.q || 0);
    parent.obj.add(obj);
    if (kind === "stl") rec.ready = this.load(rec, args[0], !!state.w);
  }

  // The mesh is built from the record's state when it arrives, not when it
  // was asked for: material, clipping and visibility may have changed since.
  async load(rec, url, wireframe) {
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
    if (rec.base.m) paint(drawable, rec.base.m);
    if (rec.base.l.length) this.clip(rec, rec.base.l);
    rec.obj.add(drawable);
    this.core.requestRender();
    return true;
  }

  update(rec, state) {
    const obj = rec.obj;
    if ("n" in state) obj.name = rec.name = state.n;
    if ("p" in state) {
      rec.base.p = state.p;
      if (!rec.holds.position) obj.position.fromArray(state.p);
    }
    if ("r" in state) {
      rec.base.r = state.r;
      obj.quaternion.fromArray(state.r);
    }
    if ("s" in state) {
      rec.base.s = state.s;
      if (!rec.holds.scale) obj.scale.fromArray(state.s);
    }
    if ("m" in state) {
      rec.base.m = state.m;
      if (rec.drawable) {
        const [color, opacity, side] = state.m;
        paint(rec.drawable, [color, rec.holds.opacity ? rec.drawable.material.opacity : opacity, side]);
      }
    }
    if ("v" in state) obj.visible = !!(rec.base.v = state.v);
    if ("g" in state) rec.glow = !!state.g;
    if ("l" in state) {
      rec.base.l = state.l;
      if (rec.drawable) this.clip(rec, state.l);
    }
    this.core.requestRender();
  }

  clip(rec, planes) {
    clip(rec.drawable, planes);
    if (planes.length) this.core.renderer.localClippingEnabled = true;
  }

  delete(id) {
    const rec = this.get(id);
    if (!rec) return;
    rec.parent.children.delete(rec);
    rec.obj.removeFromParent();
    const forget = (r) => {
      r.deleted = true;
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

  setJoint(rec, value) {
    rec.q = value;
    if (rec.kind !== "joint") return;
    if (rec.prismatic) rec.obj.position.copy(rec.axis).multiplyScalar(value);
    else rec.obj.quaternion.setFromAxisAngle(rec.axis, value);
  }

  reset() {
    this.generation++;
    const root = this.records.get(0);
    for (const rec of [...root.children]) this.delete(rec.id);
    root.children.clear();
    this.joints = [];
  }

  idle() {
    return Promise.all([...this.records.values()].map((rec) => rec.ready));
  }
}
