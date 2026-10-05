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
import { Fx } from "./fx.js";
import { Gestures } from "./gestures.js";
import { Gizmos } from "./gizmo.js";
import { Inset } from "./inset.js";
import { Menu } from "./menu.js";
import { Nodes } from "./nodes.js";
import { Pointer } from "./pointer.js";
import { surface } from "./testing.js";

const COVERED_FRAME_MS = 250;
const RESTORE_WAIT_MS = 5000;

export class SceneCore {
  constructor(root, emit, remount) {
    this.root = root;
    this.emit = emit;
    this.remount = remount;
    this.canvas = root.querySelector("canvas");
    this.lostNotice = root.querySelector(".wc-scene-lost");
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
    this.clock = new THREE.Clock();

    this.scene = new THREE.Scene();
    this.fog = new THREE.Fog(0, 0, 1);
    this.scene.fog = this.fog;
    this.camera = new THREE.PerspectiveCamera(75, 1, 0.1, 1000);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(0, -3, 5);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, canvas: this.canvas });
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
    this.controls.addEventListener("change", () => {
      this.pointer.updateSnap();
      this.requestRender();
    });

    this.ix = {};
    this.snap = [1, 5];
    this.nodes = new Nodes(this);
    this.fx = new Fx(this);
    this.cameraMove = new CameraMove(this);
    this.inset = new Inset(this);
    this.gestures = new Gestures(this);
    this.pointer = new Pointer(this);
    this.gizmos = new Gizmos(this);
    this.menu = new Menu(this);
    this.surface = surface(this);

    // A drag must not outlive the page's attention.
    this.onBlur = () => this.gestures.abort("blur");
    this.onVisibility = () => {
      if (document.visibilityState === "hidden") this.gestures.abort("hidden");
    };
    this.onPageHide = () => this.gestures.abort("pagehide");
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
      const code = op[0];
      if (code === "reset") {
        this.reset();
        continue;
      }
      // Changes queued for an earlier mount: this one's reset has the scene.
      if (!this.live) continue;
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
          this.cameraMove.start(op[1], op[2], op[3]);
          break;
        case "fx":
          this.fx.run(op[1], op.slice(2));
          break;
        case "epoch":
          this.gestures.setEpoch(op[1]);
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
          this.gizmos.miss = !!op[1];
          break;
        case "menu":
          this.menu.open(op[1], op[2], op[3]);
          break;
        default:
          console.warn(`scene: unknown op ${code}`);
      }
    }
    if (this.live) this.root.setAttribute("data-ready", "");
  }

  reset() {
    this.gestures.abort("reset");
    this.pointer.hideRing();
    this.nodes.reset();
    this.fx.tweens.clear();
    this.fx.stopPulse();
    this.ix = {};
    this.live = true;
    this.resets++;
    if (!this.lost) this.gestures.unblock();
    this.requestRender();
  }

  // What the pointer handling works from: hover sources, rings, handle
  // rules, edit and keep-out move state, colours and snap bands.
  interact(changes) {
    Object.assign(this.ix, changes);
    if ("bands" in changes) {
      this.snap = [NaN, NaN];
      this.pointer.updateSnap();
    }
    if ("rings" in changes && this.pointer.ring) this.pointer.hideRing();
    if ("edit" in changes) this.gizmos.setJoints(this.ix.edit);
    if ("shapeMove" in changes) this.gizmos.setShape(this.ix.shapeMove);
    this.pointer.refresh();
    this.requestRender();
  }

  configure(changes) {
    Object.assign(this.config, changes);
    const c = this.config;
    if ("background" in changes && c.background) {
      this.renderer.setClearColor(c.background);
      this.fog.color.set(c.background);
    }
    if ("inset" in changes || "labels" in changes) this.inset.configure(c.inset, c.labels);
    this.requestRender();
  }

  // A node is gone: nothing may keep animating or holding it.
  forget(rec) {
    this.fx.forget(rec);
    this.gizmos.forget(rec);
    this.pointer.forget(rec);
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
    return this.fx.active || this.cameraMove.active || this.inset.active || this.gizmos.active;
  }

  schedule() {
    if (this.raf || this.retry || this.disposed) return;
    this.raf = requestAnimationFrame((now) => this.frame(now));
  }

  frame(now) {
    this.raf = 0;
    if (this.lost || !this.size[0] || !this.size[1]) return;
    const covered = !!document.querySelector(".q-dialog__backdrop");
    const gap = covered ? COVERED_FRAME_MS : 1000 / (this.config.max_fps || 30);
    const wait = this.lastFrame + gap - now;
    if (wait > 1) {
      this.retry = setTimeout(() => {
        this.retry = 0;
        this.schedule();
      }, wait);
      return;
    }
    const delta = this.clock.getDelta();
    this.fx.step(now);
    this.cameraMove.step(now);
    this.gizmos.step(now);
    this.pointer.follow();
    this.pointer.syncGlow();
    this.wantFrame = false;
    this.draw(delta);
    this.lastFrame = now;
    if (this.wantFrame || this.animating) this.schedule();
  }

  draw(delta) {
    const reach = this.config.reach || 1;
    // The fog starts past the floor's edge wherever the camera is.
    this.fog.near = this.camera.position.length() + reach * 1.5;
    this.fog.far = this.fog.near + reach * 3;
    this.updateShadows();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    this.inset.render(this.renderer, delta);
    this.frameCount++;
  }

  // The shadow map is redrawn only when a shadow caster moves, appears or hides.
  updateShadows() {
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
    this.applyView();
    this.requestRender();
  }

  applyView() {
    const [W, H] = this.size;
    if (!W || !H) return;
    const cam = this.camera;
    const { left, bottom } = this.layoutInset;
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
    if (this.config.background) this.renderer.setClearColor(this.config.background);
    this.renderer.shadowMap.needsUpdate = true;
    this.casters = NaN;
    if (this.live) this.gestures.unblock();
    this.requestRender();
  }

  // The socket dropped: no drag goes on, and none starts until the app has
  // sent the scene again.
  disconnected() {
    this.gestures.block("disconnect");
  }

  offerRemount() {
    this.lostNotice.textContent = "The 3D view stopped drawing. Click to restore it.";
    this.lostNotice.style.cursor = "pointer";
    this.lostNotice.addEventListener("click", () => this.remount(), { once: true });
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
    this.pointer.dispose();
    this.gizmos.dispose();
    window.removeEventListener("wc:layout", this.onLayout);
    this.canvas.removeEventListener("webglcontextlost", this.onLost);
    this.canvas.removeEventListener("webglcontextrestored", this.onRestored);
    this.inset.destroy();
    this.controls.dispose();
    this.nodes.reset();
    this.renderer.dispose();
    // A page has room for a few WebGL contexts, and dispose() keeps this one.
    this.renderer.forceContextLoss();
  }
}
