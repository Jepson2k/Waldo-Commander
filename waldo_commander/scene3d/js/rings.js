// Joint instruments stay in the joint's plane; their lettering faces the camera.
//
// A joint's ring is built the first time it shows and kept while its spec and
// the colours stand; showing it again only hangs it back on its frame.

import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { panelDegrees, ringLabel, signed, unsigned } from "./handles.js";
import { setAttr, setStyle, setText } from "./dom.js";

const GRIP_TUBE_M = 0.012;
const TAU = Math.PI * 2;
const RAD = Math.PI / 180;
const SWEEP_SEGMENTS = 128;
// Snapping never reaches a coarser step than the scale's minor ticks.
const MINOR_DEG = 10;

const center = new THREE.Vector3();
const tip = new THREE.Vector3();
const axisX = new THREE.Vector3();
const axisY = new THREE.Vector3();
const at = [0, 0, 0];
const mid = [0, 0, 0];

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

function strokesGeometry(radius, strokes, width) {
  const points = [];
  for (const [q, inner, outer] of strokes) {
    const a = radius * inner, b = radius * outer;
    // Round strokes keep their weight when the joint plane is almost edge-on.
    const cylinder = new THREE.CylinderGeometry(radius * width, radius * width, b - a, 8);
    const stroke = cylinder.toNonIndexed();
    cylinder.dispose();
    stroke.rotateZ(q - Math.PI / 2);
    stroke.translate((a + b) * Math.cos(q) / 2, (a + b) * Math.sin(q) / 2, 0);
    points.push(...stroke.attributes.position.array);
    stroke.dispose();
  }
  return new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
}

const overlaps = (a, b) => a.left < b.right + 6 && a.right > b.left - 6 && a.top < b.bottom + 4 && a.bottom > b.top - 4;

export class Ring {
  // `spec`: {u, frame, radius, lo, hi, panel, name, R}; R turns +z onto the axis.
  constructor(core, spec) {
    this.core = core;
    this.spec = spec;
    this.u = spec.u;
    this.q = 0;
    this.q0 = null;
    this.stepDeg = null;
    this.scaleLabels = [];
    this.ends = []; // the outer end of each tick, in the ring's plane
    const colors = core.ix.colors || {};
    const { radius, lo, hi } = spec;
    const [start, arc] = lo === null || hi === null ? [0, TAU] : [lo, Math.min(TAU, hi - lo)];
    this.group = new THREE.Group();
    this.group.name = `jog:dial:${spec.u}`;
    const R = spec.R;
    this.group.quaternion.setFromRotationMatrix(
      new THREE.Matrix4().set(R[0][0], R[0][1], R[0][2], 0, R[1][0], R[1][1], R[1][2], 0, R[2][0], R[2][1], R[2][2], 0, 0, 0, 0, 1),
    );
    const track = new THREE.Mesh(
      new THREE.TorusGeometry(radius * .9925, radius * .0075, 8, 128, arc),
      this.material(colors.action, 1),
    );
    const trackHalo = this.halo(new THREE.TorusGeometry(radius * .9925, radius * .0125, 8, 128, arc), 1);
    trackHalo.rotation.z = track.rotation.z = start;
    track.renderOrder = 2;
    track.name = `jog:dial:${spec.u}:track`;
    this.grip = new THREE.Mesh(new THREE.TorusGeometry(radius, GRIP_TUBE_M, 6, 64, arc), new THREE.MeshBasicMaterial());
    this.grip.rotation.z = start;
    this.grip.visible = false;
    this.knob = new THREE.Group();
    this.knob.name = `jog:dial:${spec.u}:knob`;
    const needle = new THREE.Mesh(strokesGeometry(radius, [[0, .10, 1]], .007), this.material(colors.action, 1));
    const needleHalo = this.halo(strokesGeometry(radius, [[0, .10, 1]], .012), 3);
    needle.renderOrder = 4;
    const ball = new THREE.Mesh(new THREE.SphereGeometry(radius * .042, 16, 12), this.material(colors.action, 1));
    const ballHalo = this.halo(new THREE.SphereGeometry(radius * .047, 16, 12), 3);
    ballHalo.position.x = ball.position.x = radius;
    ball.renderOrder = 4;
    this.label = label(`jog:dial:${spec.u}:label`);
    this.label.element.style.cssText += "padding:6px 10px;border:none;border-radius:var(--wc-radius-md);background:var(--wc-surface);text-shadow:0 1px 2px var(--wc-scene-bg);";
    const readout = document.createElement("div");
    readout.style.cssText = "display:grid;grid-template-columns:max-content max-content;column-gap:12px;align-items:baseline;";
    this.title = document.createElement("div");
    this.title.style.cssText = "grid-column:1 / -1;font:500 13px/18px var(--wc-font-sans);color:var(--wc-text-muted)";
    this.value = document.createElement("div");
    this.value.style.cssText = "font:600 24px/30px var(--wc-font-mono);color:var(--wc-action)";
    this.delta = document.createElement("div");
    this.delta.style.cssText = "font:500 16px/24px var(--wc-font-mono);color:var(--wc-text)";
    readout.append(this.title, this.value, this.delta);
    this.label.element.replaceChildren(readout);
    this.label.position.x = radius;
    this.knob.add(needleHalo, needle, ballHalo, ball, this.label);
    const dashes = Array.from({length: 9}, (_, i) => [0, .10 + i * .10, .16 + i * .10]);
    const startStroke = new THREE.Mesh(strokesGeometry(radius, dashes, .006), this.material(colors.action, 1));
    startStroke.renderOrder = 3;
    this.startNeedle = new THREE.Group();
    this.startNeedle.add(this.halo(strokesGeometry(radius, dashes, .011), 2), startStroke);
    this.startNeedle.name = `jog:dial:${spec.u}:start`;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(SWEEP_SEGMENTS * 18), 3));
    // The sweep never leaves the ring.
    geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), radius);
    this.sweep = new THREE.Mesh(geometry, this.material(colors.action, .16));
    this.sweep.name = `jog:dial:${spec.u}:sweep`;
    this.group.add(trackHalo, track, this.grip, this.sweep, this.startNeedle, this.knob);
    this.drawScale();
    this.group.traverse((o) => { if(o.renderOrder<1000)o.renderOrder += 1000; });
  }

  material(color, opacity) {
    return new THREE.MeshBasicMaterial({color, opacity, transparent: true, side: THREE.DoubleSide, depthTest: false, depthWrite: false, forceSinglePass: true});
  }

  halo(geometry, renderOrder) {
    const material = this.material(this.core.config.background, .4);
    // Back faces would double the shadow's opacity over the robot.
    material.side = THREE.FrontSide;
    const mesh = new THREE.Mesh(geometry, material);
    mesh.renderOrder = renderOrder;
    return mesh;
  }

  // On the joint's frame at `q`, snapping by `stepDeg`.
  show(frame, q, stepDeg) {
    this.stepDeg = stepDeg;
    this.q0 = null;
    frame.add(this.group);
    this.place(q);
    this.fitScreen();
  }

  // The labels' divs live outside the scene: the renderer only updates
  // attached objects, so a detached ring's would stay painted where they were.
  // It puts them back when the ring is drawn again.
  hide() {
    this.group.removeFromParent();
    this.group.traverse((o) => {
      if (o.isCSS2DObject) o.element.remove();
    });
    this.core.overlays.mark("dial", null);
    this.core.requestRender();
  }

  fitScreen() {
    if (this.q0 !== null) return;
    const pixels = this.spec.radius / this.core.metresPerPixel(this.group.getWorldPosition(center));
    const scale = Math.max(145, Math.min(185, pixels)) / Math.max(pixels, 1);
    if (Math.abs(this.group.scale.x - scale) > 1e-5) {
      this.group.scale.setScalar(scale);
      this.core.requestRender();
    }
  }

  // What a press must hit to grab the ring: the track or its wider grip.
  get grips() {
    return [this.grip];
  }

  drawScale() {
    const r = this.spec.radius;
    const lo = this.spec.lo ?? -Math.PI, hi = this.spec.hi ?? Math.PI;
    const strokes = [];
    const tick = (q, inner, outer = 1) => strokes.push([q, inner, outer]);
    const [, sign, offset] = this.spec.panel;
    const bounds = [panelDegrees(this.spec.panel, lo), panelDegrees(this.spec.panel, hi)].sort((a, b) => a - b);
    const major = MINOR_DEG * 3;
    for (let deg = Math.ceil(bounds[0] / MINOR_DEG) * MINOR_DEG; deg <= bounds[1]; deg += MINOR_DEG) {
      const q = (deg * sign + offset) * RAD;
      tick(q, Math.abs(deg/major-Math.round(deg/major))<1e-6 ? .92 : .97);
      if (Math.abs(deg/major-Math.round(deg/major))<1e-6 && q - lo > 12 * RAD && hi - q > 12 * RAD) this.scaleLabel(q, `${deg}°`, false);
    }
    if (this.spec.lo !== null && this.spec.hi !== null) {
      const text=q=>`${unsigned(panelDegrees(this.spec.panel,q)).replace(/\.0$/,"")}°`;
      if(Math.abs(hi-lo-Math.PI*2)<1e-5) {
        tick(lo,.91,1.04);this.scaleLabel(lo,`${text(lo)} / ${text(hi)}`,true);
      } else for (const q of [lo, hi]) {
        tick(q, .91, 1.04);this.scaleLabel(q,text(q),true);
      }
    }
    this.ends = strokes.map(([q, , outer]) => [r * outer * Math.cos(q), r * outer * Math.sin(q)]);
    // Limits keep their place on the scale before the degrees do.
    this.scaleLabels.sort((a, b) => Number(b.userData.limit) - Number(a.userData.limit));
    const colors = this.core.ix.colors || {};
    this.ticks = new THREE.Mesh(strokesGeometry(r, strokes, .006), this.material(colors.action, 1));
    this.ticks.renderOrder = 1002;
    this.ticks.name = `jog:dial:${this.u}:ticks`;
    this.tickHalo=this.halo(strokesGeometry(r, strokes, .011),1001);
    this.group.add(this.tickHalo, this.ticks);
  }

  scaleLabel(q, text, limit) {
    const object = label(`jog:dial:${this.u}:${limit ? "limit" : "scale"}:${text}`);
    object.element.textContent = text;
    object.element.style.cssText += `border:none;padding:2px 5px;font:500 14px/20px var(--wc-font-mono);background:var(--wc-surface);color:var(--wc-${limit ? "text" : "text-muted"});text-shadow:0 1px 2px var(--wc-scene-bg);`;
    object.position.set(Math.cos(q), Math.sin(q), 0).multiplyScalar(this.spec.radius * 1.17);
    object.userData.limit = limit;
    this.scaleLabels.push(object);
    this.group.add(object);
  }

  setStep(stepDeg) {
    if(stepDeg===this.stepDeg)return;
    this.stepDeg = stepDeg;
    this.relabel();
  }

  place(q) {
    this.q = q;
    this.knob.rotation.z = q;
    this.drawDelta();
    this.relabel();
    this.core.requestRender();
  }

  grab() {
    this.q0 = this.q;
    this.drawDelta();
    this.relabel();
    this.core.requestRender();
  }

  letGo() {
    this.q0 = null;
    this.drawDelta();
    this.relabel();
    this.core.requestRender();
  }

  relabel() {
    const deg = panelDegrees(this.spec.panel, this.q);
    const title = `${this.spec.name} · ${this.q0 === null ? "actual" : "target"}`;
    setText(this.title, title);
    setText(this.value, `${unsigned(deg)}°`);
    setText(this.delta, this.q0 === null ? "" : `Δ ${signed(deg - panelDegrees(this.spec.panel, this.q0))}°`);
    setAttr(this.label.element, "aria-label", ringLabel(title, this.spec.panel, this.q, this.q0, this.stepDeg));
  }

  drawDelta() {
    this.startNeedle.visible = this.sweep.visible = this.q0 !== null;
    if (this.q0 === null) return;
    this.startNeedle.rotation.z = this.q0;
    const delta = Math.max(-TAU, Math.min(TAU, this.q - this.q0));
    const points = this.sweep.geometry.attributes.position, array = points.array;
    const outer = this.spec.radius * .85, inner = this.spec.radius * .10;
    for (let k = 0, i = 0; k < SWEEP_SEGMENTS; k++) {
      const a = this.q0 + delta * k / SWEEP_SEGMENTS, b = this.q0 + delta * (k + 1) / SWEEP_SEGMENTS;
      const ac = Math.cos(a), as = Math.sin(a), bc = Math.cos(b), bs = Math.sin(b);
      array[i++] = inner * ac; array[i++] = inner * as; array[i++] = 0;
      array[i++] = outer * ac; array[i++] = outer * as; array[i++] = 0;
      array[i++] = outer * bc; array[i++] = outer * bs; array[i++] = 0;
      array[i++] = inner * ac; array[i++] = inner * as; array[i++] = 0;
      array[i++] = outer * bc; array[i++] = outer * bs; array[i++] = 0;
      array[i++] = inner * bc; array[i++] = inner * bs; array[i++] = 0;
    }
    points.needsUpdate = true;
  }

  // The scale's labels show where they do not crowd each other; the readout
  // sits past the ticks and those labels, along the needle. Where they are
  // goes to the overlays for the cards to keep clear of. Positions come from
  // the projection, so this reads only the readout's size from the page.
  layoutLabels() {
    const overlays = this.core.overlays;
    const occupied = [];
    for (const object of this.scaleLabels) {
      const element = object.element;
      if (!element.wcSize?.[0]) element.wcSize = [element.offsetWidth, element.offsetHeight];
      const [w, h] = element.wcSize;
      const [x, y, z] = overlays.project(object.getWorldPosition(tip), at);
      const box = { left: x - w / 2, top: y - h / 2, right: x + w / 2, bottom: y + h / 2 };
      const shown = Math.abs(z) <= 1 && !occupied.some((other) => overlaps(box, other));
      setStyle(element, "visibility", shown ? "visible" : "hidden");
      if (shown) occupied.push(box);
    }
    const matrix = this.group.matrixWorld;
    const [cx, cy] = overlays.project(center.setFromMatrixPosition(matrix), mid);
    const [tx, ty] = overlays.project(tip.set(this.spec.radius * Math.cos(this.q), this.spec.radius * Math.sin(this.q), 0).applyMatrix4(matrix), at);
    const distance = Math.hypot(tx - cx, ty - cy);
    const dx = distance > 1 ? (tx - cx) / distance : 0;
    const dy = distance > 1 ? (ty - cy) / distance : -1;
    let reach = distance + 8;
    for (const [ex, ey] of this.ends) {
      const [x, y] = overlays.project(tip.set(ex, ey, 0).applyMatrix4(matrix), at);
      reach = Math.max(reach, (x - cx) * dx + (y - cy) * dy);
    }
    for (const box of occupied) {
      reach = Math.max(reach,
        Math.max((box.left - cx) * dx, (box.right - cx) * dx) +
        Math.max((box.top - cy) * dy, (box.bottom - cy) * dy));
    }
    const element = this.label.element;
    const w = element.offsetWidth, h = element.offsetHeight;
    // A screen-space gap remains legible when the joint plane is foreshortened.
    reach += 12 + (Math.abs(dx) * w + Math.abs(dy) * h) / 2;
    const [lx, ly, lz] = overlays.project(this.label.getWorldPosition(tip), at);
    const x = cx + dx * reach, y = cy + dy * reach;
    // The renderer rewrites the transform only for a label inside the depth
    // range; appending to one it left alone would grow it every frame.
    if (Math.abs(lz) <= 1) element.style.transform += ` translate(${x - lx}px, ${y - ly}px)`;
    occupied.push({ left: x - w / 2, top: y - h / 2, right: x + w / 2, bottom: y + h / 2 });
    overlays.mark("dial", occupied);
  }

  // The pointer's position in the ring's plane, in the ring's frame; null
  // close to its centre.
  at(pointer, x, y) {
    this.group.updateWorldMatrix(true, false);
    const m = this.group.matrixWorld, r = this.spec.radius;
    center.setFromMatrixPosition(m);
    axisX.set(r, 0, 0).applyMatrix4(m).sub(center);
    axisY.set(0, r, 0).applyMatrix4(m).sub(center);
    const ab = pointer.planePoint(center, axisX, axisY, x, y);
    return ab && [ab[0] * r, ab[1] * r];
  }

  dispose() {
    this.group.removeFromParent();
    this.group.traverse((o) => {
      if (o.element) o.element.remove();
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    });
    this.core.requestRender();
  }
}
