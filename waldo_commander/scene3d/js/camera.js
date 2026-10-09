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

  startOrbit(pose, durationS=0.35) {
    this.start(pose,durationS,true);
    const m=this.move;
    if (!m) return;
    const from=new THREE.Vector3(...m.from.slice(0,3)).sub(new THREE.Vector3(...m.from.slice(3)));
    const to=new THREE.Vector3(...pose.slice(0,3)).sub(new THREE.Vector3(...pose.slice(3)));
    m.orbit={radius:from.length(), endRadius:to.length(), direction:from.normalize(),
      rotation:new THREE.Quaternion().setFromUnitVectors(from,to.normalize())};
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
    if (m.orbit) {
      const o=m.orbit, target=m.from.slice(3).map((a,i)=>a+(m.to[i+3]-a)*k);
      const position=o.direction.clone().applyQuaternion(new THREE.Quaternion().slerp(o.rotation,k))
        .multiplyScalar(o.radius+(o.endRadius-o.radius)*k).add(new THREE.Vector3(...target));
      this.apply([...position.toArray(),...target]);
    } else this.apply(m.from.map((a, i) => a + (m.to[i] - a) * k));
    if (t >= 1) this.move = null;
    return this.move !== null;
  }
}
