/* Animated robot face for the connection status indicator.
 *
 * Each mood (happy / neutral / sad) plays a table of idle behaviours on
 * random timers. Python drives reactions to robot events through
 * robotFaceReact(kind) (one-shot) and robotFaceHold(name, value) (held until
 * changed: E-STOP, jog look, recording light). A reaction interrupts whatever
 * idle is playing. Held state lives outside the Robot so it survives a mood
 * swap, which re-renders the SVG.
 */

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function rand(a, b) { return a + Math.random() * (b - a); }
function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
function logUnlessAbort(e) { if (e !== ABORT) console.error('robot face:', e); }

/* Thrown out of an awaited step when a reaction or hold change interrupts it. */
const ABORT = Symbol('robot-face-abort');
const EYE_R = 1.8;
const PUPIL_R = 0.75;
const DEFAULT_MOUTH = { happy: 'smile', neutral: 'flat', sad: 'frown' };

const LOOK_MIN_MS = 450;

const _held = { estop: false, look: null, recording: false };
let _current = null;
let _timers = [];
let _lookSince = 0;
let _lookTimer = null;
let _lastPrefix = null;

function shake(amp, n) {
  const k = [{ transform: 'translateX(0)' }];
  for (let i = 1; i < n; i++) {
    const a = (i % 2 ? amp : -amp) * (1 - i / n);
    k.push({ transform: `translateX(${a}px)` });
  }
  k.push({ transform: 'translateX(0)' });
  return k;
}

const HOP = [
  { transform: 'translateY(0) scale(1, 1)' },
  { transform: 'translateY(0.4px) scale(1.06, 0.92)', offset: 0.15 },
  { transform: 'translateY(-1.4px) scale(0.97, 1.04)', offset: 0.45 },
  { transform: 'translateY(0.3px) scale(1.05, 0.94)', offset: 0.8 },
  { transform: 'translateY(0) scale(1, 1)' },
];

const SWEAT_SLIDE = [
  { opacity: 0, transform: 'translateY(-0.3px)' },
  { opacity: 1, transform: 'translateY(0)', offset: 0.2 },
  { opacity: 1, transform: 'translateY(0.8px)', offset: 0.7 },
  { opacity: 0, transform: 'translateY(1.8px)' },
];

class Robot {
  constructor(prefix) {
    this.prefix = prefix;
    this.eyeL = this.part('eyeL');
    this.eyeR = this.part('eyeR');
    this.pupilL = this.part('pupilL');
    this.pupilR = this.part('pupilR');
    this.mouthContainer = this.part('mouth');
    this.antenna = this.part('antenna');
    this.rig = this.part('rig');
    this.svg = this.eyeL ? this.eyeL.ownerSVGElement : null;
    this.defaultMouth = DEFAULT_MOUTH[prefix];
    this.busy = false;
    this.epoch = 0;
    this.anims = new Set();
    this.recAnim = null;
    this._dur = new WeakMap();
    if (this.mouthContainer) {
      for (const c of this.mouthContainer.children) {
        c.style.transition = 'opacity 0.35s ease';
      }
    }
  }

  part(name) { return document.getElementById(`${this.prefix}-${name}`); }

  /* Eyes and pupils animate transform, radius and opacity independently. */
  transition(el, prop, seconds) {
    const d = this._dur.get(el) || { transform: 0.45, r: 0.3, opacity: 0.1 };
    d[prop] = seconds;
    this._dur.set(el, d);
    el.style.transition =
      `transform ${d.transform}s ease, r ${d.r}s ease, opacity ${d.opacity}s ease`;
  }

  radius(el, r, seconds) {
    if (!el) return;
    this.transition(el, 'r', seconds);
    el.style.setProperty('r', `${r}px`);
    el.setAttribute('r', r);
  }

  movePupils(dx, dy, speed = 0.45) {
    const t = `translate(${dx}px, ${dy}px)`;
    for (const el of [this.pupilL, this.pupilR]) {
      if (!el) continue;
      this.transition(el, 'transform', speed);
      el.style.transform = t;
    }
  }

  resetPupils(speed = 0.4) { this.movePupils(0, 0, speed); }

  setEyeSize(r, speed = 0.3) {
    this.radius(this.eyeL, r, speed);
    this.radius(this.eyeR, r, speed);
  }

  resetEyeSize(speed = 0.3) { this.setEyeSize(EYE_R, speed); }

  setPupilSize(r, speed = 0.25) {
    this.radius(this.pupilL, r, speed);
    this.radius(this.pupilR, r, speed);
  }

  eyeParts(which) {
    if (which === 'L') return [this.eyeL, this.pupilL];
    if (which === 'R') return [this.eyeR, this.pupilR];
    return [this.eyeL, this.eyeR, this.pupilL, this.pupilR];
  }

  hideEyes(which) {
    for (const el of this.eyeParts(which)) {
      if (!el) continue;
      this.transition(el, 'opacity', 0.06);
      el.style.opacity = '0';
    }
  }

  showEyes(speed = 0.1, which) {
    for (const el of this.eyeParts(which)) {
      if (!el) continue;
      this.transition(el, 'opacity', speed);
      el.style.opacity = '1';
    }
  }

  fx(name, on, fadeMs = 250) {
    const el = this.part(name);
    if (!el) return;
    el.style.transition = `opacity ${fadeMs}ms ease`;
    el.setAttribute('opacity', on ? '1' : '0');
  }

  setMouth(state, fadeMs = 300) {
    if (!this.mouthContainer) return;
    for (const c of this.mouthContainer.children) {
      c.style.transition = `opacity ${fadeMs}ms ease`;
      c.setAttribute('opacity', c.dataset.state === state ? '1' : '0');
    }
  }

  async wait(ms) {
    const epoch = this.epoch;
    await sleep(ms);
    if (epoch !== this.epoch) throw ABORT;
  }

  /* Web Animations keyframes on one element; resolves when done, throws ABORT
   * when interrupted. Under reduced motion the step only takes its time. */
  async play(el, frames, opts) {
    if (!el) return;
    if (reducedMotion() || typeof el.animate !== 'function') {
      await this.wait((opts.duration || 0) * (opts.iterations || 1) + (opts.delay || 0));
      return;
    }
    const epoch = this.epoch;
    const anim = el.animate(frames, opts);
    this.anims.add(anim);
    try {
      await anim.finished;
    } catch (e) {
      if (e.name !== 'AbortError') throw e;
    }
    this.anims.delete(anim);
    if (epoch !== this.epoch) throw ABORT;
  }

  spawn(el, frames, opts) { this.play(el, frames, opts).catch(logUnlessAbort); }

  async blink(duration = 120) {
    this.hideEyes();
    this.fx('blink', true, 0);
    await this.wait(duration);
    this.fx('blink', false, 0);
    this.showEyes(0.08);
  }

  async overlay(name, holdMs, fadeIn = 400, which) {
    this.hideEyes(which);
    this.fx(name, true, fadeIn);
    await this.wait(holdMs);
    this.fx(name, false, 300);
    this.showEyes(0.2, which);
  }

  wiggle(ms = 600) {
    return this.play(this.antenna, [
      { transform: 'rotate(0deg)' },
      { transform: 'rotate(3deg)', offset: 0.3 },
      { transform: 'rotate(-2deg)', offset: 0.65 },
      { transform: 'rotate(0deg)' },
    ], { duration: ms, easing: 'ease-in-out' });
  }

  droop(ms = 2500) {
    return this.play(this.antenna, [
      { transform: 'rotate(0deg) translateY(0)' },
      { transform: 'rotate(-2.5deg) translateY(0.5px)', offset: 0.4 },
      { transform: 'rotate(-2.5deg) translateY(0.5px)', offset: 0.6 },
      { transform: 'rotate(0deg) translateY(0)' },
    ], { duration: ms, easing: 'ease-in-out' });
  }

  async sparkle(duration = 1100) {
    const group = this.part('sparkle');
    if (!group) return;
    await Promise.all([
      this.play(group, [
        { opacity: 0 }, { opacity: 1, offset: 0.2 }, { opacity: 1, offset: 0.7 }, { opacity: 0 },
      ], { duration }),
      ...[...group.children].map((star, i) => this.play(star, [
        { transform: 'scale(0.3) rotate(0deg)' },
        { transform: 'scale(1.25) rotate(45deg)', offset: 0.4 },
        { transform: 'scale(0.8) rotate(90deg)' },
      ], { duration, delay: i * 120, easing: 'ease-out' })),
    ]);
  }

  /* Neutral pose: no overlays, default eyes and mouth, no running keyframes. */
  rest() {
    for (const anim of this.anims) anim.cancel();
    this.anims.clear();
    if (this.svg) {
      for (const el of this.svg.querySelectorAll('.face-fx')) el.setAttribute('opacity', '0');
    }
    if (this.rig) this.rig.style.transform = '';
    this.showEyes(0.15);
    this.resetEyeSize(0.2);
    this.setPupilSize(PUPIL_R, 0.2);
    this.resetPupils(0.3);
    this.setMouth(this.defaultMouth, 200);
  }

  interrupt() {
    this.epoch++;
    this.busy = false;
    this.rest();
  }

  applyHeld() {
    if (_held.estop) {
      this.setEyeSize(2.2, 0.15);
      this.setPupilSize(0.45, 0.15);
      this.setMouth('o', 150);
      this.fx('sweat', true, 200);
    } else if (_held.look) {
      const [dx, dy, tilt] = _held.look;
      this.movePupils(dx * 0.55, dy * 0.45, 0.25);
      if (this.rig && !reducedMotion()) {
        this.rig.style.transition = 'transform 0.3s ease';
        this.rig.style.transform = `rotate(${tilt || 0}deg)`;
      }
    }
    this.applyRecording();
  }

  applyRecording() {
    const el = this.part('rec');
    if (!el) return;
    if (_held.recording) {
      el.setAttribute('opacity', '1');
      if (!this.recAnim && !reducedMotion() && typeof el.animate === 'function') {
        this.recAnim = el.animate(
          [{ opacity: 1 }, { opacity: 0.2 }, { opacity: 1 }],
          { duration: 1200, iterations: Infinity, easing: 'ease-in-out' },
        );
      }
    } else {
      el.setAttribute('opacity', '0');
      if (this.recAnim) this.recAnim.cancel();
      this.recAnim = null;
    }
  }

  async perform(fn, calm = false) {
    if (this.busy || _held.estop || (_held.look && !calm)) return;
    const epoch = this.epoch;
    this.busy = true;
    try {
      await fn(this);
      await this.wait(500);
    } catch (e) {
      if (e !== ABORT) throw e;
    } finally {
      if (epoch === this.epoch) this.busy = false;
    }
  }

  async react(fn) {
    this.interrupt();
    const epoch = this.epoch;
    this.busy = true;
    try {
      await fn(this);
      this.rest();
      this.applyHeld();
      await this.wait(400);
    } catch (e) {
      if (e !== ABORT) throw e;
    } finally {
      if (epoch === this.epoch) this.busy = false;
    }
  }
}

/* ===== Idle behaviours =====
 * every: random re-fire interval [min, max] in ms.
 * calm:  still plays under prefers-reduced-motion and while the eyes follow a jog.
 * solo:  antenna-only, runs outside perform() but never over another idle.
 */
const IDLES = {
  happy: [
    { every: [2500, 5000], calm: true, run: r => r.blink(100) },
    {
      every: [8000, 14000], calm: true, run: async r => {
        await r.blink(80);
        await r.wait(120);
        await r.blink(80);
      },
    },
    {
      // Squint + grin
      every: [8000, 14000], run: async r => {
        r.setMouth('grin');
        await r.overlay('joy', 1800, 350);
        r.setMouth('smile');
      },
    },
    {
      // Excited: big eyes + open smile
      every: [12000, 20000], run: async r => {
        r.setEyeSize(2.2);
        r.setMouth('open-smile');
        await r.wait(1200);
        r.resetEyeSize();
        r.setMouth('smile');
      },
    },
    {
      // Glance around
      every: [9000, 16000], run: async r => {
        r.movePupils(-0.5, -0.35, 0.35);
        await r.wait(600);
        r.movePupils(0.5, -0.35, 0.45);
        await r.wait(600);
        r.resetPupils(0.35);
      },
    },
    {
      // Wink
      every: [12000, 22000], run: async r => {
        r.setMouth('grin', 150);
        await r.overlay('wink', 450, 120, 'L');
        r.setMouth('smile');
      },
    },
    {
      // Heart eyes
      every: [16000, 28000], run: async r => {
        r.setMouth('open-smile');
        r.hideEyes();
        r.fx('hearts', true, 150);
        const hearts = [...r.part('hearts').children];
        await Promise.all(hearts.map(h => r.play(h, [
          { transform: 'scale(0.2)' },
          { transform: 'scale(1.2)', offset: 0.25 },
          { transform: 'scale(1)', offset: 0.4 },
          { transform: 'scale(1.12)', offset: 0.6 },
          { transform: 'scale(1)', offset: 0.75 },
          { transform: 'scale(1.12)', offset: 0.9 },
          { transform: 'scale(1)' },
        ], { duration: 1600, easing: 'ease-out' })));
        r.fx('hearts', false, 250);
        r.showEyes(0.25);
        r.setMouth('smile');
      },
    },
    {
      // Hum a tune
      every: [14000, 26000], run: async r => {
        const note = r.part('note');
        const float = [
          { opacity: 0, transform: 'translate(0, 0.6px)' },
          { opacity: 1, transform: 'translate(-0.3px, -0.4px)', offset: 0.3 },
          { opacity: 1, transform: 'translate(0.2px, -1.4px)', offset: 0.7 },
          { opacity: 0, transform: 'translate(-0.2px, -2.2px)' },
        ];
        r.hideEyes();
        r.fx('joy', true, 300);
        r.setMouth('o', 250);
        r.spawn(r.rig, [
          { transform: 'rotate(0deg)' },
          { transform: 'rotate(-4deg)', offset: 0.25 },
          { transform: 'rotate(4deg)', offset: 0.75 },
          { transform: 'rotate(0deg)' },
        ], { duration: 2600, easing: 'ease-in-out' });
        await r.play(note, float, { duration: 1300, easing: 'ease-out' });
        await r.play(note, float, { duration: 1300, easing: 'ease-out' });
        r.fx('joy', false);
        r.showEyes(0.2);
        r.setMouth('smile');
      },
    },
    {
      // Hop
      every: [14000, 24000], run: async r => {
        r.setMouth('open-smile', 150);
        await r.play(r.rig, HOP, { duration: 520, easing: 'ease-in-out' });
        r.spawn(r.antenna, [
          { transform: 'rotate(0deg)' }, { transform: 'rotate(4deg)' },
          { transform: 'rotate(-3deg)' }, { transform: 'rotate(0deg)' },
        ], { duration: 520 });
        await r.play(r.rig, HOP, { duration: 520, easing: 'ease-in-out' });
        r.setMouth('smile');
      },
    },
    { every: [4000, 9000], solo: true, run: r => r.wiggle() },
  ],

  neutral: [
    { every: [3000, 6000], calm: true, run: r => r.blink(130) },
    {
      // Look left then right
      every: [5000, 10000], run: async r => {
        r.movePupils(-0.5, 0, 0.5);
        await r.wait(800);
        r.movePupils(0.5, 0, 0.6);
        await r.wait(800);
        r.resetPupils();
      },
    },
    {
      // Look up, thinking
      every: [8000, 15000], run: async r => {
        r.movePupils(0.3, -0.4, 0.5);
        r.setMouth('slant');
        await r.wait(1500);
        r.resetPupils();
        r.setMouth('flat');
      },
    },
    {
      // Suspicious squint + zigzag mouth
      every: [10000, 18000], run: async r => {
        r.setEyeSize(1.3);
        r.setMouth('zigzag');
        await r.wait(1600);
        r.resetEyeSize();
        r.setMouth('flat');
      },
    },
    {
      // Mouth twitch
      every: [6000, 11000], run: async r => {
        r.setMouth('slant');
        await r.wait(600);
        r.setMouth('flat');
      },
    },
    {
      // Yawn
      every: [18000, 30000], run: async r => {
        r.hideEyes();
        r.fx('blink', true, 200);
        r.setMouth('yawn', 500);
        await r.play(r.rig, [
          { transform: 'rotate(0deg) scale(1)' },
          { transform: 'rotate(-5deg) scale(1.03)', offset: 0.5 },
          { transform: 'rotate(0deg) scale(1)' },
        ], { duration: 1800, easing: 'ease-in-out' });
        r.setMouth('flat', 400);
        r.fx('blink', false, 0);
        r.showEyes(0.1);
        await r.wait(250);
        await r.blink(90);
      },
    },
    {
      // Unimpressed: half-lidded side-eye
      every: [14000, 24000], run: async r => {
        r.fx('lids', true, 300);
        r.movePupils(0.45, 0.35, 0.5);
        r.setMouth('slant');
        await r.wait(1800);
        r.fx('lids', false, 300);
        r.resetPupils();
        r.setMouth('flat');
      },
    },
    {
      // Processing...
      every: [12000, 22000], run: async r => {
        r.movePupils(0.4, -0.45, 0.4);
        const dots = [...r.part('dots').children];
        const pulse = [
          { opacity: 0, transform: 'translateY(0)' },
          { opacity: 1, transform: 'translateY(-0.5px)', offset: 0.35 },
          { opacity: 0, transform: 'translateY(0)' },
        ];
        for (let n = 0; n < 2; n++) {
          await Promise.all(dots.map((d, i) => r.play(d, pulse, {
            duration: 900, delay: i * 180, easing: 'ease-in-out',
          })));
        }
        r.resetPupils();
      },
    },
    {
      // Diagnostic scan
      every: [16000, 28000], run: async r => {
        r.setEyeSize(1.5, 0.2);
        await r.play(r.part('scan'), [
          { opacity: 0, transform: 'translateY(0)' },
          { opacity: 0.7, transform: 'translateY(1px)', offset: 0.1 },
          { opacity: 0.7, transform: 'translateY(8.6px)', offset: 0.9 },
          { opacity: 0, transform: 'translateY(9.55px)' },
        ], { duration: 1100, easing: 'ease-in-out' });
        r.resetEyeSize();
        await r.blink(90);
      },
    },
    {
      // Curious head tilt
      every: [12000, 20000], run: async r => {
        r.setEyeSize(2.05, 0.3);
        r.movePupils(0, -0.3, 0.3);
        await r.play(r.rig, [
          { transform: 'rotate(0deg)' },
          { transform: 'rotate(9deg)', offset: 0.25 },
          { transform: 'rotate(9deg)', offset: 0.75 },
          { transform: 'rotate(0deg)' },
        ], { duration: 1600, easing: 'ease-in-out' });
        r.resetEyeSize();
        r.resetPupils();
      },
    },
    { every: [7000, 14000], solo: true, run: r => r.wiggle(400) },
  ],

  sad: [
    { every: [4000, 8000], calm: true, run: r => r.blink(200) },
    {
      // Look down
      every: [5000, 10000], run: async r => {
        r.movePupils(0, 0.5, 0.6);
        await r.wait(1800);
        r.resetPupils(0.5);
      },
    },
    {
      // Look away
      every: [7000, 13000], run: async r => {
        r.movePupils(-0.5, 0.2, 0.7);
        await r.wait(1400);
        r.resetPupils(0.5);
      },
    },
    {
      // Droopy eyes + deep frown
      every: [8000, 15000], run: async r => {
        r.setMouth('deep-frown');
        await r.overlay('droop', 2200, 500);
        r.setMouth('frown');
      },
    },
    {
      // Watery big eyes + tremble mouth
      every: [12000, 20000], run: async r => {
        r.setEyeSize(2.1);
        r.setMouth('tremble');
        await r.wait(1800);
        r.resetEyeSize();
        r.setMouth('frown');
      },
    },
    {
      // A single tear
      every: [14000, 24000], run: async r => {
        r.setEyeSize(2.05, 0.4);
        r.movePupils(0, 0.25, 0.4);
        r.setMouth('tremble');
        await r.wait(400);
        await r.play(r.part('tear'), [
          { opacity: 0, transform: 'translateY(-0.4px) scale(0.6)' },
          { opacity: 0.95, transform: 'translateY(0) scale(1)', offset: 0.2 },
          { opacity: 0.9, transform: 'translateY(1.6px)', offset: 0.75 },
          { opacity: 0, transform: 'translateY(2.5px)' },
        ], { duration: 1800, easing: 'ease-in' });
        r.resetEyeSize();
        r.resetPupils();
        r.setMouth('frown');
      },
    },
    {
      // Doze off, then startle awake
      every: [20000, 34000], run: async r => {
        r.hideEyes();
        r.fx('blink', true, 400);
        const drift = [
          { opacity: 0, transform: 'translate(0, 0)' },
          { opacity: 1, transform: 'translate(0.3px, -0.6px)', offset: 0.3 },
          { opacity: 0, transform: 'translate(0.8px, -1.8px)' },
        ];
        r.spawn(r.rig, [
          { transform: 'translateY(0) rotate(0deg)' },
          { transform: 'translateY(0.6px) rotate(4deg)', offset: 0.3 },
          { transform: 'translateY(0.6px) rotate(4deg)', offset: 0.9 },
          { transform: 'translateY(0) rotate(0deg)' },
        ], { duration: 3600, easing: 'ease-in-out' });
        const zs = [...r.part('zzz').children];
        await Promise.all(zs.map((z, i) => r.play(z, drift, {
          duration: 1500, delay: i * 600, iterations: 2, easing: 'ease-out',
        })));
        r.fx('blink', false, 0);
        r.showEyes(0.1);
        r.setEyeSize(2.1, 0.12);
        await r.wait(350);
        r.resetEyeSize(0.4);
        await r.blink(150);
      },
    },
    {
      // Sigh
      every: [12000, 22000], run: async r => {
        r.setMouth('deep-frown');
        r.spawn(r.antenna, [
          { transform: 'rotate(0deg) translateY(0)' },
          { transform: 'rotate(-2.5deg) translateY(0.5px)', offset: 0.4 },
          { transform: 'rotate(-2.5deg) translateY(0.5px)', offset: 0.6 },
          { transform: 'rotate(0deg) translateY(0)' },
        ], { duration: 2200, easing: 'ease-in-out' });
        await r.play(r.rig, [
          { transform: 'translateY(0) scale(1, 1)' },
          { transform: 'translateY(0.7px) scale(1.02, 0.95)', offset: 0.4 },
          { transform: 'translateY(0.7px) scale(1.02, 0.95)', offset: 0.6 },
          { transform: 'translateY(0) scale(1, 1)' },
        ], { duration: 2200, easing: 'ease-in-out' });
        r.setMouth('frown');
      },
    },
    {
      // Shiver
      every: [16000, 26000], run: async r => {
        r.setMouth('tremble');
        r.setEyeSize(1.6);
        await r.play(r.rig, shake(0.28, 10), { duration: 700, easing: 'linear' });
        r.resetEyeSize();
        r.setMouth('frown');
      },
    },
    {
      // Hopeful glance up, then back down
      every: [14000, 24000], run: async r => {
        r.setEyeSize(2.05, 0.3);
        r.movePupils(0.45, -0.5, 0.4);
        r.setMouth('o');
        await r.play(r.antenna, [
          { transform: 'translateY(0) rotate(0deg)' },
          { transform: 'translateY(-0.5px) rotate(2deg)', offset: 0.3 },
          { transform: 'translateY(-0.5px) rotate(-1deg)', offset: 0.55 },
          { transform: 'translateY(-0.5px) rotate(0deg)', offset: 0.75 },
          { transform: 'translateY(0) rotate(0deg)' },
        ], { duration: 1500, easing: 'ease-in-out' });
        r.resetEyeSize();
        r.movePupils(0, 0.45, 0.6);
        r.setMouth('frown');
        await r.wait(900);
        r.resetPupils(0.5);
      },
    },
    { every: [6000, 12000], solo: true, run: r => r.droop() },
  ],
};

/* ===== Reactions to robot events ===== */
const REACTIONS = {
  async error(r) {
    r.hideEyes();
    r.fx('squeeze', true, 80);
    r.setMouth('zigzag', 120);
    r.spawn(r.part('sweat'), SWEAT_SLIDE, { duration: 1100, easing: 'ease-in' });
    await r.play(r.rig, shake(0.5, 8), { duration: 600, easing: 'linear' });
    await r.wait(500);
  },

  async failure(r) {
    await REACTIONS.error(r);
    r.fx('squeeze', false, 150);
    r.showEyes(0.2);
    r.movePupils(0, 0.5, 0.4);
    r.setMouth('o', 200);
    await r.droop(1400);
  },

  async warning(r) {
    r.setEyeSize(2.2, 0.12);
    r.setPupilSize(0.55, 0.12);
    r.setMouth('o', 150);
    r.spawn(r.part('sweat'), SWEAT_SLIDE, { duration: 1300, easing: 'ease-in' });
    r.movePupils(-0.5, 0, 0.15);
    await r.wait(250);
    r.movePupils(0.5, 0, 0.2);
    await r.wait(300);
    r.resetPupils(0.2);
    await r.wait(500);
  },

  async success(r) {
    r.hideEyes();
    r.fx('joy', true, 120);
    r.setMouth('grin', 150);
    r.sparkle(1200).catch(logUnlessAbort);
    await r.play(r.rig, HOP, { duration: 480, easing: 'ease-in-out' });
    await r.play(r.rig, HOP, { duration: 480, easing: 'ease-in-out' });
    await r.wait(400);
  },

  async start(r) {
    r.setEyeSize(1.55, 0.2);
    r.spawn(r.antenna, [
      { transform: 'rotate(0deg)' }, { transform: 'rotate(4deg)' },
      { transform: 'rotate(-3deg)' }, { transform: 'rotate(0deg)' },
    ], { duration: 500 });
    await r.play(r.rig, [
      { transform: 'translateY(0)' },
      { transform: 'translateY(0.7px)', offset: 0.4 },
      { transform: 'translateY(0)' },
    ], { duration: 450, easing: 'ease-in-out' });
    await r.wait(400);
  },

  async home(r) {
    for (let i = 0; i <= 8; i++) {
      const a = (i / 8) * 2 * Math.PI - Math.PI / 2;
      r.movePupils(0.5 * Math.cos(a), 0.45 * Math.sin(a), 0.09);
      await r.wait(90);
    }
    r.resetPupils(0.15);
    await r.play(r.rig, [
      { transform: 'translateY(0)' },
      { transform: 'translateY(0.6px)', offset: 0.35 },
      { transform: 'translateY(-0.2px)', offset: 0.7 },
      { transform: 'translateY(0)' },
    ], { duration: 500, easing: 'ease-in-out' });
  },

  async 'grip-close'(r) {
    r.setMouth('o', 60);
    await r.wait(160);
    r.setMouth('zigzag', 60);
    await r.play(r.rig, [
      { transform: 'scale(1, 1)' },
      { transform: 'scale(1.05, 0.94)', offset: 0.4 },
      { transform: 'scale(1, 1)' },
    ], { duration: 320, easing: 'ease-out' });
    await r.wait(350);
  },

  async 'grip-open'(r) {
    r.setEyeSize(2.1, 0.15);
    r.setMouth('o', 100);
    await r.wait(500);
  },

  async tool(r) {
    r.sparkle(1000).catch(logUnlessAbort);
    r.setMouth('o', 120);
    await r.play(r.rig, [
      { transform: 'rotate(0deg) scale(1)' },
      { transform: 'rotate(180deg) scale(0.85)', offset: 0.5 },
      { transform: 'rotate(360deg) scale(1)' },
    ], { duration: 650, easing: 'ease-in-out' });
    r.setMouth('grin', 150);
    await r.wait(500);
  },

  async cheese(r) {
    r.setEyeSize(2.2, 0.15);
    r.setMouth('grin', 150);
    await r.wait(450);
    await r.blink(80);
    await r.wait(200);
  },

  async shock(r) {
    r.setEyeSize(2.4, 0.08);
    r.setPupilSize(0.4, 0.08);
    r.setMouth('o', 80);
    r.fx('sweat', true, 150);
    r.spawn(r.antenna, [
      { transform: 'translateY(0) rotate(0deg)' },
      { transform: 'translateY(-0.8px) rotate(5deg)', offset: 0.25 },
      { transform: 'translateY(-0.4px) rotate(-4deg)', offset: 0.5 },
      { transform: 'translateY(0) rotate(0deg)' },
    ], { duration: 500, easing: 'ease-out' });
    await r.play(r.rig, [
      { transform: 'translateY(0) scale(1)' },
      { transform: 'translateY(-1px) scale(1.08)', offset: 0.3 },
      { transform: 'translateY(0) scale(1)' },
    ], { duration: 380, easing: 'ease-out' });
  },

  async relief(r) {
    r.setMouth('o', 100);
    r.hideEyes();
    r.fx('blink', true, 150);
    r.spawn(r.part('sweat'), SWEAT_SLIDE, { duration: 1100, easing: 'ease-in' });
    await r.play(r.rig, [
      { transform: 'translateY(0) scale(1, 1)' },
      { transform: 'translateY(0.6px) scale(1.03, 0.95)', offset: 0.5 },
      { transform: 'translateY(0) scale(1, 1)' },
    ], { duration: 900, easing: 'ease-in-out' });
    r.fx('blink', false, 0);
    r.showEyes(0.15);
    r.setMouth(r.defaultMouth, 250);
    await r.wait(300);
  },
};

function schedule(r, idle) {
  const [lo, hi] = idle.every;
  const slot = _timers.length;
  const tick = () => {
    if (_current === r && (idle.calm || !reducedMotion())) {
      if (!idle.solo) {
        r.perform(idle.run, idle.calm).catch(logUnlessAbort);
      } else if (!r.busy && !_held.estop && !_held.look) {
        idle.run(r).catch(logUnlessAbort);
      }
    }
    _timers[slot] = setTimeout(tick, rand(lo, hi));
  };
  _timers[slot] = setTimeout(tick, rand(lo, hi));
}

/**
 * Cancel all scheduled animations.
 */
window.stopRobotFace = function() {
  for (const id of _timers) clearTimeout(id);
  _timers = [];
  if (_current) _current.epoch++;
  _current = null;
};

/**
 * Initialize animations for a robot face. Retries briefly when the SVG has
 * not been mounted yet (a mood swap re-renders it through Vue).
 * @param {string} prefix - "happy", "neutral", or "sad"
 */
window.initRobotFace = function(prefix, retries = 20) {
  window.stopRobotFace();
  const r = new Robot(prefix);
  if (!r.eyeL) {
    if (retries > 0) {
      _timers.push(setTimeout(() => window.initRobotFace(prefix, retries - 1), 50));
    }
    return;
  }
  _current = r;
  r.setMouth(r.defaultMouth, 0);
  r.applyHeld();
  if (_lastPrefix && _lastPrefix !== prefix) {
    // Mood swap: the new face pops in.
    r.spawn(r.rig, [
      { transform: 'scale(0.5)', opacity: 0 },
      { transform: 'scale(1.12)', opacity: 1, offset: 0.6 },
      { transform: 'scale(1)', opacity: 1 },
    ], { duration: 450, easing: 'ease-out' });
  }
  _lastPrefix = prefix;
  for (const idle of IDLES[prefix] || []) schedule(r, idle);
};

/**
 * Play a one-shot reaction. Suppressed while the E-STOP face is held.
 * @param {string} kind - a key of REACTIONS
 */
window.robotFaceReact = function(kind) {
  const r = _current;
  const fn = REACTIONS[kind];
  if (!r || !fn || _held.estop) return;
  r.react(fn).catch(logUnlessAbort);
};

/**
 * Set a held face state.
 * @param {'estop'|'look'|'recording'} name
 * @param {boolean|number[]|null} value - look takes [dx, dy, tiltDeg] or null
 */
window.robotFaceHold = function(name, value) {
  if (!(name in _held)) return;
  if (name === 'look') {
    // A jog click releases within milliseconds; hold the glance long
    // enough to read before the eyes recenter.
    clearTimeout(_lookTimer);
    if (value) {
      _lookSince = performance.now();
    } else {
      const left = LOOK_MIN_MS - (performance.now() - _lookSince);
      if (left > 0) {
        _lookTimer = setTimeout(() => window.robotFaceHold('look', null), left);
        return;
      }
    }
  }
  const was = _held[name];
  _held[name] = value;
  const r = _current;
  if (!r) return;
  if (name === 'estop') {
    if (Boolean(was) !== Boolean(value)) {
      r.react(value ? REACTIONS.shock : REACTIONS.relief).catch(logUnlessAbort);
    }
  } else if (name === 'recording') {
    r.applyRecording();
    if (value && !was && !_held.estop) r.react(REACTIONS.cheese).catch(logUnlessAbort);
  } else if (!_held.estop) {
    r.interrupt();
    r.applyHeld();
  }
};

/* ===== Bouncing sad robot for the takeover overlay =====
 * DVD-screensaver-style: the face moves at a constant velocity and reflects
 * off the viewport edges and the centered card. raf-driven so the motion
 * stays smooth at any frame rate. The slow spin lives in CSS (.takeover-face
 * animation: takeover-spin), independent of position, so it composes with
 * the SVG's own breathing animation.
 */
window.startRobotMope = function() {
  // The face element may not be in the DOM yet when this runs (NiceGUI
  // dispatches run_javascript over the websocket; the DOM mount can lag
  // by a tick or two). Poll briefly before giving up.
  let tries = 0;
  function start() {
    const face = document.querySelector('.takeover-face');
    if (!face) {
      if (tries++ < 30) {
        setTimeout(start, 50);
      } else {
        console.warn('startRobotMope: .takeover-face not found');
      }
      return;
    }
    runMope(face);
  }
  start();
};

function runMope(face) {
  const FACE_SIZE = 96;
  const SPEED = 90;        // pixels per second
  const SPIN_DEG_PER_S = 30; // 360° every 12s, slow and steady
  const CARD_W = 460;      // approximate card footprint with breathing room
  const CARD_H = 320;

  function randomVelocity() {
    // Avoid axes — pick from a quadrant rotated by a random multiple of 90°.
    const angle = (0.15 + Math.random() * 0.7) * Math.PI / 2 +
                  Math.floor(Math.random() * 4) * Math.PI / 2;
    return { vx: Math.cos(angle) * SPEED, vy: Math.sin(angle) * SPEED };
  }

  function randomStart() {
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    const cardL = cx - CARD_W / 2, cardR = cx + CARD_W / 2;
    const cardT = cy - CARD_H / 2, cardB = cy + CARD_H / 2;
    for (let i = 0; i < 32; i++) {
      const px = Math.random() * (window.innerWidth - FACE_SIZE);
      const py = Math.random() * (window.innerHeight - FACE_SIZE);
      const intersects = px < cardR && px + FACE_SIZE > cardL &&
                         py < cardB && py + FACE_SIZE > cardT;
      if (!intersects) return { x: px, y: py };
    }
    return { x: 40, y: 40 };
  }

  let { x, y } = randomStart();
  let { vx, vy } = randomVelocity();
  let angle = 0;        // degrees
  let lastTime = null;

  // Initial paint so we don't flash at (0,0).
  face.style.transform = `translate(${x}px, ${y}px) rotate(${angle}deg)`;

  function step(now) {
    if (lastTime === null) {
      lastTime = now;
      requestAnimationFrame(step);
      return;
    }
    // Cap dt so a backgrounded tab doesn't teleport the face on resume.
    const dt = Math.min(0.05, (now - lastTime) / 1000);
    lastTime = now;

    let nx = x + vx * dt;
    let ny = y + vy * dt;
    angle = (angle + SPIN_DEG_PER_S * dt) % 360;

    // Viewport edges.
    const maxX = window.innerWidth - FACE_SIZE;
    const maxY = window.innerHeight - FACE_SIZE;
    if (nx < 0)    { nx = 0;    vx = Math.abs(vx); }
    if (nx > maxX) { nx = maxX; vx = -Math.abs(vx); }
    if (ny < 0)    { ny = 0;    vy = Math.abs(vy); }
    if (ny > maxY) { ny = maxY; vy = -Math.abs(vy); }

    // Card collision: push out along the axis of shallowest penetration.
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    const cardL = cx - CARD_W / 2, cardR = cx + CARD_W / 2;
    const cardT = cy - CARD_H / 2, cardB = cy + CARD_H / 2;
    const overlapsCard = nx < cardR && nx + FACE_SIZE > cardL &&
                         ny < cardB && ny + FACE_SIZE > cardT;
    if (overlapsCard) {
      const penL = (nx + FACE_SIZE) - cardL;
      const penR = cardR - nx;
      const penT = (ny + FACE_SIZE) - cardT;
      const penB = cardB - ny;
      const minPen = Math.min(penL, penR, penT, penB);
      if (minPen === penL)      { nx = cardL - FACE_SIZE; vx = -Math.abs(vx); }
      else if (minPen === penR) { nx = cardR;             vx =  Math.abs(vx); }
      else if (minPen === penT) { ny = cardT - FACE_SIZE; vy = -Math.abs(vy); }
      else                      { ny = cardB;             vy =  Math.abs(vy); }
    }

    x = nx;
    y = ny;
    // Single transform string drives both translate and rotate so nothing
    // can race or get overridden by the browser's animation engine.
    face.style.transform = `translate(${x}px, ${y}px) rotate(${angle}deg)`;

    requestAnimationFrame(step);
  }

  requestAnimationFrame(step);
}
