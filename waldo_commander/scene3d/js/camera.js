// Camera moves Python asks for: from where the camera is to a position and
// a point to look at, over a duration, optionally eased in and out. With
// reduced motion the camera jumps there.

import * as THREE from "three";
import { reducedMotion } from "./fx.js";

const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export class CameraMove {
  constructor(core) {
    this.core = core;
    this.move = null;
  }

  get active() {
    return this.move !== null;
  }

  start(pose, durationS, ease) {
    const { camera, controls } = this.core;
    const from = [...camera.position.toArray(), ...controls.target.toArray()];
    this.move = { from, to: pose, start: performance.now(), ms: durationS * 1000, ease };
    if (durationS <= 0 || reducedMotion()) this.finish();
    else this.core.animate();
  }

  // A turn about the target, by heading and elevation: a straight path over
  // the top would flip the Z-up camera as it passed overhead.
  startOrbit(pose, durationS=0.35) {
    this.start(pose,durationS,true);
    const m=this.move;
    if (!m) return;
    const from=new THREE.Vector3(...m.from.slice(0,3)).sub(new THREE.Vector3(...m.from.slice(3)));
    const to=new THREE.Vector3(...pose.slice(0,3)).sub(new THREE.Vector3(...pose.slice(3)));
    const heading=(v)=>Math.atan2(v.y,v.x), elevation=(v)=>Math.atan2(v.z,Math.hypot(v.x,v.y));
    const turn=heading(to)-heading(from);
    m.orbit={radius:from.length(), endRadius:to.length(), heading:heading(from),
      turn:turn-2*Math.PI*Math.round(turn/(2*Math.PI)), elevation:elevation(from), rise:elevation(to)-elevation(from)};
  }

  finish() {
    if (!this.move) return;
    this.apply(this.move.to);
    this.move = null;
  }

  apply(p) {
    const { camera, controls } = this.core;
    camera.position.set(p[0], p[1], p[2]);
    controls.target.set(p[3], p[4], p[5]);
    camera.lookAt(controls.target);
    controls.update();
    this.core.requestRender();
  }

  step(now) {
    const m = this.move;
    if (!m) return false;
    const t = Math.min(1, (now - m.start) / m.ms);
    const k = m.ease ? easeInOutCubic(t) : t;
    if (m.orbit && t < 1) {
      const o=m.orbit, target=m.from.slice(3).map((a,i)=>a+(m.to[i+3]-a)*k);
      const h=o.heading+o.turn*k, e=o.elevation+o.rise*k, r=o.radius+(o.endRadius-o.radius)*k;
      this.apply([target[0]+r*Math.cos(e)*Math.cos(h),target[1]+r*Math.cos(e)*Math.sin(h),target[2]+r*Math.sin(e),...target]);
    } else if (m.orbit) this.apply(m.to);
    else this.apply(m.from.map((a, i) => a + (m.to[i] - a) * k));
    if (t >= 1) this.move = null;
    return this.move !== null;
  }
}
