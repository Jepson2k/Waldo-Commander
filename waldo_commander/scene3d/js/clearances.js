import * as THREE from "three";
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { setStyle, setText } from "./dom.js";

export class Clearances {
  constructor(core) {
    this.core = core;
    this.items = new Map();
  }

  load(lines) {
    const wanted = new Set(lines.map(line => line.obstacle));
    for (const [name, item] of this.items) {
      if (!wanted.has(name)) this.remove(name, item);
    }
    for (const data of lines) {
      let item = this.items.get(data.obstacle);
      if (!item) {
        const group = new THREE.Group();
        group.name = `clearance:${data.obstacle}`;
        group.renderOrder = 1000;
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.Float32BufferAttribute(new Float32Array(6), 3));
        // Transparent geometry renders after the opaque queue, even without depth testing.
        const options = { color: data.color, transparent: true, depthTest: false, depthWrite: false, toneMapped: false, fog: false };
        const line = new THREE.Line(geometry, new THREE.LineBasicMaterial(options));
        const dots = new THREE.Points(geometry, new THREE.PointsMaterial({ ...options, size: 6, sizeAttenuation: false }));
        dots.material.onBeforeCompile = shader => {
          shader.fragmentShader = shader.fragmentShader.replace(
            "#include <clipping_planes_fragment>",
            "#include <clipping_planes_fragment>\nif (length(gl_PointCoord - vec2(0.5)) > 0.5) discard;",
          );
        };
        line.renderOrder = 1000;
        dots.renderOrder = 1001;
        const label = new CSS2DObject(document.createElement("div"));
        label.element.className = "wc-gizmo-field wc-clearance-label";
        group.add(line, dots, label);
        this.core.scene.add(group);
        item = { group, geometry, line, dots, label };
        this.items.set(data.obstacle, item);
      }
      const previous = item.data;
      if (!previous || data.start.some((v, i) => v !== previous.start[i]) || data.end.some((v, i) => v !== previous.end[i])) {
        const position = item.geometry.attributes.position;
        position.array.set(data.start, 0);
        position.array.set(data.end, 3);
        position.needsUpdate = true;
        item.geometry.computeBoundingSphere();
        item.label.position.fromArray(data.start).add(new THREE.Vector3().fromArray(data.end)).multiplyScalar(0.5);
      }
      if (previous?.color !== data.color) {
        item.line.material.color.set(data.color);
        item.dots.material.color.set(data.color);
        setStyle(item.label.element, "color", data.color);
      }
      setText(item.label.element, data.label);
      item.data = data;
    }
    this.core.requestRender();
  }

  remove(name, item) {
    item.group.removeFromParent();
    item.label.element.remove();
    item.geometry.dispose();
    item.line.material.dispose();
    item.dots.material.dispose();
    this.items.delete(name);
  }

  dispose() {
    for (const [name, item] of this.items) this.remove(name, item);
  }
}
