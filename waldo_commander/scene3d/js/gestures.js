// Gestures the browser proposes to the app: a ring drag, a gizmo drag, a
// joint turned while editing, a keep-out moved. Each event carries the epoch
// the browser last heard, the gesture's drag id and a sequence number. The
// app admits a gesture, follows its samples and ends it once.
//
// Samples go out at most 30 times a second. While a gesture is open and
// nothing else has gone out for KEEP_MS, a keep-alive does, so the app can
// end a drag whose browser went quiet. A dialog covering the view ends it.

const KEEP_MS = 100;
const SAMPLE_MS = 1000 / 30;

export class Gestures {
  constructor(core) {
    this.core = core;
    this.epoch = 0;
    this.next = 1;
    this.current = null;
    this.blocked = false;
  }

  get active() {
    return this.current !== null;
  }

  // A new gesture; `onAbort(reason)` stops whatever drives it when it ends
  // other than by its release.
  begin(kind, fields, pointerId, onAbort) {
    if (this.blocked) return null;
    if (this.current) this.abort("superseded");
    const g = { kind, drag: this.next++, seq: 0, pointerId, onAbort, ended: null, pending: null, last: 0, timer: 0, flushTimer: 0 };
    this.current = g;
    this.send(g, "begin", fields);
    g.timer = setInterval(() => {
      if (document.querySelector(".q-dialog__backdrop")) this.abort("dialog");
      else if (performance.now() - g.last >= KEEP_MS) this.send(g, "keep", {});
    }, KEEP_MS);
    return g;
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

  release(g, fields) {
    this.finish(g, "release", fields);
  }

  // End the open gesture other than by its release: its driver stops first,
  // then the app hears it was aborted.
  abort(reason) {
    const g = this.current;
    if (!g) return;
    if (g.onAbort) g.onAbort(reason);
    this.finish(g, reason, {});
  }

  // The app refused the gesture; it never started there, so nothing is sent.
  reject(drag) {
    const g = this.current;
    if (!g || g.drag !== drag) return;
    if (g.onAbort) g.onAbort("rejected");
    this.finish(g, "rejected", null);
  }

  finish(g, reason, fields) {
    if (!g || g.ended) return;
    g.ended = reason;
    clearInterval(g.timer);
    clearTimeout(g.flushTimer);
    g.pending = null;
    if (this.current === g) this.current = null;
    if (fields !== null) this.send(g, "end", { ...fields, aborted: reason !== "release" });
  }

  send(g, t, fields) {
    g.seq++;
    g.last = performance.now();
    this.core.emit("gesture", { t, kind: g.kind, epoch: this.epoch, drag: g.drag, seq: g.seq, ...fields });
  }

  setEpoch(epoch) {
    this.epoch = epoch;
    this.abort("epoch");
  }

  // Until the next reset or a restored context, no gesture starts.
  block(reason) {
    this.blocked = true;
    this.abort(reason);
  }

  unblock() {
    this.blocked = false;
  }
}
