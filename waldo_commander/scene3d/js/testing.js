// What browser tests may see of the view: objects by name, where points
// land on screen, the camera, and the frame count. Tests reach the view
// through this surface only.

export function surface(core) {
  const { canvas } = core;
  const rect = () => canvas.getBoundingClientRect();
  const pixel = (v) => {
    const r = rect();
    return [r.left + ((v.x + 1) / 2) * r.width, r.top + ((1 - v.y) / 2) * r.height];
  };
  const byName = (name) => {
    const rec = core.nodes.byName(name);
    return rec ? rec.obj : null;
  };
  const nodeId = (name) => {
    const rec = core.nodes.byName(name);
    return rec ? rec.id : null;
  };

  const S = {
    root: core.root,
    canvas,
    get scene() {
      return core.scene;
    },
    get camera() {
      return core.camera;
    },
    get controls() {
      return core.controls;
    },
    get renderer() {
      return core.renderer;
    },
    get viewHelper() {
      return core.inset.helper;
    },
    get resets() {
      return core.resets;
    },
    byName,
    frames: () => core.frameCount,
    requestRender: () => core.requestRender(),
    idle: () => core.nodes.idle(),
    project(name, points) {
      const o = byName(name);
      if (!o) return null;
      o.updateWorldMatrix(true, false);
      const v = o.position.clone();
      return points.map(([x, y, z]) => pixel(v.set(x, y, z).applyMatrix4(o.matrixWorld).project(core.camera)));
    },
    pixelOf(name) {
      const o = byName(name);
      if (!o) return null;
      const [px, py] = pixel(o.getWorldPosition(o.position.clone()).project(core.camera));
      return document.elementFromPoint(px, py) === canvas ? [px, py] : null;
    },
    framing() {
      const cam = core.camera;
      const view = cam.view;
      return {
        view: view
          ? { enabled: view.enabled, fullWidth: view.fullWidth, fullHeight: view.fullHeight, offsetY: view.offsetY }
          : null,
        aspect: cam.aspect,
        inset: { ...core.layoutInset },
      };
    },
    cameraPose: () => [...core.camera.position.toArray(), ...core.controls.target.toArray()],
    setCameraPose([px, py, pz, tx, ty, tz]) {
      core.camera.position.set(px, py, pz);
      core.controls.target.set(tx, ty, tz);
      core.controls.update();
    },
    zoom(distance) {
      const t = core.controls.target;
      core.camera.position.sub(t).setLength(distance).add(t);
      core.controls.update();
    },
    viewDirection: () => core.camera.position.clone().sub(core.controls.target).normalize().toArray(),
    fx: {
      alarm(names, color) {
        core.fx.run("alarm", [names.map(nodeId).filter((id) => id !== null), color]);
      },
    },
  };

  // A pixel on the orientation inset's sprite for the level axis that faces
  // the viewer most, and that axis.
  S.insetAxis = () => {
    const vh = core.inset.helper;
    if (!vh) return null;
    const r = rect();
    const loc = vh.location;
    const dim = 128;
    const left = r.left + (loc.left !== null ? loc.left : canvas.offsetWidth - dim - loc.right);
    const top = r.top + (loc.top !== null ? loc.top : canvas.offsetHeight - dim - loc.bottom);
    const toInset = core.camera.quaternion.clone().invert();
    let best = null;
    for (const axis of [
      [1, 0, 0],
      [-1, 0, 0],
      [0, 1, 0],
      [0, -1, 0],
    ]) {
      const v = core.camera.position.clone().set(...axis).applyQuaternion(toInset);
      const x = left + ((v.x / 2 + 1) / 2) * dim;
      const y = top + ((1 - v.y / 2) / 2) * dim;
      if (document.elementFromPoint(x, y) !== canvas) continue;
      if (!best || v.z > best.z) best = { x, y, z: v.z, axis };
    }
    return best && [best.x, best.y, best.axis];
  };

  return S;
}
