// The view's context menu: when a right-click (or the menu key, a ctrl+click
// on macOS, a touch held still) asks for it, the app hears what is under the
// pointer, fills the menu and has it opened at the pointer. A right-drag pans
// and asks for nothing.
//
// Browsers fire the contextmenu event on press (Linux, macOS) or on release
// (Windows), so the event itself is always swallowed and the press decides at
// its release. Each request has a generation: only the newest one opens.

const DRAG_PX = 5;
const LONG_PRESS_MS = 500;

export class Menu {
  constructor(core) {
    this.core = core;
    this.gen = 0;
    this.press = null;
    this.released = false;
    this.long = null;
    const canvas = core.canvas;
    this.onDown = (e) => this.down(e);
    this.onMove = (e) => this.move(e);
    this.onUp = (e) => this.up(e);
    this.onContext = (e) => this.context(e);
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
    this.close();
    this.cancelLongPress();
    const mac = /Mac/.test(navigator.platform);
    if (e.button === 0 && e.ctrlKey && mac) {
      // A ctrl+click is macOS's right-click: it neither orbits nor grabs.
      e.stopImmediatePropagation();
      return;
    }
    if (e.button === 2) {
      this.press = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: 0 };
      return;
    }
    if (e.pointerType === "touch" && e.button === 0) {
      this.long = {
        id: e.pointerId,
        x: e.clientX,
        y: e.clientY,
        timer: setTimeout(() => {
          const long = this.long;
          this.long = null;
          if (long && !this.core.gestures.active) {
            this.core.pointer.suppressTap(long.id);
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
    if (l && e.pointerId === l.id && Math.hypot(e.clientX - l.x, e.clientY - l.y) > DRAG_PX) this.cancelLongPress();
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

  // Abandon a press that has turned into a drag of something.
  cancel() {
    this.cancelLongPress();
  }

  request(cx, cy) {
    const core = this.core;
    if (core.gestures.blocked || !core.live) return;
    const gen = ++this.gen;
    const { hits, ground } = core.pointer.contextAt(cx, cy);
    core.emit("context", { epoch: core.gestures.epoch, gen, hits, ground, cx, cy });
  }

  // The app filled the menu for request `gen`: open it there, unless a newer
  // request has come since.
  open(gen, cx, cy) {
    if (gen !== this.gen) return;
    const menu = this.menu;
    if (!menu || !menu.show) return;
    menu.show(new MouseEvent("contextmenu", { clientX: cx, clientY: cy }));
  }

  dispose() {
    const canvas = this.core.canvas;
    this.cancelLongPress();
    canvas.removeEventListener("pointerdown", this.onDown, { capture: true });
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onUp);
    window.removeEventListener("pointercancel", this.onUp);
    canvas.removeEventListener("contextmenu", this.onContext);
  }
}
