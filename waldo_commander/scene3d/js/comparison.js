import * as THREE from "three";
import { label } from "./rings.js";
import { deviations } from "./deviations.js";
import { RecordedGhost } from "./recorded_ghost.js";
import { element, setStyle, setText } from "./dom.js";
import { finite, reading, signed } from "./handles.js";
import { GAP } from "./overlays.js";

const add=(tag,parent,text="",cls="")=>element(tag,cls,parent,text);
const at=[0,0,0];
const turnA=new THREE.Quaternion(),turnB=new THREE.Quaternion(),euler=new THREE.Euler();

// The TCP (mm), tool orientation (°) and largest joint (°) difference
// between two recorded rows, or null where either is not a finite pose.
function difference(ideal,idealRow,pred,row) {
  const a=idealRow*6,b=row*6,values=[0,0,0];
  for(let k=0;k<6;k++)if(!finite(ideal.tcp[a+k])||!finite(pred.tcp[b+k]))return null;
  if(ideal.valid && !ideal.valid[idealRow] || pred.valid && !pred.valid[row])return null;
  values[0]=Math.hypot(pred.tcp[b]-ideal.tcp[a],pred.tcp[b+1]-ideal.tcp[a+1],pred.tcp[b+2]-ideal.tcp[a+2])*1000;
  turnA.setFromEuler(euler.set(ideal.tcp[a+3],ideal.tcp[a+4],ideal.tcp[a+5],"XYZ"));
  turnB.setFromEuler(euler.set(pred.tcp[b+3],pred.tcp[b+4],pred.tcp[b+5],"XYZ"));
  values[1]=2*Math.acos(Math.min(1,Math.abs(turnA.dot(turnB))))*180/Math.PI;
  for(let j=0;j<pred.dof;j++) {
    let q=pred.q[row*pred.dof+j]-ideal.q[idealRow*ideal.dof+j];
    if(!finite(q))return null;
    if(pred.continuous[j])q=Math.atan2(Math.sin(q),Math.cos(q));
    values[2]=Math.max(values[2],Math.abs(q)*180/Math.PI);
  }
  return values;
}

export class Comparison {
  constructor(inspection) {
    this.inspection=inspection; this.core=inspection.core;
    this.data=null; this.mode="auto"; this.enabled=true;this.thresholds=[1,.5,.5];
    this.ghost=new RecordedGhost(this.core,"comparison:ghost",()=>this.show(this.inspection.current));
    this.bar=add("div",this.core.root,"","wc-comparison-bar");this.bar.hidden=true;
    this.core.overlays.track("comparison",this.bar);
    this.core.overlays.readers.push(()=>{
      const e=this.rulerLabel?.visible&&this.rulerLabel.element;
      this.rulerSize=e&&e.offsetWidth?[e.offsetWidth,e.offsetHeight]:null;
    });
    const top=add("div",this.bar,"","wc-comparison-actions");
    this.summary=add("span",top);this.summary.setAttribute("aria-live","polite");
    const more=add("details",this.bar);add("summary",more,"Details");
    const navigation=add("div",more,"","wc-comparison-actions");
    this.modeInput=add("select",navigation);this.modeInput.setAttribute("aria-label","Prediction comparison visibility");
    for(const [value,title] of [["auto","Auto"],["all","Show all"],["off","Off"]]) {const o=add("option",this.modeInput,title);o.value=value;}
    this.modeInput.onchange=()=>{this.mode=this.modeInput.value;this.updateVisibility();};
    this.previous=add("button",navigation,"‹");this.previous.setAttribute("aria-label","Previous deviation");this.previous.onclick=()=>this.jump(-1);
    this.next=add("button",navigation,"›");this.next.setAttribute("aria-label","Next deviation");this.next.onclick=()=>this.jump(1);
    this.maximum=add("button",top,"Peak");this.maximum.onclick=()=>this.inspect(this.peak);
    this.duration=add("div",more,"","wc-path-hint");
    const settings=this.settings=add("details",more);add("summary",settings,"Display thresholds");
    const limits=add("div",settings,"","wc-comparison-limits");
    ["TCP · mm","Orientation · °","Any joint · °"].forEach((name,i)=>{
      const holder=add("label",limits,name), input=add("input",holder);input.type="number";input.min="0.001";input.step="any";input.value=this.thresholds[i];
      input.setAttribute("aria-label",`${name} display threshold`);
      input.onchange=()=>{const v=Number(input.value);if(!Number.isFinite(v)||v<=0){input.value=this.thresholds[i];return;}this.thresholds[i]=v;this.rebuild();this.show(this.inspection.current);};
    });
    add("div",settings,"Visualization only · not motion limits","wc-path-hint");
    this.details=add("details",this.bar,"","wc-comparison-details");
    this.pairSummary=add("summary",this.details,"Deviation");this.details.hidden=true;
    const key=this.key=add("div",this.bar,"","wc-comparison-key");
    this.bar.insertBefore(key,this.details);key.hidden=true;
    this.idealKey=add("span",key,"Ideal","wc-comparison-ideal");this.predictedKey=add("span",key,"Predicted","wc-comparison-predicted");
    this.metrics=add("div",this.details,"","wc-path-data");
    this.metricValues=["TCP mm","Orientation °","Max joint °"].map(name=>{
      const cell=add("div",this.metrics);add("span",cell,name,"wc-path-key");return add("span",cell);
    });
    this.joints=add("div",this.details,"","wc-path-hint");this.note=add("div",this.details,"","wc-path-hint");
  }

  reset() {
    this.data=null;this.bar.hidden=this.details.hidden=true;
    this.removeLines();this.hidePair();this.ghost.dispose();
    this.timeline?.remove();this.timeline=null;
    this.ruler?.removeFromParent();this.ruler?.geometry.dispose();this.ruler?.material.dispose();this.ruler=null;
    this.rulerLabel?.element.remove();this.rulerLabel?.removeFromParent();this.rulerLabel=null;
    this.core.overlays.mark("separation",null);
  }

  status({state,reason}) {
    if(this.data && state==="ready")return;
    if(this.data && state!=="ready")this.reset();
    this.bar.hidden=state==="unavailable" || state==="ready";
    this.summary.textContent=`Prediction · ${{pending:"Computing…",failed:"Failed",stale:"Stale"}[state] || ""}`;
    this.duration.textContent=reason;
    for(const item of [this.modeInput,this.previous,this.next,this.maximum,this.settings])item.hidden=true;
    this.core.requestRender();
  }

  get visibility() {return this.enabled?this.mode:"off";}

  setVisible(on) {
    this.enabled=on;this.modeInput.disabled=!on;
    this.modeInput.title=on?"":"Prediction hidden by the view setting";
    this.updateVisibility();
  }

  updateVisibility() {
    if(this.visibility==="off" && this.inspection.current?.record===1)this.inspection.clear();
    this.rebuild();this.show(this.inspection.current);
  }

  load(data) {
    if(!data || data.revision!==this.inspection.data?.revision)return;
    this.data=data;
    this.ghost.build(this.inspection.data);
    this.bar.hidden=false;this.modeInput.value=this.mode;
    for(const item of [this.modeInput,this.previous,this.next,this.maximum,this.settings])item.hidden=false;
    this.rebuild();this.show(this.inspection.current);
  }

  rebuild() {
    if(!this.data)return;
    const d=this.data, r=this.inspection.data.records[1];
    Object.assign(this,deviations(this.inspection.data.records,d,this.thresholds));
    this.visibleRows=new Uint32Array(r.rows+1);this.visibleSegments=new Uint32Array(r.rows+1);
    for(let row=0;row<r.rows;row++) {
      this.visibleRows[row+1]=this.visibleRows[row]+Number(this.visible(row));
      this.visibleSegments[row+1]=this.visibleSegments[row]+Number(row<r.rows-1 && this.segmentVisible(row));
    }
    this.summary.textContent=!this.enabled?"Prediction · Hidden by view setting":this.intervals.length?`Prediction · ${this.intervals.length} deviation${this.intervals.length===1?"":"s"}`:"Prediction · Within display thresholds";
    const missing=d.idealRows.filter(row=>row<0).length;
    this.duration.textContent=`Ideal ${reading(d.commandedDuration,2)} s · Predicted ${reading(d.predictedDuration,2)} s${d.stop!=="completed"?` · ${d.stop==="budget_exhausted"?"Partial prediction": "Prediction stopped"}`:""}${missing?" · Unmatched samples unavailable":""}`;
    this.updateNavigation();
    this.removeLines();
    const positions=[],distances=[],peaks=[];let distance=0;
    const tcp=r.tcp;
    if(this.visibility!=="off")for(const block of r.blocks) {
      if(block.instant || !block.valid)continue;
      for(let row=block.start;row<block.end-1;row++) {
        const i=row*6,j=i+6;
        if(![tcp[i],tcp[i+1],tcp[i+2],tcp[j],tcp[j+1],tcp[j+2]].every(finite)||r.valid && (!r.valid[row]||!r.valid[row+1]))continue;
        const length=Math.hypot(tcp[i]-tcp[j],tcp[i+1]-tcp[j+1],tcp[i+2]-tcp[j+2]);
        if(this.visibility==="all" || this.mask[row] || this.mask[row+1]) {positions.push(tcp[i],tcp[i+1],tcp[i+2],tcp[j],tcp[j+1],tcp[j+2]);distances.push(distance,distance+length);}
        distance+=length;
      }
    }
    if(positions.length) {
      const geometry=new THREE.BufferGeometry();geometry.setAttribute("position",new THREE.Float32BufferAttribute(positions,3));geometry.setAttribute("lineDistance",new THREE.Float32BufferAttribute(distances,1));
      this.line=new THREE.LineSegments(geometry,new THREE.LineDashedMaterial({color:this.inspection.data.predictedColor,dashSize:.004,gapSize:.003,depthTest:false,transparent:true,opacity:.95}));
      this.line.name="comparison:predicted";this.line.renderOrder=800;this.core.scene.add(this.line);
    }
    if(this.visibility!=="off")for(const {peak} of this.intervals) {
      const record=this.inspection.data.records[peak.record];
      peaks.push(...record.tcp.slice(peak.row*6,peak.row*6+3));
    }
    if(peaks.length) {
      const geometry=new THREE.BufferGeometry().setAttribute("position",new THREE.Float32BufferAttribute(peaks,3));
      this.peaks=new THREE.Points(geometry,new THREE.PointsMaterial({color:this.inspection.data.predictedColor,size:7,sizeAttenuation:false,depthTest:false}));
      this.peaks.name="comparison:peaks";this.peaks.renderOrder=801;this.core.scene.add(this.peaks);
    }
    this.rebuildTimeline();this.core.requestRender();
  }

  removeLines() {
    for(const key of ["line","peaks"]) {const o=this[key];if(o){o.removeFromParent();o.geometry.dispose();o.material.dispose();this[key]=null;}}
  }

  updateNavigation() {
    this.previous.disabled=this.next.disabled=this.maximum.disabled=!this.peak || this.visibility==="off" || !this.inspection.available;
  }

  visible(row) {return !!this.data && (this.visibility==="all" || this.visibility!=="off" && !!this.mask[row]);}

  segmentVisible(row) {
    const r=this.inspection.data?.records[1];
    return r && (!r.valid || r.valid[row] && r.valid[row+1]) && (this.visible(row) || this.visible(row+1));
  }

  blockVisible(block) {
    if(!this.data || this.visibility==="off")return false;
    return this.visibleRows[block.end]>this.visibleRows[block.start];
  }

  chunkVisible(chunk) {
    return !!this.data && this.visibility!=="off" && this.visibleSegments[chunk.end]>this.visibleSegments[chunk.start];
  }

  // The deviations marked on the program's scrub bar, when it is showing.
  rebuildTimeline() {
    this.timeline?.remove();this.timeline=null;
    if(!this.data || !this.inspection.data)return;
    const track=document.querySelector('[data-testid="editor-scrub-bar"]') || document.querySelector('.scrub-track');
    if(!track || this.visibility==="off")return;
    this.timeline=add("div",track,"","wc-comparison-timeline");
    const ideal=this.inspection.data.records[0],span=ideal.source[ideal.rows-1]+1;
    for(const interval of this.intervals) {
      const {first,last}=interval;
      if(first<0 || last<0)continue;
      const start=ideal.source[first],end=ideal.source[Math.min(ideal.rows-1,last+1)]+(last+1<ideal.rows?0:1);
      const bar=add("span",this.timeline);bar.style.left=`${100*start/span}%`;bar.style.width=`${Math.max(.3,100*(end-start)/span)}%`;
    }
  }

  // The app's panels moved: the scrub bar may have just appeared.
  follow() {
    if(this.data && !this.timeline?.isConnected)this.rebuildTimeline();
  }

  inspect(hit) {
    if(!hit || !this.inspection.available)return;
    this.inspection.inspect(hit);this.inspection.send(true);
  }

  jump(direction) {
    if(!this.intervals.length)return;
    const hit=this.inspection.current;
    const selected=this.intervals.findIndex(i=>i.peak.record===hit?.record && i.peak.row===hit?.row);
    if(selected>=0) {
      this.inspect(this.intervals[(selected+direction+this.intervals.length)%this.intervals.length].peak);return;
    }
    const row=!hit?-1:hit.record===1?this.data.idealRows[hit.row]:hit.row;
    const r=hit?this.inspection.data.records[hit.record]:null,b=r?.blocks[hit?.block];
    const key=b?`${b.command}/${b.commandOccurrence}`:null,elapsed=b?(r.source[hit.row]-r.source[b.start])*r.dt:-1;
    const order=i=>i.key===key?i.peak.elapsed-elapsed:i.peak.idealRow-row;
    const interval=direction>0?this.intervals.find(i=>order(i)>1e-9):[...this.intervals].reverse().find(i=>order(i)<-1e-9);
    this.inspect((interval || this.intervals[direction>0?0:this.intervals.length-1]).peak);
  }

  predictedRow(hit) {return !hit?-1:hit.record===1?hit.row:(this.data?.idealSamples.predictedRows[hit.row]??-1);}

  show(hit) {
    if(!this.data || !hit || this.visibility==="off" || !this.inspection.available) {this.hidePair();return;}
    const i=this.inspection, selected=i.data.records[hit.record].blocks[hit.block];
    if(selected?.instant) {this.hidePair();return;}
    // Playback pairs the arm with the ideal at the scrub bar's time; that
    // pair is what the card describes then, whatever the alignment says.
    const [ideal,pred]=i.data.records;
    const played=hit.record===1 && hit.idealRow!==undefined;
    const row=this.predictedRow(hit),idealRow=played?hit.idealRow:hit.record===1?this.data.idealRows[row]:hit.row;
    const errors=hit.record===1?this.data:this.data.idealSamples,index=hit.record===1?row:hit.row;
    const values=row<0 || idealRow<0?null:played?difference(ideal,idealRow,pred,row)
      :[errors.positionMm[index],errors.orientationDeg[index],errors.jointDeg[index]];
    if(!values) {
      this.hidePair();this.details.hidden=false;this.metrics.hidden=true;
      setText(this.pairSummary,"Deviation · unavailable");
      setText(this.note,"No corresponding prediction at this command time.");setText(this.joints,"");return;
    }
    this.details.hidden=false;this.metrics.hidden=false;
    if(this.visibility==="auto" && !values.some((v,k)=>finite(v) && v>this.thresholds[k])) {this.hidePair();return;}
    const strongest=values.reduce((best,v,k)=>v/this.thresholds[k]>values[best]/this.thresholds[best]?k:best,0);
    setText(this.pairSummary,`Deviation · ${reading(values[strongest],2)}${[" mm","° tool","° joint"][strongest]}`);
    this.metricValues.forEach((cell,k)=>setText(cell,reading(values[k],2)));
    setText(this.note,played && idealRow!==this.data.idealRows[row]?"Paired with the ideal at the playback time"
      :hit.record===1 && this.data.settling[row]?"Settling · ideal holds its completed endpoint":"Aligned by command and elapsed time");
    const differences=i.data.panel.map(([index,sign],j)=>{
      let q=pred.q[row*pred.dof+j]-ideal.q[idealRow*ideal.dof+j];if(pred.continuous[j])q=Math.atan2(Math.sin(q),Math.cos(q));
      return {name:`J${index+1}`,value:q*180/Math.PI*sign};
    }).sort((a,b)=>Math.abs(b.value)-Math.abs(a.value)).slice(0,3);
    setText(this.joints,differences.map(d=>`${d.name} ${signed(d.value,2)}°`).join(" · "));
    // The arm shows the prediction, and the ghost the ideal it was meant to follow.
    if(!this.ghost.ready) {this.hidePair();return;}
    const block=ideal.blocks.find(b=>!b.instant && b.start<=idealRow && idealRow<b.end);if(!block) {this.hidePair();return;}
    this.ghost.articulate(ideal,idealRow,block);this.ghost.visible=true;this.key.hidden=false;
    setText(this.idealKey,"Ideal · ghost");
    setText(this.predictedKey,`Predicted · ${this.core.ix.simulator?"simulator arm":"path"}`);
    this.ghost.material.color.set(i.data.color);
    if(!this.ruler) {
      const geometry=new THREE.BufferGeometry().setAttribute("position",new THREE.BufferAttribute(new Float32Array(6),3));
      this.ruler=new THREE.Line(geometry,new THREE.LineBasicMaterial({color:i.data.predictedColor,depthTest:false}));this.ruler.renderOrder=960;this.ruler.name="comparison:separation";this.core.scene.add(this.ruler);
      this.rulerLabel=label("comparison:separation:label");this.core.scene.add(this.rulerLabel);
      this.rulerLabel.element.classList.add("wc-comparison-separation");
      this.rulerLabel.center.set(.5,1.8);
    }
    const a=ideal.tcp.slice(idealRow*6,idealRow*6+3),b=pred.tcp.slice(row*6,row*6+3);
    this.ruler.geometry.attributes.position.array.set([...a,...b]);this.ruler.geometry.attributes.position.needsUpdate=true;this.ruler.geometry.computeBoundingSphere();this.ruler.visible=true;
    this.rulerLabel.position.fromArray(a.map((v,k)=>(v+b[k])/2));setText(this.rulerLabel.element,`ΔTCP ${reading(values[0],2)} mm`);this.rulerLabel.visible=true;
  }

  hidePair() {
    this.details.hidden=this.key.hidden=true;this.ghost.visible=false;if(this.ruler)this.ruler.visible=false;if(this.rulerLabel)this.rulerLabel.visible=false;
  }

  // The bar sits top left, past the panels there; the separation label is
  // where the inspection's markers and menus keep clear of.
  layout() {
    const overlays=this.core.overlays,card=overlays.sized("comparison");
    if(card) {
      const left=overlays.clearLeft(GAP,GAP+card.h);
      setStyle(this.bar,"maxWidth",`${Math.max(220,overlays.view.width-left-16)}px`);
      overlays.place(card,left,GAP);
      if(this.timeline && !this.timeline.isConnected)this.rebuildTimeline();
    }
    if(this.rulerLabel?.visible && this.rulerSize) {
      const [x,y,z]=overlays.project(this.rulerLabel.position,at),[w,h]=this.rulerSize;
      const left=x-w*this.rulerLabel.center.x,top=y-h*this.rulerLabel.center.y;
      overlays.mark("separation",Math.abs(z)<=1?[{left,top,right:left+w,bottom:top+h}]:null);
    } else overlays.mark("separation",null);
  }

  dispose() {this.reset();this.bar.remove();}
}
