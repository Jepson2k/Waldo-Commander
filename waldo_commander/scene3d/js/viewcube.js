// The view cube: a cube in the view's top right that turns with the camera,
// each face named for the view it gives. A press on a face, edge or corner
// turns the camera to look from there about the orbit target; a drag on it
// orbits the camera.
//
// It is drawn into its own square of the canvas after the scene. A box of
// the page over that square takes its pointer events, so none of them reach
// the scene's tools or orbit.

import * as THREE from "three";
import { setStyle } from "./dom.js";

// How wide an edge's or corner's band is, as a fraction of a side.
const BAND = 0.2;
// The drawing's half-width in sides: the cube and its axes past the corner.
const EXTENT = 1;
const TEXTURE = 256;
// How far a press may move and still be a click, in pixels.
const SLOP = { mouse: 4, pen: 4, touch: 10 };
// How far a drag across the whole cube turns the camera, in radians.
const TURN = Math.PI;
// The brightness of a face seen edge on; a face seen square on is full.
const SHADE = 0.62;
// How close to straight up or down an orbit may look.
const POLE = 1e-6;
// The axes run along the edges from the left front bottom corner, just
// outside the cube, and past it a little.
const AXIS_OFFSET = 0.035;
const AXIS_END = 0.62;
const AXIS_RADIUS = 0.022;

// Each face: the direction it is seen from, and its right and up as its
// label reads.
export const FACES = [
  { label: "FRONT", n: [0, -1, 0], r: [1, 0, 0], u: [0, 0, 1] },
  { label: "BACK", n: [0, 1, 0], r: [-1, 0, 0], u: [0, 0, 1] },
  { label: "RIGHT", n: [1, 0, 0], r: [0, 1, 0], u: [0, 0, 1] },
  { label: "LEFT", n: [-1, 0, 0], r: [0, -1, 0], u: [0, 0, 1] },
  { label: "TOP", n: [0, 0, 1], r: [1, 0, 0], u: [0, 1, 0] },
  { label: "BOTTOM", n: [0, 0, -1], r: [1, 0, 0], u: [0, -1, 0] },
];

const offset = new THREE.Vector3();
const facing = new THREE.Vector3();
const ndc = new THREE.Vector2();
const raycaster = new THREE.Raycaster();

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

// Where along one of a face's axes a band lies, from the face's centre.
const band = (c) => (c > 0 ? [0.5 - BAND, 0.5] : c < 0 ? [-0.5, BAND - 0.5] : [BAND - 0.5, 0.5 - BAND]);

const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(`--wc-${name}`).trim();

let reader = null;

// A CSS colour, oklch() included, as sRGB and alpha in 0-1; one the canvas
// cannot read comes back clear.
function channels(value) {
  reader ||= document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  reader.clearRect(0, 0, 1, 1);
  reader.fillStyle = "transparent";
  reader.fillStyle = value;
  reader.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = reader.getImageData(0, 0, 1, 1).data;
  return [r / 255, g / 255, b / 255, a / 255];
}

function setColor(material, value) {
  const [r, g, b, a] = channels(value);
  material.color.setRGB(r, g, b, THREE.SRGBColorSpace);
  material.opacity = a;
}

export class ViewCube {
  constructor(core) {
    this.core = core;
    this.box = null;
    this.tools = null;
    this.at = null; // {left, top, size}: the cube's square, in pixels from the view's top left
    this.area = { left: 0, top: 0, right: 0, bottom: 0 }; // the cube's square and its buttons
    this.region = null; // the face, edge or corner lit, as a direction of -1, 0 and 1
    this.hovering = null; // where a mouse over the cube is, in its box
    this.press = null;
    this.colors = null;
    this.reader = () => this.measure();
    this.listeners = [
      ["pointerdown", (e) => this.down(e)],
      ["pointermove", (e) => this.move(e)],
      ["pointerup", (e) => this.up(e)],
      ["pointercancel", (e) => this.cancel(e)],
      ["lostpointercapture", (e) => this.cancel(e)],
      ["pointerleave", () => this.leave()],
      ["contextmenu", (e) => e.preventDefault()],
    ];
  }

  get enabled() {
    return this.box !== null;
  }

  configure(on) {
    if (!on) this.dispose();
    else if (!this.box) this.build();
  }

  build() {
    const core = this.core;
    this.box = document.createElement("div");
    this.box.className = "wc-viewcube";
    this.box.setAttribute("role", "img");
    this.box.setAttribute("aria-label", "View cube");
    for (const [type, fn] of this.listeners) this.box.addEventListener(type, fn);
    core.root.append(this.box);
    core.overlays.readers.push(this.reader);

    this.scene = new THREE.Scene();
    this.camera = new THREE.OrthographicCamera(-EXTENT, EXTENT, EXTENT, -EXTENT, 0.1, 10);
    const plane = new THREE.PlaneGeometry(1, 1);
    const anisotropy = Math.min(8, core.renderer.capabilities.getMaxAnisotropy());
    this.faces = FACES.map((spec) => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = TEXTURE;
      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.anisotropy = anisotropy;
      const mesh = new THREE.Mesh(plane, new THREE.MeshBasicMaterial({ map: texture }));
      const [n, r, u] = [spec.n, spec.r, spec.u].map((v) => new THREE.Vector3(...v));
      mesh.matrix.makeBasis(r, u, n).setPosition(n.clone().multiplyScalar(0.5));
      mesh.matrixAutoUpdate = false;
      const face = { ...spec, normal: n, mesh, ctx: canvas.getContext("2d"), texture };
      mesh.userData.face = face;
      this.scene.add(mesh);
      return face;
    });
    this.meshes = this.faces.map((face) => face.mesh);

    this.edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)), new THREE.LineBasicMaterial({ transparent: true }));
    // Just outside the faces, so they never hide their own edges.
    this.edges.scale.setScalar(1.003);
    this.scene.add(this.edges);

    const corner = -0.5 - AXIS_OFFSET;
    const length = AXIS_END - corner;
    const rod = new THREE.CylinderGeometry(AXIS_RADIUS, AXIS_RADIUS, length, 10);
    this.axes = ["x", "y", "z"].map((axis) => {
      const mesh = new THREE.Mesh(rod, new THREE.MeshBasicMaterial());
      mesh.position.setScalar(corner);
      mesh.position[axis] = corner + length / 2;
      if (axis === "x") mesh.rotation.z = -Math.PI / 2;
      if (axis === "z") mesh.rotation.x = Math.PI / 2;
      this.scene.add(mesh);
      return mesh;
    });
    // A press can come before the first frame has placed the faces.
    this.scene.updateMatrixWorld();

    this.recolor();
    // The labels are drawn again once the page's font has loaded.
    document.fonts?.ready.then(() => {
      if (this.box) this.recolor();
    });
  }

  // The colours follow the theme's tokens and the app's colours.
  recolor() {
    if (!this.box) return;
    const app = this.core.ix.colors || {};
    const family = token("font-sans") || "sans-serif";
    this.colors = {
      face: token("control"),
      label: token("text-muted"),
      lit: app.action || token("action"),
      litLabel: token("on-bright"),
    };
    // One size for every label, the longest filling most of a face.
    const ctx = this.faces[0].ctx;
    ctx.font = `600 100px ${family}`;
    const widest = Math.max(...this.faces.map((face) => ctx.measureText(face.label).width));
    const size = Math.floor(Math.min(0.2 * TEXTURE, (0.72 * TEXTURE * 100) / widest));
    this.font = `600 ${size}px ${family}`;
    setColor(this.edges.material, token("glass-border"));
    ["X", "Y", "Z"].forEach((letter, i) => setColor(this.axes[i].material, app[`axis${letter}`] || token(`axis-${letter.toLowerCase()}`)));
    for (const face of this.faces) this.paint(face);
    this.core.requestRender();
  }

  paint(face) {
    const { ctx, texture } = face;
    const region = this.region;
    const c = this.colors;
    ctx.fillStyle = c.face;
    ctx.fillRect(0, 0, TEXTURE, TEXTURE);
    const lit = region !== null && dot(region, face.n) > 0;
    const centre = lit && !dot(region, face.r) && !dot(region, face.u);
    if (lit) {
      const [x0, x1] = band(dot(region, face.r));
      const [y0, y1] = band(dot(region, face.u));
      ctx.fillStyle = c.lit;
      ctx.fillRect((x0 + 0.5) * TEXTURE, (0.5 - y1) * TEXTURE, (x1 - x0) * TEXTURE, (y1 - y0) * TEXTURE);
    }
    ctx.font = this.font;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = centre ? c.litLabel : c.label;
    ctx.fillText(face.label, TEXTURE / 2, TEXTURE / 2);
    texture.needsUpdate = true;
  }

  // Lights the face, edge or corner `region` (null: none). Whether that
  // changed what is lit.
  highlight(region) {
    const was = this.region;
    if (was === region || (was && region && was.every((v, i) => v === region[i]))) return false;
    this.region = region;
    for (const face of this.faces) {
      if ((was && dot(was, face.n) > 0) || (region && dot(region, face.n) > 0)) this.paint(face);
    }
    if (!this.press?.turning) setStyle(this.box, "cursor", region ? "pointer" : "");
    return true;
  }

  // The cube's camera looks as the view's does.
  orient() {
    const q = this.core.camera.quaternion;
    this.camera.quaternion.copy(q);
    this.camera.position.set(0, 0, 4).applyQuaternion(q);
    this.camera.updateMatrixWorld();
    facing.set(0, 0, 1).applyQuaternion(q);
    for (const face of this.faces) face.mesh.material.color.setScalar(SHADE + (1 - SHADE) * Math.max(0, face.normal.dot(facing)));
  }

  // The face, edge or corner at (x, y) in the cube's box, as a direction of
  // -1, 0 and 1 along each axis; null off the cube.
  regionAt(x, y) {
    const size = this.at?.size;
    if (!size) return null;
    this.orient();
    ndc.set((x / size) * 2 - 1, 1 - (y / size) * 2);
    raycaster.setFromCamera(ndc, this.camera);
    const hit = raycaster.intersectObjects(this.meshes, false)[0];
    if (!hit) return null;
    const face = hit.object.userData.face;
    const p = hit.point.toArray();
    const along = (axis) => {
      const c = dot(p, axis);
      return Math.abs(c) > 0.5 - BAND ? Math.sign(c) : 0;
    };
    const a = along(face.r);
    const b = along(face.u);
    return face.n.map((v, i) => v + a * face.r[i] + b * face.u[i]);
  }

  // Before anything is written in a frame: where the cube and its buttons are.
  measure() {
    const box = this.box;
    const size = box.offsetWidth;
    if (!size) {
      this.at = null;
      this.core.overlays.control("viewcube", null);
      return;
    }
    const at = (this.at ||= { left: 0, top: 0, size: 0 });
    at.left = box.offsetLeft;
    at.top = box.offsetTop;
    at.size = size;
    const area = this.area;
    area.left = at.left;
    area.top = at.top;
    area.right = at.left + size;
    area.bottom = at.top + size;
    if (!this.tools?.isConnected) this.tools = this.core.root.parentElement?.querySelector(".wc-viewcube-tools") || null;
    if (this.tools) {
      const r = this.tools.getBoundingClientRect();
      const view = this.core.overlays.view;
      if (r.width) {
        area.left = Math.min(area.left, r.left - view.left);
        area.top = Math.min(area.top, r.top - view.top);
        area.bottom = Math.max(area.bottom, r.bottom - view.top);
      }
    }
    this.core.overlays.control("viewcube", area);
  }

  // Drawn over the scene into its own square: clearing the colour would wipe
  // the scene, and the scene's depth would hide the cube.
  render(renderer) {
    const at = this.at;
    if (!this.box || !at) return;
    if (this.hovering && !this.press) this.highlight(this.regionAt(...this.hovering));
    this.orient();
    const [width, height] = this.core.size;
    const bottom = height - at.top - at.size;
    const autoClear = renderer.autoClear;
    renderer.autoClear = false;
    renderer.setScissorTest(true);
    renderer.setScissor(at.left, bottom, at.size, at.size);
    renderer.setViewport(at.left, bottom, at.size, at.size);
    renderer.clearDepth();
    renderer.render(this.scene, this.camera);
    renderer.setScissorTest(false);
    renderer.setScissor(0, 0, width, height);
    renderer.setViewport(0, 0, width, height);
    renderer.autoClear = autoClear;
  }

  local(e) {
    const rect = this.box.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  // A drag on the scene re-projects against the camera, which must not turn.
  get held() {
    return !!this.core.pointer.drag || this.core.gestures.active;
  }

  down(e) {
    if (e.button !== 0 || this.press) return;
    e.preventDefault();
    if (this.held) return;
    try {
      this.box.setPointerCapture(e.pointerId);
    } catch (err) {
      // A pointer the browser no longer tracks: its release cannot come.
      return;
    }
    this.press = { id: e.pointerId, x: e.clientX, y: e.clientY, lastX: e.clientX, lastY: e.clientY, slop: SLOP[e.pointerType] ?? SLOP.mouse, turning: false, region: this.regionAt(...this.local(e)) };
    if (this.highlight(this.press.region)) this.core.requestRender();
  }

  move(e) {
    const press = this.press;
    if (!press) {
      if (e.pointerType === "touch") return;
      this.hovering = this.local(e);
      if (this.highlight(this.regionAt(...this.hovering))) this.core.requestRender();
      return;
    }
    if (e.pointerId !== press.id) return;
    if (!press.turning) {
      if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < press.slop || this.held) return;
      press.turning = true;
      this.core.navigation.halt();
      this.highlight(null);
      setStyle(this.box, "cursor", "grabbing");
    }
    this.orbit(e.clientX - press.lastX, e.clientY - press.lastY);
    press.lastX = e.clientX;
    press.lastY = e.clientY;
  }

  up(e) {
    const press = this.press;
    if (!press || e.pointerId !== press.id) return;
    this.press = null;
    if (!press.turning && press.region && !this.held) this.core.navigation.action("view", press.region);
    this.hovering = e.pointerType === "touch" ? null : this.local(e);
    setStyle(this.box, "cursor", "");
    this.highlight(this.hovering && this.regionAt(...this.hovering));
    this.core.requestRender();
  }

  cancel(e) {
    if (!this.press || e.pointerId !== this.press.id) return;
    this.press = null;
    setStyle(this.box, "cursor", "");
    this.highlight(null);
    this.core.requestRender();
  }

  leave() {
    this.hovering = null;
    if (!this.press && this.highlight(null)) this.core.requestRender();
  }

  // Turns the camera about its target as a drag on the scene would, by
  // (dx, dy) pixels dragged across the cube.
  orbit(dx, dy) {
    const { camera, controls } = this.core;
    const turn = TURN / this.at.size;
    offset.copy(camera.position).sub(controls.target);
    const radius = offset.length();
    if (!radius) return;
    const theta = Math.atan2(offset.y, offset.x) - turn * dx;
    const phi = THREE.MathUtils.clamp(Math.acos(THREE.MathUtils.clamp(offset.z / radius, -1, 1)) - turn * dy, POLE, Math.PI - POLE);
    offset.set(Math.sin(phi) * Math.cos(theta), Math.sin(phi) * Math.sin(theta), Math.cos(phi)).multiplyScalar(radius);
    camera.position.copy(controls.target).add(offset);
    camera.lookAt(controls.target);
    controls.update();
    this.core.requestRender();
  }

  dispose() {
    if (!this.box) return;
    const readers = this.core.overlays.readers;
    readers.splice(readers.indexOf(this.reader), 1);
    this.core.overlays.control("viewcube", null);
    this.box.remove();
    this.box = null;
    this.tools = null;
    this.at = null;
    this.press = null;
    this.hovering = null;
    this.region = null;
    this.scene.traverse((o) => {
      o.geometry?.dispose();
      o.material?.map?.dispose();
      o.material?.dispose();
    });
    this.scene = null;
  }
}
