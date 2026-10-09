import * as THREE from "three";

const DIRECTIONS = {front:[0,-1,0], back:[0,1,0], right:[1,0,0], left:[-1,0,0], top:[0,0,1], bottom:[0,0,-1]};

export class CameraTools {
  constructor(core) {
    this.core = core;
    this.history = [];
    this.home = null;
    this.height = 1;
    core.controls.addEventListener("start", () => this.halt());
  }

  // A camera turn or tween stops where it is.
  halt() {
    this.core.cameraMove.move = null;
    if (this.core.inset.helper) this.core.inset.helper.animating = false;
  }

  snapshot() {
    const {camera, controls} = this.core;
    return {ortho:!!camera.isOrthographicCamera, height:this.height, zoom:camera.zoom,
      pose:[...camera.position.toArray(), ...controls.target.toArray()]};
  }

  remember(snapshot=this.snapshot()) {
    this.history.push(snapshot);
    if (this.history.length > 32) this.history.shift();
    this.notify();
  }

  notify() {
    this.core.emit("camera", {orthographic:!!this.core.camera.isOrthographicCamera, back:this.history.length > 0,...this.core.section?.status(),...this.core.selection?.isolationStatus()});
  }

  install(ortho, height, zoom=1) {
    const core=this.core, old=core.camera;
    const camera=ortho ? new THREE.OrthographicCamera(-1,1,1,-1,0.001,1000) : new THREE.PerspectiveCamera(75,1,0.1,1000);
    camera.up.copy(old.up); camera.position.copy(old.position); camera.quaternion.copy(old.quaternion);
    camera.zoom=zoom;
    this.height=height;
    core.camera=camera;
    core.controls.object=camera;
    core.inset.dispose(); core.inset.configure(core.config.inset,core.config.labels);
    core.applyView(); core.controls.update();
    core.requestRender();
  }

  projection(ortho) {
    const {camera,controls}=this.core;
    if (ortho === !!camera.isOrthographicCamera) return;
    const distance=camera.position.distanceTo(controls.target);
    const height=camera.isOrthographicCamera ? this.height/camera.zoom : 2*distance*Math.tan(THREE.MathUtils.degToRad(camera.fov/2))/camera.zoom;
    if (!ortho) camera.position.sub(controls.target).setLength(height/(2*Math.tan(THREE.MathUtils.degToRad(75/2)))).add(controls.target);
    this.install(ortho,height);
  }

  bounds() {
    const core=this.core, bounds=new THREE.Box3();
    const add=(object) => {
      if (!object) return;
      object.updateWorldMatrix(true,true);
      object.traverseVisible(mesh => {
        if (!mesh.isMesh || !mesh.geometry || mesh.material?.visible === false) return;
        if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
        if (mesh.geometry.boundingBox) bounds.union(mesh.geometry.boundingBox.clone().applyMatrix4(mesh.matrixWorld));
      });
    };
    if (core.selection?.selected.length) {
      for (const entity of core.selection.entities.values()) if (core.selection.selected.includes(entity.id)) add(core.nodes.get(entity.node)?.obj);
    } else if (core.ix.shapeMove) add(core.nodes.get(core.ix.shapeMove.node)?.obj);
    else {
      // The arm shows the inspected pose; a compared one is beside it.
      for (const id of Object.keys(core.ix.sources || {})) add(core.nodes.get(Number(id))?.obj);
      const ghost = core.inspection.comparison.ghost;
      if (core.inspection.current && ghost.visible) add(ghost.root);
    }
    return bounds;
  }

  focus() {
    const bounds=this.bounds();
    if (bounds.isEmpty()) return;
    const core=this.core, camera=core.camera;
    const center=bounds.getCenter(new THREE.Vector3());
    const radius=Math.max(0.025,bounds.getSize(new THREE.Vector3()).length()/2);
    const [w,h]=core.size, {left,bottom}=core.layoutInset;
    if (!w || !h) return;
    const availableW=Math.max(w*0.2,w-left-48), availableH=Math.max(h*0.2,h-bottom-100);
    const aspect=availableW/availableH;
    const height=2*radius*1.15/Math.min(1,aspect);
    const distance=height*(h+bottom)/availableH/(2*Math.sin(THREE.MathUtils.degToRad(75/2)));
    const position=camera.position.clone().sub(core.controls.target).normalize().multiplyScalar(distance).add(center);
    if (![...position.toArray(),...center.toArray()].every(Number.isFinite)) return;
    if (camera.isOrthographicCamera) {
      this.height=height*(h+bottom)/availableH; camera.zoom=1; core.applyView();
    }
    core.cameraMove.start([...position.toArray(),...center.toArray()],0.35,true);
  }

  action(action, surface=null) {
    const core=this.core;
    if (action === "status") { this.notify(); return; }
    if(action==="isolate" || action==="restore_objects") {core.gestures.abort("view isolation");core.selection.isolate(action==="isolate");return;}
    if(action.startsWith("section")){core.gestures.abort("section change");core.section.set(action,surface);return;}
    if(action === "surface" && (!surface || !Array.isArray(surface.origin) || !Array.isArray(surface.normal)
      || surface.origin.length !== 3 || surface.normal.length !== 3
      || ![...surface.origin,...surface.normal].every(Number.isFinite)
      || Math.hypot(...surface.normal) < 1e-6)) return;
    // Any drag re-projects against the camera, which is about to move.
    core.pointer.abortDrag("camera action");
    core.gestures.abort("camera action");
    core.cameraMove.finish();
    if (core.inset.helper) core.inset.helper.animating=false;
    if (action === "previous") {
      const saved=this.history.pop();
      if (!saved) return;
      this.install(saved.ortho,saved.height,saved.zoom);
      core.cameraMove.startOrbit(saved.pose);
    } else if (action === "perspective" || action === "orthographic") {
      const ortho=action === "orthographic";
      if (ortho === !!core.camera.isOrthographicCamera) return;
      this.remember(); this.projection(ortho);
    } else if (action === "focus") {
      if (this.bounds().isEmpty()) return;
      this.remember(); this.focus();
    } else if (action === "reset" && this.home) {
      this.remember(); this.install(false,1);
      core.cameraMove.startOrbit(this.home,0.5);
    } else if (action === "surface") {
      this.remember();
      const target=new THREE.Vector3(...surface.origin);
      const distance=core.camera.position.distanceTo(core.controls.target);
      const position=new THREE.Vector3(...surface.normal).normalize().multiplyScalar(distance).add(target);
      core.cameraMove.startOrbit([...position.toArray(),...target.toArray()]);
    } else if (DIRECTIONS[action]) {
      this.remember();
      const target=core.controls.target, distance=core.camera.position.distanceTo(target);
      const position=new THREE.Vector3(...DIRECTIONS[action]).multiplyScalar(distance).add(target);
      core.cameraMove.startOrbit([...position.toArray(),...target.toArray()]);
    }
    this.notify();
  }
}
