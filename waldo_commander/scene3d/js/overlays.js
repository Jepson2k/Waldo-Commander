// Where the cards and markers the view draws over itself may sit: inside the
// view, clear of the app's panels around it, of the cards they must not
// cover, and of the handles drawn in the scene.
//
// The panels' boxes come with each `wc:layout`. Each frame measures the view
// and the cards' sizes once, before anything is written, so placing a card
// forces no layout. A card keeps clear of where the others were placed, this
// frame or the last. Positions are in pixels from the view's top left.

import * as THREE from "three";
import { setStyle } from "./dom.js";

export const GAP = 12;
// The cards Python builds over the view; the view places them.
const HOSTED = { teaching: ".wc-surface-teaching" };
const HOSTED_ANY = Object.values(HOSTED).join(",");

const point = new THREE.Vector3();
const corner = new THREE.Vector3();
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function overlap(box, obstacles, pad) {
  let area = 0;
  for (const o of obstacles) {
    const w = Math.min(box.right + pad, o.right) - Math.max(box.left - pad, o.left);
    const h = Math.min(box.bottom + pad, o.bottom) - Math.max(box.top - pad, o.top);
    if (w > 0 && h > 0) area += w * h;
  }
  return area;
}

export class Overlays {
  constructor(core) {
    this.core = core;
    this.layout = [];
    this.controls = new Map(); // name -> where the view's own controls are
    this.chrome = [];
    this.view = { left: 0, top: 0, width: 0, height: 0 };
    this.cards = new Map(); // name -> {node, w, h, scroll, offset, box}
    this.marks = new Map(); // name -> boxes in the scene others keep clear of
    this.readers = []; // what else a frame measures before writing
    // The app's cards arrive hidden and are seated by a frame; the view only
    // draws on demand, so a card added with nothing else changing asks for one.
    this.hostObserver = new MutationObserver((records) => {
      for (const record of records)
        for (const node of record.addedNodes)
          if (node.nodeType === 1 && (node.matches(HOSTED_ANY) || node.querySelector(HOSTED_ANY))) {
            core.requestRender();
            return;
          }
    });
    this.watched = null;
  }

  dispose() {
    this.hostObserver.disconnect();
  }

  // The panels' boxes, as `wc:layout` published them.
  follow(layout) {
    this.layout = layout.obstacles || [];
    this.relative();
  }

  relative() {
    const { left, top } = this.view;
    this.chrome = this.layout.map((b) => ({ left: b.left - left, top: b.top - top, right: b.right - left, bottom: b.bottom - top }));
    for (const box of this.controls.values()) this.chrome.push(box);
  }

  // One of the view's own controls, at `box` or gone: cards keep clear of it
  // as they do of the app's panels.
  control(name, box) {
    const was = this.controls.get(name);
    if (box ? was && was.left === box.left && was.top === box.top && was.right === box.right && was.bottom === box.bottom : !was) return;
    if (box) this.controls.set(name, { left: box.left, top: box.top, right: box.right, bottom: box.bottom });
    else this.controls.delete(name);
    this.relative();
  }

  // A card the view places.
  track(name, node) {
    this.cards.set(name, { node, w: 0, h: 0, scroll: 0, offset: [0, 0], box: null });
  }

  // A tracked card that is showing.
  card(name) {
    const card = this.cards.get(name);
    return card && card.w ? card : null;
  }

  // One of the view's own cards, shown this frame: the first frame it shows
  // it is measured here.
  sized(name) {
    const card = this.cards.get(name);
    if (!card) return null;
    if (!card.w && !card.node.hidden) {
      card.w = card.node.offsetWidth;
      card.h = card.node.offsetHeight;
      card.scroll = card.node.scrollHeight;
    }
    return card.w ? card : null;
  }

  measure() {
    this.framing = true;
    const view = this.core.root.getBoundingClientRect();
    const moved = view.left !== this.view.left || view.top !== this.view.top;
    this.view = view;
    if (moved) this.relative();
    const host = this.core.root.parentElement;
    if (host && host !== this.watched) {
      this.hostObserver.disconnect();
      this.hostObserver.observe(host, { childList: true, subtree: true });
      this.watched = host;
    }
    for (const [name, selector] of Object.entries(HOSTED)) {
      const node = host?.querySelector(selector) || null;
      if (this.cards.get(name)?.node === node) continue;
      if (node) this.track(name, node);
      else this.cards.delete(name);
    }
    for (const card of this.cards.values()) {
      const node = card.node;
      card.w = node.isConnected ? node.offsetWidth : 0;
      if (!card.w) {
        card.box = null;
        continue;
      }
      card.h = node.offsetHeight;
      card.scroll = node.scrollHeight;
      const parent = node.offsetParent?.getBoundingClientRect();
      card.offset = parent ? [parent.left - view.left, parent.top - view.top] : [0, 0];
    }
    for (const read of this.readers) read();
  }

  // Puts a card with its top left at (x, y).
  place(card, x, y) {
    x = Math.round(x);
    y = Math.round(y);
    setStyle(card.node, "left", `${x - card.offset[0]}px`);
    setStyle(card.node, "top", `${y - card.offset[1]}px`);
    card.box = { left: x, top: y, right: x + card.w, bottom: y + card.h };
  }

  mark(name, boxes) {
    if (boxes && boxes.length) this.marks.set(name, boxes);
    else this.marks.delete(name);
  }

  // What a card must keep clear of: the app's panels, the named cards and
  // marks, and any boxes given.
  obstacles(names = [], extra = []) {
    return [...this.chrome, ...this.boxes(names), ...extra];
  }

  // Where the named cards and marks are.
  boxes(names) {
    const out = [];
    for (const name of names) {
      const box = this.cards.get(name)?.box;
      if (box) out.push(box);
      const marks = this.marks.get(name);
      if (marks) out.push(...marks);
    }
    return out;
  }

  // Where cards may go: the view less a gap at its edges and what covers
  // its bottom.
  bounds() {
    return { left: GAP, top: GAP, right: this.view.width - GAP, bottom: this.view.height - this.core.layoutInset.bottom - GAP };
  }

  // The left edge of the view's free middle for a card from `top` to
  // `bottom`: past the panels on the left that it would meet.
  clearLeft(top, bottom) {
    let x = Math.max(0, this.core.layoutInset.left) + GAP;
    for (const b of this.chrome) if (b.left < this.view.width / 2 && b.top < bottom && b.bottom > top) x = Math.max(x, b.right + GAP);
    return x;
  }

  // The best of `candidates`, each [x, y] or [x, y, height] for a card `w`
  // wide: moved inside `bounds`, covering the obstacles least, then cheapest
  // by `cost(x, y, i, wanted)`, which by default keeps a candidate where it
  // was asked for and prefers the earlier ones.
  seat(candidates, w, h, options = {}) {
    return this.rank(candidates, w, h, options)[0];
  }

  // Every candidate as `seat` would weigh it, best first.
  rank(candidates, w, h, { bounds = this.bounds(), obstacles = this.chrome, cost = null, pad = GAP / 2 } = {}) {
    return candidates
      .map(([wx, wy, height = h], i) => {
        const x = clamp(wx, bounds.left, bounds.right - w);
        const y = clamp(wy, bounds.top, bounds.bottom - height);
        const covered = overlap({ left: x, top: y, right: x + w, bottom: y + height }, obstacles, pad);
        const score = (covered > 0 ? 1e6 + covered : 0) + (cost ? cost(x, y, i, wx, wy) : 10 * (Math.abs(x - wx) + Math.abs(y - wy)) + i);
        return { x, y, h: height, score, index: i, covered: covered > 0 };
      })
      .sort((p, q) => p.score - q.score);
  }

  // The free stretches of the column from `left` to `right`, top down.
  spans(left, right, obstacles, bounds = this.bounds(), pad = GAP * 2 / 3) {
    let free = [[bounds.top, bounds.bottom]];
    for (const o of obstacles) {
      if (right <= o.left - pad || left >= o.right + pad) continue;
      free = free.flatMap(([a, b]) =>
        o.bottom + pad <= a || o.top - pad >= b ? [[a, b]] : [[a, Math.min(b, o.top - pad)], [Math.max(a, o.bottom + pad), b]].filter(([a, b]) => b > a),
      );
    }
    return free;
  }

  // Where a world point lands; z beyond ±1 is off screen.
  project(world, out = [0, 0, 0]) {
    const [w, h] = this.core.size;
    point.copy(world).project(this.core.camera);
    out[0] = ((point.x + 1) * w) / 2;
    out[1] = ((1 - point.y) * h) / 2;
    out[2] = point.z;
    return out;
  }

  // The screen box of a world box's corners, grown by `pad`.
  projectBox(box, pad = 0) {
    const [w, h] = this.core.size;
    const out = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
    for (let i = 0; i < 8; i++) {
      corner.set(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z).project(this.core.camera);
      const x = ((corner.x + 1) * w) / 2;
      const y = ((1 - corner.y) * h) / 2;
      out.left = Math.min(out.left, x - pad);
      out.right = Math.max(out.right, x + pad);
      out.top = Math.min(out.top, y - pad);
      out.bottom = Math.max(out.bottom, y + pad);
    }
    return out;
  }

  // ---- Python's cards -----------------------------------------------------

  // The surface-teaching card and the workcell library sit top left, in
  // that order down, each as tall as the space below it allows.
  layoutTopLeft() {
    let top = GAP;
    for (const name of ["teaching"]) {
      const card = this.card(name);
      if (!card) continue;
      const left = this.clearLeft(top, top + card.h);
      setStyle(card.node, "maxWidth", `${Math.max(180, this.view.width - left - GAP)}px`);
      let bottom = this.view.height - this.core.layoutInset.bottom - GAP;
      for (const b of this.chrome) if (b.right > left && b.left < left + card.w && b.top > top) bottom = Math.min(bottom, b.top - GAP);
      setStyle(card.node, "maxHeight", `${Math.max(120, bottom - top)}px`);
      this.place(card, left, top);
      top += Math.min(card.h, Math.max(120, bottom - top)) + GAP;
    }
  }
}
