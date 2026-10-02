export default {
  template: `
    <svg :viewBox="'0 0 ' + size + ' ' + size" class="joint-dial-svg">
      <path class="dial-track" :d="track" />
      <path class="dial-fill" :d="fillPath" />
      <path class="dial-tick" :d="ticks" />
      <circle class="dial-knob" :cx="knobAt[0]" :cy="knobAt[1]" :r="knobRadius" />
    </svg>
  `,
  props: {
    size: Number,
    track: String,
    fill: String,
    ticks: String,
    knob: Array,
    knobRadius: Number,
  },
  data() {
    return { fillPath: this.fill, knobAt: this.knob };
  },
  watch: {
    fill(value) {
      this.fillPath = value;
    },
    knob(value) {
      this.knobAt = value;
    },
  },
  methods: {
    show(fill, knob) {
      this.fillPath = fill;
      this.knobAt = knob;
    },
  },
};
