import * as THREE from "three";
import { mm, signed } from "./handles.js";
import { setText } from "./dom.js";
import { GAP } from "./overlays.js";
import { facingPlane } from "./pointer.js";

const axisVector = (axis, out) => out.set(0, 0, 0).setComponent(axis, 1);
const center = new THREE.Vector3();
const normal = new THREE.Vector3();
const eye = new THREE.Vector3();
const rotation = new THREE.Quaternion();
const at = [0, 0, 0];
// The card sits off the object's centre, past its handles.
const ACROSS = 45, DOWN = 55;
// How far round a handle's centre a card keeps clear, in pixels.
const HANDLE_PAD = 14;

// Handles on a selected primitive's faces that resize it along its own axes:
// the opposite face stays put, or with shift the size grows from the centre.
export class PrimitiveResize {
  constructor(core) {
    this.core = core;
    this.spec = null;
    this.drag = null;
    this.confirming = null; // a released resize, until the app answers it
    this.screenBox = { left: 0, top: 0, right: 0, bottom: 0 };
    this.screenBoxes = [this.screenBox];
    this.group = new THREE.Group();
    this.group.name = "shape:resize";
    this.group.visible = false;
    core.scene.add(this.group);
    this.geometry = new THREE.BoxGeometry(1, 1, 1);
    this.material = new THREE.MeshBasicMaterial({depthTest:false, depthWrite:false, transparent:true});
    this.handles = [];
    for (let axis=0; axis<3; axis++) for (const sign of [-1,1]) {
      const mesh = new THREE.Mesh(this.geometry, this.material);
      mesh.name = `resize:${axis}:${sign}`;
      mesh.userData = {axis,sign};
      mesh.renderOrder = 1005;
      this.group.add(mesh);
      this.handles.push(mesh);
    }
    this.card = document.createElement("div");
    this.card.className = "wc-gizmo-readout wc-resize-readout";
    this.card.hidden = true;
    this.card.innerHTML = '<div class="wc-gizmo-title">Resize · local axes</div><div class="wc-resize-value"></div><div class="wc-gizmo-hint">Dimensions in the inspector · Esc cancels</div>';
    core.root.append(this.card);
    core.overlays.track("resize", this.card);
    this.title = this.card.querySelector(".wc-gizmo-title");
    this.value = this.card.querySelector(".wc-resize-value");
  }

  load(spec) {
    const next = spec?.mode==="resize" && spec.axes ? spec : null;
    if(this.drag && (!next || next.node!==this.spec.node || next.session!==this.spec.session || next.revision!==this.spec.revision))this.core.gestures.abort("shape changed");
    this.spec=next;
    if(!next){this.group.visible=false;this.card.hidden=true;}
    this.core.requestRender();
  }

  // The dimension a handle on local `axis` changes, as the app defines it.
  dimension(axis) {
    return this.spec.axes[axis];
  }

  get dimensions() {
    return this.drag?.dimensions || (this.confirming?.rec.id === this.spec.node ? this.confirming.dimensions : this.spec.dimensions);
  }

  layoutHandles() {
    const core=this.core, spec=this.spec, rec=spec && core.nodes.get(spec.node);
    this.group.visible=!!rec?.obj.visible;
    this.card.hidden=!this.group.visible;
    if(!this.group.visible) {
      core.overlays.mark("resizeHandles",null);
      return;
    }
    this.material.color.set(core.ix.colors.action);
    rec.obj.updateWorldMatrix(true,false);
    rec.obj.getWorldPosition(center);
    rotation.fromArray(spec.quaternion);
    core.camera.getWorldDirection(eye).negate();
    const dimensions=this.dimensions;
    for(const mesh of this.handles) {
      const {axis,sign}=mesh.userData, key=this.dimension(axis);
      axisVector(axis,normal).applyQuaternion(rotation).multiplyScalar(sign);
      const distance=dimensions[key]*(key==="radius"?1:.5);
      mesh.position.copy(center).addScaledVector(normal,distance);
      const pixel=core.metresPerPixel(mesh.position);
      mesh.quaternion.copy(core.camera.quaternion);
      mesh.scale.set(pixel*12,pixel*12,pixel*2);
      const facing=normal.dot(eye);
      // Back faces stay quiet; nearly end-on axes cannot be dragged accurately.
      mesh.visible=(facing>-.05 && Math.abs(facing)<.97) || this.drag?.mesh===mesh;
    }
    // Where the handles are on screen, for the cards to keep clear of.
    const box=this.screenBox;
    box.left=box.top=Infinity;box.right=box.bottom=-Infinity;
    for(const mesh of this.handles) {
      if(!mesh.visible)continue;
      const [x,y]=core.overlays.project(mesh.position,at);
      box.left=Math.min(box.left,x-HANDLE_PAD);box.right=Math.max(box.right,x+HANDLE_PAD);
      box.top=Math.min(box.top,y-HANDLE_PAD);box.bottom=Math.max(box.bottom,y+HANDLE_PAD);
    }
    core.overlays.mark("resizeHandles",box.left<box.right?this.screenBoxes:null);
  }

  // The card reads the size, and the change while dragging, beside the object.
  layout() {
    if(!this.group.visible)return;
    const spec=this.spec,dimensions=this.dimensions;
    if(this.drag) {
      const d=this.drag, change=d.dimensions[d.key]-spec.dimensions[d.key];
      setText(this.value,`${d.key.toUpperCase()} ${mm(d.dimensions[d.key])} mm · Δ ${signed(change*1000)}`);
    } else setText(this.value,Object.entries(dimensions).map(([key,value])=>`${key.toUpperCase()} ${mm(value)}`).join(" · ")+" mm");
    setText(this.title,this.confirming && !this.drag?"Confirming resize":this.drag ? (this.drag.symmetric?"Resize · from center":"Resize · fixed opposite face") : "Resize · local axes");
    const overlays=this.core.overlays,card=overlays.sized("resize");
    if(!card)return;
    const [x,y]=overlays.project(center,at),{w,h}=card;
    const bounds={...overlays.bounds(),left:this.core.layoutInset.left+GAP};
    const seat=overlays.seat([[x+ACROSS,y+DOWN],[x-ACROSS-w,y+DOWN],[x+ACROSS,y-DOWN-h],[x-ACROSS-w,y-DOWN-h]],w,h,{bounds,obstacles:overlays.obstacles(["inspector"])});
    overlays.place(card,seat.x,seat.y);
  }

  // ---- the pointer's tool ---------------------------------------------------

  // The handle at (x, y), if a press there may grab it.
  hit(x,y) {
    if(!this.spec || !this.group.visible || this.confirming || this.core.gestures.active)return null;
    return this.core.pointer.aim(x,y).intersectObjects(this.handles.filter(h=>h.visible),false)[0]?.object || null;
  }

  begin(e) {
    const mesh=this.hit(e.clientX,e.clientY);
    if(!mesh)return null;
    const core=this.core,spec=this.spec,rec=core.nodes.get(spec.node);
    const {axis,sign}=mesh.userData, key=this.dimension(axis);
    const direction=axisVector(axis,new THREE.Vector3()).applyQuaternion(rotation.fromArray(spec.quaternion)).multiplyScalar(sign);
    const ray=core.pointer.aim(e.clientX,e.clientY).ray;
    const plane=facingPlane(ray,direction,mesh.position,new THREE.Plane(),1e-4);
    const start=plane && ray.intersectPlane(plane,new THREE.Vector3());
    if(!start)return null;
    const symmetric=key==="radius" || e.shiftKey;
    const g=core.gestures.begin("shape",{session:spec.session,node:spec.node,revision:spec.revision,axis,sign,symmetric},e.pointerId,()=>this.finish(false));
    if(!g)return null;
    core.fx.finish(rec);
    core.gestures.hold(g,rec,"scale","position");
    this.drag={g,mesh,rec,axis,sign,key,direction,plane,start,symmetric,
      dimensions:{...spec.dimensions},position:rec.obj.position.clone(),scale:rec.obj.scale.clone(),
      step:core.gizmos.precision.incrementAt(mesh.position,"translate")};
    core.requestRender();
    return {g,move:(event)=>this.move(event),release:()=>this.release(),cancel:(reason)=>core.gestures.abort(reason)};
  }

  move(e) {
    const d=this.drag;
    if(!d)return;
    const at=this.core.pointer.aim(e.clientX,e.clientY).ray.intersectPlane(d.plane,new THREE.Vector3());
    if(!at)return;
    const factor=d.key!=="radius" && d.symmetric?2:1;
    let change=at.sub(d.start).dot(d.direction)*factor;
    if(d.step)change=Math.round(change/d.step)*d.step;
    const size=Math.max(.0001,this.spec.dimensions[d.key]+change);
    d.dimensions[d.key]=size;
    const ratio=size/this.spec.dimensions[d.key];
    d.rec.obj.scale.copy(d.scale);
    if(this.spec.primitive==="sphere")d.rec.obj.scale.multiplyScalar(ratio);
    else if(this.spec.primitive==="cylinder") {
      if(d.key==="radius"){d.rec.obj.scale.x*=ratio;d.rec.obj.scale.z*=ratio;}
      else d.rec.obj.scale.y*=ratio;
    } else d.rec.obj.scale.setComponent(d.axis,d.scale.getComponent(d.axis)*ratio);
    d.rec.obj.position.copy(d.position);
    if(!d.symmetric)d.rec.obj.position.addScaledVector(d.direction,(size-this.spec.dimensions[d.key])/2);
    this.core.requestRender();
  }

  release() {
    const d=this.drag;
    if(!d)return;
    const fields={session:this.spec.session,node:this.spec.node,size:d.dimensions[d.key]};
    this.finish(true);
    this.core.gestures.release(d.g,fields,()=>{this.confirming=null;this.core.requestRender();});
  }

  // A released resize shows its size until the app answers; an aborted one
  // goes back as the gesture lets go of the object.
  finish(released) {
    const d=this.drag;if(!d)return;
    this.drag=null;
    if(released)this.confirming={rec:d.rec,dimensions:d.dimensions};
    this.core.requestRender();
  }

  forget(rec) {
    if(this.confirming?.rec===rec)this.confirming=null;
    if(this.spec?.node===rec.id){if(this.drag)this.core.gestures.abort("deleted");this.load(null);}
  }

  dispose() {
    this.group.removeFromParent();this.geometry.dispose();this.material.dispose();this.card.remove();
  }
}
