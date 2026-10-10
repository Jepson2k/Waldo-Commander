import * as THREE from "three";
import { dispose, loadStl } from "./kinds.js";
import { finite } from "./handles.js";

const linear = new THREE.Vector3();
// The app turns a mesh by Rz·Ry·Rx, three.js's "ZYX" order.
const ORDER = "ZYX";

// The ghost lies this far behind where it is drawn, so where it and the arm
// coincide the arm wins.
const BEHIND = 2;

// A see-through copy of the arm, its tools and the objects it carries, posed
// from a recorded sample. It is one layer, its own hidden surfaces left out,
// and whatever is in front of it covers it: where it matches the arm, only
// the arm shows. It is built from the scene's nodes once they have loaded;
// `onReady` hears when there is something to pose.
export class RecordedGhost {
  constructor(core, name, onReady) {
    this.core = core;
    this.name = name;
    this.onReady = onReady;
    this.data = null;
    this.root = null;
    this.material = null;
    this.depth = null;
    this.clones = new Map();
    this.toolGroups = new Map();
    this.objectClones = new Map();
  }

  get ready() {
    return this.clones.size > 0;
  }

  build(data) {
    if (this.data === data) return;
    this.dispose();
    this.data = data;
    const behind = { polygonOffset: true, polygonOffsetFactor: BEHIND, polygonOffsetUnits: BEHIND };
    this.material = new THREE.MeshLambertMaterial({ transparent: true, opacity: .4, depthWrite: false, ...behind });
    this.depth = new THREE.MeshBasicMaterial({ transparent: true, colorWrite: false, ...behind });
    this.root = new THREE.Group();
    this.root.name = this.name;
    this.root.visible = false;
    this.core.scene.add(this.root);
    this.core.nodes.idle().then(() => {
      if (this.data !== data) return;
      for (const id of [...data.meshes, data.anchor, ...data.joints]) if (id !== null) this.clone(id);
      for (const record of data.records) for (const obj of record.objects) {
        if (this.objectClones.has(obj.node)) continue;
        const rec = this.core.nodes.get(obj.node);
        if (!rec?.drawable) continue;
        // Recorded object poses are world poses, even for a currently held object.
        // The live node may be mid reveal, so its authored scale is taken.
        const copy = this.dress(rec.drawable.clone()); copy.scale.fromArray(rec.base.s);
        copy.visible = false;
        this.root.add(copy); this.objectClones.set(obj.node, copy);
      }
      for (const [key, tool] of Object.entries(data.tools)) this.loadTool(key, tool, data);
      this.onReady();
    });
  }

  // A copy drawn as the ghost: its depth first, then its colour where that
  // depth is what is nearest.
  dress(mesh) {
    mesh.material = this.material; mesh.castShadow = false; mesh.receiveShadow = false; mesh.renderOrder = 901;
    if (mesh.isMesh) {
      const depth = new THREE.Mesh(mesh.geometry, this.depth);
      depth.renderOrder = 900; depth.raycast = () => {};
      mesh.add(depth);
    }
    return mesh;
  }

  clone(id) {
    if (this.clones.has(id)) return this.clones.get(id);
    const rec = this.core.nodes.get(id);
    if (!rec || id === 0) return this.root;
    const parent = this.clone(rec.parent.id);
    const copy = new THREE.Group();
    copy.position.fromArray(rec.base.p); copy.quaternion.fromArray(rec.base.r); copy.scale.fromArray(rec.base.s);
    copy.name = `${this.name}:${rec.name || id}`;
    if (rec.drawable) {
      const mesh = this.dress(rec.drawable.clone());
      if (rec.drawable === rec.obj) { mesh.position.set(0,0,0); mesh.quaternion.identity(); mesh.scale.set(1,1,1); }
      copy.add(mesh);
    }
    parent.add(copy); this.clones.set(id, copy);
    return copy;
  }

  async loadTool(key, tool, data) {
    const group = new THREE.Group(); group.visible = false;
    this.toolGroups.set(key, group);
    const roles = new Map();
    for (const spec of tool.meshes) {
      // Counted before loading, as the app counts every mesh with a file, so
      // a jaw whose STL fails does not flip its partner's direction.
      const count = roles.get(spec.role) || 0; roles.set(spec.role, count + 1);
      try {
        const mesh = await loadStl(spec.url, false);
        if (this.data !== data) { dispose(mesh); return; }
        mesh.material.dispose(); this.dress(mesh); mesh.scale.setScalar(data.scale);
        mesh.position.fromArray(spec.origin); mesh.rotation.set(...spec.rpy, ORDER);
        const motion = tool.motions.find((m) => m.role === spec.role);
        mesh.userData.inspection = {spec, motion, sign:motion?.symmetric && count % 2 ? -1 : 1};
        group.add(mesh);
      } catch (error) { console.warn("Inspection tool mesh unavailable", error); }
    }
    if (this.data !== data) return;
    this.clone(data.anchor).add(group);
    this.onReady();
  }

  // The arm, tool and objects as record `r` has them at `row` of `block`.
  articulate(r, row, block) {
    const data = this.data, nodes = this.core.nodes;
    data.joints.forEach((id, i) => {
      const node = this.clones.get(id), rec = nodes.get(id), q = r.q[row * r.dof + i];
      if (node && rec && finite(q)) nodes.applyJoint(rec, node, q);
    });
    for (const node of this.objectClones.values()) node.visible = false;
    for (const object of r.objects) {
      const node = this.objectClones.get(object.node); if (!node) continue;
      const i = Math.min(row, object.poses.length / 7 - 1) * 7, p = object.poses;
      node.visible = i >= 0 && [0, 1, 2, 3, 4, 5, 6].every((k) => finite(p[i + k]));
      if (node.visible) {
        node.position.set(p[i], p[i + 1], p[i + 2]); node.quaternion.set(p[i + 4], p[i + 5], p[i + 6], p[i + 3]);
      }
    }
    const tool = `${block.tool.key}/${block.tool.variant}`;
    for (const [key, group] of this.toolGroups) {
      group.visible = key === tool;
      if (!group.visible) continue;
      for (const mesh of group.children) {
        const { spec, motion, sign } = mesh.userData.inspection;
        mesh.position.fromArray(spec.origin); mesh.rotation.set(...spec.rpy, ORDER);
        if (!motion || !finite(r.closed[row])) continue;
        const f = Math.max(0, Math.min(1, r.closed[row])) * sign;
        if (motion.kind === "LinearMotion") mesh.position.addScaledVector(linear.fromArray(motion.axis), -motion.travel_m * f);
        else mesh.rotation.set(...spec.rpy.map((v, i) => v + motion.axis[i] * motion.travel_rad * f), ORDER);
      }
    }
  }

  set visible(on) {
    if (this.root) this.root.visible = on;
  }

  get visible() {
    return !!this.root?.visible;
  }

  dispose() {
    this.root?.removeFromParent();
    for (const group of this.toolGroups.values()) group.traverse((o) => o.geometry?.dispose());
    this.material?.dispose(); this.depth?.dispose();
    this.data = this.root = this.material = this.depth = null;
    this.clones.clear(); this.toolGroups.clear(); this.objectClones.clear();
  }
}
