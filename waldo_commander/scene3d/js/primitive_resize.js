import * as THREE from "three";
import { FINGER_PX, tapPx } from "./handles.js";
import { FIELD_AVOIDS, HandleField, amount } from "./precision.js";
import { facingPlane } from "./pointer.js";

const axisVector = (axis, out) => out.set(0, 0, 0).setComponent(axis, 1);
const center = new THREE.Vector3();
const normal = new THREE.Vector3();
const eye = new THREE.Vector3();
const rotation = new THREE.Quaternion();
const at = [0, 0, 0];
const from = [0, 0, 0];
// How far round a handle's centre a card keeps clear, in pixels.
const HANDLE_PAD = 14;

// Handles on a selected primitive's faces that resize it along its own axes:
// the opposite face stays put, or with shift the size grows from the centre.
// The change rides on the handle; a handle clicked, or that value clicked
// after a drag, takes a typed change.
export class PrimitiveResize {
  constructor(core) {
    this.core = core;
    this.spec = null;
    this.drag = null;
    this.confirming = null; // a released resize, until the app answers it
    this.entry = null; // a change being typed: {mesh, symmetric, base}
    this.settled = null; // the last drag's change: {mesh, symmetric, change, key, text}
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
    this.field = new HandleField(core, "resizeValue", {open: () => this.reopen(), commit: (v) => this.commit(v),
      cancel: () => this.closeEntry(), cycle: (step) => this.cycle(step)});
  }

  load(spec) {
    const next = spec?.mode==="resize" && spec.axes ? spec : null;
    if(this.drag && (!next || next.node!==this.spec.node || next.session!==this.spec.session || next.revision!==this.spec.revision))this.core.gestures.abort("shape changed");
    if(!next || next.node!==this.spec?.node || next.session!==this.spec?.session){this.closeEntry();this.settled=null;}
    this.spec=next;
    if(!next){this.group.visible=false;this.field.hide();}
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

  // The change rides on its handle: the one being dragged, typed along, or
  // last let go.
  layout() {
    if(!this.group.visible){this.field.hide();return;}
    const d=this.drag,e=this.entry,s=this.settled;
    let mesh;
    if(d){mesh=d.mesh;this.field.show(d.text,"mm",d.key);}
    else if(e)mesh=e.mesh;
    else if(s){mesh=s.mesh;this.field.show(s.text,"mm",s.key);}
    else {this.field.hide();return;}
    const overlays=this.core.overlays;
    overlays.project(mesh.position,at);overlays.project(center,from);
    this.field.place(at,from,overlays.obstacles(FIELD_AVOIDS));
  }

  // A handle let go where it was pressed, or the value left by a drag
  // clicked: a change to type along it, from what the drag did.
  openEntry(mesh,symmetric,base) {
    this.entry={mesh,symmetric,base};
    this.field.edit(base?String(Number(base.toFixed(2))):"","mm",this.dimension(mesh.userData.axis));
    this.core.requestRender();
  }

  reopen() {
    const s=this.settled;
    if(s && !this.drag && !this.confirming)this.openEntry(s.mesh,s.symmetric,s.change*1000);
  }

  // Tab: the next of the primitive's dimensions, from the handle on the same side.
  cycle(step) {
    const e=this.entry;if(!e)return;
    const {axis,sign}=e.mesh.userData,key=this.dimension(axis);
    for(let k=1;k<3;k++) {
      const next=(axis+step*k+3)%3,nextKey=this.dimension(next);
      if(nextKey===key)continue;
      const mesh=this.handles.find(h=>h.userData.axis===next&&h.userData.sign===sign);
      this.openEntry(mesh,nextKey==="radius"||this.spec.symmetric||e.symmetric&&key!=="radius",0);
      return;
    }
  }

  closeEntry() {
    if(!this.entry)return;
    this.entry=null;this.field.close();
    this.core.requestRender();
  }

  // Anything else pressed: the change on the handles goes.
  dismiss() {
    this.closeEntry();this.settled=null;
  }

  // The typed change replaces what the last drag did along that dimension.
  commit(value) {
    const e=this.entry,spec=this.spec;
    if(!e || !spec || this.drag || this.confirming){this.closeEntry();return;}
    const key=this.dimension(e.mesh.userData.axis),change=(value-e.base)/1000;
    const size=spec.dimensions[key]+change;
    if(!(size>0)){this.field.invalid();return;}
    this.closeEntry();
    if(change!==0) {
      const d=this.start(e.mesh,e.symmetric,this.core.pointer.lastPointerId);
      if(!d)return;
      this.resizeTo(d,size);this.release();
    }
    this.settle(e.mesh,e.symmetric,value/1000,null);
  }

  // The change a resize leaves on its handle, until anything else is pressed.
  settle(mesh,symmetric,change,step) {
    this.settled={mesh,symmetric,change,key:this.dimension(mesh.userData.axis),text:amount(change*1000,step&&step*1000)};
  }

  // ---- the pointer's tool ---------------------------------------------------

  // The handle at (x, y), if a press there may grab it; for a finger, the
  // nearest within FINGER_PX when none is under it.
  hit(x,y,pointerType="mouse") {
    if(!this.spec || !this.group.visible || this.confirming || this.core.gestures.active)return null;
    const shown=this.handles.filter(h=>h.visible),pointer=this.core.pointer;
    const exact=pointer.aim(x,y).intersectObjects(shown,false)[0]?.object;
    if(exact || pointerType!=="touch")return exact || null;
    let nearest=null,best=FINGER_PX;
    for(const mesh of shown) {
      const distance=pointer.pixelsFrom(mesh.position,x,y);
      if(distance<=best){best=distance;nearest=mesh;}
    }
    return nearest;
  }

  begin(e) {
    const mesh=this.hit(e.clientX,e.clientY,e.pointerType);
    if(!mesh)return null;
    const ray=this.core.pointer.aim(e.clientX,e.clientY).ray;
    const direction=this.direction(mesh,new THREE.Vector3());
    const plane=facingPlane(ray,direction,mesh.position,new THREE.Plane(),1e-4);
    const start=plane && ray.intersectPlane(plane,new THREE.Vector3());
    if(!start)return null;
    const d=this.start(mesh,this.dimension(mesh.userData.axis)==="radius" || this.spec.symmetric || e.shiftKey,e.pointerId);
    if(!d)return null;
    Object.assign(d,{plane,start,x:e.clientX,y:e.clientY,step:this.core.gizmos.precision.incrementAt(mesh.position,"translate")});
    return {g:d.g,move:(event)=>this.move(event),release:(event)=>this.release(event),cancel:(reason)=>this.core.gestures.abort(reason)};
  }

  // Outward through `mesh`'s face, in the world.
  direction(mesh,out) {
    const {axis,sign}=mesh.userData;
    return axisVector(axis,out).applyQuaternion(rotation.fromArray(this.spec.quaternion)).multiplyScalar(sign);
  }

  // A resize gesture through `mesh`, or null if none may begin.
  start(mesh,symmetric,pointerId) {
    const core=this.core,spec=this.spec,rec=core.nodes.get(spec.node);
    if(!rec)return null;
    const {axis,sign}=mesh.userData;
    const g=core.gestures.begin("shape",{session:spec.session,node:spec.node,revision:spec.revision,axis,sign,symmetric},pointerId,()=>this.finish(false));
    if(!g)return null;
    core.fx.finish(rec);
    core.gestures.hold(g,rec,"scale","position");
    this.dismiss();
    this.drag={g,mesh,rec,axis,sign,key:this.dimension(axis),direction:this.direction(mesh,new THREE.Vector3()),symmetric,step:null,
      dimensions:{...spec.dimensions},position:rec.obj.position.clone(),scale:rec.obj.scale.clone(),text:amount(0,null)};
    core.requestRender();
    return this.drag;
  }

  move(e) {
    const d=this.drag;
    if(!d)return;
    const at=this.core.pointer.aim(e.clientX,e.clientY).ray.intersectPlane(d.plane,new THREE.Vector3());
    if(!at)return;
    const factor=d.key!=="radius" && d.symmetric?2:1;
    let change=at.sub(d.start).dot(d.direction)*factor;
    if(d.step)change=Math.round(change/d.step)*d.step;
    this.resizeTo(d,Math.max(.0001,this.spec.dimensions[d.key]+change));
  }

  // The object drawn at `size` along the drag's dimension.
  resizeTo(d,size) {
    if(size!==d.dimensions[d.key])d.text=amount((size-this.spec.dimensions[d.key])*1000,d.step&&d.step*1000);
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

  // Let go where it was pressed, a handle resizes nothing: its change is typed.
  release(e=null) {
    const d=this.drag;
    if(!d)return;
    if(e && Math.hypot(e.clientX-d.x,e.clientY-d.y)<=tapPx(e.pointerType)) {
      this.core.gestures.abort("click");
      this.openEntry(d.mesh,d.symmetric,0);
      return;
    }
    const fields={session:this.spec.session,node:this.spec.node,size:d.dimensions[d.key]};
    this.finish(true);
    this.core.gestures.release(d.g,fields,(confirmed)=>{
      this.confirming=null;
      if(!confirmed)this.settled=null;
      this.core.requestRender();
    });
  }

  // A released resize shows its size until the app answers, and leaves its
  // change on the handle; an aborted one goes back as the gesture lets go of
  // the object.
  finish(released) {
    const d=this.drag;if(!d)return;
    this.drag=null;
    if(released) {
      this.confirming={rec:d.rec,dimensions:d.dimensions};
      this.settle(d.mesh,d.symmetric,d.dimensions[d.key]-this.spec.dimensions[d.key],d.step);
    }
    this.core.requestRender();
  }

  forget(rec) {
    if(this.confirming?.rec===rec)this.confirming=null;
    if(this.spec?.node===rec.id){if(this.drag)this.core.gestures.abort("deleted");this.load(null);}
  }

  dispose() {
    this.group.removeFromParent();this.geometry.dispose();this.material.dispose();this.field.node.remove();
  }
}
