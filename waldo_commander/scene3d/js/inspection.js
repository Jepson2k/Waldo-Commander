import * as THREE from "three";
import { Comparison } from "./comparison.js";
import { SweptVolume } from "./swept_volume.js";
import { element, setAttr, setHidden, setStyle, setText } from "./dom.js";
import { GAP, overlap } from "./overlays.js";
import { finite, reading, tapPx } from "./handles.js";

// The cards a path's markers and menus keep clear of, besides the panels.
const COVERS = ["comparison", "separation"];
const PICK_PX = 10;
const CHUNK = 128;
// The hovered path's glow and grip, in pixels.
const GLOW_PX = 3;
const GRIP_PX = 6;
// Points a hovered move's glow follows at most.
const GLOW_POINTS = 160;
const sample = new THREE.Vector3();
const rowAt = [0, 0, 0];

const place = (into, values, at) => { for (let i = 0; i < values.length; i++) into[at + i] = values[i]; };

// A record as the rest of the view reads it, put back together from the
// spans it is sent as; null while one of its spans has not arrived.
function assemble(r, store) {
  const n = r.rows, dof = r.dof;
  const source = new Array(n), q = new Array(n * dof), tcp = new Array(n * 6), closed = new Array(n);
  const objects = r.objects.map((node) => ({ node, poses: new Array(n * 7) }));
  let valid = null, at = 0;
  for (const span of r.spans) {
    const c = store.get(span.key);
    if (!c) return null;
    for (let i = 0; i < c.offsets.length; i++) source[at + i] = span.base + c.offsets[i];
    place(q, c.q, at * dof); place(tcp, c.tcp, at * 6); place(closed, c.closed, at);
    if (c.valid) place(valid ??= new Array(n), c.valid, at);
    objects.forEach((o, k) => place(o.poses, c.objects[k], at * 7));
    at += c.offsets.length;
  }
  return { ...r, source, q, tcp, closed, valid, objects };
}

// The program's recorded paths as something to point at: a press on a path
// scrubs the simulator to that moment and a drag along it scrubs on, the
// timeline following. Stationary moments (a dwell, a turn on the spot, a
// tool change) are markers beside the path.
export class Inspection {
  constructor(core) {
    this.core = core;
    core.canvas.tabIndex=0;
    core.canvas.setAttribute("aria-label","3D workspace");
    this.data = null; this.current = null; this.dragging = false; this.hits = [];
    this.spans = new Map(); // a path's spans by content key, as the app sent them
    this.seq = 0; this.lastSend = 0; this.timer = 0;
    this.cursor = null; this.markers = [];
    this.hovered = null; this.glow = null; // the move under the pointer, lit
    this.grip = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), new THREE.MeshBasicMaterial({depthTest:false, transparent:true, opacity:.95}));
    this.grip.name = "inspection:grip"; this.grip.renderOrder = 951; this.grip.visible = false;
    core.scene.add(this.grip);
    this.projected = []; this.chunks = []; this.alternatives = []; this.radius = 13;
    this.seenWorld = new THREE.Matrix4(); this.seenProjection = new THREE.Matrix4(); this.seenView = [NaN, NaN, NaN, NaN];
    this.choices = element("div", "wc-path-choices", core.root);
    this.choices.hidden=true;
    this.other = element("button", "wc-path-other", core.root, "Other pass");
    this.other.hidden=true;this.other.onclick=()=>this.choosePasses();
    core.overlays.track("choices", this.choices);
    core.overlays.track("other", this.other);
    core.overlays.readers.push(() => { this.radius = (this.markers[0]?.element.offsetWidth || 26) / 2; });
    this.comparison = new Comparison(this);
    this.sweep = new SweptVolume(this);
    this.onDocumentDown = (e) => {
      if(!this.choices.contains(e.target) && e.target!==this.other && !e.target.closest?.(".wc-path-event"))this.choices.hidden=true;
    };
    document.addEventListener("pointerdown", this.onDocumentDown, true);
  }

  // A press scrubs while a recorded sample is shown.
  get pinned() {
    return this.current !== null;
  }

  // The app sends only the spans this view does not hold yet; those no
  // longer used are let go once the paths are put together.
  load(sent) {
    let data = null;
    if (sent) {
      for (const [key, span] of Object.entries(sent.chunks || {})) this.spans.set(key, span);
      const records = sent.records.map((r) => assemble(r, this.spans));
      if (records.every(Boolean)) {
        const used = new Set(sent.records.flatMap((r) => r.spans.map((s) => s.key)));
        for (const key of this.spans.keys()) if (!used.has(key)) this.spans.delete(key);
        data = { ...sent, chunks: undefined, records };
      }
    }
    this.sweep.dispose();
    this.comparison.reset();
    this.clear(false); this.removeCursor(); this.hoverAt(null);
    for (const m of this.markers) { m.element.remove(); m.leader.remove(); }
    this.core.overlays.mark("pathEvents", null);
    this.markers = []; this.data = data; this.projected = []; this.seq = 0;
    this.seenView[0] = NaN;
    if (!data) { this.chunks = []; return; }
    this.comparison.setVisible(data.predictionVisible);
    this.cursor = new THREE.Mesh(new THREE.SphereGeometry(.003, 16, 12), new THREE.MeshBasicMaterial({color:data.color, depthTest:false}));
    this.cursor.renderOrder = 950; this.cursor.name = "inspection:cursor"; this.cursor.visible = false;
    this.core.scene.add(this.cursor);
    this.chunks = data.records.map((r) => r.blocks.map((block) => {
      const chunks = [];
      if (!block.stationary) for (let start = block.start; start < block.end - 1; start += CHUNK) {
        chunks.push({start, end: Math.min(block.end - 1, start + CHUNK), left: 0, right: 0, top: 0, bottom: 0});
      }
      return chunks;
    }));
    data.records.forEach((r, record) => r.blocks.forEach((block, bi) => {
      if (!block.stationary) return;
      const button = element("button", "wc-path-event", this.core.root);
      const leader=element("span","wc-path-event-leader",this.core.root);
      const marker={element:button, leader, record, block:bi, row:block.start,members:[]};
      button.dataset.pathRecord = record; button.dataset.pathBlock = bi;
      button.onclick=()=>{
        if(marker.members.length>1) {this.chooseEvents(marker.members,button.getBoundingClientRect());return;}
        this.inspect({record:marker.record,row:marker.row,block:marker.block});
      };
      this.markers.push(marker);
    }));
    this.core.requestRender();
  }

  get available() {
    return !!this.core.ix.simulator && this.core.ix.pathScrubAvailable!==false && !this.core.ix.edit?.active;
  }

  availabilityChanged() {
    if(!this.available)this.clear();
    this.comparison.updateNavigation();
  }

  removeCursor() {
    if (!this.cursor) return;
    this.cursor.removeFromParent(); this.cursor.geometry.dispose(); this.cursor.material.dispose();
    this.cursor = null;
  }

  // Where every recorded TCP sample lands on screen, kept until the camera
  // or the view moves.
  project() {
    if (!this.data) return;
    const camera = this.core.camera, view = this.core.overlays.view, seen = this.seenView;
    camera.updateMatrixWorld();
    if (this.seenWorld.equals(camera.matrixWorld) && this.seenProjection.equals(camera.projectionMatrix)
      && seen[0] === view.left && seen[1] === view.top && seen[2] === view.width && seen[3] === view.height) return;
    this.seenWorld.copy(camera.matrixWorld); this.seenProjection.copy(camera.projectionMatrix);
    seen[0] = view.left; seen[1] = view.top; seen[2] = view.width; seen[3] = view.height;
    this.data.records.forEach((r, record) => {
      let out = this.projected[record];
      if (out?.length !== r.rows * 3) out = this.projected[record] = new Float32Array(r.rows * 3);
      const tcp = r.tcp;
      for (let i = 0; i < r.rows; i++) {
        const x = tcp[i*6], y = tcp[i*6+1], z = tcp[i*6+2];
        if (!finite(x) || !finite(y) || !finite(z)) { out[i*3] = NaN; out[i*3+1] = NaN; out[i*3+2] = Infinity; continue; }
        sample.set(x, y, z).project(camera);
        out[i*3] = view.left + (sample.x+1)*view.width/2;
        out[i*3+1] = view.top + (1-sample.y)*view.height/2;
        out[i*3+2] = sample.z;
      }
      for (const chunks of this.chunks[record]) for (const chunk of chunks) {
        chunk.left = chunk.top = Infinity; chunk.right = chunk.bottom = -Infinity;
        for (let i = chunk.start; i <= chunk.end; i++) {
          const px = out[i*3], py = out[i*3+1];
          if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
          if (px < chunk.left) chunk.left = px; if (px > chunk.right) chunk.right = px;
          if (py < chunk.top) chunk.top = py; if (py > chunk.bottom) chunk.bottom = py;
        }
      }
    });
  }

  // Where one sample is on the page, [x, y, depth]: what layout reads,
  // without projecting every sample when the camera moves.
  rowScreen(record, row, out = rowAt) {
    const tcp = this.data.records[record].tcp, i = row * 6;
    const x = tcp[i], y = tcp[i+1], z = tcp[i+2];
    if (!finite(x) || !finite(y) || !finite(z)) { out[0] = out[1] = NaN; out[2] = Infinity; return out; }
    const view = this.core.overlays.view;
    sample.set(x, y, z).project(this.core.camera);
    out[0] = view.left + (sample.x+1)*view.width/2;
    out[1] = view.top + (1-sample.y)*view.height/2;
    out[2] = sample.z;
    return out;
  }

  pick(x, y, continuity = false) {
    this.project();
    const hits = [];
    if (!this.data) return hits;
    const records = this.data.records;
    for (let record = 0; record < records.length; record++) {
      if (continuity && this.current && record !== this.current.record) continue;
      const r = records[record], p = this.projected[record];
      for (let bi = 0; bi < r.blocks.length; bi++) {
        const block = r.blocks[bi];
        if (block.stationary || record===1 && (!block.valid || !this.comparison.blockVisible(block))) continue;
        let best = null, first = 0, last = -1;
        const finish = () => { if (best) hits.push({...best, first, last}); best = null; };
        for (const chunk of this.chunks[record][bi]) {
          if (record===1 && !this.comparison.chunkVisible(chunk)) {finish();continue;}
          if (x<chunk.left-PICK_PX || x>chunk.right+PICK_PX || y<chunk.top-PICK_PX || y>chunk.bottom+PICK_PX) { finish(); continue; }
          for (let i=chunk.start; i<chunk.end; i++) {
            if (record===1 && (!block.valid || !this.comparison.segmentVisible(i))) {finish();continue;}
            if (Math.abs(p[i*3+2]) > 1 || Math.abs(p[(i+1)*3+2]) > 1) { finish(); continue; }
            const ax=p[i*3], ay=p[i*3+1], dx=p[(i+1)*3]-ax, dy=p[(i+1)*3+1]-ay;
            const f=Math.max(0,Math.min(1,((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy || 1)));
            const distance=Math.hypot(x-ax-f*dx,y-ay-f*dy);
            if (distance > PICK_PX || !Number.isFinite(distance)) { finish(); continue; }
            if (!best) first=i;
            last=i+1;
            const row=i+(f>.5 ? 1:0);
            const drift=continuity && this.current ? Math.abs(row-this.current.row) : 0;
            const score=distance + Math.min(100, drift * .1);
            if (!best || score < best.score) best={record,row,block:bi,distance,score};
          }
        }
        finish();
      }
    }
    hits.sort((a,b) => a.score-b.score);
    if (continuity && this.current) {
      const blocks=records[this.current.record].blocks;
      let before=this.current.block-1, after=this.current.block+1;
      while (before>=0 && blocks[before].stationary) before--;
      while (after<blocks.length && blocks[after].stationary) after++;
      return hits.filter((h) => h.block>=before && h.block<=after);
    }
    return hits;
  }

  // ---- the pointer's tool -------------------------------------------------

  hit(x, y) {
    if (!this.data || !this.available || this.core.gestures.active) return false;
    this.hits = this.pick(x, y);
    return this.hits.length > 0;
  }

  // A move a press would scrub lights up under the pointer, with a grip at
  // the moment a press would take, and the pointer becomes a hand: what
  // can be dragged says so before it is pressed. Whether it is one.
  hoverAt(at) {
    const hit = at && this.data && this.available && !this.core.gestures.active ? this.pick(at[0], at[1])[0] || null : null;
    const key = hit ? `${hit.record}:${hit.block}` : null;
    if (key !== this.hovered) {
      this.hovered = key;
      this.removeGlow();
      if (hit) this.glow = this.makeGlow(hit);
    }
    this.grip.visible = !!hit;
    if (hit) {
      const r = this.data.records[hit.record], i = hit.row * 6;
      this.grip.position.set(r.tcp[i], r.tcp[i+1], r.tcp[i+2]);
      this.grip.scale.setScalar(this.core.metresPerPixel(this.grip.position) * GRIP_PX);
    }
    this.core.canvas.style.cursor = hit ? "grab" : this.dragging ? "grabbing" : "";
    this.core.requestRender();
    return !!hit;
  }

  makeGlow(hit) {
    const r = this.data.records[hit.record], block = r.blocks[hit.block];
    const step = Math.max(1, Math.ceil((block.end - block.start) / GLOW_POINTS));
    const points = [];
    for (let row = block.start; row < block.end; row += step) {
      const i = row * 6;
      if (finite(r.tcp[i]) && finite(r.tcp[i+1]) && finite(r.tcp[i+2])) points.push(new THREE.Vector3(r.tcp[i], r.tcp[i+1], r.tcp[i+2]));
    }
    if (points.length < 2) return null;
    const middle = points[points.length >> 1];
    const radius = this.core.metresPerPixel(middle) * GLOW_PX;
    const color = this.core.ix.colors?.action;
    const glow = new THREE.Mesh(
      new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points), points.length * 2, radius, 6, false),
      new THREE.MeshBasicMaterial({color, transparent:true, opacity:.45, depthTest:false, depthWrite:false}),
    );
    glow.name = "inspection:hover"; glow.renderOrder = 949;
    this.grip.material.color.set(color);
    this.core.scene.add(glow);
    return glow;
  }

  removeGlow() {
    if (!this.glow) return;
    this.glow.removeFromParent(); this.glow.geometry.dispose(); this.glow.material.dispose();
    this.glow = null;
  }

  begin(event) {
    this.choices.hidden=true;this.core.canvas.focus({preventScroll:true});
    const first = (this.current && this.hits.find(h=>this.samePass(h))) || this.hits[0];
    // A held finger may ask for the path menu without moving the simulator.
    let pending = event.pointerType === "touch";
    if (!pending) this.inspect(first);
    this.dragging = true;
    this.core.canvas.style.cursor = "grabbing";
    return {
      move: (e) => {
        if (pending) {
          if (Math.hypot(e.clientX-event.clientX, e.clientY-event.clientY) <= tapPx("touch")) return;
          pending = false; this.inspect(first);
        }
        const hits=this.pick(e.clientX,e.clientY,true);
        if (hits.length) this.show(hits[0],true);
      },
      release: () => { if (pending) this.inspect(first); this.dragging = false; this.send(true); },
      cancel: (reason) => {
        this.dragging = false;
        if (!pending && reason !== "released unseen") this.clear();
      },
    };
  }

  // A press on nothing the view acts on lets the shown sample go.
  missed(x, y) {
    if (this.current && !this.core.gestures.active && !this.core.pointer.hit(x, y)) this.clear();
  }

  key(e) {
    if (this.current && e.target===this.core.canvas && ["ArrowLeft", "ArrowRight"].includes(e.key)) {
      this.seekRow(this.current.row + (e.key === "ArrowRight" ? 1 : -1)); e.preventDefault();
    }
  }

  // Escape lets the shown sample and the open menu go.
  escape() {
    if (!this.current && this.choices.hidden) return false;
    this.clear(); this.core.canvas.focus({preventScroll:true});
    return true;
  }

  // ---- the shown sample -----------------------------------------------------

  inspect(hit) {
    if (!this.available || !hit) {this.clear();return;}
    this.current = hit;
    this.core.pointer.forgetHover();
    this.show(hit, true);
  }

  samePass(hit) {
    return hit.record===this.current.record && hit.block===this.current.block
      && this.current.row>=(hit.first ?? hit.row)-1 && this.current.row<=(hit.last ?? hit.row)+1;
  }

  // `hit.idealRow` is the ideal row playback shows the arm against; a
  // sample playback reaches outside every drawn block is still shown.
  show(hit, notify=false) {
    if (!this.available) {this.clear();return;}
    if (!this.data || !hit) return;
    const r=this.data.records[hit.record], row=hit.row;
    const selected=r.blocks[hit.block];
    const bi=selected && selected.start<=row && row<selected.end ? hit.block : r.blocks.findIndex((b)=>!b.instant && b.start<=row && row<b.end);
    if (bi<0 && hit.idealRow===undefined) return;
    this.current={...hit,block:bi}; const block=r.blocks[bi];
    this.project();
    const px=this.projected[hit.record][row*3], py=this.projected[hit.record][row*3+1];
    this.alternatives=block?this.pick(px,py):[];
    if (block) for (const marker of this.markers) {
      if(marker.record===1 && !this.comparison.blockVisible(this.data.records[1].blocks[marker.block]))continue;
      const p=this.projected[marker.record];
      if (Math.hypot(p[marker.row*3]-px,p[marker.row*3+1]-py)<PICK_PX) this.alternatives.push({record:marker.record,row:marker.row,block:marker.block});
    }
    if (this.cursor) {
      const i=row*6;
      this.cursor.visible=!block?.instant && finite(r.tcp[i]) && finite(r.tcp[i+1]) && finite(r.tcp[i+2]);
      if (this.cursor.visible) this.cursor.position.set(r.tcp[i], r.tcp[i+1], r.tcp[i+2]);
    }
    this.other.hidden=!block || block.stationary || !this.alternatives.some(h=>!this.samePass(h));
    this.comparison.show(this.current);
    this.core.requestRender();
    if (notify) this.send();
  }

  // ---- markers and menus ----------------------------------------------------

  layout() {
    if (!this.data) return;
    this.layoutMarkers();
    const overlays=this.core.overlays,view=overlays.view;
    if(!this.choices.hidden && this.choiceAnchor)this.place("choices",this.choiceAnchor.left-view.left,this.choiceAnchor.bottom-view.top);
    if(!this.other.hidden && this.current) {
      const [px,py]=this.rowScreen(this.current.record,this.current.row);
      this.place("other",px-view.left,py-view.top);
    }
  }

  // Event markers must stay reachable while another marker's menu is open.
  place(name,sx,sy) {
    const overlays=this.core.overlays,card=overlays.sized(name);
    if(!card)return;
    const {w,h}=card,{width,height}=overlays.view;
    const candidates=[];
    for(const x of [sx+14,sx-w-14,this.core.layoutInset.left+GAP,width-w-GAP])for(const y of [sy+10,sy-h-10,GAP,height-h-this.core.layoutInset.bottom-GAP])candidates.push([x,y]);
    const seat=overlays.seat(candidates,w,h,{bounds:{left:8,top:8,right:width-8,bottom:height-this.core.layoutInset.bottom-GAP},
      obstacles:overlays.obstacles([...COVERS,"pathEvents"]),pad:8,cost:(x,y)=>Math.hypot(x-sx,y-sy)});
    overlays.place(card,seat.x,seat.y);
  }

  // Each marker sits nearest its moment, spiralling out from markers
  // already placed and from the panels; markers that would meet group into
  // one that counts them.
  layoutMarkers() {
    const overlays=this.core.overlays,view=overlays.view,radius=this.radius;
    const used=[],shown=new Set(),groups=[];
    const obstacles=overlays.obstacles(COVERS);
    for (const marker of this.markers) {
      if(marker.record===1 && !this.comparison.blockVisible(this.data.records[1].blocks[marker.block]))continue;
      const [px,py,pz]=this.rowScreen(marker.record,marker.row);
      const sx=px-view.left, sy=py-view.top;
      if (Math.abs(pz)>1 || sx<0 || sx>view.width || sy<0 || sy>view.height) continue;
      const group=groups.find(g=>Math.hypot(g.sx-sx,g.sy-sy)<g.radius+radius+4);
      if (group) group.members.push(marker);
      else groups.push({sx,sy,radius,members:[marker]});
    }
    for (const {sx,sy,members} of groups) {
      const marker=members[0]; marker.members=members;
      const button=marker.element;
      button.disabled=!this.available;
      const r=this.data.records[marker.record], block=r.blocks[marker.block];
      const selected=members.some(m=>m.record===this.current?.record && m.block===this.current?.block);
      setText(button,members.length>1 ? String(members.length) : /sleep|delay/.test(block.name)?"Ⅱ":/tool/.test(block.name)?"T":"↻");
      const title=members.length>1 ? `${members.length} events at this point` : `${r.label} · ${block.name} · line ${block.line}`;
      setAttr(button,"title",title);
      setAttr(button,"aria-label",title);
      setAttr(button,"aria-pressed",String(selected));
      setAttr(button,"data-lines",members.map(m=>this.data.records[m.record].blocks[m.block].line).join(" "));
      let seat=null;
      for (let ring=0;ring<20 && !seat;ring++) for (let k=0;k<(ring?16:1);k++) {
        const angle=k*Math.PI/8, x=sx+Math.cos(angle)*ring*(radius*2+4), y=sy+Math.sin(angle)*ring*(radius*2+4);
        if (x-radius<4 || x+radius>view.width-4 || y-radius<4 || y+radius>view.height-4) continue;
        if (used.some((b)=>Math.hypot(b.x-x,b.y-y)<b.radius+radius+4)) continue;
        if (overlap({left:x-radius,top:y-radius,right:x+radius,bottom:y+radius},obstacles,4)>0) continue;
        seat={x,y,radius}; break;
      }
      if (!seat) continue;
      shown.add(marker);
      used.push(seat);
      setHidden(button,false);
      setStyle(button,"left",`${Math.round(seat.x-radius)}px`); setStyle(button,"top",`${Math.round(seat.y-radius)}px`);
      const dx=seat.x-sx, dy=seat.y-sy, length=Math.hypot(dx,dy);
      setHidden(marker.leader,length<=radius*2);
      setStyle(marker.leader,"left",`${Math.round(sx)}px`); setStyle(marker.leader,"top",`${Math.round(sy)}px`);
      setStyle(marker.leader,"width",`${Math.round(Math.max(0,length-radius))}px`); setStyle(marker.leader,"transform",`rotate(${Math.atan2(dy,dx).toFixed(3)}rad)`);
    }
    for(const marker of this.markers)if(!shown.has(marker)){setHidden(marker.element,true);setHidden(marker.leader,true);}
    overlays.mark("pathEvents",used.map(({x,y,radius})=>{
      const left=Math.round(x-radius),top=Math.round(y-radius);
      return {left,top,right:left+radius*2,bottom:top+radius*2};
    }));
  }

  chooseEvents(members,anchor) {
    this.choices.replaceChildren();this.choices.hidden=false;
    this.choiceAnchor=anchor;
    const paired=new Set(members.map(m=>m.record)).size>1;
    for(const marker of members) {
      const r=this.data.records[marker.record],b=r.blocks[marker.block];
      const button=element("button","",this.choices,`${paired?`${r.label} · `:""}${b.name} · Line ${b.line} · ${reading(r.source[marker.row]*r.dt,2)} s`);
      button.setAttribute("aria-label",`${r.label} ${b.name} line ${b.line}`);
      button.dataset.pathRecord = marker.record; button.dataset.pathBlock = marker.block;
      button.onclick=()=>{
        this.choices.hidden=true;
        this.inspect({record:marker.record,block:marker.block,row:marker.row});this.send(true);this.core.canvas.focus({preventScroll:true});
      };
    }
    const view=this.core.overlays.view;
    this.place("choices",anchor.left-view.left,anchor.bottom-view.top);this.choices.firstElementChild?.focus({preventScroll:true});
  }

  choosePasses() {this.chooseEvents(this.alternatives,this.other.getBoundingClientRect());}

  send(force=false, pinned=true) {
    if (!this.current || !this.data) return;
    const elapsed=performance.now()-this.lastSend;
    if (!force && elapsed<80) {
      if (!this.timer) this.timer=setTimeout(()=>{this.timer=0;this.send(true);},80-elapsed);
      return;
    }
    clearTimeout(this.timer); this.timer=0; this.lastSend=performance.now();
    let {record,block,row}=this.current;
    // The arm shows what it will do: a point on the ideal path pins the
    // prediction paired with it, which the ghost of the ideal is set against.
    const paired=record===0 && this.comparison.visibility!=="off"?this.comparison.predictedRow(this.current):-1;
    const at=paired<0?-1:this.data.records[1].blocks.findIndex(b=>!b.instant && b.start<=paired && paired<b.end);
    if(at>=0){record=1;row=paired;block=at;}
    this.core.emit("inspect",{revision:this.data.revision,seq:++this.seq,record,block,row,pinned});
  }

  seekRow(row) {
    if (!this.current) return;
    const {record,block}=this.current,r=this.data.records[record];
    this.show({record,block,row:Math.max(0,Math.min(r.rows-1,row))},true); this.send(true);
  }

  follow(cursor) {
    if(!this.current || this.dragging || this.timer || !this.data || !this.core.ix.simulator)return;
    if(cursor.revision!==this.data.revision || (cursor.inspectionSeq ?? -1)<this.seq)return;
    const record=this.data.records[cursor.record];if(!record)return;
    // A point picked on the ideal path stays picked while the arm shows the
    // prediction paired with it.
    if(this.current.record===0 && cursor.record===1 && cursor.row===this.comparison.predictedRow(this.current))return;
    const row=Math.max(0,Math.min(record.rows-1,cursor.row));
    const ideal=this.data.records[0].rows;
    this.show({record:cursor.record,row,block:cursor.block ?? -1,idealRow:Math.max(0,Math.min(ideal-1,cursor.idealRow ?? row))},false);
  }

  // The shown sample goes; the app hears it is no longer shown.
  clear(notify=true) {
    if (this.dragging) { this.dragging = false; this.core.pointer.endDrag(); }
    if (this.current && notify) this.send(true, false);
    clearTimeout(this.timer); this.timer=0;
    this.current=null; this.choices.hidden=this.other.hidden=true;
    this.comparison.hidePair();
    if (this.cursor) this.cursor.visible=false;
    this.core.requestRender();
  }

  dispose() {
    this.sweep.dispose();
    this.comparison.dispose();
    this.clear(false); this.removeCursor(); this.hoverAt(null);
    this.grip.removeFromParent(); this.grip.geometry.dispose(); this.grip.material.dispose();
    for (const m of this.markers) { m.element.remove(); m.leader.remove(); }
    this.core.overlays.mark("pathEvents", null);
    this.choices.remove();this.other.remove();
    document.removeEventListener("pointerdown", this.onDocumentDown, true);
  }
}
