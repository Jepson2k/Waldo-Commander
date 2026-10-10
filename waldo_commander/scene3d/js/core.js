// The 3D view in the browser: one renderer, camera and orbit control, the
// nodes Python sends, and a draw loop that runs only while something changes.
//
// It draws at most `max_fps` frames a second, and a few a second while a
// dialog covers it. A view with no size (hidden at phone width) draws
// nothing until it has one.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { CameraMove } from "./camera.js";
import { CameraTools } from "./camera_tools.js";
import { Fx } from "./fx.js";
import { Gestures } from "./gestures.js";
import { Gizmos } from "./gizmo.js";
import { Inspection } from "./inspection.js";
import { Menu } from "./menu.js";
import { Nodes } from "./nodes.js";
import { Overlays } from "./overlays.js";
import { PrimitiveResize } from "./primitive_resize.js";
import { PrimitiveDrawing } from "./primitive_drawing.js";
import { Pointer } from "./pointer.js";
import { Selection } from "./selection.js";
import { surface } from "./testing.js";
import { ViewCube } from "./viewcube.js";

const COVERED_FRAME_MS = 250;
const RESTORE_WAIT_MS = 5000;

const viewed = new THREE.Vector3();

export class SceneCore {
  constructor(root, emit, remount) {
    this.root = root;
    this.emit = emit;
    this.remount = remount;
    this.canvas = root.querySelector("canvas");
    this.lostNotice = root.querySelector(".wc-scene-lost");
    this.onRemount = null; // the notice's click, while it offers a remount
    this.coveredAt = -Infinity;
    this.coveredBy = false;
    this.config = { max_fps: 30, reach: 1 };
    this.live = false;
    this.lost = false;
    this.disposed = false;
    this.frameCount = 0;
    this.resets = 0;
    this.size = [0, 0];
    this.layoutInset = { left: 0, bottom: 0 };
    this.raf = 0;
    this.retry = 0;
    this.lastFrame = -Infinity;
    this.wantFrame = false;

    this.scene = new THREE.Scene();
    this.fog = new THREE.Fog(0, 0, 1);
    this.scene.fog = this.fog;
    this.camera = new THREE.PerspectiveCamera(75, 1, 0.1, 1000);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(0, -3, 5);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference:"high-performance", canvas: this.canvas });
    // No tone mapping, so unlit token colours render as their hex.
    this.renderer.toneMapping = THREE.NoToneMapping;
    const shadows = this.renderer.shadowMap;
    shadows.enabled = true;
    shadows.type = THREE.PCFShadowMap;
    shadows.autoUpdate = false;
    shadows.needsUpdate = true;
    this.casters = NaN;
    this.labels = new CSS2DRenderer({ element: root.querySelector(".wc-scene-labels") });
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.addEventListener("change", () => this.requestRender());

    this.ix = {};
    this.nodes = new Nodes(this);
    this.overlays = new Overlays(this);
    this.fx = new Fx(this);
    this.cameraMove = new CameraMove(this);
    this.cube = new ViewCube(this);
    this.gestures = new Gestures(this);
    this.pointer = new Pointer(this);
    this.gizmos = new Gizmos(this);
    this.resizeHandles = new PrimitiveResize(this);
    this.drawingTool = new PrimitiveDrawing(this);
    this.menu = new Menu(this);
    this.inspection = new Inspection(this);
    this.navigation = new CameraTools(this);
    this.selection = new Selection(this);
    this.pointer.tools = [this.drawingTool, this.resizeHandles, this.gizmos, this.pointer.ringTool, this.inspection];
    this.surface = surface(this);

    // A drag must not outlive the page's attention.
    this.onBlur = () => this.pointer.interrupt("blur");
    this.onVisibility = () => {
      if (document.visibilityState === "hidden") this.pointer.interrupt("hidden");
    };
    this.onPageHide = () => this.pointer.interrupt("pagehide");
    window.addEventListener("blur", this.onBlur);
    document.addEventListener("visibilitychange", this.onVisibility);
    window.addEventListener("pagehide", this.onPageHide);

    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(root);
    this.onLayout = (e) => this.follow(e.detail);
    window.addEventListener("wc:layout", this.onLayout);
    if (window.PanelResize) this.follow(window.PanelResize.layout());
    this.onLost = () => this.contextLost();
    this.onRestored = () => this.contextRestored();
    this.canvas.addEventListener("webglcontextlost", this.onLost);
    this.canvas.addEventListener("webglcontextrestored", this.onRestored);
    this.resize();
  }

  // ---- ops from Python --------------------------------------------------

  apply(ops) {
    for (const op of ops) {
      // One change that fails must not cost the rest of its batch.
      try {
        this.applyOne(op);
      } catch (err) {
        console.error(`scene: op ${op[0]} failed`, err);
      }
    }
    if (this.nodes.objectsChanged) {
      this.nodes.objectsChanged = false;
      this.selection.prune();
    }
    if (this.live) this.root.setAttribute("data-ready", "");
  }

  applyOne(op) {
    const code = op[0];
    if (code === "reset") {
      this.reset();
      return;
    }
    // Changes queued for an earlier mount: this one's reset has the scene.
    if (!this.live) return;
    switch (code) {
      case "c":
        this.nodes.create(op[1], op[2], op[3], op[4], op[5]);
        break;
      case "u": {
        const rec = this.nodes.get(op[1]);
        if (rec) this.nodes.update(rec, op[2]);
        break;
      }
      case "d":
        this.nodes.delete(op[1]);
        break;
      case "J":
        this.nodes.defineJoints(op[1]);
        break;
      case "q":
        this.nodes.setJoints(op[1]);
        break;
      case "cfg":
        this.configure(op[1]);
        break;
      case "cam":
        this.navigation.home ??= op[1];
        // A drag must not re-project against a moving camera.
        this.pointer.abortDrag("camera");
        this.cameraMove.start(op[1], op[2], op[3]);
        break;
      case "fx":
        this.fx.run(op[1], op.slice(2));
        break;
      case "epoch":
        this.gestures.setEpoch(op[1]);
        this.drawingTool.cancel();
        this.menu.resend();
        break;
      case "reject":
        this.gestures.reject(op[1]);
        break;
      case "ix":
        this.interact(op[1]);
        break;
      case "tcp":
        this.gizmos.place(op[1], op[2], op[3]);
        break;
      case "miss":
        this.gizmos.setMiss(!!op[1]);
        break;
      case "menu":
        this.menu.open(op[1], op[2], op[3]);
        break;
      default:
        console.warn(`scene: unknown op ${code}`);
    }
  }

  reset() {
    this.inspection.load(null);
    this.selection.load(null);
    this.resizeHandles.load(null);
    this.drawingTool.load(null);
    this.gestures.abort("reset");
    this.pointer.forgetRings();
    this.pointer.forgetHover();
    this.nodes.reset();
    this.gestures.reset();
    this.fx.tweens.clear();
    this.fx.stopPulse();
    this.ix = {};
    this.live = true;
    this.resets++;
    this.navigation.notify();
    this.gestures.unblock("disconnect");
    this.requestRender();
  }

  // What the pointer handling works from: hover sources, rings, handle
  // rules, edit and keep-out move state, colours.
  interact(changes) {
    Object.assign(this.ix, changes);
    if ("entities" in changes) this.selection.load(changes.entities);
    if ("shapeDraw" in changes) this.drawingTool.load(changes.shapeDraw);
    if ("colors" in changes) {
      this.gizmos.precision.prepare();
      this.pointer.recolor(changes.colors);
      this.selection.recolor(changes.colors);
      this.cube.recolor();
    }
    if ("pathData" in changes) this.inspection.load(changes.pathData);
    if ("comparisonData" in changes) this.inspection.comparison.load(changes.comparisonData);
    if ("comparisonStatus" in changes) this.inspection.comparison.status(changes.comparisonStatus);
    if ("playbackCursor" in changes) this.inspection.follow(changes.playbackCursor);
    if ("comparisonVisible" in changes) this.inspection.comparison.setVisible(changes.comparisonVisible);
    if ("pathScrubAvailable" in changes || "simulator" in changes || "edit" in changes) this.inspection.availabilityChanged();
    if ("edit" in changes) this.gizmos.paintBall();
    if ("rings" in changes || "colors" in changes) this.pointer.forgetRings();
    if ("shapeMove" in changes) {
      this.resizeHandles.load(this.ix.shapeMove);
      this.gizmos.setShape(this.ix.shapeMove?.mode === "resize" ? null : this.ix.shapeMove);
    }
    this.pointer.refresh();
    this.requestRender();
  }

  configure(changes) {
    Object.assign(this.config, changes);
    const c = this.config;
    if ("background" in changes && c.background) {
      this.renderer.setClearColor(c.background);
      this.fog.color.set(c.background);
      // The rings' halos are the background's colour.
      this.pointer.forgetRings();
    }
    if ("view_cube" in changes) this.cube.configure(c.view_cube);
    this.requestRender();
  }

  // A node is gone: nothing may keep animating or holding it.
  forget(rec) {
    this.fx.forget(rec);
    this.gizmos.forget(rec);
    this.pointer.forget(rec);
    this.resizeHandles.forget(rec);
  }

  // ---- drawing ----------------------------------------------------------

  requestRender() {
    this.wantFrame = true;
    this.schedule();
  }

  animate() {
    this.requestRender();
  }

  get animating() {
    return this.fx.active || this.cameraMove.active || this.gizmos.active;
  }

  schedule() {
    if (this.raf || this.retry || this.disposed) return;
    this.raf = requestAnimationFrame((now) => this.frame(now));
  }

  frame(now) {
    this.raf = 0;
    if (this.lost || !this.size[0] || !this.size[1]) return;
    const covered = this.covered;
    const gap = covered ? COVERED_FRAME_MS : 1000 / (this.config.max_fps || 30);
    const wait = this.lastFrame + gap - now;
    if (wait > 1) {
      this.retry = setTimeout(() => {
        this.retry = 0;
        this.schedule();
      }, wait);
      return;
    }
    this.overlays.measure();
    this.fx.step(now);
    this.cameraMove.step(now);
    this.gizmos.step(now);
    this.pointer.follow();
    this.pointer.syncGlow();
    this.wantFrame = false;
    try {
      this.draw();
    } finally {
      this.overlays.framing = false;
      this.lastFrame = now;
      if (this.wantFrame || this.animating) this.schedule();
    }
  }

  // The scene's 3D parts are brought up to date and drawn, then the cards
  // and labels over it are placed where the drawing leaves them room.
  draw() {
    this.fitNear();
    for(const tc of this.gizmos.all())tc.update();
    const reach = this.config.reach || 1;
    // The fog starts past the floor's edge wherever the camera is.
    this.fog.near = this.camera.position.length() + reach * 1.5;
    this.fog.far = this.fog.near + reach * 3;
    this.selection.outlineSelected();
    this.resizeHandles.layoutHandles();
    this.drawingTool.layoutHandle();
    this.updateShadows();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    this.gizmos.layout();
    this.selection.layout();
    this.resizeHandles.layout();
    this.drawingTool.layout();
    this.pointer.ring?.layoutLabels();
    this.pointer.occludeBadge();
    this.inspection.layout();
    this.gizmos.precision.layout();
    this.gizmos.precision.layoutLabels();
    this.overlays.layoutTopLeft();
    this.inspection.comparison.layout();
    this.cube.render(this.renderer);
    this.frameCount++;
  }

  // Whether a dialog covers the page: a walk of the whole document, so it is
  // looked up at most every 100 ms rather than on every frame.
  get covered() {
    const now = performance.now();
    if (now - this.coveredAt > 100) {
      this.coveredAt = now;
      this.coveredBy = !!document.querySelector(".q-dialog__backdrop");
    }
    return this.coveredBy;
  }

  // The near plane follows the camera's distance to what it looks at: close
  // enough not to clip a focused part, far enough to keep depth precision.
  fitNear() {
    const camera = this.camera;
    if (camera.isOrthographicCamera) return;
    const near = THREE.MathUtils.clamp(camera.position.distanceTo(this.controls.target) / 100, 0.001, 0.1);
    if (Math.abs(near - camera.near) <= camera.near * 0.25) return;
    camera.near = near;
    camera.updateProjectionMatrix();
  }

  // How many metres one pixel spans at `position`: what a handle's size and
  // a drag's snapping follow.
  metresPerPixel(position) {
    const camera = this.camera;
    camera.updateMatrixWorld();
    const depth = camera.isOrthographicCamera ? 1 : Math.max(0.001, -viewed.copy(position).applyMatrix4(camera.matrixWorldInverse).z);
    return (2 * depth) / (camera.projectionMatrix.elements[5] * Math.max(1, this.size[1]));
  }

  // The shadow map is redrawn only when a shadow caster moves, appears or hides.
  updateShadows() {
    // The render brings world matrices up to date, after this has looked.
    this.scene.updateMatrixWorld();
    let sum = 0;
    this.scene.traverseVisible((o) => {
      if (!o.castShadow) return;
      sum += o.id;
      const e = o.matrixWorld.elements;
      for (let i = 0; i < 16; i++) sum += e[i] * (i + 1);
    });
    if (sum !== this.casters) {
      this.casters = sum;
      this.renderer.shadowMap.needsUpdate = true;
    }
  }

  // ---- size and framing -------------------------------------------------

  resize() {
    const w = this.root.clientWidth;
    const h = this.root.clientHeight;
    this.size = [w, h];
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.labels.setSize(w, h);
    this.applyView();
    this.requestRender();
  }

  // The panels over the view cover some of it; the camera centres the image
  // on the part they leave uncovered.
  follow(layout) {
    this.layoutInset = { left: Math.max(0, layout.columnRight), bottom: Math.max(0, layout.bottomCover) };
    this.overlays.follow(layout);
    this.inspection.comparison.follow();
    this.applyView();
    this.requestRender();
  }

  applyView() {
    const [W, H] = this.size;
    if (!W || !H) return;
    const cam = this.camera;
    const { left, bottom } = this.layoutInset;
    if (cam.isOrthographicCamera) {
      const half = this.navigation.height / 2;
      cam.top = half; cam.bottom = -half;
      cam.right = half * (W + left) / (H + bottom); cam.left = -cam.right;
    }
    if (left <= 0 && bottom <= 0) {
      if (cam.view && cam.view.enabled) cam.clearViewOffset();
      cam.aspect = W / H;
    } else {
      cam.aspect = (W + left) / (H + bottom);
      cam.setViewOffset(W + left, H + bottom, 0, bottom, W, H);
    }
    cam.updateProjectionMatrix();
  }

  // ---- WebGL context ------------------------------------------------------

  contextLost() {
    this.inspection.clear();
    this.pointer.setGlow(null);
    this.lost = true;
    this.gestures.block("context lost");
    this.root.setAttribute("data-gl", "lost");
    this.lostNotice.style.display = "grid";
    this.restoreTimer = setTimeout(() => this.offerRemount(), RESTORE_WAIT_MS);
  }

  // The renderer rebuilds its GL state from the scene, which is all still
  // here; only what it keeps outside the scene is set again.
  contextRestored() {
    clearTimeout(this.restoreTimer);
    this.lost = false;
    this.root.removeAttribute("data-gl");
    this.lostNotice.style.display = "none";
    this.withdrawRemount();
    if (this.config.background) this.renderer.setClearColor(this.config.background);
    this.renderer.shadowMap.needsUpdate = true;
    this.casters = NaN;
    this.gizmos.precision.prepare(true);
    this.gestures.unblock("context lost");
    this.requestRender();
  }

  // The socket dropped: no drag goes on, and none starts until the app has
  // sent the scene again.
  disconnected() {
    this.inspection.clear(false);
    this.pointer.setGlow(null);
    this.gestures.block("disconnect");
  }

  // One offer at a time: a second remount from the same click would leave
  // the view unmounted.
  offerRemount() {
    if (this.onRemount) return;
    this.onRemount = () => {
      this.withdrawRemount();
      this.remount();
    };
    this.lostNotice.textContent = "The 3D view stopped drawing. Click to restore it.";
    this.lostNotice.style.cursor = "pointer";
    this.lostNotice.addEventListener("click", this.onRemount);
  }

  withdrawRemount() {
    if (!this.onRemount) return;
    this.lostNotice.removeEventListener("click", this.onRemount);
    this.onRemount = null;
    this.lostNotice.textContent = "";
    this.lostNotice.style.cursor = "";
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.raf);
    clearTimeout(this.retry);
    clearTimeout(this.restoreTimer);
    this.observer.disconnect();
    this.gestures.abort("unmount");
    window.removeEventListener("blur", this.onBlur);
    document.removeEventListener("visibilitychange", this.onVisibility);
    window.removeEventListener("pagehide", this.onPageHide);
    this.menu.dispose();
    this.overlays.dispose();
    this.inspection.dispose();
    this.selection.dispose();
    this.resizeHandles.dispose();
    this.drawingTool.dispose();
    this.pointer.dispose();
    this.gizmos.dispose();
    window.removeEventListener("wc:layout", this.onLayout);
    this.canvas.removeEventListener("webglcontextlost", this.onLost);
    this.canvas.removeEventListener("webglcontextrestored", this.onRestored);
    this.cube.dispose();
    this.controls.dispose();
    this.nodes.reset();
    this.renderer.dispose();
    // A page has room for a few WebGL contexts, and dispose() keeps this one.
    this.renderer.forceContextLoss();
  }
}
