// The orientation inset: an axis gizmo in a corner of the view that turns
// with the camera. Clicking one of its axes turns the camera to look along
// it, about the orbit target.

import { ViewHelper } from "three/addons/helpers/ViewHelper.js";

export class Inset {
  constructor(core) {
    this.core = core;
    this.helper = null;
    this.onPointerDown = (event) => {
      if (this.helper && this.helper.handleClick(event)) {
        // The click is the inset's; the orbit controls must not see it.
        event.stopImmediatePropagation();
        this.core.animate();
      }
    };
    core.canvas.addEventListener("pointerdown", this.onPointerDown, { capture: true });
  }

  get active() {
    return !!(this.helper && this.helper.animating);
  }

  configure(inset, labels) {
    if (!inset) {
      this.dispose();
      return;
    }
    if (!this.helper) {
      this.helper = new ViewHelper(this.core.camera, this.core.canvas);
      this.helper.center = this.core.controls.target;
    }
    const anchor = inset.anchor || "bottom-right";
    const mx = inset.margin_x ?? 0;
    const my = inset.margin_y ?? 0;
    this.helper.location = {
      top: anchor.includes("top") ? my : null,
      bottom: anchor.includes("top") ? null : my,
      left: anchor.includes("left") ? mx : null,
      right: anchor.includes("left") ? null : mx,
    };
    if (labels) {
      this.helper.setLabelStyle(labels.font || "24px Arial", labels.color, labels.radius || 14);
      this.helper.setLabels("X", "Y", "Z");
    }
    this.core.requestRender();
  }

  // Drawn over the scene: clearing first would wipe it.
  render(renderer, delta) {
    if (!this.helper) return;
    const autoClear = renderer.autoClear;
    renderer.autoClear = false;
    this.helper.render(renderer);
    renderer.autoClear = autoClear;
    if (this.helper.animating) this.helper.update(delta);
  }

  step() {
    return this.active;
  }

  dispose() {
    if (!this.helper) return;
    this.helper.dispose();
    this.helper = null;
  }

  destroy() {
    this.core.canvas.removeEventListener("pointerdown", this.onPointerDown, { capture: true });
    this.dispose();
  }
}
