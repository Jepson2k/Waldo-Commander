import * as THREE from "three";
import { visibleHit } from "./pointer.js";

const normals = new THREE.Matrix3();

// What is selected, outlined, and isolated: a tap on an object selects it
// (shift adds to the selection), a tap on nothing clears it.
export class Selection {
  constructor(core) {
    this.core=core;
    this.entities=new Map(); this.selected=[]; this.isolated=null; this.renames=[];
    // One box per selected object, so a selection of several reads as those
    // objects rather than one box around the lot.
    this.outlines=[];
    this.outlineMaterial=new THREE.LineBasicMaterial({depthTest:false,toneMapped:false});
  }

  load(data) {
    const before=!!this.isolated;
    if(!data)this.isolated=null;
    this.revision=data?.revision;
    this.entities=new Map((data?.items||[]).map(e=>[e.node,e]));
    const ids=new Set([...this.entities.values()].map(e=>e.id));
    this.selected=(data?.selected||[]).filter(id=>ids.has(id));
    this.renames=data?.renames||[];
    if(this.isolated)for(const id of this.selected)this.isolated.add(id);
    this.retainRenames();
    this.core.nodes.setIsolated(this.isolated);
    this.outlineSelected();
    if(before!==!!this.isolated)this.core.navigation?.notify();
  }

  isolationStatus() {return {isolated:!!this.isolated};}

  retainRenames() {
    if(this.isolated)for(const [from,to] of this.renames)if(this.isolated.has(from))this.isolated.add(to);
  }

  isolate(on) {
    if(on && !this.selected.length)return;
    this.isolated=on?new Set(this.selected):null;
    this.core.nodes.setIsolated(this.isolated);
    this.core.pointer.forgetHover();this.core.navigation.notify();this.core.requestRender();
  }

  // Objects came or went: isolated ones gone with no rename to follow are
  // dropped, and with none of them left, the view shows everything again.
  prune() {
    if(!this.isolated)return;
    const names=new Set();
    for(const rec of this.core.nodes.records.values())if(rec.object && !rec.deleted)names.add(rec.name);
    this.retainRenames();
    if(!this.renames.length)this.isolated=new Set([...this.isolated].filter(name=>names.has(name)));
    if(![...this.isolated].some(name=>names.has(name))) {
      this.isolated=null;this.core.navigation?.notify();
    }
    this.core.nodes.setIsolated(this.isolated);
  }

  hits(x,y) {
    const core=this.core;
    const objects=[...this.entities.keys()].map(id=>core.nodes.get(id)?.obj).filter(o=>o && o.visible);
    const found=[],seen=new Set();
    const candidates=[...new Set([...objects,...core.pointer.targets()])];
    for(const hit of core.pointer.aim(x,y).intersectObjects(candidates,true)) {
      if(!visibleHit(hit))continue;
      let node=hit.object;
      while(node && !this.entities.has(node.userData.wcId))node=node.parent;
      const entity=node && this.entities.get(node.userData.wcId);
      if(!entity) { if(!found.length)return []; continue; }
      if(seen.has(entity.id))continue;
      seen.add(entity.id);
      const normal=hit.face ? hit.face.normal.clone().applyNormalMatrix(normals.getNormalMatrix(hit.object.matrixWorld)).toArray() : null;
      found.push({id:entity.id,point:hit.point.toArray(),normal});
    }
    return found;
  }

  // A tap at (x, y) that the pointer found `hit` under.
  tap(x,y,extend,hit) {
    const hits=this.hits(x,y);
    if(hits.length || !hit)this.core.emit("select",{revision:this.revision,hits,extend});
  }

  outlineSelected() {
    let shown=0;
    for(const entity of this.entities.values()) {
      if(!this.selected.includes(entity.id))continue;
      const obj=this.core.nodes.get(entity.node)?.obj;
      if(!obj?.visible)continue;
      const outline=this.outlines[shown++]??this.addOutline();
      outline.box.setFromObject(obj);
      outline.visible=!outline.box.isEmpty();
    }
    for(const outline of this.outlines.slice(shown))outline.visible=false;
  }

  addOutline() {
    const outline=new THREE.Box3Helper(new THREE.Box3());
    outline.material.dispose();outline.material=this.outlineMaterial;
    outline.name="selection:outline";outline.renderOrder=20;
    this.core.scene.add(outline);this.outlines.push(outline);
    return outline;
  }

  recolor(colors) {
    if(colors?.action)this.outlineMaterial.color.set(colors.action);
  }

  dispose() {
    this.isolated=null;this.core.nodes.setIsolated(null);
    for(const outline of this.outlines){outline.removeFromParent();outline.geometry.dispose();}
    this.outlines=[];this.outlineMaterial.dispose();
  }
}
