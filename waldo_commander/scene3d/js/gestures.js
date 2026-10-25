// Gestures the browser proposes to the app: a ring drag, a gizmo drag, a
// joint turned while editing, a keep-out moved. Each event carries the epoch
// the browser last heard, the gesture's drag id, a sequence number and the
// longest the page has lately been too busy to run anything, since it cannot
// report while it is. A software renderer blocks the page for a few hundred
// milliseconds a frame. The app admits a gesture, follows its samples and
// ends it once.
//
// Samples go out at most 30 times a second. While a gesture is open and
// nothing else has gone out for KEEP_MS, a keep-alive does, on a timer or
// with its pointer's next move, so the app can end a drag whose browser went
// quiet. A dialog covering the view ends it.
//
// The nodes a gesture moves are held: the app's state does not land on them
// while the drag runs, and after its release they keep what the drag showed
// until the app sends their state or refuses the drag. Any other end lets go
// at once, and they return to the app's state.

const KEEP_MS = 100;
const SAMPLE_MS = 1000 / 30;
// How late a timer due every PROBE_MS runs is how long the page was busy; a
// busy spell's length halves every BUSY_HALF_LIFE_MS after it.
const PROBE_MS = 100;
const BUSY_HALF_LIFE_MS = 2000;

export class Gestures {
  constructor(core) {
    this.core = core;
    this.epoch = 0;
    this.next = 1;
    this.current = null;
    this.blocks = new Set(); // why no gesture may start: "context lost", "disconnect"
    this.pending = new Map(); // drag -> a released gesture still holding nodes
    this.busyMs = 0;
    this.probed = performance.now();
    this.probe = setInterval(() => this.measureBusy(), PROBE_MS);
  }

  dispose() {
    clearInterval(this.probe);
  }

  measureBusy() {
    const now = performance.now();
    const fade = 0.5 ** ((now - this.probed) / BUSY_HALF_LIFE_MS);
    this.busyMs = Math.max(now - this.probed - PROBE_MS, this.busyMs * fade);
    this.probed = now;
  }

  // As busy as lately, or busier if the probe is overdue now.
  get busy() {
    return Math.max(this.busyMs, performance.now() - this.probed - PROBE_MS);
  }

  get active() {
    return this.current !== null;
  }

  get blocked() {
    return this.blocks.size > 0;
  }

  // A new gesture; `onAbort(reason)` stops whatever drives it when it ends
  // other than by its release.
  begin(kind, fields, pointerId, onAbort) {
    if (this.blocked) return null;
    if (this.current) this.abort("superseded");
    const g = { kind, drag: this.next++, seq: 0, pointerId, onAbort, ended: null, pending: null, last: 0, timer: 0, flushTimer: 0, held: [], settled: null };
    this.current = g;
    this.send(g, "begin", fields);
    g.timer = setInterval(() => {
      if (this.core.covered) this.abort("dialog");
      else this.heard();
    }, KEEP_MS);
    return g;
  }

  // The open gesture is still there. A page too busy drawing to run its
  // timers still hears the pointer, so a move counts as well as the timer.
  heard() {
    const g = this.current;
    if (g && performance.now() - g.last >= KEEP_MS) this.send(g, "keep", {});
  }

  // `rec`'s properties follow the drag, not the app, until the gesture ends.
  // A node has one hold entry at a time: a drag on what an earlier drag still
  // holds takes that entry over, holding before letting go so the node does
  // not jump back to the app's state in between.
  hold(g, rec, ...properties) {
    this.core.nodes.hold(rec, ...properties);
    const earlier = rec.held === g.drag ? g : rec.held !== null ? this.pending.get(rec.held) : null;
    const i = earlier ? earlier.held.findIndex(([r]) => r === rec) : -1;
    if (i >= 0) {
      const [[, previous]] = earlier.held.splice(i, 1);
      this.core.nodes.release(rec, ...previous);
      if (earlier !== g && !earlier.held.length) this.settle(earlier, true);
    }
    rec.held = g.drag;
    g.held.push([rec, properties]);
  }

  sample(g, fields) {
    if (!g || g.ended) return;
    g.pending = fields;
    const wait = g.last + SAMPLE_MS - performance.now();
    if (wait <= 0) this.flush(g);
    else if (!g.flushTimer)
      g.flushTimer = setTimeout(() => {
        g.flushTimer = 0;
        this.flush(g);
      }, wait);
  }

  flush(g) {
    if (!g.pending || g.ended) return;
    const fields = g.pending;
    g.pending = null;
    this.send(g, "move", fields);
  }

  // `settled(confirmed)` runs once the held nodes have the app's answer:
  // their state, or a refusal.
  release(g, fields, settled = null) {
    if (!g || g.ended) return;
    g.settled = settled;
    this.finish(g, "release", fields);
    if (g.held.length) this.pending.set(g.drag, g);
    else this.settle(g, true);
  }

  // End the open gesture other than by its release: its driver stops first,
  // then the app hears it was aborted.
  abort(reason) {
    const g = this.current;
    if (!g) return;
    if (g.onAbort) g.onAbort(reason);
    this.finish(g, reason, {});
  }

  // The app refused the gesture: an open one never started there, so nothing
  // is sent; a released one's nodes go back to the app's state.
  reject(drag) {
    const g = this.current;
    if (g && g.drag === drag) {
      if (g.onAbort) g.onAbort("rejected");
      this.finish(g, "rejected", null);
      return;
    }
    const released = this.pending.get(drag);
    if (released) this.letGo(released);
  }

  // The app sent `rec`'s state: what it showed for the drag gives way to it.
  confirm(rec) {
    const g = this.pending.get(rec.held);
    const i = g ? g.held.findIndex(([r]) => r === rec) : -1;
    if (i < 0) return;
    const [[, properties]] = g.held.splice(i, 1);
    rec.held = null;
    this.core.nodes.release(rec, ...properties);
    if (!g.held.length) this.settle(g, true);
  }

  letGo(g) {
    for (const [rec, properties] of g.held) {
      if (rec.held === g.drag) rec.held = null;
      this.core.nodes.release(rec, ...properties);
    }
    g.held = [];
    this.settle(g, false);
  }

  settle(g, confirmed) {
    this.pending.delete(g.drag);
    const settled = g.settled;
    g.settled = null;
    if (settled) settled(confirmed);
  }

  finish(g, reason, fields) {
    if (!g || g.ended) return;
    g.ended = reason;
    clearInterval(g.timer);
    clearTimeout(g.flushTimer);
    g.pending = null;
    if (this.current === g) this.current = null;
    if (reason !== "release") this.letGo(g);
    this.core.pointer.ended(g);
    if (fields !== null) this.send(g, "end", { ...fields, aborted: reason !== "release" });
  }

  send(g, t, fields) {
    g.seq++;
    g.last = performance.now();
    this.core.emit("gesture", { t, kind: g.kind, epoch: this.epoch, drag: g.drag, seq: g.seq, busy_ms: Math.round(this.busy), ...fields });
  }

  setEpoch(epoch) {
    this.epoch = epoch;
    this.abort("epoch");
  }

  // No gesture starts until every reason given here is withdrawn.
  block(reason) {
    this.blocks.add(reason);
    this.abort(reason);
  }

  unblock(reason) {
    this.blocks.delete(reason);
  }

  // The scene was sent again: no node is held for an answer any more.
  reset() {
    for (const g of this.pending.values()) g.held = [];
    this.pending.clear();
  }
}
