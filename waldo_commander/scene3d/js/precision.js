import * as THREE from "three";
import { label } from "./rings.js";
import { signed } from "./handles.js";
import { element, setAttr, setHidden, setText } from "./dom.js";
import { overlap } from "./overlays.js";

const RAD=Math.PI/180;
const MAX_TRANSLATION=.010,MAX_ROTATION=5*RAD;
const axes={X:new THREE.Vector3(1,0,0),Y:new THREE.Vector3(0,1,0),Z:new THREE.Vector3(0,0,1)};
const LETTERS="XYZ";
const NICE=[1,2,5,10];
const ORIGIN=new THREE.Vector3();
const a=new THREE.Vector3(),b=new THREE.Vector3(),delta=new THREE.Vector3(),across=new THREE.Vector3(),scratch=new THREE.Vector3(),turn=new THREE.Quaternion(),spin=new THREE.Quaternion();
const at=[0,0,0],from=[0,0,0];
// The offsets a scale label may take to stay readable.
const NUDGES=[[0,0],[0,-26],[0,26],[-48,0],[48,0],[0,-52],[0,52],[-48,-26],[48,26]];
// How far past its handle a value sits, in pixels.
const FIELD_PAD=10;

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

// A signed change to the precision of the snap `step`, or as typed without one.
export const amount=(v,step)=>signed(v,step?(step>=1?0:step>=.1?1:2):decimals(v));
const decimals=(v)=>(String(Number(Math.abs(v).toFixed(2))).split(".")[1]||"").length;
// A value as the field offers it for retyping.
const typed=(v)=>String(Number(v.toFixed(2)));

// A value riding on a handle: what a drag changes, and, clicked, a field to
// type the change into. Enter applies it, Esc puts it away, Tab moves it on
// to the next axis. For a finger, whose keypad may have neither Enter nor a
// minus, keys beside it flip the sign and apply it.
export class HandleField {
  constructor(core,name,{open,commit,cancel,cycle,hover=null}) {
    this.core=core;this.name=name;this.text=null;this.candidates=[[0,0],[0,0],[0,0]];
    const node=this.node=element("form","wc-gizmo-field",core.root);node.hidden=true;node.noValidate=true;
    this.value=element("button","wc-gizmo-field-value",node);this.value.type="button";
    this.input=element("input","",node);this.input.hidden=true;this.input.type="text";
    this.input.inputMode="decimal";this.input.autocomplete="off";this.input.spellcheck=false;
    this.unit=element("span","wc-gizmo-field-unit",node);
    this.keys=element("span","wc-gizmo-field-keys",node);this.keys.hidden=true;
    // A key's press leaves the focus, and so the keypad, with the field.
    this.keys.onpointerdown=(e)=>e.preventDefault();
    const negate=element("button","",this.keys,"±");negate.type="button";negate.setAttribute("aria-label","Flip the sign");
    const apply=element("button","",this.keys,"✓");apply.type="button";apply.setAttribute("aria-label","Apply");
    core.overlays.track(name,node);
    this.value.onclick=()=>open();
    const submit=()=>{
      const text=this.input.value.trim().replace(/\u2212/g,"-");
      if(!text){cancel();return;}
      const value=Number(text);
      if(Number.isFinite(value))commit(value);
      else this.invalid();
    };
    node.onsubmit=(e)=>{e.preventDefault();submit();};
    apply.onclick=submit;
    negate.onclick=()=>{
      const text=this.input.value.trim();
      this.input.value=/^[-\u2212]/.test(text)?text.slice(1):`-${text.replace(/^\+/,"")}`;
      this.input.focus();
    };
    node.onkeydown=(e)=>{
      if(this.typing&&e.key==="Escape"){cancel();e.preventDefault();}
      else if(this.typing&&e.key==="Tab"){cycle(e.shiftKey?-1:1);e.preventDefault();}
      e.stopPropagation();
    };
    if(hover){node.onpointerenter=()=>hover(true);node.onpointerleave=()=>hover(false);}
  }

  get typing() {
    return !this.input.hidden;
  }

  // Shows `text` in `unit` for the change `what` names.
  show(text,unit,what) {
    if(this.typing)this.close();
    if(text!==this.text) {
      this.text=text;setText(this.value,text);setText(this.unit,unit);
      setAttr(this.value,"aria-label",`${what} ${text} ${unit}, click to type`);
    }
    setHidden(this.node,false);
  }

  // A field for the change `what` names, holding `initial`.
  edit(initial,unit,what) {
    this.text=null;
    setHidden(this.node,false);setHidden(this.value,true);setHidden(this.input,false);setHidden(this.keys,false);
    this.input.removeAttribute("aria-invalid");this.input.value=initial;
    setText(this.unit,unit);setAttr(this.input,"aria-label",`${what} change in ${unit==="°"?"degrees":"millimetres"}`);
    this.input.focus();this.input.select();
  }

  invalid() {
    setAttr(this.input,"aria-invalid","true");this.input.select();
  }

  close() {
    setHidden(this.input,true);setHidden(this.keys,true);setHidden(this.value,false);this.text=null;
  }

  hide() {
    this.close();setHidden(this.node,true);
  }

  // Past the handle at `anchor` on screen, away from the handles' `center`,
  // inside the view and off `obstacles`.
  place(anchor,center,obstacles) {
    const overlays=this.core.overlays,card=overlays.sized(this.name);
    if(!card)return;
    const {w,h}=card;
    let ux=anchor[0]-center[0],uy=anchor[1]-center[1];
    const length=Math.hypot(ux,uy);
    if(length<1){ux=1;uy=0;}else{ux/=length;uy/=length;}
    this.beside(0,anchor,ux,uy,w,h);this.beside(1,anchor,-uy,ux,w,h);this.beside(2,anchor,uy,-ux,w,h);
    const seat=overlays.seat(this.candidates,w,h,{obstacles,pad:4});
    overlays.place(card,seat.x,seat.y);
  }

  // Candidate `i`: the field just clear of `anchor` in direction (dx, dy).
  beside(i,anchor,dx,dy,w,h) {
    const reach=Math.abs(dx)*w/2+Math.abs(dy)*h/2+FIELD_PAD,c=this.candidates[i];
    c[0]=anchor[0]+dx*reach-w/2;c[1]=anchor[1]+dy*reach-h/2;
  }
}

// What a handle's change is called and measured in.
const ROTATIONS={X:"Rx",Y:"Ry",Z:"Rz"};
const unitOf=(mode)=>mode==="rotate"?"°":"mm";
const nameOf=(mode,letter)=>mode==="rotate"?ROTATIONS[letter]:letter;
// What a value on a handle keeps clear of.
export const FIELD_AVOIDS=["teaching","gizmoFrame"];

// The gizmos' instrument and values: a drag draws its scale in the scene and
// its change on the handle it moves; a handle clicked, or the value clicked
// after a drag, takes a typed change through the same gesture a drag makes.
export class Precision {
  constructor(gizmos) {
    this.gizmos=gizmos; this.core=gizmos.core;
    this.instrument=null;
    this.entry=null; // a change being typed: {tc, object, session, mode, letter, base}
    this.settled=null; // the last drag's change, until anything else is pressed
    this.field=new HandleField(this.core,"gizmoValue",{open:()=>this.reopen(),commit:(v)=>this.commit(v),
      cancel:()=>this.cancel(),cycle:(step)=>this.cycle(step),hover:(on)=>this.hovered(on)});
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
    this.end(); this.dismiss(); this.prepare();
    if(tc.mode==="rotate" && !exact)tc.rotationAngle=0;
    const origin=tc.object.getWorldPosition(new THREE.Vector3());
    const quaternion=tc.dragging?tc.basis.clone():tc.frameQuaternion(new THREE.Quaternion());
    const pixel=this.core.metresPerPixel(origin);
    const {group,lines,fill,originDot}=this.resources;
    group.position.copy(origin);group.quaternion.copy(quaternion);group.name=`${tc.wcKind}:ticks`;
    originDot.scale.setScalar(pixel*2.5);
    lines.geometry.setDrawRange(0,0);fill.geometry.setDrawRange(0,0);
    fill.visible=tc.mode==="rotate";
    this.core.scene.add(group);
    const snap=exact?null:tc.mode==="translate"?tc.translationSnap:tc.rotationSnap;
    const m={tc,group,lines,fill,labels:[],pixel,origin,quaternion,inverse:quaternion.clone().invert(),snap,exact,
      startInverse:tc.object.getWorldQuaternion(new THREE.Quaternion()).invert(),statics:0,fixedLabels:0,steps:0,
      record:this.record(tc),handle:tc.axis,mode:tc.mode,byLetter:{},text:"",
      shownStep:snap&&(tc.mode==="translate"?snap*1000:snap/RAD)};
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
    let count=m.statics,labels=m.fixedLabels;
    if(tc.mode==="translate") {
      tc.object.getWorldPosition(delta).sub(m.origin).applyQuaternion(m.inverse);
      const steps=Math.max(5,Math.min(40,Math.ceil(Math.max(pixel*90,delta.length())/m.spacing)));
      if(steps!==m.steps){m.steps=steps;this.drawStatic(m);count=m.statics;labels=m.fixedLabels;}
      count=this.stroke(m,count,ORIGIN,delta);
      for(const letter of m.letters)m.byLetter[letter]=delta[letter.toLowerCase()]*1000;
    } else {
      const q=tc.object.getWorldQuaternion(turn).multiply(m.startInverse);
      q.premultiply(spin.copy(m.inverse)).multiply(m.quaternion);
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
      m.byLetter[m.handle]=value/RAD;
    }
    m.text=this.describe(m.handle,m.byLetter,m.shownStep);
    for(let i=labels;i<m.labels.length;i++)m.labels[i].visible=false;
    m.lines.geometry.attributes.position.needsUpdate=true;m.lines.geometry.setDrawRange(0,count);
    this.core.requestRender();
  }

  // The change along handle `axis`: one value, or a plane's two.
  describe(axis,byLetter,step) {
    if(axis.length===1)return amount(byLetter[axis],step);
    return `${axis[0]} ${amount(byLetter[axis[0]],step)} · ${axis[1]} ${amount(byLetter[axis[1]],step)}`;
  }

  sessionOf(tc) {
    return tc===this.gizmos.tcp?this.core.ix.edit?.session:this.gizmos.shape?.session;
  }

  record(tc) {
    return {tc,object:tc.object,session:this.sessionOf(tc)};
  }

  // Whether `r` is still on the handles it was made on, and they show.
  live(r) {
    return r.tc.object===r.object && r.tc.getHelper().visible && r.session===this.sessionOf(r.tc);
  }

  // A handle let go where it was pressed: a change to type along it.
  click(tc,axis,mode) {
    this.settled=null;
    this.open(tc,mode,axis[0],{});
  }

  // The value left by a drag, clicked: what the drag did, to retype.
  reopen() {
    const s=this.settled;
    if(s && this.live(s) && !this.gizmos.drag)this.open(s.tc,s.mode,s.axis[0],s.byLetter);
  }

  open(tc,mode,letter,base) {
    if(!tc.visibleAxes(mode).includes(letter))return;
    this.entry={...this.record(tc),mode,letter,base};
    if(tc===this.gizmos.tcp)this.core.pointer.hover.pinned=true;
    tc.setHover({axis:letter,mode});
    const value=base[letter];
    this.field.edit(value===undefined?"":typed(value),unitOf(mode),nameOf(mode,letter));
    this.core.requestRender();
  }

  // Tab: the same kind of change along the next axis.
  cycle(step) {
    const e=this.entry;if(!e)return;
    const letters=[...LETTERS].filter(letter=>e.tc.visibleAxes(e.mode).includes(letter));
    this.open(e.tc,e.mode,letters[(letters.indexOf(e.letter)+step+letters.length)%letters.length],e.base);
  }

  close() {
    const e=this.entry;if(!e)return;
    this.entry=null;this.field.close();
    e.tc.setHover(null);
  }

  cancel() {
    this.close();this.core.pointer.settle();
  }

  // Anything else pressed: the values on the handles go.
  dismiss() {
    this.close();this.settled=null;
  }

  // The typed value replaces what was there: the change along that axis
  // since the last drag began, or from here when nothing was.
  commit(value) {
    const e=this.entry;
    if(!e || !this.live(e) || this.gizmos.drag){this.cancel();return;}
    const {tc,mode,letter}=e,change=value-(e.base[letter]??0);
    const s=this.settled,axis=s?.tc===tc && s.mode===mode && s.axis.includes(letter)?s.axis:letter;
    const byLetter={...e.base,[letter]:value};
    this.close();
    if(change!==0 && !this.apply(tc,mode,letter,change))return;
    this.settled={...this.record(tc),axis,mode,byLetter,text:this.describe(axis,byLetter,null)};
    this.core.pointer.settle();this.core.requestRender();
  }

  // `change` (mm or degrees) along handle `letter`, as one gesture; whether
  // the gesture began.
  apply(tc,mode,letter,change) {
    tc.mode=mode;tc.axis=letter;this.gizmos.down(tc.wcKind,tc,true);
    if(!this.gizmos.drag)return false;
    const object=tc.object,q=object.getWorldQuaternion(spin);
    a.copy(axes[letter]).applyQuaternion(this.instrument.quaternion);
    if(mode==="translate")object.position.copy(object.parent.worldToLocal(object.getWorldPosition(b).addScaledVector(a,change/1000)));
    else {
      turn.setFromAxisAngle(a,change*RAD).multiply(q);
      object.quaternion.copy(object.parent.getWorldQuaternion(q).invert().multiply(turn));
    }
    object.updateWorldMatrix(true,false);
    this.gizmos.changed(tc.wcKind,tc);this.gizmos.up(tc.wcKind,tc);
    return true;
  }

  // The scale's labels keep clear of the cards and the axis letters, nearest
  // where they belong first. Everything is measured before any moves.
  layoutLabels() {
    const m=this.instrument;
    if(!m)return;
    const overlays=this.core.overlays;
    const boxes=overlays.boxes(["gizmoValue","drawing","resizeValue"]);
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

  // The value rides on its handle: the one being dragged, typed along, or
  // last let go.
  layout() {
    this.refresh();
    if(this.entry && !this.live(this.entry))this.close();
    if(this.settled && !this.live(this.settled))this.settled=null;
    const m=this.instrument,e=this.entry,s=this.settled;
    let tc,axis,mode;
    if(m){({tc,handle:axis,mode}=m);this.field.show(m.text,unitOf(mode),nameOf(mode,axis));}
    else if(e)({tc,letter:axis,mode}=e);
    else if(s){({tc,axis,mode}=s);this.field.show(s.text,unitOf(mode),nameOf(mode,axis));}
    else {this.field.hide();return;}
    if(!tc.anchor(axis,mode,a))return;
    const overlays=this.core.overlays;
    overlays.project(a,at);overlays.project(tc.getHelper().position,from);
    this.field.place(at,from,overlays.obstacles(FIELD_AVOIDS));
  }

  // The pointer on the value is still on the TCP gizmo; while a change is
  // typed the gizmo is pinned.
  hovered(on) {
    if(this.entry || (this.settled||this.instrument?.record)?.tc!==this.gizmos.tcp)return;
    this.core.pointer.hover.gizmo(on,performance.now());
    if(!on)this.core.pointer.settle();
  }

  // A released drag leaves its change on the handle.
  end(released=false) {
    const m=this.instrument;if(!m)return;this.instrument=null;
    m.group.removeFromParent();
    for(const l of m.labels){l.removeFromParent();l.element.remove();}
    if(released && !m.exact)this.settled={...m.record,axis:m.handle,mode:m.mode,byLetter:m.byLetter,text:m.text};
  }

  dispose() {
    this.end();this.field.node.remove();
    this.resources?.group.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});
    this.resources=null;
  }
}
