<template>
  <div
    class="robot-buddy"
    :class="rootClasses"
    :style="rootStyle"
    @click="onPoke"
  >
    <svg viewBox="3 3 18 16" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <clipPath v-for="e in EYES" :key="e.side" :id="uid + e.side">
          <circle :cx="e.cx" cy="12" r="1.8" />
        </clipPath>
      </defs>
      <g class="bb-pose" :style="poseStyle">
        <g class="bb-rig" :class="rigAnim ? 'bb-anim-' + rigAnim : ''">
          <g
            v-for="a in ANTENNAE"
            :key="a.side"
            class="bb-antenna"
            :class="'bb-antenna-' + a.side + (antennaWave === a.side ? ' bb-wave' : '')"
            :style="{ transformOrigin: a.x + 'px 7.5px', transform: 'rotate(' + antenna[a.side] + 'deg)' }"
          >
            <rect :x="a.x - 0.5" y="4.5" width="1" height="3" rx="0.5" fill="currentColor" />
            <circle class="bb-halo" :cx="a.x" cy="4.2" r="1.6" />
            <circle class="bb-bulb" :cx="a.x" cy="4.2" r="0.8" fill="currentColor" />
            <circle class="bb-led" :cx="a.x" cy="4.2" r="0.45" />
          </g>

          <rect x="4.5" y="8.5" width="15" height="10" rx="2" fill="currentColor" />
          <rect class="bb-gloss" x="5.6" y="9.3" width="4.4" height="0.6" rx="0.3" />

          <g class="bb-eyes">
            <g class="bb-eyes-open" :style="{ opacity: eyes === 'open' || eyes === 'wink' ? 1 : 0 }">
              <g
                v-for="e in EYES"
                :key="e.side"
                class="bb-eye"
                :style="{
                  transformOrigin: e.cx + 'px 12px',
                  transform: 'scale(' + eyeScale + ')',
                  opacity: e.side === 'R' && eyes === 'wink' ? 0 : 1,
                }"
              >
                <circle class="bb-cut" :cx="e.cx" cy="12" r="1.8" />
                <g class="bb-pupil" :style="pupilStyle">
                  <circle
                    :cx="e.cx"
                    cy="12"
                    r="0.75"
                    fill="currentColor"
                    :style="{ transformOrigin: e.cx + 'px 12px', transform: 'scale(' + pupilScale + ')' }"
                  />
                </g>
                <g :clip-path="'url(#' + uid + e.side + ')'">
                  <rect
                    class="bb-lid"
                    :x="e.cx - 3"
                    y="6"
                    width="6"
                    height="6"
                    fill="currentColor"
                    :style="lidStyle(e)"
                  />
                </g>
              </g>
            </g>
            <g class="bb-eyes-alt bb-eyes-happy bb-stroke" :style="{ opacity: eyes === 'happy' ? 1 : 0 }">
              <path d="M6.2 12.4 Q8 10.5 9.8 12.4" />
              <path d="M14.2 12.4 Q16 10.5 17.8 12.4" />
            </g>
            <g class="bb-eyes-alt bb-eyes-wink bb-stroke" :style="{ opacity: eyes === 'wink' ? 1 : 0 }">
              <path d="M14.2 12.4 Q16 10.5 17.8 12.4" />
            </g>
            <g class="bb-eyes-alt bb-eyes-closed bb-stroke" :style="{ opacity: eyes === 'closed' ? 1 : 0 }">
              <path d="M6.2 11.8 Q8 13.5 9.8 11.8" />
              <path d="M14.2 11.8 Q16 13.5 17.8 11.8" />
            </g>
            <g class="bb-eyes-alt bb-eyes-x bb-stroke" :style="{ opacity: eyes === 'x' ? 1 : 0 }">
              <path d="M6.9 10.9 L9.1 13.1 M9.1 10.9 L6.9 13.1" />
              <path d="M14.9 10.9 L17.1 13.1 M17.1 10.9 L14.9 13.1" />
            </g>
            <g class="bb-eyes-alt bb-eyes-scan" :style="{ opacity: eyes === 'scan' ? 1 : 0 }">
              <rect class="bb-cut" x="5.8" y="11" width="12.4" height="2" rx="1" opacity="0.22" />
              <rect class="bb-scan-bar bb-cut" x="5.8" y="11" width="2.6" height="2" rx="1" />
            </g>
          </g>

          <g class="bb-mouth">
            <path class="bb-stroke" :style="m('smile')" d="M9 15.5 Q12 17.8 15 15.5" />
            <path class="bb-stroke" :style="m('grin')" stroke-width="1.2" d="M8.2 15 Q12 18.8 15.8 15" />
            <path class="bb-fill" :style="m('open')" d="M9 15.2 Q12 18.2 15 15.2 Z" />
            <path class="bb-stroke" :style="m('flat')" d="M9 16 H15" />
            <path class="bb-stroke" :style="m('slant')" d="M9 16.3 L15 15.7" />
            <path class="bb-stroke" :style="m('zigzag')" stroke-width="0.8" d="M9 16 L10.2 15.2 L11.4 16.8 L12.6 15.2 L13.8 16.8 L15 16" />
            <path class="bb-stroke" :style="m('frown')" d="M9 16.8 Q12 14.5 15 16.8" />
            <path class="bb-stroke" :style="m('deep-frown')" stroke-width="1.2" d="M9.5 17.2 Q12 13.5 14.5 17.2" />
            <path class="bb-stroke" :style="m('wavy')" stroke-width="0.8" d="M9 16.2 Q10 15.3 11 16.2 T13 16.2 T15 16.2" />
            <ellipse class="bb-fill" :style="m('o')" cx="12" cy="16.1" rx="0.9" ry="1" />
            <ellipse class="bb-fill" :style="m('small-o')" cx="12" cy="16.2" rx="0.5" ry="0.55" />
            <ellipse class="bb-fill" :style="m('yawn')" cx="12" cy="16.2" rx="1.3" ry="1.55" />
          </g>
        </g>
      </g>

      <g class="bb-fx">
        <g v-if="fx.zzz">
          <g v-for="(z, i) in ZZZ" :key="i" :transform="'translate(' + z.x + ' ' + z.y + ') scale(' + z.s + ')'">
            <path class="bb-glyph bb-z" :style="{ animationDelay: i * 0.9 + 's' }" d="M0 0 H1.4 L0 1.6 H1.4" />
          </g>
        </g>
        <g v-if="fx.exclaim" transform="translate(20.6 4.6)">
          <g class="bb-pop">
            <rect class="bb-alert" x="-0.45" y="-2.3" width="0.9" height="2.4" rx="0.45" />
            <circle class="bb-alert" cx="0" cy="1" r="0.5" />
          </g>
        </g>
        <g v-if="fx.question" transform="translate(20.4 5)">
          <g class="bb-pop">
            <path class="bb-glyph" d="M-0.8 -1.3 Q-0.8 -2.5 0.2 -2.5 Q1.2 -2.5 1.1 -1.5 Q1 -0.8 0.2 -0.5 L0.2 0.2" />
            <circle class="bb-glyph-dot" cx="0.2" cy="1.1" r="0.32" />
          </g>
        </g>
      </g>
    </svg>
  </div>
</template>

<script>
// Resting face per mood. Reactions animate away from this and settle back.
const MOODS = {
  happy: { eyes: "open", mouth: "smile", lid: 0, tilt: 0, eyeScale: 1, pupilScale: 1, look: [0, 0], leds: "" },
  neutral: { eyes: "open", mouth: "flat", lid: 0.08, tilt: 0, eyeScale: 1, pupilScale: 1, look: [0, 0], leds: "" },
  sad: { eyes: "open", mouth: "frown", lid: 0.32, tilt: 16, eyeScale: 1, pupilScale: 1, look: [0, 0.3], leds: "" },
  alarmed: { eyes: "open", mouth: "o", lid: 0, tilt: 0, eyeScale: 1.2, pupilScale: 0.55, look: [0, 0], leds: "alarm" },
  booting: { eyes: "scan", mouth: "flat", lid: 0, tilt: 0, eyeScale: 1, pupilScale: 1, look: [0, 0], leds: "pulse" },
};

// Moods that follow the pointer, doze off, and answer pokes.
const LIVELY = new Set(["happy", "neutral", "sad"]);

const BLINK_GAP_S = { happy: [2.5, 5], neutral: [3, 6], sad: [4, 8], alarmed: [1.5, 3] };
const IDLE_GAP_S = { happy: [3, 7], neutral: [3.5, 8], sad: [4, 9], alarmed: [1.2, 2.5] };

const TRACK_RADIUS_PX = 360;
const POKE_WINDOW_MS = 1600;
const POKES_TO_DIZZY = 4;

const rand = (a, b) => a + Math.random() * (b - a);
const pick = (weighted) => {
  let r = Math.random() * weighted.reduce((sum, [w]) => sum + w, 0);
  for (const [w, fn] of weighted) {
    if ((r -= w) <= 0) return fn;
  }
  return weighted[weighted.length - 1][1];
};

class Superseded extends Error {}

const ignoreSuperseded = (e) => {
  if (!(e instanceof Superseded)) throw e;
};

let nextUid = 0;

export default {
  props: {
    mood: { type: String, default: "happy" },
    color: { type: String, default: null },
    busy: { type: Boolean, default: false },
    interactive: { type: Boolean, default: false },
    sleepAfter: { type: Number, default: 0 },
    reaction: { type: Object, default: null },
    roam: { type: Boolean, default: false },
    roamAvoid: { type: String, default: "" },
    calm: { type: Boolean, default: false },
    light: { type: String, default: "" },
  },

  data() {
    return {
      uid: "bb" + nextUid++,
      EYES: [
        { side: "L", cx: 8 },
        { side: "R", cx: 16 },
      ],
      ANTENNAE: [
        { side: "l", x: 7.5 },
        { side: "r", x: 16.5 },
      ],
      ZZZ: [
        { x: 19.6, y: 7.4, s: 0.7 },
        { x: 20.9, y: 5.3, s: 0.9 },
        { x: 22.4, y: 2.9, s: 1.1 },
      ],
      eyes: "open",
      mouth: "smile",
      lid: 0,
      tilt: 0,
      lidSpeed: 0.25,
      eyeScale: 1,
      pupilScale: 1,
      look: [0, 0],
      lookSpeed: 0.4,
      leds: "",
      antenna: { l: 0, r: 0 },
      antennaWave: "",
      rigAnim: "",
      headTilt: 0,
      sink: 0,
      fx: { zzz: false, exclaim: false, question: false },
      asleep: false,
    };
  },

  computed: {
    rootClasses() {
      return [
        "bb-mood-" + this.mood,
        this.leds ? "bb-leds-" + this.leds : "",
        this.asleep ? "bb-asleep" : "",
        this.calm ? "bb-calm" : "",
        this.roam ? "bb-roam" : "",
      ];
    },
    rootStyle() {
      return this.color ? { "--bb-color": this.color } : {};
    },
    poseStyle() {
      return { transform: "translateY(" + this.sink + "px) rotate(" + this.headTilt + "deg)" };
    },
    pupilStyle() {
      return {
        transform: "translate(" + this.look[0] + "px, " + this.look[1] + "px)",
        transitionDuration: this.lookSpeed + "s",
      };
    },
  },

  watch: {
    mood(now, before) {
      this.asleep = false;
      this.fx.zzz = false;
      this.scheduleBlink();
      this.scheduleIdle();
      if (before === "alarmed") this.react("relief");
      else if (now === "alarmed") this.react("jolt");
      else if (now === "happy") this.react("cheer");
      else if (now === "sad") this.react("slump");
      else if (!this.acting) this.restFace();
    },
    busy() {
      if (!this.acting) this.restFace();
    },
    light() {
      if (!this.acting) this.restFace();
    },
    calm(now) {
      if (now) {
        this.tracking = false;
        this.asleep = false;
        if (!this.acting) this.restFace();
      }
      this.scheduleIdle();
    },
    sleepAfter(now) {
      if (now <= 0 && this.asleep) {
        this.asleep = false;
        this.react("wake");
      }
    },
    // NiceGUI resends every prop on each update, so a fresh object alone is
    // not a new reaction; the server bumps `seq` for each one it sends.
    reaction(r, old) {
      if (r && r.name && (!old || r.seq !== old.seq)) this.react(r.name);
    },
  },

  created() {
    this.token = 0;
    this.acting = false;
    this.current = "";
    this.timers = new Set();
    this.blinkTimer = null;
    this.idleTimer = null;
    this.sleepTimer = null;
    this.trackFrame = null;
    this.tracking = false;
    this.lastTrackAt = 0;
    this.lastActivity = Date.now();
    this.pokes = [];
    this.pointer = null;
    this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  },

  mounted() {
    this.restFace();
    this.scheduleBlink();
    this.scheduleIdle();
    if (this.interactive || this.sleepAfter > 0) {
      this.onActivity = this.onActivity.bind(this);
      for (const type of ["pointermove", "pointerdown", "keydown", "wheel"]) {
        window.addEventListener(type, this.onActivity, { passive: true });
      }
    }
    if (this.interactive || this.sleepAfter > 0) {
      this.sleepTimer = setInterval(() => this.checkSleep(), 1000);
    }
    if (this.roam) this.startRoaming();
    if (this.reaction && this.reaction.name) this.react(this.reaction.name);
  },

  unmounted() {
    this.token++;
    for (const id of this.timers) clearTimeout(id);
    clearTimeout(this.blinkTimer);
    clearTimeout(this.idleTimer);
    clearInterval(this.sleepTimer);
    cancelAnimationFrame(this.trackFrame);
    cancelAnimationFrame(this.roamFrame);
    if (this.onActivity) {
      for (const type of ["pointermove", "pointerdown", "keydown", "wheel"]) {
        window.removeEventListener(type, this.onActivity);
      }
    }
  },

  methods: {
    // ---------- drawing helpers ----------

    m(name) {
      return { opacity: this.mouth === name ? 1 : 0 };
    },

    lidStyle(e) {
      // The lid is a body-coloured square clipped to the eye; its lower edge
      // sits `offset` below the eye centre, tilted about that centre so sad
      // lids droop at the outer corners and cross lids at the inner ones.
      const offset = -2.6 + 4.5 * this.lid;
      const tilt = e.side === "L" ? -this.tilt : this.tilt;
      return {
        transformOrigin: e.cx + "px 12px",
        transform: "rotate(" + tilt + "deg) translateY(" + offset + "px)",
        transitionDuration: this.lidSpeed + "s",
      };
    },

    // ---------- face state ----------

    restLook() {
      if (this.busy && LIVELY.has(this.mood)) {
        // Watch the arm: it is drawn in the middle of the viewport.
        const r = this.$el.getBoundingClientRect();
        const dx = window.innerWidth / 2 - (r.left + r.width / 2);
        const dy = window.innerHeight / 2 - (r.top + r.height / 2);
        const d = Math.hypot(dx, dy) || 1;
        return [(dx / d) * 0.65, (dy / d) * 0.55];
      }
      return MOODS[this.mood] ? MOODS[this.mood].look : [0, 0];
    },

    restFace() {
      const face = MOODS[this.mood] || MOODS.happy;
      this.eyes = this.asleep ? "closed" : face.eyes;
      this.mouth = this.asleep ? "small-o" : face.mouth;
      this.lid = face.lid + (this.busy && LIVELY.has(this.mood) ? 0.22 : 0);
      this.lidSpeed = 0.25;
      this.tilt = face.tilt;
      this.eyeScale = face.eyeScale;
      this.pupilScale = face.pupilScale;
      this.lookSpeed = 0.4;
      if (!this.tracking) this.look = this.restLook();
      this.leds = face.leds || this.light || (this.busy && LIVELY.has(this.mood) ? "chase" : "");
      const droop = this.asleep ? 14 : this.mood === "sad" ? 5 : 0;
      this.antenna = { l: -droop, r: droop };
      this.antennaWave = "";
      this.headTilt = this.asleep ? 6 : 0;
      this.sink = this.asleep ? 0.5 : 0;
      for (const k of Object.keys(this.fx)) this.fx[k] = false;
      this.fx.zzz = this.asleep;
    },

    lookAt(x, y, speed = 0.4) {
      this.lookSpeed = speed;
      this.look = [x, y];
    },

    // ---------- sequencing ----------

    wait(ms, token) {
      return new Promise((resolve, reject) => {
        const id = setTimeout(() => {
          this.timers.delete(id);
          if (token === this.token) resolve();
          else reject(new Superseded());
        }, ms);
        this.timers.add(id);
      });
    },

    // Run an animation sequence. A newer sequence supersedes an older one:
    // the older one's next wait() rejects, and the newer owns the face.
    async play(sequence, name = "") {
      const token = ++this.token;
      this.current = name;
      this.acting = true;
      this.restFace();
      try {
        await sequence((ms) => this.wait(ms, token));
      } catch (e) {
        if (e instanceof Superseded) return;
        throw e;
      }
      if (token === this.token) {
        this.acting = false;
        this.current = "";
        this.restFace();
      }
    },

    async anim(name, ms, wait) {
      this.rigAnim = "";
      await this.$nextTick();
      await new Promise((r) => requestAnimationFrame(r));
      this.rigAnim = name;
      await wait(ms);
      this.rigAnim = "";
    },

    async blinkOnce(wait, holdMs = 90) {
      const rest = this.lid;
      this.lidSpeed = 0.07;
      this.lid = 1;
      await wait(70 + holdMs);
      this.lid = rest;
      await wait(80);
      this.lidSpeed = 0.25;
    },

    scheduleBlink() {
      clearTimeout(this.blinkTimer);
      const gap = BLINK_GAP_S[this.mood];
      if (!gap) return;
      this.blinkTimer = setTimeout(() => {
        if (!this.acting && !this.asleep && this.eyes === "open") {
          const hold = this.mood === "sad" ? 200 : this.mood === "neutral" ? 130 : 90;
          const token = this.token;
          this.blinkOnce((ms) => this.wait(ms, token), hold).catch(ignoreSuperseded);
        }
        this.scheduleBlink();
      }, rand(gap[0], gap[1]) * 1000);
    },

    // Calm (a user setting) and reduced motion (an OS setting) both drop the
    // idle fidgets; reactions to what the robot does still play.
    still() {
      return this.calm || this.reducedMotion;
    },

    scheduleIdle() {
      clearTimeout(this.idleTimer);
      const gap = IDLE_GAP_S[this.mood];
      if (!gap || this.still()) return;
      this.idleTimer = setTimeout(() => {
        const trackedRecently = Date.now() - this.lastTrackAt < 2500;
        if (!this.acting && !this.asleep && !this.busy && !trackedRecently && !this.still()) {
          const fn = pick(this.idleActions()[this.mood] || [[1, async () => {}]]);
          this.play(fn);
        }
        this.scheduleIdle();
      }, rand(gap[0], gap[1]) * 1000);
    },

    idleActions() {
      return {
        happy: [
          [3, async (wait) => { await this.blinkOnce(wait, 70); await wait(110); await this.blinkOnce(wait, 70); }],
          [2, async (wait) => { this.eyes = "happy"; this.mouth = "grin"; await wait(1700); }],
          [2, async (wait) => { this.eyeScale = 1.2; this.mouth = "open"; await wait(1200); }],
          [3, async (wait) => { this.lookAt(rand(-0.7, 0.7), rand(-0.5, 0.4), 0.35); await wait(rand(800, 1500)); }],
          [2, async (wait) => { this.wiggleAntennae(); await wait(700); }],
          [1, async (wait) => { this.headTilt = -5; await wait(900); this.headTilt = 5; await wait(900); }],
        ],
        neutral: [
          [3, async (wait) => {
            this.lookAt(-0.6, 0, 0.5); await wait(800);
            this.lookAt(0.6, 0, 0.6); await wait(800);
          }],
          [2, async (wait) => {
            this.lookAt(0.35, -0.5, 0.5); this.mouth = "slant";
            if (Math.random() < 0.4) this.fx.question = true;
            await wait(1600);
          }],
          [2, async (wait) => { this.lid = 0.45; this.mouth = "zigzag"; await wait(1600); }],
          [2, async (wait) => { this.mouth = "slant"; await wait(600); }],
          [2, async (wait) => { this.headTilt = 7; this.lookAt(0.2, -0.1); await wait(1300); }],
          [1, async (wait) => { this.wiggleAntennae(0.6); await wait(500); }],
        ],
        sad: [
          [3, async (wait) => { this.lookAt(0, 0.55, 0.6); await wait(1800); }],
          [2, async (wait) => { this.lookAt(-0.55, 0.25, 0.7); await wait(1400); }],
          [2, async (wait) => { this.eyes = "closed"; this.mouth = "deep-frown"; await wait(2200); }],
          [2, async (wait) => {
            this.mouth = "small-o"; this.sink = 0.6; this.antenna = { l: -12, r: 12 };
            await wait(1300); this.mouth = "frown"; await wait(700);
          }],
        ],
        alarmed: [
          [3, async (wait) => {
            this.lookAt(-0.6, 0, 0.12); await wait(300);
            this.lookAt(0.6, 0, 0.12); await wait(300);
            this.lookAt(-0.6, 0, 0.12); await wait(300);
          }],
          [2, async (wait) => { await this.anim("shake", 500, wait); }],
          [2, async (wait) => { this.mouth = "zigzag"; this.lookAt(0, 0.4, 0.3); await wait(1100); }],
        ],
      };
    },

    wiggleAntennae(scale = 1) {
      const token = this.token;
      const wait = (ms) => this.wait(ms, token);
      (async () => {
        this.antenna = { l: -8 * scale, r: 8 * scale };
        await wait(180);
        this.antenna = { l: 6 * scale, r: -6 * scale };
        await wait(200);
        this.antenna = { l: 0, r: 0 };
      })().catch(ignoreSuperseded);
    },

    // ---------- reactions ----------

    react(name) {
      const sequences = {
        greet: async (wait) => {
          this.lookAt(0, 0);
          this.eyes = "wink";
          this.mouth = "grin";
          this.antennaWave = "r";
          await this.anim("hop", 450, wait);
          await wait(1300);
        },
        cheer: async (wait) => {
          this.eyes = "happy";
          this.mouth = "open";
          await this.anim("hop", 450, wait);
          await wait(350);
        },
        slump: async (wait) => {
          this.eyes = "closed";
          this.mouth = "deep-frown";
          this.sink = 0.8;
          this.antenna = { l: -16, r: 16 };
          await wait(1100);
        },
        jolt: async (wait) => {
          this.fx.exclaim = true;
          await this.anim("jolt", 350, wait);
          await wait(600);
        },
        celebrate: async (wait) => {
          this.eyes = "happy";
          this.mouth = "open";
          this.leds = "party";
          await this.anim("hop2", 900, wait);
          this.antennaWave = "l";
          await wait(1200);
        },
        oops: async (wait) => {
          this.eyeScale = 1.25;
          this.pupilScale = 0.7;
          this.mouth = "o";
          await this.anim("jolt", 350, wait);
          this.mouth = "wavy";
          this.eyeScale = 1;
          this.pupilScale = 1;
          this.lid = 0.2;
          this.tilt = 10;
          this.lookAt(-0.55, 0.45, 0.5);
          await wait(1800);
        },
        startle: async (wait) => {
          this.lid = 0;
          this.eyeScale = 1.3;
          this.pupilScale = 0.6;
          this.mouth = "o";
          this.fx.exclaim = true;
          this.antenna = { l: -18, r: 18 };
          await this.anim("jolt", 350, wait);
          this.antenna = { l: 0, r: 0 };
          await wait(900);
        },
        relief: async (wait) => {
          this.eyes = "closed";
          this.mouth = "small-o";
          this.sink = 0.7;
          await wait(900);
          this.sink = 0;
          this.mouth = "smile";
          await wait(500);
          this.eyes = "open";
          await this.blinkOnce(wait);
          await wait(300);
        },
        giggle: async (wait) => {
          this.eyes = "happy";
          this.mouth = "open";
          await this.anim("hop", 450, wait);
          await wait(500);
        },
        dizzy: async (wait) => {
          this.eyes = "x";
          this.mouth = "wavy";
          await this.anim("wobble", 1800, wait);
          this.eyes = "open";
          await this.anim("shake", 450, wait);
          await this.blinkOnce(wait);
        },
        shrug: async (wait) => {
          this.mouth = "slant";
          this.lid = 0.3;
          this.lookAt(0.5, -0.45, 0.3);
          this.headTilt = 6;
          this.antenna = { l: -22, r: 22 };
          await this.anim("shrug", 520, wait);
          await wait(700);
        },
        nod: async (wait) => {
          this.eyes = "happy";
          this.mouth = "smile";
          await this.anim("nod", 720, wait);
          await wait(200);
        },
        bonk: async (wait) => {
          this.lid = 1;
          this.lidSpeed = 0.05;
          await this.anim("squash", 300, wait);
        },
        yawn: async (wait) => {
          this.mouth = "yawn";
          this.lid = 0.6;
          this.lidSpeed = 0.6;
          this.headTilt = -4;
          await wait(1400);
          // Someone moved mid-yawn: stay awake.
          this.asleep = Date.now() - this.lastActivity > this.sleepAfter * 1000;
        },
        wake: async (wait) => {
          this.eyeScale = 1.15;
          this.antenna = { l: -10, r: 10 };
          await wait(220);
          this.antenna = { l: 0, r: 0 };
          await this.blinkOnce(wait, 60);
          await wait(120);
          await this.blinkOnce(wait, 60);
        },
      };
      const sequence = sequences[name];
      if (sequence) this.play(sequence, name);
    },

    // ---------- pointer, sleep, pokes ----------

    onActivity(e) {
      this.lastActivity = Date.now();
      if (this.asleep) {
        this.asleep = false;
        this.react("wake");
        return;
      }
      if (this.interactive && e.type === "pointermove") {
        this.pointer = [e.clientX, e.clientY];
        if (!this.trackFrame) {
          this.trackFrame = requestAnimationFrame(() => {
            this.trackFrame = null;
            this.track();
          });
        }
      }
    },

    track() {
      if (!this.pointer || !this.$el) return;
      const r = this.$el.getBoundingClientRect();
      const dx = this.pointer[0] - (r.left + r.width / 2);
      const dy = this.pointer[1] - (r.top + r.height * 0.55);
      const d = Math.hypot(dx, dy);
      const free = !this.acting && !this.busy && !this.calm && LIVELY.has(this.mood);
      if (d > TRACK_RADIUS_PX || !free) {
        if (this.tracking) {
          this.tracking = false;
          if (free) this.lookAt(...this.restLook(), 0.5);
        }
        return;
      }
      const k = Math.min(1, d / (r.width * 1.5 + 30));
      this.tracking = true;
      this.lastTrackAt = Date.now();
      this.lookAt(d ? (dx / d) * 0.7 * k : 0, d ? (dy / d) * 0.6 * k : 0, 0.12);
    },

    checkSleep() {
      if (this.sleepAfter <= 0 || this.calm || this.asleep || this.acting || this.busy) return;
      if (!LIVELY.has(this.mood)) return;
      if (Date.now() - this.lastActivity > this.sleepAfter * 1000) this.react("yawn");
    },

    onPoke() {
      if (!this.interactive || !LIVELY.has(this.mood) || this.asleep) return;
      // Still reeling: pokes don't land until it has shaken it off.
      if (this.current === "dizzy") return;
      const now = Date.now();
      this.pokes = this.pokes.filter((t) => now - t < POKE_WINDOW_MS);
      this.pokes.push(now);
      if (this.pokes.length >= POKES_TO_DIZZY) {
        this.pokes = [];
        this.react("dizzy");
      } else {
        this.react("giggle");
      }
    },

    // ---------- roaming (takeover overlay) ----------

    startRoaming() {
      const el = this.$el;
      const SPEED = 90;
      const SPIN = 30;
      const avoidRect = () => {
        const target = this.roamAvoid && document.querySelector(this.roamAvoid);
        return target ? target.getBoundingClientRect() : null;
      };
      const size = () => el.getBoundingClientRect().width || 96;
      if (this.reducedMotion) {
        el.style.transform = "translate(24px, 24px)";
        return;
      }
      let w = size();
      let x = 0;
      let y = 0;
      for (let i = 0; i < 32; i++) {
        x = Math.random() * (window.innerWidth - w);
        y = Math.random() * (window.innerHeight - w);
        const a = avoidRect();
        if (!a || x > a.right || x + w < a.left || y > a.bottom || y + w < a.top) break;
      }
      const heading = (0.15 + Math.random() * 0.7) * (Math.PI / 2) + Math.floor(Math.random() * 4) * (Math.PI / 2);
      let vx = Math.cos(heading) * SPEED;
      let vy = Math.sin(heading) * SPEED;
      let angle = 0;
      let last = null;
      const bonk = () => {
        if (!this.acting) this.react("bonk");
      };
      const step = (now) => {
        // A backgrounded tab resumes with a huge dt; cap it so nothing teleports.
        const dt = last === null ? 0 : Math.min(0.05, (now - last) / 1000);
        last = now;
        w = size();
        let nx = x + vx * dt;
        let ny = y + vy * dt;
        angle = (angle + SPIN * dt) % 360;
        let hit = false;
        const maxX = window.innerWidth - w;
        const maxY = window.innerHeight - w;
        if (nx < 0) { nx = 0; vx = Math.abs(vx); hit = true; }
        if (nx > maxX) { nx = maxX; vx = -Math.abs(vx); hit = true; }
        if (ny < 0) { ny = 0; vy = Math.abs(vy); hit = true; }
        if (ny > maxY) { ny = maxY; vy = -Math.abs(vy); hit = true; }
        const a = avoidRect();
        if (a && nx < a.right && nx + w > a.left && ny < a.bottom && ny + w > a.top) {
          // Push out along the axis of shallowest penetration.
          const pen = [nx + w - a.left, a.right - nx, ny + w - a.top, a.bottom - ny];
          const least = Math.min(...pen);
          if (least === pen[0]) { nx = a.left - w; vx = -Math.abs(vx); }
          else if (least === pen[1]) { nx = a.right; vx = Math.abs(vx); }
          else if (least === pen[2]) { ny = a.top - w; vy = -Math.abs(vy); }
          else { ny = a.bottom; vy = Math.abs(vy); }
          hit = true;
        }
        if (hit && dt > 0) bonk();
        x = nx;
        y = ny;
        el.style.transform = "translate(" + x + "px, " + y + "px) rotate(" + angle + "deg)";
        this.roamFrame = requestAnimationFrame(step);
      };
      el.style.transform = "translate(" + x + "px, " + y + "px)";
      this.roamFrame = requestAnimationFrame(step);
    },
  },
};
</script>

<style>
.robot-buddy {
  display: inline-block;
  flex-shrink: 0;
  line-height: 0;
  color: var(--bb-color);
  transition: color 0.6s ease;
  /* Eyes, mouth and LEDs are drawn in on-fill; a chip whose fill shows
     through instead sets its own. */
  --bb-cut: var(--wc-on-fill);
  /* Floating glyphs sit on the page, not the body: follow the theme's text. */
  --bb-glyph: var(--wc-text);
}
.robot-buddy.bb-mood-happy { --bb-color: var(--wc-positive); }
.robot-buddy.bb-mood-neutral { --bb-color: var(--wc-mode-sim); }
.robot-buddy.bb-mood-sad { --bb-color: var(--wc-error); }
.robot-buddy.bb-mood-alarmed { --bb-color: var(--wc-error); }
.robot-buddy.bb-mood-booting { --bb-color: var(--wc-text-muted); }

.robot-buddy svg {
  width: 100%;
  height: 100%;
  overflow: visible;
  animation: bb-breathe 6s ease-in-out infinite;
}
.robot-buddy.bb-mood-neutral svg { animation-duration: 7s; animation-delay: -2s; }
.robot-buddy.bb-mood-sad svg { animation-duration: 8s; animation-delay: -4s; }
.robot-buddy.bb-mood-alarmed svg { animation-duration: 1.4s; }
.robot-buddy.bb-asleep svg { animation-duration: 4.5s; }
.robot-buddy.bb-calm svg { animation: none; }
@keyframes bb-breathe {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2px); }
}

.robot-buddy .bb-pose,
.robot-buddy .bb-antenna,
.robot-buddy .bb-eye,
.robot-buddy .bb-eye circle { transition: transform 0.3s ease; }
.robot-buddy .bb-pose { transition-duration: 0.5s; }
.robot-buddy .bb-pupil,
.robot-buddy .bb-lid { transition: transform 0.25s ease; }
.robot-buddy .bb-eyes > g,
.robot-buddy .bb-eye,
.robot-buddy .bb-mouth > * { transition: opacity 0.15s ease, transform 0.3s ease; }

.robot-buddy .bb-stroke,
.robot-buddy .bb-stroke path,
.robot-buddy .bb-glyph {
  fill: none;
  stroke: var(--bb-cut);
  stroke-width: 1;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.robot-buddy .bb-eyes-alt.bb-stroke path { stroke-width: 1.1; }
.robot-buddy .bb-eyes-alt.bb-eyes-x path { stroke-width: 0.75; }
.robot-buddy .bb-cut { fill: var(--bb-cut); }
.robot-buddy .bb-fill { fill: var(--bb-cut); stroke: var(--bb-cut); stroke-width: 0.4; stroke-linejoin: round; }
.robot-buddy .bb-gloss { fill: var(--wc-on-fill); opacity: 0.16; }
.robot-buddy .bb-scan-bar { animation: bb-scan 1.1s ease-in-out infinite alternate; }

.robot-buddy .bb-led { fill: var(--bb-cut); opacity: 0; }
.robot-buddy .bb-halo { fill: none; stroke: currentColor; stroke-width: 0.35; opacity: 0; }
.robot-buddy.bb-leds-chase .bb-led { animation: bb-led 1.2s ease-in-out infinite; }
.robot-buddy.bb-leds-chase .bb-antenna-r .bb-led { animation-delay: 0.6s; }
.robot-buddy.bb-leds-pulse .bb-led { animation: bb-led 1.6s ease-in-out infinite; }
.robot-buddy.bb-leds-pulse .bb-antenna-r .bb-led { animation-delay: 0.3s; }
.robot-buddy.bb-leds-alarm .bb-led { animation: bb-led 0.5s steps(2, jump-none) infinite; }
.robot-buddy.bb-leds-alarm .bb-halo { animation: bb-halo 0.5s ease-out infinite; }
.robot-buddy.bb-leds-alarm .bb-antenna-r .bb-led,
.robot-buddy.bb-leds-alarm .bb-antenna-r .bb-halo { animation-delay: 0.25s; }
.robot-buddy.bb-leds-party .bb-led { animation: bb-led 0.3s ease-in-out infinite; }
.robot-buddy.bb-leds-party .bb-antenna-r .bb-led { animation-delay: 0.15s; }

/* Steady lights for standing conditions, big enough to read at chip size:
   the left bulb turns record red while recording, and both bulbs pulse while
   an AI agent drives (the screen-edge glow is what says who has control). */
.robot-buddy.bb-leds-rec .bb-antenna-l .bb-bulb { fill: var(--wc-record); }
.robot-buddy.bb-leds-rec .bb-antenna-l .bb-led,
.robot-buddy.bb-leds-agent .bb-led { animation: bb-glow 2s ease-in-out infinite; }
@keyframes bb-glow {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 0; }
}

.robot-buddy .bb-alert { fill: var(--wc-warning); stroke: var(--wc-scrim); stroke-width: 0.15; }
.robot-buddy .bb-glyph { stroke: var(--bb-glyph); stroke-width: 0.45; }
.robot-buddy .bb-glyph-dot { fill: var(--bb-glyph); }
.robot-buddy .bb-z { stroke-width: 0.42; opacity: 0; animation: bb-z 2.7s ease-out infinite; }
.robot-buddy .bb-pop { transform-box: fill-box; transform-origin: bottom center; animation: bb-pop 0.35s cubic-bezier(0.3, 1.8, 0.5, 1); }

.robot-buddy .bb-rig { transform-origin: 12px 18.5px; }
.robot-buddy .bb-anim-hop { animation: bb-hop 0.45s ease-out; }
.robot-buddy .bb-anim-hop2 { animation: bb-hop 0.45s ease-out 2; }
.robot-buddy .bb-anim-jolt { animation: bb-jolt 0.35s ease-out; }
.robot-buddy .bb-anim-shake { animation: bb-shake 0.45s linear; }
.robot-buddy .bb-anim-wobble { animation: bb-wobble 0.6s ease-in-out 3; }
.robot-buddy .bb-anim-squash { animation: bb-squash 0.3s ease-out; }
.robot-buddy .bb-anim-shrug { animation: bb-shrug 0.52s ease-in-out; }
.robot-buddy .bb-anim-nod { animation: bb-nod 0.36s ease-in-out 2; }
.robot-buddy .bb-wave { animation: bb-wave 0.45s ease-in-out 3; }

@keyframes bb-hop {
  0% { transform: translateY(0) scale(1, 1); }
  15% { transform: translateY(0) scale(1.06, 0.92); }
  45% { transform: translateY(-2.2px) scale(0.96, 1.05); }
  80% { transform: translateY(0) scale(1.05, 0.94); }
  100% { transform: translateY(0) scale(1, 1); }
}
@keyframes bb-jolt {
  0% { transform: translateY(0) scale(1); }
  30% { transform: translateY(-1.2px) scale(1.06); }
  100% { transform: translateY(0) scale(1); }
}
@keyframes bb-shake {
  0%, 100% { transform: translateX(0); }
  20%, 60% { transform: translateX(-0.6px); }
  40%, 80% { transform: translateX(0.6px); }
}
@keyframes bb-wobble {
  0%, 100% { transform: rotate(0); }
  25% { transform: rotate(-9deg); }
  75% { transform: rotate(9deg); }
}
@keyframes bb-shrug {
  0%, 100% { transform: translateY(0); }
  30%, 70% { transform: translateY(-0.8px); }
}
@keyframes bb-nod {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(0.9px) scale(1, 0.97); }
}
@keyframes bb-squash {
  0% { transform: scale(1, 1); }
  35% { transform: scale(1.14, 0.84); }
  100% { transform: scale(1, 1); }
}
@keyframes bb-wave {
  0%, 100% { transform: rotate(0); }
  50% { transform: rotate(28deg); }
}
@keyframes bb-scan { to { transform: translateX(9.8px); } }
@keyframes bb-led {
  0%, 100% { opacity: 0; }
  50% { opacity: 0.95; }
}
@keyframes bb-halo {
  0% { opacity: 0.9; transform: scale(0.5); }
  100% { opacity: 0; transform: scale(1.4); }
}
.robot-buddy .bb-halo { transform-box: fill-box; transform-origin: center; }
@keyframes bb-z {
  0% { opacity: 0; transform: translate(0, 0.6px); }
  25% { opacity: 1; }
  100% { opacity: 0; transform: translate(0.9px, -1.4px); }
}
@keyframes bb-pop {
  from { transform: scale(0); }
  to { transform: scale(1); }
}

.robot-buddy.bb-roam {
  position: fixed;
  top: 0;
  left: 0;
  pointer-events: none;
}

@media (prefers-reduced-motion: reduce) {
  .robot-buddy svg,
  .robot-buddy .bb-rig,
  .robot-buddy .bb-scan-bar,
  .robot-buddy .bb-wave { animation: none !important; }
}
</style>
