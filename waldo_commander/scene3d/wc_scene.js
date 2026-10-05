// The 3D view's Vue component: it hosts the SceneCore and passes it the ops
// Python sends. It asks for the whole scene when it mounts, and again when
// the socket reconnects, since anything sent meanwhile may be lost.

import { SceneCore } from "wc-scene";

export default {
  template: `
    <div style="position:relative;overflow:hidden">
      <canvas style="display:block;width:100%;height:100%"></canvas>
      <div class="wc-scene-labels" style="position:absolute;top:0;left:0;pointer-events:none"></div>
      <div class="wc-scene-lost" style="position:absolute;inset:0;display:none;place-items:center;color:var(--wc-text-muted)"></div>
    </div>`,

  mounted() {
    this.core = new SceneCore(
      this.$el,
      (name, args) => this.$emit(name, args),
      () => {
        const definition = mounted_app.elements[this.$el.id.slice(1)];
        const tag = definition.tag;
        definition.tag = "";
        this.$nextTick(() => (definition.tag = tag));
      },
    );
    // NiceGUI opens its socket after the page's elements have mounted.
    this.onConnect = () => {
      if (this.connected) this.$emit("init");
      this.connected = true;
    };
    const hook = () => {
      if (this.unmounted) return;
      if (!window.socket) {
        this.hookTimer = setTimeout(hook, 0);
        return;
      }
      this.connected = window.socket.connected;
      window.socket.on("connect", this.onConnect);
    };
    hook();
    this.$emit("init");
  },

  beforeUnmount() {
    this.unmounted = true;
    clearTimeout(this.hookTimer);
    if (window.socket) window.socket.off("connect", this.onConnect);
    this.core.dispose();
  },

  methods: {
    apply(ops) {
      this.core.apply(ops);
    },
  },
};
