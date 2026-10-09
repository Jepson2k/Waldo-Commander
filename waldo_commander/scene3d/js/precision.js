import * as THREE from "three";
import { label } from "./rings.js";
import { signed } from "./handles.js";
import { element } from "./dom.js";
import { GAP, overlap } from "./overlays.js";

const RAD=Math.PI/180;
const MAX_TRANSLATION=.010,MAX_ROTATION=5*RAD;
const axes={X:new THREE.Vector3(1,0,0),Y:new THREE.Vector3(0,1,0),Z:new THREE.Vector3(0,0,1)};
const NICE=[1,2,5,10];
const ORIGIN=new THREE.Vector3();
const a=new THREE.Vector3(),b=new THREE.Vector3(),delta=new THREE.Vector3(),across=new THREE.Vector3(),scratch=new THREE.Vector3(),turn=new THREE.Quaternion();
const anchor=[0,0,0],at=[0,0,0];
// Where the card may sit around its handle, and the offsets a scale label
// may take to stay readable.
const BESIDE=115,ABOVE=110;
const NUDGES=[[0,0],[0,-26],[0,26],[-48,0],[48,0],[0,-52],[0,52],[-48,-26],[48,26]];
const OCCLUSION=[[.5,.5],[.2,.2],[.8,.8]];

// A label's size, read once its text is laid out.
function sizeOf(item) {
  if(!item.wcSize?.[0])item.wcSize=[item.element.offsetWidth,item.element.offsetHeight];
  return item.wcSize;
}

// The 1, 2 or 5 times a power of ten nearest `value`.
export const niceStep=(value)=>{
  const power=10**Math.floor(Math.log10(Math.max(value,1e-9)));
  let best=power;
  for(const k of NICE)if(Math.abs(Math.log(k*power/value))<Math.abs(Math.log(best/value)))best=k*power;
  return best;
};

export class Precision {
  constructor(gizmos) {
    this.gizmos=gizmos; this.core=gizmos.core;
    this.instrument=null; this.editing=false; this.seat=null; this.solved=null;
    this.card=element("div","wc-gizmo-readout",this.core.root); this.card.hidden=true;
    this.card.setAttribute("aria-label","Precision movement");
    this.core.overlays.track("precision",this.card);
    this.title=element("div","wc-gizmo-title",this.card);
    this.value=element("button","wc-gizmo-value",this.card);
    this.value.onclick=()=>this.enterNumber();
    this.entry=element("form","wc-gizmo-entry",this.card); this.entry.hidden=true;
    this.axis=element("select","",this.entry); this.axis.setAttribute("aria-label","Movement axis");
    for (const a of ["X","Y","Z","Rx","Ry","Rz"]) this.axis.add(new Option(a,a));
    this.axis.onchange=()=>{this.unit.textContent=this.axis.value.length===1?"mm":"°";};
    this.input=element("input","",this.entry); this.input.type="number"; this.input.step="any";
    this.input.required=true; this.input.setAttribute("aria-label","Exact displacement");
    this.unit=element("span","",this.entry);
    this.apply=element("button","",this.entry,"Apply"); this.apply.type="submit";
    this.entry.onsubmit=e=>{e.preventDefault();this.commitNumber();};
    this.hint=element("div","wc-gizmo-hint",this.card);
    this.card.onpointerenter=()=>{if(this.control()===this.gizmos.tcp)this.core.pointer.hover.gizmo(true,performance.now());};
    this.card.onpointerleave=()=>{if(!this.editing && this.control()===this.gizmos.tcp){this.core.pointer.hover.gizmo(false,performance.now());this.core.pointer.settle();}};
    this.card.onkeydown=e=>{
      if (e.key==="Escape" && this.editing) {this.leaveNumber();e.preventDefault();}
      e.stopPropagation();
    };
  }

  control() {
    return this.gizmos.drag?.tc || (this.gizmos.shape?.tc.object ? this.gizmos.shape.tc : this.gizmos.gizmoShown?this.gizmos.tcp:null);
  }

  increment(tc,mode=tc.mode) {
    return this.incrementAt(tc.object.getWorldPosition(scratch),mode);
  }

  // A step spans a few pixels: of the view for a translation, of the gizmo's
  // arc for a turn. The gizmo is never drawn under 65 px, and no step is
  // coarser than MAX_TRANSLATION or MAX_ROTATION however far out the view is.
  incrementAt(position,mode) {
    const pixel=this.core.metresPerPixel(position);
    if(mode!=="rotate")return Math.min(MAX_TRANSLATION,niceStep(pixel*8));
    return Math.min(MAX_ROTATION,niceStep(4/Math.max(65,.085/pixel)/RAD)*RAD);
  }

  refresh() {
    if(this.gizmos.drag) return;
    for (const tc of [this.gizmos.tcp,this.gizmos.shape?.tc]) {
      if(!tc?.object) continue;
      const translation=this.increment(tc,"translate"),rotation=this.increment(tc,"rotate");
      if(tc.translationSnap!==translation)tc.setTranslationSnap(translation);
      if(tc.rotationSnap!==rotation)tc.setRotationSnap(rotation);
    }
  }

  prepare(recompile=false) {
    const color=this.core.ix.colors?.action;
    if(!color)return;
    if(this.resources){
      for(const mesh of [this.resources.lines,this.resources.fill,this.resources.originDot])mesh.material.color.set(color);
      if(recompile)this.core.renderer.compile(this.resources.group,this.core.camera,this.core.scene);
      return;
    }
    const group=new THREE.Group();
    const geometry=new THREE.BufferGeometry(); geometry.setAttribute("position",new THREE.BufferAttribute(new Float32Array(4096*3),3));
    geometry.setDrawRange(0,0);
    const material=new THREE.LineBasicMaterial({color,depthTest:false,depthWrite:false,transparent:true});
    const lines=new THREE.LineSegments(geometry,material); lines.renderOrder=1004; lines.frustumCulled=false; group.add(lines);
    const fillGeometry=new THREE.BufferGeometry(); fillGeometry.setAttribute("position",new THREE.BufferAttribute(new Float32Array(128*9),3));fillGeometry.setDrawRange(0,0);
    const fill=new THREE.Mesh(fillGeometry,new THREE.MeshBasicMaterial({color,opacity:.15,transparent:true,side:THREE.DoubleSide,depthTest:false,depthWrite:false,forceSinglePass:true}));
    fill.renderOrder=1002;fill.frustumCulled=false;group.add(fill);
    const originDot=new THREE.Mesh(new THREE.SphereGeometry(1,12,8),new THREE.MeshBasicMaterial({color,depthTest:false,depthWrite:false}));
    originDot.renderOrder=1005;group.add(originDot);
    this.resources={group,lines,fill,originDot};
    // Shader compilation must finish before a held drag starts its liveness clock.
    this.core.renderer.compile(group,this.core.camera,this.core.scene);
  }

  // The instrument for a drag through `tc`: its scale is drawn here once,
  // and each move redraws only what moved.
  begin(tc,exact=false) {
    this.end(); this.leaveNumber();this.prepare();
    if(tc.mode==="rotate" && !exact)tc.rotationAngle=0;
    const origin=tc.object.getWorldPosition(new THREE.Vector3());
    const quaternion=tc.space==="local"?tc.object.getWorldQuaternion(new THREE.Quaternion()):new THREE.Quaternion();
    const pixel=this.core.metresPerPixel(origin);
    const {group,lines,fill,originDot}=this.resources;
    group.position.copy(origin);group.quaternion.copy(quaternion);group.name=`${tc.wcKind}:ticks`;
    originDot.scale.setScalar(pixel*2.5);
    lines.geometry.setDrawRange(0,0);fill.geometry.setDrawRange(0,0);
    fill.visible=tc.mode==="rotate";
    this.core.scene.add(group);
    const snap=exact?null:tc.mode==="translate"?tc.translationSnap:tc.rotationSnap;
    const m={tc,group,lines,fill,labels:[],pixel,origin,quaternion,inverse:quaternion.clone().invert(),snap,exact,
      startInverse:tc.object.getWorldQuaternion(new THREE.Quaternion()).invert(),statics:0,fixedLabels:0,steps:0};
    this.instrument=m;
    if(tc.mode==="translate") {
      m.spacing=snap || niceStep(pixel*10);
      m.letters=[...new Set([...tc.axis||"XYZ"].filter(a=>axes[a]))];
    } else {
      const axis=m.axis=axes[tc.axis].clone();
      const ref=!exact&&tc.startHit?tc.startHit.clone().sub(origin).applyQuaternion(m.inverse):Math.abs(axis.z)<.8?new THREE.Vector3(0,0,1):new THREE.Vector3(1,0,0);
      m.u=ref.addScaledVector(axis,-ref.dot(axis)).normalize();m.v=axis.clone().cross(m.u);
      m.radius=tc.radius*.75 || pixel*85;
      m.step=snap || 5*RAD;
      this.drawStatic(m);
    }
    this.update();
  }

  point(m,angle,r=m.radius,out=a) {
    return out.copy(m.u).multiplyScalar(Math.cos(angle)*r).addScaledVector(m.v,Math.sin(angle)*r);
  }

  stroke(m,count,p,q) {
    const positions=m.lines.geometry.attributes.position;
    positions.setXYZ(count,p.x,p.y,p.z);positions.setXYZ(count+1,q.x,q.y,q.z);
    return count+2;
  }

  scaleLabel(m,index,text,position) {
    let item=m.labels[index];
    if(!item){item=label(`${m.tc.wcKind}:scale:${index}`);item.element.className="wc-gizmo-scale";item.element.style.cssText="";m.group.add(item);m.labels.push(item);item.wcSize=null;}
    if(item.element.textContent!==text){item.element.textContent=text;item.wcSize=null;}
    item.position.copy(position);item.visible=true;
    return index+1;
  }

  // What stays put for the whole drag: the axes, and the scale along the
  // dragged axes or around the turned one.
  drawStatic(m) {
    const {pixel}=m;let count=0,labels=0;
    for (const axis of Object.values(axes))count=this.stroke(m,count,a.copy(axis).multiplyScalar(-pixel*5),b.copy(axis).multiplyScalar(pixel*5));
    if(m.tc.mode==="translate") {
      const eye=scratch.copy(this.core.camera.position).sub(m.origin).applyQuaternion(m.inverse).normalize();
      labels=this.scaleLabel(m,labels,"0",a.set(0,0,pixel*12));
      for (const letter of m.letters) {
        const axis=axes[letter],perp=across.copy(axis).cross(eye).normalize(),reach=m.steps*m.spacing;
        count=this.stroke(m,count,a.copy(axis).multiplyScalar(-reach),b.copy(axis).multiplyScalar(reach));
        for(let k=-m.steps;k<=m.steps;k++){
          const h=pixel*(k%5===0?5:2.5);
          a.copy(axis).multiplyScalar(k*m.spacing);b.copy(a).addScaledVector(perp,h);a.addScaledVector(perp,-h);
          count=this.stroke(m,count,a,b);
        }
        for(const k of [-5,5])labels=this.scaleLabel(m,labels,`${signed(k*m.spacing*1000,Math.abs(m.spacing)<.001?1:0)}`,a.copy(axis).multiplyScalar(k*m.spacing).addScaledVector(perp,pixel*12));
      }
    } else {
      for(let k=0;k<128;k++)count=this.stroke(m,count,this.point(m,k*Math.PI/64,m.radius,a),this.point(m,(k+1)*Math.PI/64,m.radius,b));
      for(let angle=0;angle<Math.PI*2-1e-6;angle+=Math.max(m.step,10*RAD))count=this.stroke(m,count,this.point(m,angle,m.radius-pixel*5,a),this.point(m,angle,m.radius,b));
      for(let k=0;k<9;k++)count=this.stroke(m,count,this.point(m,0,m.radius*(.10+k*.10),a),this.point(m,0,m.radius*(.16+k*.10),b));
      labels=this.scaleLabel(m,labels,"0°",this.point(m,0,m.radius+pixel*12,a));
    }
    m.statics=count;m.fixedLabels=labels;
  }

  update() {
    const m=this.instrument;if(!m)return;
    const {tc,pixel}=m;
    let count=m.statics,labels=m.fixedLabels,values="";
    if(tc.mode==="translate") {
      tc.object.getWorldPosition(delta).sub(m.origin).applyQuaternion(m.inverse);
      const steps=Math.max(5,Math.min(40,Math.ceil(Math.max(pixel*90,delta.length())/m.spacing)));
      if(steps!==m.steps){m.steps=steps;this.drawStatic(m);count=m.statics;labels=m.fixedLabels;}
      count=this.stroke(m,count,ORIGIN,delta);
      values=m.letters.map(a=>`${a} ${signed(delta[a.toLowerCase()]*1000)}`).join(" · ")+" mm";
    } else {
      const q=tc.object.getWorldQuaternion(turn).premultiply(m.startInverse);
      const value=m.exact?2*Math.atan2(q.x*m.axis.x+q.y*m.axis.y+q.z*m.axis.z,q.w):tc.dragging?tc.rotationAngle:0;
      count=this.stroke(m,count,ORIGIN,this.point(m,value));
      const attr=m.fill.geometry.attributes.position;
      for(let k=0;k<128;k++) {
        const t0=value*k/128,t1=value*(k+1)/128,r=m.radius;
        attr.setXYZ(k*3,0,0,0);
        attr.setXYZ(k*3+1,(m.u.x*Math.cos(t0)+m.v.x*Math.sin(t0))*r,(m.u.y*Math.cos(t0)+m.v.y*Math.sin(t0))*r,(m.u.z*Math.cos(t0)+m.v.z*Math.sin(t0))*r);
        attr.setXYZ(k*3+2,(m.u.x*Math.cos(t1)+m.v.x*Math.sin(t1))*r,(m.u.y*Math.cos(t1)+m.v.y*Math.sin(t1))*r,(m.u.z*Math.cos(t1)+m.v.z*Math.sin(t1))*r);
      }
      attr.needsUpdate=true;m.fill.geometry.setDrawRange(0,384);
      values=`${tc.axis} ${signed(value/RAD)}°`;
      if(Math.abs(value)>m.step/2)labels=this.scaleLabel(m,labels,`${tc.axis} Δ ${signed(value/RAD)}°`,this.point(m,value,m.radius+pixel*18));
    }
    for(let i=labels;i<m.labels.length;i++)m.labels[i].visible=false;
    m.lines.geometry.attributes.position.needsUpdate=true;m.lines.geometry.setDrawRange(0,count);
    this.value.textContent=values; this.core.requestRender();
  }

  enterNumber() {
    const tc=this.control();if(!tc||this.gizmos.drag)return;
    this.editing=true;this.entry.hidden=false;this.value.hidden=true;
    this.draft={tc,object:tc.object,session:this.core.ix.edit?.session};
    const axis=axes[tc.axis]?tc.axis:"X";
    this.axis.value=tc.mode==="rotate"?`R${axis.toLowerCase()}`:axis;this.input.value="";
    this.unit.textContent=tc.mode==="translate"?"mm":"°";
    if(tc===this.gizmos.tcp)this.core.pointer.hover.pinned=true;
    this.input.focus();this.core.requestRender();
  }

  leaveNumber() {
    this.editing=false;this.draft=null;this.entry.hidden=true;this.value.hidden=false;
  }

  draftMatches(tc) {
    const d=this.draft;
    return d && d.tc===tc && d.object===tc?.object && d.session===this.core.ix.edit?.session;
  }

  commitNumber() {
    const tc=this.control(),value=this.input.valueAsNumber;
    if(!this.draftMatches(tc)){this.leaveNumber();return;}
    if(!Number.isFinite(value)||this.gizmos.drag)return;
    const coordinate=this.axis.value,axis=coordinate.slice(-1).toUpperCase();this.leaveNumber();
    tc.mode=coordinate.length===1?"translate":"rotate";
    tc.axis=axis;this.gizmos.down(tc.wcKind,tc,true);
    if(!this.gizmos.drag)return;
    if(tc.mode==="translate") {
      const move=axes[axis].clone().multiplyScalar(value/1000);
      if(tc.space==="local")move.applyQuaternion(tc.object.getWorldQuaternion(new THREE.Quaternion()));
      const position=tc.object.getWorldPosition(new THREE.Vector3()).add(move);
      tc.object.position.copy(tc.object.parent.worldToLocal(position));
    } else {
      const turn=new THREE.Quaternion().setFromAxisAngle(axes[axis],value*RAD);
      if(tc.space==="world")tc.object.quaternion.premultiply(turn);
      else tc.object.quaternion.multiply(turn);
    }
    this.gizmos.changed(tc.wcKind,tc);this.gizmos.up(tc.wcKind,tc);this.core.pointer.settle();this.core.requestRender();
  }

  // The scale's labels keep clear of the cards and the axis letters, nearest
  // where they belong first. Everything is measured before any moves.
  layoutLabels() {
    const m=this.instrument;
    if(!m)return;
    const overlays=this.core.overlays;
    const boxes=overlays.boxes(["edit","precision","drawing","resize"]);
    for(const letter of m.tc.letters) {
      if(!letter.visible)continue;
      const [x,y]=overlays.project(letter.getWorldPosition(scratch),at),[w,h]=sizeOf(letter);
      boxes.push({left:x-w/2,top:y-h/2,right:x+w/2,bottom:y+h/2});
    }
    const shown=m.labels.filter(item=>item.visible);
    for(const item of shown)sizeOf(item);
    const {width,height}=overlays.view;
    for(const item of shown) {
      const [x,y]=overlays.project(item.getWorldPosition(scratch),at),[w,h]=item.wcSize;
      let seat=NUDGES[0],cost=Infinity,box=null;
      for(const [dx,dy] of NUDGES) {
        const candidate={left:x-w/2+dx,right:x+w/2+dx,top:y-h/2+dy,bottom:y+h/2+dy};
        const clipped=candidate.left<4||candidate.top<4||candidate.right>width-4||candidate.bottom>height-4;
        const score=Math.hypot(dx,dy)+overlap(candidate,boxes,4)*100+(clipped?1e6:0);
        if(score<cost){cost=score;seat=[dx,dy];box=candidate;}
      }
      const translate=`${seat[0]}px ${seat[1]}px`;
      if(item.element.style.translate!==translate)item.element.style.translate=translate;
      boxes.push(box);
    }
  }

  // The card sits beside the handle, off the panels and the inspector and,
  // where it can, off the robot; it stays where it was while that still holds.
  layout() {
    this.refresh();
    const tc=this.control(); this.card.hidden=!tc;
    if(!tc){this.leaveNumber();this.seat=null;return;}
    if(this.editing && !this.draftMatches(tc))this.leaveNumber();
    const active=!!this.instrument;
    if(tc===this.gizmos.shape?.tc && !this.gizmos.shape.members && !active && !this.editing){this.card.hidden=true;return;}
    this.value.setAttribute("aria-disabled",String(active));
    if(this.core.ix.edit?.active && tc===this.gizmos.tcp){this.card.hidden=true;return;}
    const frame=tc.space==="world"?"World":"Tool";
    this.title.textContent=`${frame}${active?` ${tc.axis} · Requested Δ`:""}`;
    if(!active)this.value.textContent="Adjust by…";
    this.hint.textContent=this.editing?"Enter to apply · Esc to cancel":active?"Esc to cancel":"Drag an axis · click value to type";
    const overlays=this.core.overlays,card=overlays.sized("precision");
    if(!card)return;
    const [x,y]=overlays.project(this.instrument?.origin || tc.object.getWorldPosition(scratch),anchor);
    const {w,h}=card,{width,height}=overlays.view;
    const candidates=[];
    for(const left of [x+BESIDE,x-w-BESIDE,width-w-GAP,null])for(const top of [y-h/2,y+ABOVE,y-h-ABOVE,GAP])candidates.push([left??overlays.clearLeft(top,top+h),top]);
    const options={bounds:{left:GAP,top:GAP,right:width-GAP,bottom:height-GAP},obstacles:overlays.obstacles(["inspector"]),pad:8,
      cost:(cx,cy,i)=>Math.hypot(cx+w/2-x,cy+h/2-y)+i*3};
    let seat=this.seat?.tc===tc?{...overlays.seat([candidates[this.seat.index]],w,h,{...options,cost:null}),index:this.seat.index}:null;
    if(!seat || seat.covered) {
      const inputs=[Math.round(x),Math.round(y),w,h,width,height,overlays.layout,...(overlays.cards.get("inspector")?.box ? Object.values(overlays.cards.get("inspector").box) : [])];
      if(this.solved?.tc===tc && this.solved.inputs.length===inputs.length && this.solved.inputs.every((v,i)=>v===inputs[i]))seat=this.solved.seat;
      else {
        seat=this.choose(overlays.rank(candidates,w,h,options),w,h);
        this.solved={tc,inputs,seat};
      }
    }
    this.seat={tc,index:seat.index};
    overlays.place(card,seat.x,seat.y);
  }

  // The best seat that does not hide the robot; the robot is looked for
  // under each seat only until one is clear of it.
  choose(ranked,w,h) {
    const view=this.core.overlays.view;
    for(const seat of ranked) {
      if(seat.covered)break;
      if(!OCCLUSION.some(([u,v])=>this.core.pointer.hit(view.left+seat.x+u*w,view.top+seat.y+v*h)))return seat;
    }
    return ranked.find(seat=>!seat.covered) || ranked[0];
  }

  end() {
    const m=this.instrument;if(!m)return;this.instrument=null;
    m.group.removeFromParent();
    for(const l of m.labels){l.removeFromParent();l.element.remove();}
  }

  dispose() {
    this.end();this.card.remove();
    this.resources?.group.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});
    this.resources=null;
  }
}
