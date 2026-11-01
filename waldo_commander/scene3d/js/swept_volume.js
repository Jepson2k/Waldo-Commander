import * as THREE from "three";
import { RecordedGhost, setBack } from "./recorded_ghost.js";
import { finite } from "./handles.js";

const SAMPLE_COUNT = 24;

export class SweptVolume {
  constructor(inspection) {
    this.inspection = inspection;
    this.core = inspection.core;
    this.block = null;
    this.root = null;
    this.samples = [];
    this.materials = [];
    this.ghost = new RecordedGhost(this.core, "sweep:pose", () => this.build());
  }

  toggle(revision, index) {
    const data = this.inspection.data, block = data?.records[0].blocks[index];
    if (revision !== data?.revision || !block || block.instant || block.end - block.start < 2) return;
    const hide = this.block === index;
    this.dispose();
    if (hide) return;
    this.block = index;
    const tool = `${block.tool.key}/${block.tool.variant}`;
    this.ghost.build({ ...data, records: [], tools: { [tool]: data.tools[tool] } });
  }

  build() {
    const ghost = this.ghost, data = this.inspection.data;
    if (this.root || this.block === null || !ghost.ready || !data) return;
    if ([...ghost.toolGroups.values()].some(group => !group.parent)) return;
    const record = data.records[0], block = record.blocks[this.block];
    const rows = [], count = Math.min(SAMPLE_COUNT, block.end - block.start);
    const first = record.source[block.start], last = record.source[block.end - 1];
    let row = block.start;
    for (let i = 0; i < count; i++) {
      const at = first + (last - first) * i / (count - 1);
      while (row < block.end - 1 && Math.abs(record.source[row + 1] - at) < Math.abs(record.source[row] - at)) row++;
      if (rows.at(-1) !== row) rows.push(row);
    }
    if (rows.some(r => !record.q.slice(r * record.dof, (r + 1) * record.dof).every(finite))) {
      this.dispose();
      return;
    }
    const depth = setBack(new THREE.MeshBasicMaterial({ transparent: true, colorWrite: false }));
    // Depth chooses the nearest surface; stencil also covers coincident links only once.
    const layer = color => setBack(new THREE.MeshLambertMaterial({
      color, transparent: true, opacity: .24, depthWrite: false, depthFunc: THREE.EqualDepth,
      stencilWrite: true, stencilRef: 0, stencilFunc: THREE.EqualStencilFunc,
      stencilZPass: THREE.IncrementStencilOp,
    }));
    const clear = layer(data.color), collision = layer(data.collisionColor);
    this.materials = [depth, clear, collision];
    this.root = new THREE.Group(); this.root.name = "sweep:volume";
    for (const row of rows) {
      ghost.articulate(record, row, block);
      const sample = ghost.root.clone(true);
      sample.visible = true;
      const colliding = block.collisionRow !== null && record.source[row] >= block.collisionRow;
      sample.traverse(o => {
        if (!o.isMesh) return;
        const prepass = o.material === ghost.depth;
        o.material = prepass ? depth : colliding ? collision : clear;
        o.renderOrder = prepass ? 880 : colliding ? 881 : 882;
        o.raycast = () => {};
      });
      this.samples.push({ row, colliding, object: sample });
      this.root.add(sample);
    }
    this.core.scene.add(this.root);
    this.core.requestRender();
  }

  dispose() {
    this.root?.removeFromParent();
    this.root?.clear();
    this.root = null;
    this.samples = [];
    for (const material of this.materials) material.dispose();
    this.materials = [];
    this.ghost.dispose();
    this.block = null;
    this.core.requestRender();
  }
}
