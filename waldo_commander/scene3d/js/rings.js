// The ring shown for the joint whose link is under the pointer: a track over
// the joint's travel, a wider invisible grip, a knob at the joint's value
// with its label, and dots at whole steps either side of where the knob is,
// or of where the drag began.

import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { ringLabel } from "./handles.js";

const TUBE_M = 0.0015;
const GRIP_TUBE_M = 0.012;
const KNOB_RADIUS_M = 0.006;
const LABEL_GAP_M = 0.03;
const TICKS_EACH_SIDE = 8;

export const LABEL_STYLE =
  "background: var(--wc-glass); color: var(--wc-text);" +
  " border: 1px solid var(--wc-glass-border); border-radius: var(--wc-radius-sm);" +
  " padding: 2px 6px; font: 500 12px/16px var(--wc-font-mono);" +
  " white-space: pre; pointer-events: none;";

export function label(name) {
  const div = document.createElement("div");
  div.style.cssText = LABEL_STYLE;
  const object = new CSS2DObject(div);
  object.name = name;
  return object;
}

export class Ring {
  // `spec`: {u, frame, radius, lo, hi, panel, name, R}; R turns +z onto the axis.
  constructor(core, spec, q, stepDeg) {
    this.core = core;
    this.spec = spec;
    this.u = spec.u;
    this.q = q;
    this.q0 = null;
    const colors = core.ix.colors || {};
    const { radius, lo, hi } = spec;
    const [start, arc] = lo === null || hi === null ? [0, 2 * Math.PI] : [lo, hi - lo];
    this.group = new THREE.Group();
    this.group.name = `jog:dial:${spec.u}`;
    const R = spec.R;
    this.group.quaternion.setFromRotationMatrix(
      new THREE.Matrix4().set(R[0][0], R[0][1], R[0][2], 0, R[1][0], R[1][1], R[1][2], 0, R[2][0], R[2][1], R[2][2], 0, 0, 0, 0, 1),
    );
    const track = new THREE.Mesh(
      new THREE.TorusGeometry(radius, TUBE_M, 8, 96, arc),
      new THREE.MeshPhongMaterial({ color: colors.action, transparent: true, opacity: 0.55 }),
    );
    track.rotation.z = start;
    this.grip = new THREE.Mesh(new THREE.TorusGeometry(radius, GRIP_TUBE_M, 6, 64, arc), new THREE.MeshBasicMaterial());
    this.grip.rotation.z = start;
    this.grip.visible = false;
    this.knob = new THREE.Group();
    this.knob.name = `jog:dial:${spec.u}:knob`;
    const ball = new THREE.Mesh(
      new THREE.SphereGeometry(KNOB_RADIUS_M, 16, 12),
      new THREE.MeshPhongMaterial({ color: colors.action, transparent: true }),
    );
    ball.position.x = radius;
    this.label = label(`jog:dial:${spec.u}:label`);
    this.label.position.x = radius + LABEL_GAP_M;
    this.knob.add(ball, this.label);
    this.ticks = null;
    this.group.add(track, this.grip, this.knob);
    this.place(q);
    this.drawTicks(stepDeg);
    this.stepDeg = stepDeg;
    this.relabel();
    const frame = core.nodes.get(spec.frame);
    if (frame) frame.obj.add(this.group);
  }

  // What a press must hit to grab the ring: the track or its wider grip.
  get grips() {
    return [this.grip];
  }

  drawTicks(stepDeg) {
    if (this.ticks) {
      this.group.remove(this.ticks);
      this.ticks.geometry.dispose();
      this.ticks.material.dispose();
    }
    const s = (stepDeg * Math.PI) / 180;
    const r = this.spec.radius;
    const points = [];
    for (let k = -TICKS_EACH_SIDE; k <= TICKS_EACH_SIDE; k++) points.push(r * Math.cos(k * s), r * Math.sin(k * s), 0);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
    this.ticks = new THREE.Points(
      geometry,
      new THREE.PointsMaterial({
        size: Math.max(0.0008, Math.min(0.003, 0.5 * r * s)),
        color: (this.core.ix.colors || {}).muted,
        transparent: true,
      }),
    );
    this.ticks.name = `jog:dial:${this.u}:ticks`;
    this.ticks.rotation.z = this.q0 === null ? this.q : this.q0;
    this.group.add(this.ticks);
  }

  setStep(stepDeg) {
    this.stepDeg = stepDeg;
    this.drawTicks(stepDeg);
    this.relabel();
  }

  place(q) {
    this.q = q;
    this.knob.rotation.z = q;
    if (this.q0 === null && this.ticks) this.ticks.rotation.z = q;
    this.relabel();
    this.core.requestRender();
  }

  grab() {
    this.q0 = this.q;
    this.relabel();
  }

  letGo() {
    this.q0 = null;
    if (this.ticks) this.ticks.rotation.z = this.q;
    this.relabel();
  }

  relabel() {
    if (this.stepDeg === undefined) return;
    const text = ringLabel(this.spec.name, this.spec.panel, this.q, this.q0, this.stepDeg);
    if (this.label.element.textContent !== text) this.label.element.textContent = text;
  }

  // The pointer's position on the ring's plane, in the ring's frame; null
  // where the ray runs along the plane.
  onPlane(raycaster) {
    this.group.updateWorldMatrix(true, false);
    const normal = new THREE.Vector3(0, 0, 1).transformDirection(this.group.matrixWorld);
    const origin = new THREE.Vector3().setFromMatrixPosition(this.group.matrixWorld);
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin);
    const hit = raycaster.ray.intersectPlane(plane, new THREE.Vector3());
    if (!hit) return null;
    const local = this.group.worldToLocal(hit);
    return [local.x, local.y];
  }

  dispose() {
    this.group.removeFromParent();
    this.label.element.remove();
    this.group.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    });
    this.core.requestRender();
  }
}
