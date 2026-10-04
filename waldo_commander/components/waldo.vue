<template>
  <div
    class="waldo"
    :class="rootClasses"
    :style="rootStyle"
    @click="onPoke"
  >
    <svg viewBox="3 3 18 16" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <clipPath v-for="e in EYES" :key="e.side" :id="uid + e.side">
          <circle :cx="e.cx" cy="12" r="1.8" />
        </clipPath>
        <!-- Eyes, mouth and LEDs are holes in the body, so whatever is
             behind Waldo shows through them. -->
        <mask :id="uid + 'cut'" maskUnits="userSpaceOnUse" x="-2" y="-2" width="28" height="26">
          <rect class="waldo-keep" x="-2" y="-2" width="28" height="26" />
          <g
            v-for="a in ANTENNAE"
            :key="a.side"
            class="waldo-antenna"
            :class="antennaClass(a)"
            :style="antennaStyle(a)"
          >
            <circle class="waldo-led" :cx="a.x" cy="4.2" r="0.45" />
          </g>

          <g class="waldo-eyes">
            <g class="waldo-eyes-open" :style="{ opacity: eyesOpen ? 1 : 0 }">
              <g v-for="e in EYES" :key="e.side" class="waldo-eye" :style="eyeStyle(e)">
                <circle class="waldo-cut" :cx="e.cx" cy="12" r="1.8" />
              </g>
            </g>
            <g class="waldo-eyes-alt waldo-eyes-happy waldo-stroke" :style="{ opacity: eyes === 'happy' ? 1 : 0 }">
              <path d="M6.2 12.4 Q8 10.5 9.8 12.4" />
              <path d="M14.2 12.4 Q16 10.5 17.8 12.4" />
            </g>
            <g class="waldo-eyes-alt waldo-eyes-wink waldo-stroke" :style="{ opacity: eyes === 'wink' ? 1 : 0 }">
              <path d="M14.2 12.4 Q16 10.5 17.8 12.4" />
            </g>
            <g class="waldo-eyes-alt waldo-eyes-closed waldo-stroke" :style="{ opacity: eyes === 'closed' ? 1 : 0 }">
              <path d="M6.2 11.8 Q8 13.5 9.8 11.8" />
              <path d="M14.2 11.8 Q16 13.5 17.8 11.8" />
            </g>
            <g class="waldo-eyes-alt waldo-eyes-x waldo-stroke" :style="{ opacity: eyes === 'x' ? 1 : 0 }">
              <path d="M6.9 10.9 L9.1 13.1 M9.1 10.9 L6.9 13.1" />
              <path d="M14.9 10.9 L17.1 13.1 M17.1 10.9 L14.9 13.1" />
            </g>
            <g class="waldo-eyes-alt waldo-eyes-squeeze waldo-stroke" :style="{ opacity: eyes === 'squeeze' ? 1 : 0 }">
              <path d="M6.7 10.9 L9.3 12 L6.7 13.1" />
              <path d="M17.3 10.9 L14.7 12 L17.3 13.1" />
            </g>
            <g v-if="eyes === 'hearts'" class="waldo-eyes-alt waldo-eyes-hearts">
              <path class="waldo-cut waldo-heart" d="M8 13.6 C5.4 11.9 6.6 9.6 8 11.1 C9.4 9.6 10.6 11.9 8 13.6 Z" />
              <path class="waldo-cut waldo-heart" d="M16 13.6 C13.4 11.9 14.6 9.6 16 11.1 C17.4 9.6 18.6 11.9 16 13.6 Z" />
            </g>
            <g class="waldo-eyes-alt waldo-eyes-scan" :style="{ opacity: eyes === 'scan' ? 1 : 0 }">
              <rect class="waldo-cut" x="5.8" y="11" width="12.4" height="2" rx="1" opacity="0.22" />
              <rect class="waldo-scan-bar waldo-cut" x="5.8" y="11" width="2.6" height="2" rx="1" />
            </g>
          </g>
          <rect v-if="fx.scan" class="waldo-cut waldo-sweep" x="5.2" y="8.5" width="13.6" height="0.45" />

          <g class="waldo-mouth">
            <path class="waldo-stroke" :style="m('smile')" data-mouth="smile" d="M9 15.5 Q12 17.8 15 15.5" />
            <path class="waldo-stroke" :style="m('grin')" data-mouth="grin" stroke-width="1.2" d="M8.2 15 Q12 18.8 15.8 15" />
            <path class="waldo-fill" :style="m('open')" data-mouth="open" d="M9 15.2 Q12 18.2 15 15.2 Z" />
            <path class="waldo-stroke" :style="m('flat')" data-mouth="flat" d="M9 16 H15" />
            <path class="waldo-stroke" :style="m('slant')" data-mouth="slant" d="M9 16.3 L15 15.7" />
            <path class="waldo-stroke" :style="m('zigzag')" data-mouth="zigzag" stroke-width="0.8" d="M9 16 L10.2 15.2 L11.4 16.8 L12.6 15.2 L13.8 16.8 L15 16" />
            <path class="waldo-stroke" :style="m('frown')" data-mouth="frown" d="M9 16.8 Q12 14.5 15 16.8" />
            <path class="waldo-stroke" :style="m('deep-frown')" data-mouth="deep-frown" stroke-width="1.2" d="M9.5 17.2 Q12 13.5 14.5 17.2" />
            <path class="waldo-stroke" :style="m('tremble')" data-mouth="tremble" stroke-width="0.8" d="M9.2 17 Q12 14.8 14.8 17" />
            <path class="waldo-stroke" :style="m('wavy')" data-mouth="wavy" stroke-width="0.8" d="M9 16.2 Q10 15.3 11 16.2 T13 16.2 T15 16.2" />
            <ellipse class="waldo-fill" :style="m('o')" data-mouth="o" cx="12" cy="16.1" rx="0.9" ry="1" />
            <ellipse class="waldo-fill" :style="m('small-o')" data-mouth="small-o" cx="12" cy="16.2" rx="0.5" ry="0.55" />
            <ellipse class="waldo-fill" :style="m('yawn')" data-mouth="yawn" cx="12" cy="16.2" rx="1.3" ry="1.55" />
          </g>
        </mask>
      </defs>

      <g class="waldo-pose" :style="poseStyle">
        <g class="waldo-rig" :class="rigAnim ? 'waldo-anim-' + rigAnim : ''">
          <g :mask="'url(#' + uid + 'cut)'">
            <g
              v-for="a in ANTENNAE"
              :key="a.side"
              class="waldo-antenna"
              :class="antennaClass(a)"
              :style="antennaStyle(a)"
            >
              <rect :x="a.x - 0.5" y="4.5" width="1" height="3" rx="0.5" fill="currentColor" />
              <circle class="waldo-halo" :cx="a.x" cy="4.2" r="1.6" />
              <circle class="waldo-bulb" :cx="a.x" cy="4.2" r="0.8" fill="currentColor" />
            </g>
            <rect x="4.5" y="8.5" width="15" height="10" rx="2" fill="currentColor" />
          </g>

          <!-- An AI agent's colour on the antenna tips. -->
          <g
            v-for="a in ANTENNAE"
            :key="'tip' + a.side"
            class="waldo-antenna"
            :class="antennaClass(a)"
            :style="antennaStyle(a)"
          >
            <circle class="waldo-tip" :cx="a.x" cy="4.2" r="0.95" />
          </g>
          <g v-if="fx.waves" class="waldo-waves">
            <path d="M6.5 3.36 A1.3 1.3 0 0 1 8.5 3.36" style="transform-origin: 7.5px 4.2px" />
            <path d="M15.5 3.36 A1.3 1.3 0 0 1 17.5 3.36" style="transform-origin: 16.5px 4.2px" />
            <path class="waldo-wave-outer" d="M5.89 2.85 A2.1 2.1 0 0 1 9.11 2.85" style="transform-origin: 7.5px 4.2px" />
            <path class="waldo-wave-outer" d="M14.89 2.85 A2.1 2.1 0 0 1 18.11 2.85" style="transform-origin: 16.5px 4.2px" />
          </g>

          <g class="waldo-eyes-open" :style="{ opacity: eyesOpen ? 1 : 0 }">
            <g v-for="e in EYES" :key="e.side" class="waldo-eye" :style="eyeStyle(e)">
              <g class="waldo-pupil" :style="pupilStyle">
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
                  class="waldo-lid"
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
          <rect v-if="fx.aiScan" class="waldo-ai-scan" x="5.2" y="8.5" width="13.6" height="0.45" />
          <path v-if="mood === 'alarmed'" class="waldo-drop" d="M18.2 9.3 Q17.4 10.6 18.2 11.1 Q19 10.6 18.2 9.3 Z" />
          <path v-if="fx.sweat" class="waldo-drop waldo-sweat-slide" d="M18.2 9.3 Q17.4 10.6 18.2 11.1 Q19 10.6 18.2 9.3 Z" />
          <path v-if="fx.tear" class="waldo-drop waldo-tear" d="M7.4 13.9 Q6.75 14.95 7.4 15.45 Q8.05 14.95 7.4 13.9 Z" />
        </g>
      </g>

      <g class="waldo-fx">
        <g v-if="fx.zzz">
          <g v-for="(z, i) in ZZZ" :key="i" :transform="'translate(' + z.x + ' ' + z.y + ') scale(' + z.s + ')'">
            <path class="waldo-glyph waldo-z" :style="{ animationDelay: i * 0.9 + 's' }" d="M0 0 H1.4 L0 1.6 H1.4" />
          </g>
        </g>
        <g v-if="fx.exclaim" transform="translate(20.6 4.6)">
          <g class="waldo-pop">
            <rect class="waldo-alert" x="-0.45" y="-2.3" width="0.9" height="2.4" rx="0.45" />
            <circle class="waldo-alert" cx="0" cy="1" r="0.5" />
          </g>
        </g>
        <g v-if="fx.question || asking" transform="translate(20.4 5)" :class="{ 'waldo-ask': asking }">
          <g class="waldo-pop">
            <path class="waldo-glyph" d="M-0.8 -1.3 Q-0.8 -2.5 0.2 -2.5 Q1.2 -2.5 1.1 -1.5 Q1 -0.8 0.2 -0.5 L0.2 0.2" />
            <circle class="waldo-glyph-dot" cx="0.2" cy="1.1" r="0.32" />
          </g>
        </g>
        <g v-if="fx.sparkle" class="waldo-sparkle">
          <path d="M5.2 4.7 L5.45 5.35 L6.1 5.6 L5.45 5.85 L5.2 6.5 L4.95 5.85 L4.3 5.6 L4.95 5.35 Z" />
          <path d="M19.2 5.5 L19.4 6.05 L19.95 6.25 L19.4 6.45 L19.2 7 L19 6.45 L18.45 6.25 L19 6.05 Z" />
        </g>
        <g v-if="fx.note" class="waldo-note">
          <ellipse class="waldo-glyph-dot" cx="18.9" cy="7.3" rx="0.75" ry="0.6" />
          <path class="waldo-glyph" d="M19.55 7.2 V4.4 Q20.7 4.9 20.4 6.1" />
        </g>
        <g v-if="fx.dots" class="waldo-dots">
          <circle class="waldo-glyph-dot" cx="18.3" cy="6.6" r="0.4" />
          <circle class="waldo-glyph-dot" cx="19.35" cy="6.6" r="0.4" />
          <circle class="waldo-glyph-dot" cx="20.4" cy="6.6" r="0.4" />
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
// A jog click lets go within milliseconds; hold its glance long enough to read.
const LOOK_MIN_MS = 450;
const PEEK_HOLD_MS = 1600;

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

// The reaction to an AI agent arriving, leaving, or changing hands.
const agentReaction = (was, now) => {
  if (now === was) return "";
  if (now === "driving") return "ai-take";
  if (was === "driving") return "ai-release";
  return now ? "ai-hello" : "ai-bye";
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
    agent: { type: String, default: "" },
    asking: { type: Boolean, default: false },
    look: { type: Array, default: null },
  },

  data() {
    return {
      uid: "waldo" + nextUid++,
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
      gaze: [0, 0],
      gazeSpeed: 0.4,
      leds: "",
      antenna: { l: 0, r: 0 },
      antennaWave: "",
      rigAnim: "",
      headTilt: 0,
      sink: 0,
      aiLit: "",
      held: null,
      fx: {
        zzz: false, exclaim: false, question: false, sparkle: false, note: false, dots: false,
        tear: false, sweat: false, scan: false, aiScan: false, waves: false, tipFlash: false,
        tipsFade: false,
      },
      asleep: false,
    };
  },

  computed: {
    rootClasses() {
      return [
        "waldo-mood-" + this.mood,
        this.leds ? "waldo-leds-" + this.leds : "",
        this.asleep ? "waldo-asleep" : "",
        this.calm ? "waldo-calm" : "",
        this.roam ? "waldo-roam" : "",
        this.aiLit ? "waldo-ai-" + this.aiLit : "",
        this.fx.tipFlash ? "waldo-tip-flash" : "",
        this.fx.tipsFade ? "waldo-tips-fade" : "",
      ];
    },
    rootStyle() {
      return this.color ? { "--waldo-color": this.color } : {};
    },
    poseStyle() {
      return { transform: "translateY(" + this.sink + "px) rotate(" + this.headTilt + "deg)" };
    },
    pupilStyle() {
      return {
        transform: "translate(" + this.gaze[0] + "px, " + this.gaze[1] + "px)",
        transitionDuration: this.gazeSpeed + "s",
      };
    },
    eyesOpen() {
      return this.eyes === "open" || this.eyes === "wink";
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
    light(now, before) {
      if (now === "rec" && before !== "rec") this.react("cheese");
      else if (!this.acting) this.restFace();
    },
    agent(now, was) {
      const kind = agentReaction(was, now);
      if (kind) this.react(kind);
    },
    asking(now, was) {
      if (now && !was) this.react("ai-ask");
      else if (!this.acting) this.restFace();
    },
    look(now, was) {
      if (JSON.stringify(now) === JSON.stringify(was)) return;
      clearTimeout(this.lookTimer);
      if (now) {
        this.lookSince = Date.now();
        this.holdLook(now);
        return;
      }
      const left = LOOK_MIN_MS - (Date.now() - this.lookSince);
      if (left > 0) this.lookTimer = setTimeout(() => this.holdLook(null), left);
      else this.holdLook(null);
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
      if (r && r.name && (!old || r.seq !== old.seq)) this.start(r);
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
    this.lookTimer = null;
    this.lookSince = 0;
    this.trackFrame = null;
    this.tracking = false;
    this.lastTrackAt = 0;
    this.lastActivity = Date.now();
    this.pokes = [];
    this.pointer = null;
    this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  },

  mounted() {
    this.held = this.look;
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
    if (this.reaction && this.reaction.name) this.start(this.reaction);
    else if (this.asking) this.react("ai-ask");
  },

  unmounted() {
    this.token++;
    for (const id of this.timers) clearTimeout(id);
    clearTimeout(this.blinkTimer);
    clearTimeout(this.idleTimer);
    clearTimeout(this.lookTimer);
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

    antennaClass(a) {
      return "waldo-antenna-" + a.side + (this.antennaWave === a.side ? " waldo-wave" : "");
    },

    antennaStyle(a) {
      return { transformOrigin: a.x + "px 7.5px", transform: "rotate(" + this.antenna[a.side] + "deg)" };
    },

    eyeStyle(e) {
      return {
        transformOrigin: e.cx + "px 12px",
        transform: "scale(" + this.eyeScale + ")",
        opacity: e.side === "R" && this.eyes === "wink" ? 0 : 1,
      };
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
      if (this.held) return [this.held[0] * 0.55, this.held[1] * 0.45];
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
      this.gazeSpeed = this.held ? 0.25 : 0.4;
      if (!this.tracking) this.gaze = this.restLook();
      this.leds = face.leds || this.light || (this.busy && LIVELY.has(this.mood) ? "chase" : "");
      this.aiLit = this.agent;
      const droop = this.asleep ? 14 : this.mood === "sad" ? 5 : 0;
      this.antenna = { l: -droop, r: droop };
      this.antennaWave = "";
      this.headTilt = this.asleep ? 6 : this.held ? this.held[2] || 0 : 0;
      this.sink = this.asleep ? 0.5 : 0;
      for (const k of Object.keys(this.fx)) this.fx[k] = false;
      this.fx.zzz = this.asleep;
    },

    lookAt(x, y, speed = 0.4) {
      this.gazeSpeed = speed;
      this.gaze = [x, y];
    },

    // A jog's direction holds the eyes (and a rotation, the head) until the
    // jog lets go; whatever is playing gives way to it.
    holdLook(look) {
      this.held = look;
      if (look && this.acting) {
        this.token++;
        this.acting = false;
        this.current = "";
      }
      if (!this.acting) this.restFace();
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
        if (!this.acting && !this.asleep && !this.held && this.eyes === "open") {
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
        const free = !this.acting && !this.asleep && !this.busy && !this.held;
        if (free && !trackedRecently && !this.still()) {
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
          [2, async (wait) => { this.mouth = "grin"; this.eyes = "wink"; await wait(450); }],
          [1, async (wait) => { this.eyes = "hearts"; this.mouth = "open"; await wait(1600); }],
          [1, async (wait) => {
            this.eyes = "happy"; this.mouth = "o"; this.fx.note = true;
            await this.anim("sway", 2600, wait);
          }],
          [1, async (wait) => {
            this.mouth = "open";
            await this.anim("hop", 450, wait);
            this.wiggleAntennae(0.5);
            await this.anim("hop", 450, wait);
          }],
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
          [1, async (wait) => {
            this.lid = 0.45; this.lookAt(0.45, 0.35, 0.5); this.mouth = "slant";
            await wait(1800);
          }],
          [1, async (wait) => { this.lookAt(0.4, -0.45, 0.4); this.fx.dots = true; await wait(2000); }],
          [1, async (wait) => {
            this.eyeScale = 0.83; this.fx.scan = true;
            await wait(1100);
            this.fx.scan = false; this.eyeScale = 1;
            await this.blinkOnce(wait);
          }],
          [1, async (wait) => {
            this.lid = 0.7; this.lidSpeed = 0.5; this.mouth = "yawn";
            await this.anim("yawn", 1800, wait);
            this.mouth = "flat"; this.lid = 0.08;
            await wait(250);
            await this.blinkOnce(wait);
          }],
        ],
        sad: [
          [3, async (wait) => { this.lookAt(0, 0.55, 0.6); await wait(1800); }],
          [2, async (wait) => { this.lookAt(-0.55, 0.25, 0.7); await wait(1400); }],
          [2, async (wait) => { this.eyes = "closed"; this.mouth = "deep-frown"; await wait(2200); }],
          [2, async (wait) => {
            this.mouth = "small-o"; this.sink = 0.6; this.antenna = { l: -12, r: 12 };
            await wait(1300); this.mouth = "frown"; await wait(700);
          }],
          [1, async (wait) => { this.eyeScale = 1.17; this.mouth = "tremble"; await wait(1800); }],
          [1, async (wait) => {
            this.eyeScale = 1.14; this.lookAt(0, 0.25, 0.4); this.mouth = "tremble";
            await wait(400);
            this.fx.tear = true;
            await wait(1800);
          }],
          [1, async (wait) => { this.eyeScale = 0.89; this.mouth = "tremble"; await this.anim("shiver", 700, wait); }],
          [1, async (wait) => {
            this.eyeScale = 1.14; this.lookAt(0.45, -0.5, 0.4); this.mouth = "o";
            this.antenna = { l: 3, r: -3 };
            await wait(1500);
            this.eyeScale = 1; this.antenna = { l: -5, r: 5 };
            this.lookAt(0, 0.45, 0.6); this.mouth = "frown";
            await wait(900);
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
          this.mouth = "grin";
          this.leds = "party";
          this.fx.sparkle = true;
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
        warning: async (wait) => {
          this.lid = 0;
          this.eyeScale = 1.22;
          this.pupilScale = 0.73;
          this.mouth = "o";
          this.fx.sweat = true;
          this.lookAt(-0.5, 0, 0.15);
          await wait(250);
          this.lookAt(0.5, 0, 0.2);
          await wait(300);
          this.lookAt(0, 0, 0.2);
          await wait(700);
        },
        error: async (wait) => {
          this.eyes = "squeeze";
          this.mouth = "zigzag";
          this.fx.sweat = true;
          await this.anim("shake", 450, wait);
          await this.anim("shake", 450, wait);
          await wait(500);
        },
        relief: async (wait) => {
          this.eyes = "closed";
          this.mouth = "small-o";
          this.sink = 0.7;
          this.fx.sweat = true;
          await wait(900);
          this.sink = 0;
          this.mouth = "smile";
          await wait(500);
          this.eyes = "open";
          await this.blinkOnce(wait);
          await wait(300);
        },
        start: async (wait) => {
          this.eyeScale = 0.86;
          this.wiggleAntennae(0.5);
          await this.anim("dip", 450, wait);
          await wait(400);
        },
        home: async (wait) => {
          // The eyes roll once around, then a nod: back where it started.
          for (let i = 0; i <= 8; i++) {
            const a = (i / 8) * 2 * Math.PI - Math.PI / 2;
            this.lookAt(0.5 * Math.cos(a), 0.45 * Math.sin(a), 0.09);
            await wait(90);
          }
          this.lookAt(0, 0, 0.15);
          this.eyes = "happy";
          this.mouth = "smile";
          await this.anim("nod", 720, wait);
          await wait(200);
        },
        "grip-close": async (wait) => {
          this.mouth = "o";
          await wait(160);
          this.mouth = "zigzag";
          await this.anim("squeeze", 320, wait);
          await wait(350);
        },
        "grip-open": async (wait) => {
          this.eyeScale = 1.17;
          this.mouth = "o";
          await wait(500);
        },
        tool: async (wait) => {
          this.fx.sparkle = true;
          this.mouth = "o";
          await this.anim("spin", 650, wait);
          this.mouth = "grin";
          await wait(500);
        },
        cheese: async (wait) => {
          this.eyeScale = 1.22;
          this.mouth = "grin";
          await wait(450);
          await this.blinkOnce(wait, 80);
          await wait(200);
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
        headshake: async (wait) => {
          this.mouth = "slant";
          await this.anim("headshake", 650, wait);
          await wait(300);
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
        // An AI agent connects: its colour lights the tips and it sends a hello.
        "ai-hello": async (wait) => {
          this.eyeScale = 1.19;
          this.mouth = "o";
          this.fx.waves = true;
          await this.anim("lift", 450, wait);
          await wait(850);
          this.fx.waves = false;
          this.eyeScale = 1;
          this.mouth = "grin";
          await wait(450);
        },
        // It leaves: the tips go dark and the antennae droop.
        "ai-bye": async (wait) => {
          this.aiLit = "present";
          this.fx.tipsFade = true;
          this.lookAt(0, 0.4, 0.4);
          this.antenna = { l: -12, r: 12 };
          await wait(600);
          this.aiLit = "";
          await wait(600);
        },
        // It takes the controls: a scan sweeps down the face, then the eyes
        // take its colour.
        "ai-take": async (wait) => {
          this.aiLit = "present";
          this.eyeScale = 0.86;
          this.mouth = "flat";
          this.fx.aiScan = true;
          await wait(750);
          this.fx.aiScan = false;
          this.aiLit = "driving";
          this.fx.waves = true;
          await wait(650);
        },
        // The human takes them back: shake it off, blink, breathe out.
        "ai-release": async (wait) => {
          this.mouth = "zigzag";
          await this.anim("shake", 450, wait);
          await this.blinkOnce(wait, 140);
          this.mouth = "o";
          await this.anim("sigh", 700, wait);
        },
        // A request waits for approval: a tilt and a look up at the question.
        "ai-ask": async (wait) => {
          this.mouth = "slant";
          this.lookAt(0.5, -0.4, 0.3);
          await this.anim("ask", 1100, wait);
        },
        // A new AI control mode: the tips flash in its colour.
        "ai-mode": async (wait) => {
          this.wiggleAntennae(0.5);
          this.fx.tipFlash = true;
          await wait(650);
        },
      };
      const sequence = sequences[name];
      return sequence ? this.play(sequence, name) : Promise.resolve();
    },

    start(r) {
      if (r.peek) this.peek(r.name);
      else this.react(r.name);
    },

    sleepMs(ms) {
      return new Promise((resolve) => {
        const id = setTimeout(() => {
          this.timers.delete(id);
          resolve();
        }, ms);
        this.timers.add(id);
      });
    },

    // Rise out of the clipping window this Waldo rests below, play a
    // reaction, and sink back out of sight.
    async peek(name) {
      const el = this.$el;
      const frame = el.parentElement;
      if (!frame || typeof el.animate !== "function" || frame.classList.contains("waldo-peeking")) {
        this.react(name);
        return;
      }
      const up = [{ transform: "translateY(105%)" }, { transform: "translateY(0)" }];
      frame.classList.add("waldo-peeking");
      const rise = el.animate(up, {
        duration: this.reducedMotion ? 0 : 320,
        easing: "cubic-bezier(0.34, 1.56, 0.64, 1)",
        fill: "forwards",
      });
      await rise.finished.catch(() => {});
      await this.react(name);
      await this.sleepMs(PEEK_HOLD_MS);
      const sink = el.animate([...up].reverse(), {
        duration: this.reducedMotion ? 0 : 260,
        easing: "ease-in",
        fill: "forwards",
      });
      await sink.finished.catch(() => {});
      frame.classList.remove("waldo-peeking");
      rise.cancel();
      sink.cancel();
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
      const free = !this.acting && !this.busy && !this.calm && !this.held && LIVELY.has(this.mood);
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
.waldo {
  display: inline-block;
  flex-shrink: 0;
  line-height: 0;
  color: var(--waldo-color);
  transition: color 0.6s ease;
  /* Floating glyphs sit on the page, not the body: follow the theme's text. */
  --waldo-glyph: var(--wc-text);
}
.waldo.waldo-mood-happy { --waldo-color: var(--wc-positive); }
.waldo.waldo-mood-neutral { --waldo-color: var(--wc-mode-sim); }
.waldo.waldo-mood-sad { --waldo-color: var(--wc-error); }
.waldo.waldo-mood-alarmed { --waldo-color: var(--wc-error); }
.waldo.waldo-mood-booting { --waldo-color: var(--wc-text-muted); }

.waldo svg {
  width: 100%;
  height: 100%;
  overflow: visible;
  animation: waldo-breathe 6s ease-in-out infinite;
}
.waldo.waldo-mood-neutral svg { animation-duration: 7s; animation-delay: -2s; }
.waldo.waldo-mood-sad svg { animation-duration: 8s; animation-delay: -4s; }
.waldo.waldo-mood-alarmed svg { animation-duration: 1.4s; }
.waldo.waldo-asleep svg { animation-duration: 4.5s; }
.waldo.waldo-calm svg { animation: none; }
@keyframes waldo-breathe {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2px); }
}

.waldo .waldo-pose,
.waldo .waldo-antenna,
.waldo .waldo-eye,
.waldo .waldo-eye circle { transition: transform 0.3s ease; }
.waldo .waldo-pose { transition-duration: 0.5s; }
.waldo .waldo-pupil,
.waldo .waldo-lid { transition: transform 0.25s ease; }
.waldo .waldo-eyes > g,
.waldo .waldo-eyes-open,
.waldo .waldo-eye,
.waldo .waldo-mouth > * { transition: opacity 0.15s ease, transform 0.3s ease; }

/* The cut mask: keep the body, cut the features out of it. */
.waldo .waldo-keep { fill: var(--wc-mask-keep); }
.waldo .waldo-cut { fill: var(--wc-mask-cut); }
.waldo .waldo-stroke,
.waldo .waldo-stroke path {
  fill: none;
  stroke: var(--wc-mask-cut);
  stroke-width: 1;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.waldo .waldo-fill { fill: var(--wc-mask-cut); stroke: var(--wc-mask-cut); stroke-width: 0.4; stroke-linejoin: round; }
.waldo .waldo-eyes-alt.waldo-stroke path { stroke-width: 1.1; }
.waldo .waldo-eyes-alt.waldo-eyes-x path { stroke-width: 0.75; }
.waldo .waldo-eyes-alt.waldo-eyes-squeeze path { stroke-width: 0.9; }
.waldo .waldo-scan-bar { animation: waldo-scan 1.1s ease-in-out infinite alternate; }
.waldo .waldo-sweep { animation: waldo-sweep 1.1s ease-in-out forwards; }
.waldo .waldo-heart { transform-box: fill-box; transform-origin: center; animation: waldo-heart 1.6s ease-out; }

.waldo .waldo-led { fill: var(--wc-mask-cut); opacity: 0; }
.waldo .waldo-halo { fill: none; stroke: currentColor; stroke-width: 0.35; opacity: 0; }
.waldo.waldo-leds-chase .waldo-led { animation: waldo-led 1.2s ease-in-out infinite; }
.waldo.waldo-leds-chase .waldo-antenna-r .waldo-led { animation-delay: 0.6s; }
.waldo.waldo-leds-pulse .waldo-led { animation: waldo-led 1.6s ease-in-out infinite; }
.waldo.waldo-leds-pulse .waldo-antenna-r .waldo-led { animation-delay: 0.3s; }
.waldo.waldo-leds-alarm .waldo-led { animation: waldo-led 0.5s steps(2, jump-none) infinite; }
.waldo.waldo-leds-alarm .waldo-halo { animation: waldo-halo 0.5s ease-out infinite; }
.waldo.waldo-leds-alarm .waldo-antenna-r .waldo-led,
.waldo.waldo-leds-alarm .waldo-antenna-r .waldo-halo { animation-delay: 0.25s; }
.waldo.waldo-leds-party .waldo-led { animation: waldo-led 0.3s ease-in-out infinite; }
.waldo.waldo-leds-party .waldo-antenna-r .waldo-led { animation-delay: 0.15s; }

/* The left bulb turns record red while the motion recorder runs. */
.waldo.waldo-leds-rec .waldo-antenna-l .waldo-bulb { fill: var(--wc-record); }
.waldo.waldo-leds-rec .waldo-antenna-l .waldo-led { animation: waldo-glow 2s ease-in-out infinite; }
@keyframes waldo-glow {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 0; }
}

/* An AI agent: its control mode's colour on the antenna tips while it is
   connected; while it drives, the tips pulse and the eyes take it too. A
   placement outside the status chip has no mode and keeps Waldo's colour. */
.waldo .waldo-tip {
  fill: var(--waldo-ai, currentColor);
  opacity: 0;
  transform-box: fill-box;
  transform-origin: center;
  transition: opacity 0.3s ease;
}
.waldo.waldo-ai-present .waldo-tip,
.waldo.waldo-ai-driving .waldo-tip { opacity: 1; }
.waldo.waldo-ai-driving .waldo-tip { animation: waldo-tip-pulse 0.9s ease-in-out infinite; }
.waldo.waldo-ai-driving .waldo-pupil circle { fill: var(--waldo-ai, currentColor); }
.waldo.waldo-tip-flash .waldo-tip { animation: waldo-tip-flash 0.45s ease-out; }
.waldo.waldo-tips-fade .waldo-tip { animation: waldo-tip-fade 0.6s ease-in forwards; }
.waldo .waldo-waves path {
  fill: none;
  stroke: var(--waldo-ai, currentColor);
  stroke-width: 0.35;
  stroke-linecap: round;
  opacity: 0;
  animation: waldo-wave-out 0.65s ease-out 2;
}
.waldo .waldo-waves .waldo-wave-outer { animation-delay: 0.14s; }
.waldo .waldo-ai-scan {
  fill: var(--waldo-ai, currentColor);
  opacity: 0;
  animation: waldo-sweep 0.75s ease-in-out forwards;
}
.waldo .waldo-ask .waldo-glyph { stroke: var(--waldo-ai, var(--waldo-glyph)); }
.waldo .waldo-ask .waldo-glyph-dot { fill: var(--waldo-ai, var(--waldo-glyph)); }

.waldo .waldo-drop { fill: var(--wc-info); }
.waldo .waldo-sweat-slide { animation: waldo-sweat 1.2s ease-in forwards; }
.waldo .waldo-tear {
  opacity: 0;
  transform-box: fill-box;
  transform-origin: center;
  animation: waldo-tear 1.8s ease-in forwards;
}

.waldo .waldo-alert { fill: var(--wc-warning); stroke: var(--wc-scrim); stroke-width: 0.15; }
.waldo .waldo-glyph {
  fill: none;
  stroke: var(--waldo-glyph);
  stroke-width: 0.45;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.waldo .waldo-glyph-dot { fill: var(--waldo-glyph); }
.waldo .waldo-z { stroke-width: 0.42; opacity: 0; animation: waldo-z 2.7s ease-out infinite; }
.waldo .waldo-pop { transform-box: fill-box; transform-origin: bottom center; animation: waldo-pop 0.35s cubic-bezier(0.3, 1.8, 0.5, 1); }
.waldo .waldo-note { animation: waldo-float 1.3s ease-out 2 forwards; opacity: 0; }
.waldo .waldo-note .waldo-glyph { stroke-width: 0.35; }
.waldo .waldo-dots circle { opacity: 0; animation: waldo-dot 0.9s ease-in-out 2; }
.waldo .waldo-dots circle:nth-child(2) { animation-delay: 0.18s; }
.waldo .waldo-dots circle:nth-child(3) { animation-delay: 0.36s; }
.waldo .waldo-sparkle path {
  fill: currentColor;
  opacity: 0;
  transform-box: fill-box;
  transform-origin: center;
  animation: waldo-sparkle 1.1s ease-out forwards;
}
.waldo .waldo-sparkle path:nth-child(2) { animation-delay: 0.12s; }

.waldo .waldo-rig { transform-origin: 12px 18.5px; }
.waldo .waldo-anim-hop { animation: waldo-hop 0.45s ease-out; }
.waldo .waldo-anim-hop2 { animation: waldo-hop 0.45s ease-out 2; }
.waldo .waldo-anim-jolt { animation: waldo-jolt 0.35s ease-out; }
.waldo .waldo-anim-shake { animation: waldo-shake 0.45s linear; }
.waldo .waldo-anim-shiver { animation: waldo-shiver 0.7s linear; }
.waldo .waldo-anim-wobble { animation: waldo-wobble 0.6s ease-in-out 3; }
.waldo .waldo-anim-squash { animation: waldo-squash 0.3s ease-out; }
.waldo .waldo-anim-squeeze { animation: waldo-squeeze 0.32s ease-out; }
.waldo .waldo-anim-shrug { animation: waldo-shrug 0.52s ease-in-out; }
.waldo .waldo-anim-nod { animation: waldo-nod 0.36s ease-in-out 2; }
.waldo .waldo-anim-dip { animation: waldo-dip 0.45s ease-in-out; }
.waldo .waldo-anim-lift { animation: waldo-lift 0.45s ease-out; }
.waldo .waldo-anim-sigh { animation: waldo-sigh 0.7s ease-in-out; }
.waldo .waldo-anim-headshake { animation: waldo-headshake 0.65s ease-in-out; }
.waldo .waldo-anim-ask { animation: waldo-ask 1.1s ease-in-out; }
.waldo .waldo-anim-sway { animation: waldo-sway 2.6s ease-in-out; }
.waldo .waldo-anim-yawn { animation: waldo-yawn 1.8s ease-in-out; }
.waldo .waldo-anim-spin { transform-origin: 12px 11px; animation: waldo-spin 0.65s ease-in-out; }
.waldo .waldo-wave { animation: waldo-wave 0.45s ease-in-out 3; }

@keyframes waldo-hop {
  0% { transform: translateY(0) scale(1, 1); }
  15% { transform: translateY(0) scale(1.06, 0.92); }
  45% { transform: translateY(-2.2px) scale(0.96, 1.05); }
  80% { transform: translateY(0) scale(1.05, 0.94); }
  100% { transform: translateY(0) scale(1, 1); }
}
@keyframes waldo-jolt {
  0% { transform: translateY(0) scale(1); }
  30% { transform: translateY(-1.2px) scale(1.06); }
  100% { transform: translateY(0) scale(1); }
}
@keyframes waldo-shake {
  0%, 100% { transform: translateX(0); }
  20%, 60% { transform: translateX(-0.6px); }
  40%, 80% { transform: translateX(0.6px); }
}
@keyframes waldo-shiver {
  0%, 100% { transform: translateX(0); }
  10%, 30%, 50%, 70%, 90% { transform: translateX(-0.28px); }
  20%, 40%, 60%, 80% { transform: translateX(0.28px); }
}
@keyframes waldo-wobble {
  0%, 100% { transform: rotate(0); }
  25% { transform: rotate(-9deg); }
  75% { transform: rotate(9deg); }
}
@keyframes waldo-shrug {
  0%, 100% { transform: translateY(0); }
  30%, 70% { transform: translateY(-0.8px); }
}
@keyframes waldo-nod {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(0.9px) scale(1, 0.97); }
}
@keyframes waldo-squash {
  0% { transform: scale(1, 1); }
  35% { transform: scale(1.14, 0.84); }
  100% { transform: scale(1, 1); }
}
@keyframes waldo-squeeze {
  0%, 100% { transform: scale(1, 1); }
  40% { transform: scale(1.05, 0.94); }
}
@keyframes waldo-dip {
  0%, 100% { transform: translateY(0); }
  40% { transform: translateY(0.7px); }
}
@keyframes waldo-lift {
  0%, 100% { transform: translateY(0); }
  30% { transform: translateY(-0.6px); }
}
@keyframes waldo-sigh {
  0%, 100% { transform: translateY(0) scale(1, 1); }
  50% { transform: translateY(0.5px) scale(1.03, 0.96); }
}
@keyframes waldo-headshake {
  0%, 100% { transform: rotate(0); }
  20% { transform: rotate(-8deg); }
  45% { transform: rotate(8deg); }
  70% { transform: rotate(-5deg); }
}
@keyframes waldo-ask {
  0%, 100% { transform: rotate(0); }
  40% { transform: rotate(-7deg); }
  80% { transform: rotate(-5deg); }
}
@keyframes waldo-sway {
  0%, 100% { transform: rotate(0); }
  25% { transform: rotate(-4deg); }
  75% { transform: rotate(4deg); }
}
@keyframes waldo-yawn {
  0%, 100% { transform: rotate(0) scale(1); }
  50% { transform: rotate(-5deg) scale(1.03); }
}
@keyframes waldo-spin {
  0% { transform: rotate(0) scale(1); }
  50% { transform: rotate(180deg) scale(0.85); }
  100% { transform: rotate(360deg) scale(1); }
}
@keyframes waldo-wave {
  0%, 100% { transform: rotate(0); }
  50% { transform: rotate(28deg); }
}
@keyframes waldo-scan { to { transform: translateX(9.8px); } }
@keyframes waldo-sweep {
  0% { opacity: 0; transform: translateY(0); }
  10% { opacity: 0.9; transform: translateY(1px); }
  90% { opacity: 0.9; transform: translateY(8.6px); }
  100% { opacity: 0; transform: translateY(9.55px); }
}
@keyframes waldo-led {
  0%, 100% { opacity: 0; }
  50% { opacity: 0.95; }
}
@keyframes waldo-halo {
  0% { opacity: 0.9; transform: scale(0.5); }
  100% { opacity: 0; transform: scale(1.4); }
}
.waldo .waldo-halo { transform-box: fill-box; transform-origin: center; }
@keyframes waldo-tip-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
@keyframes waldo-tip-flash {
  40% { transform: scale(1.8); }
}
@keyframes waldo-tip-fade {
  from { opacity: 1; }
  to { opacity: 0; }
}
@keyframes waldo-wave-out {
  0% { opacity: 0; transform: scale(0.6); }
  35% { opacity: 1; }
  100% { opacity: 0; transform: scale(1.3); }
}
@keyframes waldo-sweat {
  0% { opacity: 0; transform: translateY(-0.3px); }
  20% { opacity: 1; transform: translateY(0); }
  70% { opacity: 1; transform: translateY(0.8px); }
  100% { opacity: 0; transform: translateY(1.8px); }
}
@keyframes waldo-tear {
  0% { opacity: 0; transform: translateY(-0.4px) scale(0.6); }
  20% { opacity: 0.95; transform: translateY(0) scale(1); }
  75% { opacity: 0.9; transform: translateY(1.6px); }
  100% { opacity: 0; transform: translateY(2.5px); }
}
@keyframes waldo-heart {
  0% { transform: scale(0.2); }
  25% { transform: scale(1.2); }
  40%, 75% { transform: scale(1); }
  60%, 90% { transform: scale(1.12); }
  100% { transform: scale(1); }
}
@keyframes waldo-z {
  0% { opacity: 0; transform: translate(0, 0.6px); }
  25% { opacity: 1; }
  100% { opacity: 0; transform: translate(0.9px, -1.4px); }
}
@keyframes waldo-float {
  0% { opacity: 0; transform: translate(0, 0.6px); }
  30% { opacity: 1; transform: translate(-0.3px, -0.4px); }
  70% { opacity: 1; transform: translate(0.2px, -1.4px); }
  100% { opacity: 0; transform: translate(-0.2px, -2.2px); }
}
@keyframes waldo-dot {
  0%, 100% { opacity: 0; transform: translateY(0); }
  35% { opacity: 1; transform: translateY(-0.5px); }
}
@keyframes waldo-sparkle {
  0% { opacity: 0; transform: scale(0.3) rotate(0); }
  20% { opacity: 1; }
  40% { transform: scale(1.25) rotate(45deg); }
  70% { opacity: 1; }
  100% { opacity: 0; transform: scale(0.8) rotate(90deg); }
}
@keyframes waldo-pop {
  from { transform: scale(0); }
  to { transform: scale(1); }
}

.waldo.waldo-roam {
  position: fixed;
  top: 0;
  left: 0;
  pointer-events: none;
}

@media (prefers-reduced-motion: reduce) {
  .waldo svg,
  .waldo .waldo-rig,
  .waldo .waldo-scan-bar,
  .waldo .waldo-heart,
  .waldo .waldo-tip,
  .waldo .waldo-wave { animation: none !important; }
}
</style>
