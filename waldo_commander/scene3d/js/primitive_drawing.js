import * as THREE from "three";
import { FINGER_PX, mm } from "./handles.js";
import { setStyle } from "./dom.js";
import { GAP } from "./overlays.js";
import { facingPlane } from "./pointer.js";
import { FIELD_AVOIDS } from "./precision.js";

const box=new THREE.Box3();
const at=[0,0,0];
// Away from the card's corner of the view, and away from the drawing.
const MARGIN=16,PAD=24;

// Drawing a primitive on a plane: drag its footprint (or radius), then pull
// its height up by the handle on top, or type it. One gesture spans both.
export class PrimitiveDrawing {
  constructor(core) {
    this.core=core;this.spec=null;this.g=null;this.phase="armed";this.dragging=false;this.retrying=false;
    this.inverse=new THREE.Quaternion();this.heightPlane=new THREE.Plane();
    this.group=new THREE.Group();this.group.name="draw:preview";this.group.visible=false;core.scene.add(this.group);
    this.material=new THREE.MeshPhongMaterial({transparent:true,opacity:.28,depthWrite:false});
    this.meshes={
      box:new THREE.Mesh(new THREE.BoxGeometry(1,1,1),this.material),
      cylinder:new THREE.Mesh(new THREE.CylinderGeometry(.5,.5,1,32),this.material),
      sphere:new THREE.Mesh(new THREE.SphereGeometry(.5,32,20),this.material),
    };
    this.edgeMaterial=new THREE.LineBasicMaterial({transparent:true,opacity:.7,depthWrite:false});
    for(const kind of ["box","cylinder"]) {
      const mesh=this.meshes[kind];
      mesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry,25),this.edgeMaterial));
    }
    this.meshes.cylinder.rotation.x=Math.PI/2;
    for(const [kind,mesh] of Object.entries(this.meshes)){mesh.name=`draw:${kind}`;this.group.add(mesh);}
    this.handle=new THREE.Mesh(new THREE.SphereGeometry(1,12,8),new THREE.MeshBasicMaterial({transparent:true,depthTest:false,depthWrite:false}));
    this.handle.name="draw:height";this.handle.renderOrder=1005;this.handle.visible=false;core.scene.add(this.handle);
    this.card=document.createElement("div");this.card.className="wc-gizmo-readout wc-draw-readout";this.card.hidden=true;
    this.card.innerHTML='<div class="wc-gizmo-title"></div><div class="wc-resize-value"></div><div class="wc-gizmo-hint"></div><form class="wc-gizmo-entry"><label>Height <input aria-label="Drawn height in millimetres" type="number" min="0.1" step="any" required> mm</label><button type="submit">Create</button></form><button type="button" aria-label="Cancel drawing">Cancel</button>';
    core.root.append(this.card);
    core.overlays.track("drawing",this.card);
    this.title=this.card.querySelector(".wc-gizmo-title");this.value=this.card.querySelector(".wc-resize-value");this.hint=this.card.querySelector(".wc-gizmo-hint");
    this.form=this.card.querySelector("form");this.input=this.card.querySelector("input");
    this.form.onsubmit=e=>{e.preventDefault();const height=this.input.valueAsNumber/1000;if(Number.isFinite(height)&&height>0){this.height=height;this.show();this.commit();}};
    this.input.oninput=()=>{const value=this.input.valueAsNumber/1000;if(Number.isFinite(value)&&value>0){this.height=value;this.show();}};
    this.card.querySelector('button[type="button"]').onclick=()=>this.cancel();
    this.card.onkeydown=e=>{if(e.key==="Escape")this.cancel();e.stopPropagation();};
  }

  get active(){return !!this.spec;}

  load(spec) {
    if(spec?.session===this.spec?.session)return;
    if(this.g)this.core.gestures.abort("drawing changed");
    this.clear();this.spec=spec;
    if(!spec)return;
    this.origin=new THREE.Vector3().fromArray(spec.origin);
    this.rotation=new THREE.Quaternion().fromArray(spec.quaternion);
    this.inverse.copy(this.rotation).invert();
    this.normal=new THREE.Vector3(0,0,1).applyQuaternion(this.rotation);
    this.plane=new THREE.Plane().setFromNormalAndCoplanarPoint(this.normal,this.origin);
    this.phase="armed";this.width=0;this.depth=0;this.height=.1;this.u=0;this.v=0;this.moved=0;this.step=0;
    this.cardAnchor=null;this.cardOrigin=null;
    this.group.quaternion.copy(this.rotation);
    this.card.hidden=false;this.core.pointer.forgetHover();this.core.controls.enabled=false;
    for(const [kind,mesh] of Object.entries(this.meshes))mesh.visible=kind===spec.primitive;
    this.material.color.set(this.core.ix.colors.action);this.edgeMaterial.color.set(this.core.ix.colors.action);this.handle.material.color.set(this.core.ix.colors.action);
    this.core.renderer.compile(this.group,this.core.camera,this.core.scene);
    this.core.requestRender();
  }

  point(e,plane=this.plane) {
    return this.core.pointer.aim(e.clientX,e.clientY).ray.intersectPlane(plane,new THREE.Vector3());
  }

  // ---- the pointer's tool: while drawing, every press is the drawing's ----

  hit() {
    return this.active;
  }

  begin(e) {
    const core=this.core;
    if(this.phase==="armed") {
      const p=this.point(e);if(!p)return null;
      const session=this.spec.session;
      const g=core.gestures.begin("draw",{session},e.pointerId,()=>{
        if(this.retrying)return;
        this.clear();core.emit("draw_cancel",{session});
      });
      if(!g)return null;
      this.g=g;this.phase="footprint";
      this.start=p.sub(this.origin).applyQuaternion(this.inverse);
      this.startPixel=[e.clientX,e.clientY];
      this.step=core.gizmos.precision.incrementAt(this.origin,"translate");
    } else if(this.phase==="height") {
      this.handle.updateWorldMatrix(true,false);
      const ray=core.pointer.aim(e.clientX,e.clientY);
      const near=e.pointerType==="touch" && core.pointer.pixelsFrom(this.handle.position,e.clientX,e.clientY)<=FINGER_PX;
      if(!near && !ray.intersectObject(this.handle,false).length)return null;
      if(!facingPlane(ray.ray,this.normal,this.handle.position,this.heightPlane,.08))return null;
      this.heightStart=this.point(e,this.heightPlane);if(!this.heightStart)return null;
      this.initialHeight=this.height;this.g.pointerId=e.pointerId;
    } else return null;
    this.cardAnchor=null;this.cardOrigin=[e.clientX,e.clientY];
    this.dragging=true;
    core.requestRender();
    return {g:this.g,move:(event)=>this.move(event),release:(event)=>this.up(event),cancel:(reason)=>core.gestures.abort(reason)};
  }

  move(e) {
    if(!this.dragging)return;
    if(this.phase==="footprint") {
      const p=this.point(e);if(!p)return;
      p.sub(this.origin).applyQuaternion(this.inverse);
      const snap=value=>this.step?Math.round(value/this.step)*this.step:value;
      if(this.spec.primitive==="box") {
        const x=snap(p.x-this.start.x),y=snap(p.y-this.start.y);
        this.u=this.start.x+x/2;this.v=this.start.y+y/2;
        this.width=Math.abs(x);this.depth=Math.abs(y);
      } else {
        this.u=this.start.x;this.v=this.start.y;
        this.width=2*Math.max(0,snap(Math.hypot(p.x-this.start.x,p.y-this.start.y)));this.depth=this.width;
      }
      this.moved=Math.hypot(e.clientX-this.startPixel[0],e.clientY-this.startPixel[1]);
    } else {
      const p=this.point(e,this.heightPlane);if(!p)return;
      let delta=p.sub(this.heightStart).dot(this.normal);
      if(this.step)delta=Math.round(delta/this.step)*this.step;
      this.height=Math.max(.0001,this.initialHeight+delta);
      this.input.value=mm(this.height);
    }
    this.show();
  }

  show() {
    if(!this.spec)return;
    this.group.visible=Math.min(this.width,this.depth)>=.0001;
    const sphere=this.spec.primitive==="sphere";
    const height=sphere?this.width:this.phase==="footprint"?.001:this.height;
    const local=new THREE.Vector3(this.u,this.v,height/2);
    this.group.position.copy(local.applyQuaternion(this.rotation).add(this.origin));
    const mesh=this.meshes[this.spec.primitive];
    if(this.spec.primitive==="cylinder")mesh.scale.set(Math.max(.0001,this.width),Math.max(.0001,height),Math.max(.0001,this.width));
    else mesh.scale.set(Math.max(.0001,this.width),Math.max(.0001,this.depth),Math.max(.0001,height));
    this.handle.visible=this.phase==="height";
    this.handle.position.set(this.u,this.v,height).applyQuaternion(this.rotation).add(this.origin);
    this.core.requestRender();
  }

  up(e) {
    if(!this.dragging)return;
    this.move(e);
    this.dragging=false;
    if(!this.g)return;
    if(this.phase==="footprint") {
      if(this.moved<4){this.cancel();return;}
      if(Math.min(this.width,this.depth)<.0001){this.retry();return;}
      if(this.spec.primitive==="sphere"){this.height=this.width;this.commit();return;}
      this.phase="height";this.cardAnchor=null;this.g.pointerId=null;this.input.value=mm(this.height);this.show();
    } else this.commit();
  }

  commit() {
    if(!this.g || !this.spec)return;
    const g=this.g;
    const fields={session:this.spec.session,u:this.u,v:this.v,width:this.width,depth:this.depth,height:this.height};
    this.g=null;this.clear();this.core.gestures.release(g,fields);
    this.core.emit("draw_cancel",{session:fields.session});
  }

  // A stroke too thin to make a shape ends, and the drawing waits for another.
  retry() {
    this.retrying=true;
    this.core.gestures.abort("drawing too thin");
    this.retrying=false;
    this.g=null;this.phase="armed";this.width=0;this.depth=0;this.moved=0;
    this.group.visible=false;this.handle.visible=false;this.cardAnchor=null;
    this.core.requestRender();
  }

  cancel() {
    if(!this.spec)return;
    const session=this.spec.session;
    if(this.g)this.core.gestures.abort("drawing cancelled");
    else {this.clear();this.core.emit("draw_cancel",{session});}
  }

  clear() {
    this.dragging=false;this.g=null;this.spec=null;
    this.group.visible=false;this.handle.visible=false;this.card.hidden=true;
    this.core.pointer.restoreOrbit();this.core.requestRender();
  }

  // The card takes the view's corner that keeps it clearest of the panels
  // and the drawing, and farthest from where the drawing began.
  placeCard(card) {
    const overlays=this.core.overlays,{w,h}=card,{width,height}=overlays.view;
    const right=width-w-MARGIN,bottom=Math.max(MARGIN,height-this.core.layoutInset.bottom-h-MARGIN);
    const extra=[];
    if(this.group.visible) {
      this.group.updateWorldMatrix(true,true);
      extra.push(overlays.projectBox(box.setFromObject(this.meshes[this.spec.primitive]),PAD));
    }
    const view=overlays.view;
    const [ox,oy]=this.cardOrigin?[this.cardOrigin[0]-view.left,this.cardOrigin[1]-view.top]:overlays.project(this.origin,at);
    const candidates=[[overlays.clearLeft(MARGIN,MARGIN+h),MARGIN],[right,MARGIN],[overlays.clearLeft(bottom,bottom+h),bottom],[right,bottom]];
    const seat=overlays.seat(candidates,w,h,{bounds:{left:0,top:0,right:width,bottom:height},obstacles:overlays.obstacles(FIELD_AVOIDS,extra),pad:GAP,
      cost:(x,y)=>-Math.hypot(x+w/2-ox,y+h/2-oy)});
    this.cardAnchor=[seat.x,seat.y];
  }

  layoutHandle() {
    if(this.handle.visible)this.handle.scale.setScalar(this.core.metresPerPixel(this.handle.position)*7);
  }

  layout() {
    if(!this.spec)return;
    const kind=this.spec.primitive;
    this.title.textContent=`Draw ${kind} · ${this.spec.label}${this.step?` · ${mm(this.step)} mm step`:""}`;
    this.value.textContent=this.phase==="armed"?"Drag to draw":kind==="box"?`${mm(this.width)} × ${mm(this.depth)}${this.phase==="height"?` × ${mm(this.height)}`:""} mm`:`Radius ${mm(this.width/2)} mm${kind==="cylinder"&&this.phase==="height"?` · Height ${mm(this.height)} mm`:""}`;
    this.hint.textContent=this.phase==="height"?"Pull the top handle; release to create. Or enter height below.":kind==="box"?"Drag a footprint on the plane · Esc cancels":"Drag from the center to set radius · Esc cancels";
    this.form.hidden=this.phase!=="height";
    this.input.disabled=this.dragging;
    const overlays=this.core.overlays,{width,height}=overlays.view;
    setStyle(this.card,"maxWidth",`${Math.max(200,Math.min(360,width-this.core.layoutInset.left-40))}px`);
    setStyle(this.card,"bottom","auto");
    const card=overlays.sized("drawing");
    if(!card)return;
    if(!this.cardAnchor)this.placeCard(card);
    overlays.place(card,Math.max(0,Math.min(this.cardAnchor[0],width-card.w-GAP)),Math.max(0,Math.min(this.cardAnchor[1],height-this.core.layoutInset.bottom-card.h-GAP)));
  }

  dispose() {
    this.group.removeFromParent();this.handle.removeFromParent();this.card.remove();
    this.group.traverse(object=>object.geometry?.dispose());
    this.material.dispose();this.edgeMaterial.dispose();this.handle.geometry.dispose();this.handle.material.dispose();
  }
}
