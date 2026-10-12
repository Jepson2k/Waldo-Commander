// The view's context menu: when a right-click (or the menu key, a ctrl+click
// on macOS, a lone touch held still) asks for it, the app hears what is under
// the pointer, fills the menu and has it opened at the pointer. A right-drag
// pans and asks for nothing.
//
// Browsers fire the contextmenu event on press (Linux, macOS) or on release
// (Windows), so the event itself is always swallowed and the press decides at
// its release. Each request has a generation: only the newest one opens.

import { tapPx } from "./handles.js";

const DRAG_PX = 5;
const LONG_PRESS_MS = 500;

export class Menu {
  constructor(core) {
    this.core = core;
    this.gen = 0;
    this.pending = null; // the request the app has not answered yet
    this.press = null;
    this.released = false;
    this.long = null;
    const canvas = core.root;
    this.onDown = (e) => this.down(e);
    this.onMove = (e) => this.move(e);
    this.onUp = (e) => this.up(e);
    this.onContext = (e) => this.context(e);
    this.onClick = (e) => {
      if (this.suppressClick && e.target.closest?.("[data-path-record]") === this.suppressClick) {
        e.preventDefault(); e.stopImmediatePropagation(); this.suppressClick = null;
      }
    };
    canvas.addEventListener("click", this.onClick, { capture: true });
    canvas.addEventListener("pointerdown", this.onDown, { capture: true });
    window.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
    window.addEventListener("pointercancel", this.onUp);
    canvas.addEventListener("contextmenu", this.onContext);
  }

  get menu() {
    const id = this.core.ix.menu;
    return id === undefined || id === null ? null : getElement(id);
  }

  close() {
    const menu = this.menu;
    if (menu && menu.hide) menu.hide();
  }

  down(e) {
    if (e.target !== this.core.canvas && !e.target.closest?.("[data-path-record]")) return;
    this.suppressClick = null;
    this.close();
    this.cancelLongPress();
    const mac = /Mac/.test(navigator.platform);
    if (e.button === 0 && e.ctrlKey && mac) {
      // A ctrl+click is macOS's right-click: it neither orbits, grabs nor selects.
      this.core.pointer.suppressTap(e.pointerId);
      e.stopImmediatePropagation();
      return;
    }
    if (e.button === 2) {
      this.press = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: 0 };
      return;
    }
    // Two fingers down pinch or turn the camera, however still one holds.
    if (e.pointerType === "touch" && e.button === 0 && this.core.pointer.pressed.size <= 1) {
      this.long = {
        id: e.pointerId,
        x: e.clientX,
        y: e.clientY,
        timer: setTimeout(() => {
          const long = this.long;
          this.long = null;
          if (long && !this.core.gestures.active) {
            this.core.pointer.suppressTap(long.id);
            this.suppressClick = document.elementFromPoint(long.x, long.y)?.closest("[data-path-record]");
            if (this.core.inspection.dragging) this.core.pointer.abortDrag("path menu");
            this.request(long.x, long.y);
          }
        }, LONG_PRESS_MS),
      };
    }
  }

  move(e) {
    const p = this.press;
    if (p && e.pointerId === p.id) p.moved = Math.max(p.moved, Math.hypot(e.clientX - p.x, e.clientY - p.y));
    const l = this.long;
    // A finger held for the menu may wander as far as a tap may.
    if (l && e.pointerId === l.id && Math.hypot(e.clientX - l.x, e.clientY - l.y) > tapPx(e.pointerType)) this.cancelLongPress();
  }

  up(e) {
    const l = this.long;
    if (l && e.pointerId === l.id) this.cancelLongPress();
    const p = this.press;
    if (!p || e.pointerId !== p.id) return;
    this.press = null;
    const moved = Math.max(p.moved, Math.hypot(e.clientX - p.x, e.clientY - p.y));
    // Windows sends its contextmenu after this release.
    this.released = true;
    setTimeout(() => (this.released = false));
    if (e.type === "pointerup" && moved <= DRAG_PX) this.request(e.clientX, e.clientY);
  }

  context(e) {
    if (e.target !== this.core.canvas && !e.target.closest?.("[data-path-record]")) return;
    e.preventDefault();
    e.stopPropagation();
    // A right press decides at its release; this one had none: the menu
    // key, a long press, or a ctrl+click.
    if (this.press === null && !this.released) this.request(e.clientX, e.clientY);
    this.released = false;
  }

  cancelLongPress() {
    if (this.long) clearTimeout(this.long.timer);
    this.long = null;
  }

  // Abandon a press that has turned into a drag of something, and any menu
  // still on its way: none opens over a drag.
  cancel(keepLongPress = false) {
    if (!keepLongPress) this.cancelLongPress();
    this.gen++;
    this.pending = null;
  }

  get dragging() {
    return this.core.gestures.active || !!this.core.pointer.drag;
  }

  request(cx, cy, gizmo = false) {
    const core = this.core;
    if (core.gestures.blocked || !core.live || this.dragging) return;
    const gen = ++this.gen;
    const { hits, ground, surface } = core.pointer.contextAt(cx, cy);
    const event = document.elementFromPoint(cx, cy)?.closest("[data-path-record]");
    const marker = core.inspection.markers.find(m => m.element === event);
    if (marker?.members.length > 1) {
      core.inspection.chooseEvents(marker.members, event.getBoundingClientRect());
      return;
    }
    const hit = event ? (event.dataset.pathRecord === "0" ? {block: Number(event.dataset.pathBlock)} : null)
      : core.inspection.pick(cx, cy).find(h => h.record === 0);
    if (event) hits.length = 0;
    const path = hit ? { revision: core.inspection.data.revision, block: hit.block, shown: core.inspection.sweep.block === hit.block } : null;
    if(!event && (gizmo || core.pointer.handlesAt(cx,cy).some(h=>h.tc===core.gizmos.tcp) ||
       core.gizmos.gizmoShown && core.pointer.hit(cx,cy)?.object===core.gizmos.ball)) {
      hits.unshift("gizmo:tcp");core.pointer.hover.pinned=true;
    }
    this.pending = { gen, hits, ground, surface, path, cx, cy, epoch: core.gestures.epoch };
    core.emit("context", this.pending);
  }

  // The app drops a request carrying an epoch it has just left; one sent as
  // the epoch changed is asked again under the new one, however many times
  // the epoch moves on before the app answers.
  resend() {
    const p = this.pending;
    const epoch = this.core.gestures.epoch;
    if (!p || p.gen !== this.gen || p.epoch === epoch) return;
    p.epoch = epoch;
    this.core.emit("context", p);
  }

  // The app filled the menu for request `gen`: open it there, unless a newer
  // request has come since or a drag has begun.
  open(gen, cx, cy) {
    if (gen !== this.gen) return;
    this.pending = null;
    if (this.dragging) return;
    const menu = this.menu;
    if (!menu || !menu.show) return;
    // A menu that never finished closing ignores show(); close it first.
    menu.hide();
    menu.show(new MouseEvent("contextmenu", { clientX: cx, clientY: cy }));
  }

  dispose() {
    const canvas = this.core.root;
    this.cancelLongPress();
    canvas.removeEventListener("click", this.onClick, { capture: true });
    canvas.removeEventListener("pointerdown", this.onDown, { capture: true });
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onUp);
    window.removeEventListener("pointercancel", this.onUp);
    canvas.removeEventListener("contextmenu", this.onContext);
  }
}
