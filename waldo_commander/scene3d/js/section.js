import * as THREE from "three";

const Z = new THREE.Vector3(0, 0, 1);

export class SectionView {
  constructor(core) {
    this.core=core; this.active=false; this.axis="view"; this.offset=0; this.flipped=false;
    this.center=new THREE.Vector3(); this.normal=new THREE.Vector3(0,0,1);
    this.plane=new THREE.Plane(); this.extent=1; this.message="";
    const geometry=new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-1,-1,0),new THREE.Vector3(1,-1,0),new THREE.Vector3(1,1,0),new THREE.Vector3(-1,1,0),new THREE.Vector3(-1,-1,0),
    ]);
    this.outline=new THREE.Line(geometry,new THREE.LineBasicMaterial({transparent:true,opacity:0.45,depthWrite:false}));
    this.outline.visible=false; core.scene.add(this.outline);
  }

  status() { return {section:this.active,sectionAxis:this.axis,sectionOffset:this.offset*1000,sectionRange:Math.round(this.extent*1000),sectionMessage:this.message}; }

  // How many materials the section cuts now.
  get materials() {
    if (!this.active) return 0;
    let count = 0;
    for (const rec of this.core.nodes.cutRecords()) count += [rec.drawable.material].flat().length;
    return count;
  }

  set(action,value) {
    if(action==="section") {
      if(!value){this.activate(false);this.message="";this.core.navigation.notify();return;}
      const bounds=new THREE.Box3();
      for(const rec of this.core.nodes.cutRecords())if(shown(rec.obj))bounds.expandByObject(rec.obj);
      if(bounds.isEmpty()){this.message="Add or show an object to section.";this.core.navigation.notify();return;}
      bounds.getCenter(this.center);
      this.extent=Math.ceil(Math.max(0.05,bounds.getSize(new THREE.Vector3()).length())*1000)/1000;
      this.offset=0;this.flipped=false;this.message="";
      this.setNormal(this.axis);this.activate(true);
    } else if(!this.active)return;
    else if(action==="section_axis") {
      if(!["view","x","y","z"].includes(value))return;
      this.axis=value;this.offset=0;this.setNormal(value);
    } else if(action==="section_offset") {
      if(typeof value!=="number" || !Number.isFinite(value))return;
      this.offset=Math.max(-this.extent,Math.min(this.extent,value/1000));
    } else if(action==="section_flip")this.flipped=!this.flipped;
    else return;
    this.updatePlane();
    if(action!=="section_offset" || Math.abs(value-this.offset*1000)>1e-6)this.core.navigation.notify();
  }

  activate(on) {
    this.active=on;this.outline.visible=on;
    this.core.nodes.setSection(on?this.plane:null);
  }

  setNormal(axis) {
    if(axis==="view")this.core.camera.getWorldDirection(this.normal);
    else this.normal.set(axis==="x"?1:0,axis==="y"?1:0,axis==="z"?1:0);
  }

  updatePlane() {
    const point=this.outline.position.copy(this.center).addScaledVector(this.normal,this.offset);
    this.plane.normal.copy(this.normal).multiplyScalar(this.flipped?-1:1);
    this.plane.constant=-this.plane.normal.dot(point);
    this.outline.quaternion.setFromUnitVectors(Z,this.normal);
    this.outline.scale.setScalar(this.extent/2);
    if(this.core.ix.colors?.action)this.outline.material.color.set(this.core.ix.colors.action);
    this.core.casters=NaN;
    this.core.requestRender();
  }

  dispose() {
    this.activate(false);
    this.outline.removeFromParent();this.outline.geometry.dispose();this.outline.material.dispose();
  }
}

export function shown(object) {
  for (let o = object; o; o = o.parent) if (!o.visible) return false;
  return true;
}

export function visibleHit(hit) {
  if(!shown(hit.object))return false;
  const list=Array.isArray(hit.object.material)?[hit.object.material[hit.face?.materialIndex||0]]:[hit.object.material];
  return list.every(mat=>!mat || mat.visible!==false && !(mat.clippingPlanes||[]).some(plane=>plane.distanceToPoint(hit.point)<-1e-7));
}
