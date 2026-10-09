import * as THREE from "three";
import { label } from "./rings.js";
import { facingPlane } from "./pointer.js";
import { stickySnap } from "./handles.js";

const axes={X:new THREE.Vector3(1,0,0),Y:new THREE.Vector3(0,1,0),Z:new THREE.Vector3(0,0,1)};
const NO_COLORS={};
const hit=new THREE.Vector3(),moved=new THREE.Vector3(),cross=new THREE.Vector3(),turn=new THREE.Quaternion(),parent=new THREE.Quaternion();
const TAU=Math.PI*2;

// Arrows, plane patches and rotation arcs around one pivot, all grabbable at
// once. The Pointer routes the press and owns the pointer while a drag runs;
// the same drag serves authored previews and admitted live jogs.
export class SceneTransform extends THREE.EventDispatcher {
  constructor(core) {
    super();this.core=core;
    this.mode="translate";this.space="local";this.axis=null;this.object=null;
    this.enabled=true;this.dragging=false;this.translationSnap=null;this.rotationSnap=null;
    this.rotationAngle=0;this.rawAngle=0;this.radius=0;
    this.styled={axis:undefined,mode:undefined,dragging:undefined,colors:undefined,background:undefined};
    this.raycaster=new THREE.Raycaster();
    this.origin=new THREE.Vector3();this.startQ=new THREE.Quaternion();this.basis=new THREE.Quaternion();
    this.plane=new THREE.Plane();this.startHit=null;this.worldAxes=[];this.snapped=[0,0];
    this.arcX=new THREE.Vector3();this.arcY=new THREE.Vector3();this.theta=0;
    this.helper=new THREE.Group();this.helper.name="movement:handles";this.helper.visible=false;
    this.groups={};this.pickers=[];this.letters=[];
    for(const mode of ["translate","rotate"]) {
      const group=new THREE.Group();this.groups[mode]=group;this.helper.add(group);
      for(const [letter,axis] of Object.entries(axes)) {
        const root=new THREE.Group();root.name=letter;
        const u=axes[{X:"Y",Y:"Z",Z:"X"}[letter]],v=axes[{X:"Z",Y:"X",Z:"Y"}[letter]];
        root.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(u,v,axis));group.add(root);
        const material=new THREE.MeshBasicMaterial({depthTest:false,depthWrite:false,transparent:true});
        const halo=new THREE.MeshBasicMaterial({depthTest:false,depthWrite:false,transparent:true,opacity:.4});
        root.userData={letter,material,halo};
        if(mode==="translate") {
          for(const [radius,mat,order] of [[.022,halo,1000],[.012,material,1001]]) {
            const shaft=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,.90,8),mat);
            shaft.rotation.x=Math.PI/2;shaft.position.z=.60;shaft.renderOrder=order;root.add(shaft);
          }
          const cap=new THREE.Mesh(new THREE.ConeGeometry(.062,.15,16),material);cap.rotation.x=-Math.PI/2;cap.position.z=1.1;cap.renderOrder=1002;root.add(cap);
          const picker=new THREE.Mesh(new THREE.CylinderGeometry(.075,.075,1,8),material);
          picker.rotation.x=Math.PI/2;picker.position.z=.65;picker.visible=false;picker.name=letter;picker.userData.mode=mode;root.add(picker);this.pickers.push(picker);
        } else {
          for(const [tube,mat,order] of [[.019,halo,1000],[.010,material,1001]]) {
            const ring=new THREE.Mesh(new THREE.TorusGeometry(.75,tube,8,32,Math.PI*.38),mat);ring.rotation.z=Math.PI*.06;ring.renderOrder=order;root.add(ring);
          }
          const grip=new THREE.Mesh(new THREE.SphereGeometry(.035,12,8),material);grip.position.set(.75/Math.sqrt(2),.75/Math.sqrt(2),0);grip.renderOrder=1002;root.add(grip);
          const picker=new THREE.Mesh(new THREE.TorusGeometry(.75,.065,6,32,Math.PI*.38),material);picker.rotation.z=Math.PI*.06;picker.visible=false;picker.name=letter;picker.userData.mode=mode;root.add(picker);this.pickers.push(picker);
        }
        const text=label(`movement:${mode}:${letter}`);text.element.className="wc-axis-label";text.element.style.cssText="";
        text.element.textContent=mode==="rotate"?`R${letter.toLowerCase()}`:letter;text.position.set(mode==="rotate"?.68:0,mode==="rotate"?.68:0,mode==="translate"?1.30:0);root.add(text);root.userData.label=text;this.letters.push(text);
      }
    }
    for(const [normal,letters] of [["Z","XY"],["X","YZ"],["Y","XZ"]]) {
      const root=new THREE.Group();root.name=letters;root.quaternion.setFromUnitVectors(axes.Z,axes[normal]);
      const center=axes[letters[0]].clone().add(axes[letters[1]]).multiplyScalar(.26);
      root.position.copy(center);this.groups.translate.add(root);
      const material=new THREE.MeshBasicMaterial({transparent:true,opacity:.22,side:THREE.DoubleSide,depthTest:false,depthWrite:false,forceSinglePass:true});
      const patch=new THREE.Mesh(new THREE.PlaneGeometry(.18,.18),material);patch.renderOrder=1001;root.add(patch);
      const border=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints([[-.09,-.09,0],[.09,-.09,0],[.09,.09,0],[-.09,.09,0]].map(p=>new THREE.Vector3(...p))),new THREE.LineBasicMaterial({transparent:true,depthTest:false,depthWrite:false}));border.renderOrder=1002;root.add(border);
      const picker=new THREE.Mesh(new THREE.PlaneGeometry(.23,.23),material);picker.visible=false;picker.name=letters;picker.userData.mode="translate";root.add(picker);this.pickers.push(picker);
      root.userData={letter:letters,colorAxis:normal,material,border};
    }
  }

  getHelper(){return this.helper;}
  attach(object){this.object=object;this.helper.visible=true;this.update();}
  detach(){this.object=null;this.helper.visible=false;this.axis=null;}
  setSpace(space){this.space=space;this.update();}
  setTranslationSnap(step){this.translationSnap=step;}
  setRotationSnap(step){this.rotationSnap=step;}

  update() {
    if(!this.object)return;
    if(this.dragging){this.helper.position.copy(this.origin);this.helper.quaternion.copy(this.basis);}
    else {
      this.object.getWorldPosition(this.helper.position);
      if(this.space==="local")this.object.getWorldQuaternion(this.helper.quaternion);else this.helper.quaternion.identity();
      const pixel=this.core.metresPerPixel(this.helper.position);
      this.radius=Math.max(pixel*65,Math.min(pixel*115,.085));
    }
    this.helper.scale.setScalar(this.radius);
    this.restyle();
    this.helper.updateWorldMatrix(true,true);
  }

  // The colours follow the handle in use, the theme and the background.
  restyle() {
    const colors=this.core.ix.colors||NO_COLORS,background=this.core.config.background,s=this.styled;
    if(s.axis===this.axis&&s.mode===this.mode&&s.dragging===this.dragging&&s.colors===colors&&s.background===background)return;
    s.axis=this.axis;s.mode=this.mode;s.dragging=this.dragging;s.colors=colors;s.background=background;
    for(const [mode,group] of Object.entries(this.groups)) {
      for(const root of group.children) {
        const {letter,colorAxis,material,halo,border,label}=root.userData;
        const active=this.axis===letter&&this.mode===mode,dim=!!this.axis&&!active;
        material.color.set(active?colors.action:colors[`axis${colorAxis||letter}`] || colors.action);
        material.opacity=(border?(active?.5:.18):1)*(dim?.32:1);
        if(halo)halo.color.set(background);
        if(border){border.material.color.copy(material.color);border.material.opacity=dim?.25:.8;}
        if(label)label.visible=mode==="translate" || active&&!this.dragging;
      }
    }
  }

  // The handle at `point` (normalised device coordinates), if any.
  pick(point) {
    if(!this.object||!this.enabled||this.dragging)return null;
    this.update();this.raycaster.setFromCamera(point,this.core.camera);
    const picked=this.raycaster.intersectObjects(this.pickers,false)[0]?.object;
    return picked?{axis:picked.name,mode:picked.userData.mode}:null;
  }

  // The handle under the pointer is drawn lit.
  setHover(handle) {
    if(this.dragging||!this.object||!this.enabled)return;
    const axis=handle?.axis||null,mode=handle?.mode||this.mode;
    if(axis===this.axis&&mode===this.mode)return;
    this.axis=axis;this.mode=mode;this.update();this.dispatchEvent({type:"change"});
  }

  // A press on handle `axis` in `mode`; whether a drag began.
  begin(e) {
    if(!this.object||!this.enabled||!this.axis)return false;
    this.object.getWorldPosition(this.origin);
    this.object.getWorldQuaternion(this.startQ);
    if(this.space==="local")this.basis.copy(this.startQ);else this.basis.identity();
    this.worldAxes=[...this.axis].map(letter=>axes[letter].clone().applyQuaternion(this.basis));
    this.worldAxis=this.worldAxes[0];
    this.rawAngle=0;this.rotationAngle=0;this.snapped[0]=this.snapped[1]=0;
    if(this.mode==="rotate") {
      // The arc's plane, spanned so that arcX × arcY runs along the axis.
      this.arcX.crossVectors(this.worldAxis,Math.abs(this.worldAxis.z)<.9?axes.Z:axes.X).normalize();
      this.arcY.crossVectors(this.worldAxis,this.arcX);
      this.arcX.multiplyScalar(this.radius);this.arcY.multiplyScalar(this.radius);
      const at=this.arcPoint(e);if(!at)return false;
      this.theta=Math.atan2(at[1],at[0]);
      this.startHit=this.origin.clone().addScaledVector(this.arcX,at[0]).addScaledVector(this.arcY,at[1]);
    } else {
      const ray=this.core.pointer.aim(e.clientX,e.clientY).ray;
      if(this.worldAxes.length===2)this.plane.setFromNormalAndCoplanarPoint(cross.crossVectors(this.worldAxes[0],this.worldAxes[1]),this.origin);
      else if(!facingPlane(ray,this.worldAxis,this.origin,this.plane,1e-4))return false;
      this.startHit=ray.intersectPlane(this.plane,new THREE.Vector3());if(!this.startHit)return false;
    }
    this.dragging=true;
    this.dispatchEvent({type:"mouseDown"});
    if(!this.core.gizmos.drag){this.dragging=false;return false;}
    return true;
  }

  // Where the pointer is in the rotation arc's plane, in arc radii; read on
  // screen, so an arc seen nearly edge-on cannot turn a pixel into a large angle.
  arcPoint(e) {
    return this.core.pointer.planePoint(this.origin,this.arcX,this.arcY,e.clientX,e.clientY);
  }

  drag(e) {
    if(!this.dragging)return;
    if(this.mode==="translate") {
      if(!this.core.pointer.aim(e.clientX,e.clientY).ray.intersectPlane(this.plane,hit))return;
      hit.sub(this.startHit);moved.copy(this.origin);
      this.worldAxes.forEach((axis,i)=>{
        this.snapped[i]=stickySnap(hit.dot(axis),this.translationSnap,this.snapped[i]);
        moved.addScaledVector(axis,this.snapped[i]);
      });
      this.object.position.copy(this.object.parent.worldToLocal(moved));
    } else {
      const at=this.arcPoint(e);if(!at)return;
      const theta=Math.atan2(at[1],at[0]);
      let d=theta-this.theta;
      if(d>Math.PI)d-=TAU;else if(d<-Math.PI)d+=TAU;
      this.theta=theta;this.rawAngle+=d;
      this.rotationAngle=stickySnap(this.rawAngle,this.rotationSnap,this.rotationAngle);
      turn.setFromAxisAngle(this.worldAxis,this.rotationAngle).multiply(this.startQ);
      this.object.quaternion.copy(this.object.parent.getWorldQuaternion(parent).invert().multiply(turn));
    }
    this.object.updateWorldMatrix(true,false);this.dispatchEvent({type:"objectChange"});this.dispatchEvent({type:"change"});
  }

  release(e) {
    if(!this.dragging)return;
    this.drag(e);this.pointerUp();
  }

  pointerUp() {
    if(!this.dragging)return;
    this.dragging=false;
    this.dispatchEvent({type:"mouseUp"});
    this.update();
  }

  dispose() {
    const materials=new Set();this.helper.traverse(o=>{o.geometry?.dispose();if(o.material)materials.add(o.material);});
    for(const material of materials)material.dispose();for(const text of this.letters)text.element.remove();
  }
}
