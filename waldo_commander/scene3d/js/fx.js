// Effects Python asks for by node: a path drawing in, a marker popping in,
// a glow, a collision flash, a fade, a ripple. The browser runs them on its
// own frame clock, so an effect costs one op instead of a stream of poses.
//
// An effect holds the properties it animates. Python's ops meanwhile change
// only the node's base value, and the effect ends on the base, so a value
// Python set during it is the one left showing. Nothing runs under reduced
// motion.

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
const easeOutBack = (t) => {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
};

// A ripple wave: sin(t / RIPPLE_RATE), staggered RIPPLE_STAGGER per object.
const RIPPLE_RATE = 260;
const RIPPLE_STAGGER = 0.55;
const RIPPLE_WAVES = 2;

export class Fx {
  constructor(core) {
    this.core = core;
    this.tweens = new Set();
    this.pulse = null;
    this.pulseCall = 0;
    // A material's own emissive while effects glow it.
    this.emissive = new WeakMap();
  }

  get active() {
    return this.tweens.size > 0 || this.pulse !== null;
  }

  // A node, once its mesh has loaded; null if that takes longer than `waitMs`.
  async ready(id, waitMs) {
    const rec = this.core.nodes.get(id);
    if (!rec) return null;
    const loaded = await Promise.race([rec.ready, new Promise((r) => setTimeout(() => r(false), waitMs))]);
    return loaded && !rec.deleted ? rec : null;
  }

  run(name, args) {
    if (reducedMotion()) {
      if (name === "pulse") this.stopPulse();
      return;
    }
    const effect = {
      reveal: () => this.reveal(...args),
      flash: () => this.each(args[0], (rec) => this.glowPop(rec), 750),
      alarm: () => this.each(args[0], (rec) => this.alarm(rec, args[1]), 700),
      fade: () => this.each(args[0], (rec) => this.fadeIn(rec, args[1]), args[1]),
      pulse: () => this.startPulse(args[0]),
    }[name];
    if (effect) effect();
  }

  async each(ids, start, waitMs) {
    const recs = await Promise.all(ids.map((id) => this.ready(id, waitMs)));
    for (const rec of recs) if (rec) start(rec);
  }

  tween(delayMs, ms, step, done) {
    const tw = { start: performance.now() + delayMs, ms, step, done };
    step(0);
    this.tweens.add(tw);
    this.core.animate();
  }

  // One frame of every running effect; whether any is still running.
  step(now) {
    for (const tw of this.tweens) {
      if (now < tw.start) continue;
      const t = Math.min(1, (now - tw.start) / tw.ms);
      tw.step(t);
      if (t >= 1) {
        this.tweens.delete(tw);
        if (tw.done) tw.done();
      }
    }
    if (this.pulse) {
      const pulse = this.pulse;
      if (now >= pulse.until) this.stopPulse();
      else {
        for (const p of pulse.items) {
          // From a trough, so each object starts and ends at its own size.
          const a = (now - pulse.start) / RIPPLE_RATE - p.phase;
          const s = a < 0 || a > RIPPLE_WAVES * 2 * Math.PI ? -1 : Math.sin(a - Math.PI / 2);
          p.rec.obj.scale.fromArray(p.rec.base.s).multiplyScalar(1 + 0.45 * Math.max(0, s) ** 3);
        }
      }
    }
    return this.active;
  }

  hold(rec, property) {
    rec.holds[property]++;
  }

  release(rec, property) {
    if (--rec.holds[property] > 0 || rec.deleted) return;
    if (property === "scale") rec.obj.scale.fromArray(rec.base.s);
    if (property === "position") rec.obj.position.fromArray(rec.base.p);
  }

  reveal(segments, markers) {
    const lineMs = 380;
    segments.forEach(async (ids, k) => {
      const delay = Math.min(k * 70, 700);
      const [line, ...cones] = await Promise.all(ids.map((id) => this.ready(id, lineMs)));
      if (line) this.drawIn(line, delay, lineMs);
      cones.forEach((cone, j) => {
        if (cone) this.popIn(cone, delay + ((j + 1) / (cones.length + 1)) * lineMs, 260);
      });
    });
    markers.forEach(async (id, k) => {
      const rec = await this.ready(id, 420);
      if (rec) this.popIn(rec, Math.min(k * 50, 500) + 150, 420);
    });
  }

  drawIn(rec, delayMs, ms) {
    const g = rec.drawable && rec.drawable.geometry;
    const n = g && g.attributes.position ? g.attributes.position.count : 0;
    if (!n) return;
    this.tween(
      delayMs,
      ms,
      (t) => g.setDrawRange(0, t <= 0 ? 0 : Math.max(2, Math.ceil(n * easeOutCubic(t)))),
      () => g.setDrawRange(0, Infinity),
    );
  }

  popIn(rec, delayMs, ms) {
    this.hold(rec, "scale");
    const scale = rec.obj.scale;
    this.tween(
      delayMs,
      ms,
      (t) => scale.fromArray(rec.base.s).multiplyScalar(Math.max(1e-3, easeOutBack(t))),
      () => this.release(rec, "scale"),
    );
  }

  emissives(rec) {
    const mats = [];
    rec.obj.traverse((child) => {
      const m = child.material;
      if (!m || !m.emissive) return;
      let own = this.emissive.get(m);
      if (!own) this.emissive.set(m, (own = { value: m.emissive.clone(), holds: 0 }));
      own.holds++;
      mats.push({ m, own });
    });
    return mats;
  }

  restoreEmissives(mats) {
    for (const { m, own } of mats) {
      m.emissive.copy(own.value);
      if (--own.holds === 0) this.emissive.delete(m);
    }
  }

  glowPop(rec) {
    this.hold(rec, "scale");
    const mats = this.emissives(rec);
    this.tween(
      0,
      750,
      (t) => {
        const k = t < 0.3 ? 0.88 + 0.2 * easeOutCubic(t / 0.3) : 1.08 - 0.08 * easeOutCubic((t - 0.3) / 0.7);
        rec.obj.scale.fromArray(rec.base.s).multiplyScalar(k);
        const glow = 0.55 * (1 - easeOutCubic(t));
        for (const { m, own } of mats) m.emissive.copy(own.value).addScalar(glow);
      },
      () => {
        this.release(rec, "scale");
        this.restoreEmissives(mats);
      },
    );
  }

  // Two hard emissive flashes in `color`, e.g. on geometry that just collided.
  alarm(rec, color) {
    const mats = this.emissives(rec);
    if (!mats.length) return;
    const hot = mats[0].own.value.clone().set(color);
    this.tween(
      0,
      700,
      (t) => {
        const k = Math.max(0, Math.sin(t * 2 * Math.PI * 2 - Math.PI / 2) * 0.5 + 0.5) * (1 - t);
        for (const { m, own } of mats) m.emissive.copy(own.value).lerp(hot, k);
      },
      () => this.restoreEmissives(mats),
    );
  }

  fadeIn(rec, ms) {
    const mats = rec.drawable ? [rec.drawable.material].flat() : [];
    if (!mats.length) return;
    this.hold(rec, "opacity");
    const own = () => (rec.base.m ? rec.base.m[1] : 1);
    // Blending is compiled into the material's program, so flipping
    // transparency needs a recompile.
    const transparent = mats.map((m) => m.transparent);
    const setTransparent = (m, on) => {
      if (m.transparent !== on) {
        m.transparent = on;
        m.needsUpdate = true;
      }
    };
    this.tween(
      0,
      ms,
      (t) => {
        const k = easeOutCubic(t);
        for (const m of mats) {
          setTransparent(m, true);
          m.opacity = own() * k;
        }
      },
      () => {
        if (--rec.holds.opacity > 0) return;
        mats.forEach((m, i) => {
          m.opacity = own();
          setTransparent(m, transparent[i]);
        });
      },
    );
  }

  // Ripple a set of nodes in order, a few waves travelling along them; the
  // next pulse replaces the set, and an empty one stops it.
  async startPulse(ids) {
    const call = ++this.pulseCall;
    this.stopPulse();
    if (!ids.length) return;
    const recs = await Promise.all(ids.map((id) => this.ready(id, 2000)));
    if (call !== this.pulseCall) return;
    this.stopPulse();
    const items = [];
    recs.forEach((rec, i) => {
      if (!rec) return;
      this.hold(rec, "scale");
      items.push({ rec, phase: i * RIPPLE_STAGGER });
    });
    if (!items.length) return;
    const start = performance.now();
    const span = RIPPLE_WAVES * 2 * Math.PI + (items.length - 1) * RIPPLE_STAGGER;
    this.pulse = { items, start, until: start + span * RIPPLE_RATE };
    this.core.animate();
  }

  stopPulse() {
    if (!this.pulse) return;
    for (const p of this.pulse.items) this.release(p.rec, "scale");
    this.pulse = null;
    this.core.requestRender();
  }

  // A node is gone: its effects stop touching it.
  forget(rec) {
    if (this.pulse) this.pulse.items = this.pulse.items.filter((p) => p.rec !== rec);
  }
}
