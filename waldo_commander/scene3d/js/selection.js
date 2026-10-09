import * as THREE from "three";
import { setStyle } from "./dom.js";
import { GAP } from "./overlays.js";
import { visibleHit } from "./section.js";

const normals = new THREE.Matrix3();
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// The least of the inspector worth showing.
const SHORTEST = 150;

// What is selected, outlined, and isolated: a tap on an object selects it
// (shift adds to the selection), a tap on nothing clears it. The inspector
// Python builds for the selection sits beside where it was asked for.
export class Selection {
  constructor(core) {
    this.core=core;
    this.entities=new Map(); this.selected=[]; this.isolated=null; this.renames=[]; this.anchor=null;
    this.bounds=new THREE.Box3();
    this.outline=new THREE.Box3Helper(this.bounds);
    this.outline.name="selection:outline";
    this.outline.material.depthTest=false;
    this.outline.renderOrder=20;
    this.outline.visible=false;
    core.scene.add(this.outline);
  }

  load(data) {
    const before=this.isolationKey();
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
    if(before!==this.isolationKey())this.core.navigation?.notify();
  }

  isolationStatus() {return {isolated:!!this.isolated,canIsolate:this.selected.length>0};}

  isolationKey() {return `${!!this.isolated}:${this.selected.length>0}`;}

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
    if(hits.length)this.anchor={x,y};
    if(hits.length || !hit)this.core.emit("select",{revision:this.revision,hits,extend});
  }

  outlineSelected() {
    this.bounds.makeEmpty();
    for(const entity of this.entities.values()) {
      if(!this.selected.includes(entity.id))continue;
      const obj=this.core.nodes.get(entity.node)?.obj;
      if(obj?.visible)this.bounds.expandByObject(obj);
    }
    this.outline.visible=!this.bounds.isEmpty();
  }

  recolor(colors) {
    if(colors?.action)this.outline.material.color.set(colors.action);
  }

  // The inspector sits beside where it was asked for, as tall as it can be
  // in the free stretch there, clear of the panels and the object's handles.
  layout() {
    const overlays=this.core.overlays,card=overlays.card("inspector");
    if(!card)return;
    const view=overlays.view;
    const [ax,ay]=this.anchor?[this.anchor.x-view.left,this.anchor.y-view.top]:[view.width/2,view.height/2];
    const w=card.w,height=card.scroll+2;
    const bounds={left:this.core.layoutInset.left+GAP,top:GAP,right:view.width-GAP,bottom:view.height-GAP};
    const obstacles=overlays.obstacles(["handles","resizeHandles"]);
    const columns=[ax+18,ax-w-18,bounds.left,bounds.right-w];
    for(const name of ["handles","resizeHandles"]) {
      const handles=overlays.marks.get(name)?.[0];
      if(handles)columns.push(handles.right+GAP,handles.left-w-GAP);
    }
    const candidates=[];
    for(const column of columns) {
      const x=clamp(column,bounds.left,bounds.right-w);
      for(const [top,bottom] of overlays.spans(x,x+w,obstacles,bounds)) {
        const h=Math.min(height,bottom-top);
        if(h>=SHORTEST)candidates.push([x,clamp(ay-24,top,bottom-h),h]);
      }
    }
    const seat=candidates.length?overlays.seat(candidates,w,height,{bounds,obstacles:[],cost:(x,y,i)=>{
      const h=candidates[i][2];
      const dx=Math.max(x-ax,0,ax-x-w),dy=Math.max(y-ay,0,ay-y-h);
      const covers=ax>x-10 && ax<x+w+10 && ay>y-10 && ay<y+h+10;
      return Math.hypot(dx,dy)+(height-h)*.5+(covers?1000:0);
    }}):{x:bounds.left,y:GAP,h:Math.max(SHORTEST,view.height-64)};
    overlays.place(card,seat.x,seat.y);
    setStyle(card.node,"maxHeight",`${seat.h}px`);
    setStyle(card.node,"visibility","visible");
  }

  dispose() {
    this.isolated=null;this.core.nodes.setIsolated(null);
    this.outline.removeFromParent();this.outline.geometry.dispose();this.outline.material.dispose();
  }
}
