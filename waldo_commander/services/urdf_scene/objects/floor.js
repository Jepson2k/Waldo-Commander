import { THREE } from "nicegui-scene";

const smoothstep = (e0, e1, x) => {
  const t = Math.min(Math.max((x - e0) / (e1 - e0), 0), 1);
  return t * t * (3 - 2 * t);
};

// The floor under the robot, in the XY plane (the scene is z-up). Three
// coplanar layers: an unlit tint that dissolves into the background past the
// reach, a shadow catcher, and a polar grid whose lines fade out with radius.
export default class Floor {
  create_mesh(reach, sectors, rings, color, gridColor) {
    const group = new THREE.Group();
    const outer = reach * 1.5;

    // Tint alpha sampled per pixel in units of the reach: 0.85 under the
    // robot, gone by 1.3 reach. The disc's UV square maps its radius to 0.5.
    const size = 512;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(size, size);
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const d = (Math.hypot(x - size / 2 + 0.5, y - size / 2 + 0.5) / (size / 2)) * (outer / reach);
        const a = 0.85 * (1 - smoothstep(0.45, 1.3, d));
        const i = (y * size + x) * 4;
        img.data[i] = img.data[i + 1] = img.data[i + 2] = Math.round(a * 255);
        img.data[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    const tint = new THREE.Mesh(
      new THREE.CircleGeometry(outer, 96),
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        alphaMap: new THREE.CanvasTexture(canvas),
        depthWrite: false,
      }),
    );
    tint.position.z = -0.0006;
    tint.renderOrder = 0;
    group.add(tint);

    const shadow = new THREE.Mesh(
      new THREE.CircleGeometry(outer, 96),
      new THREE.ShadowMaterial({ opacity: 0.55, transparent: true, depthWrite: false }),
    );
    shadow.receiveShadow = true;
    shadow.position.z = -0.0004;
    shadow.renderOrder = 1;
    group.add(shadow);

    // Rings every reach/rings out to 1.2 reach, and spokes, as line segments
    // whose per-vertex alpha fades from 0.35 reach to nothing at 1.15 reach.
    const line = new THREE.Color(gridColor);
    const positions = [];
    const colors = [];
    const alpha = (r) => 1 - smoothstep(0.35, 1.15, r / reach);
    const push = (x, y, r) => {
      positions.push(x, y, 0.0002);
      colors.push(line.r, line.g, line.b, alpha(r));
    };
    const divisions = 96;
    const spacing = reach / rings;
    const ringCount = Math.ceil((1.2 * reach) / spacing);
    for (let ring = 1; ring <= ringCount; ring++) {
      const r = spacing * ring;
      for (let i = 0; i < divisions; i++) {
        const a0 = (i / divisions) * Math.PI * 2;
        const a1 = ((i + 1) / divisions) * Math.PI * 2;
        push(r * Math.cos(a0), r * Math.sin(a0), r);
        push(r * Math.cos(a1), r * Math.sin(a1), r);
      }
    }
    const steps = 24;
    const spokeLength = 1.2 * reach;
    for (let s = 0; s < sectors; s++) {
      const a = (s / sectors) * Math.PI * 2;
      for (let i = 0; i < steps; i++) {
        const r0 = (spokeLength * i) / steps;
        const r1 = (spokeLength * (i + 1)) / steps;
        push(r0 * Math.cos(a), r0 * Math.sin(a), r0);
        push(r1 * Math.cos(a), r1 * Math.sin(a), r1);
      }
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 4));
    const grid = new THREE.LineSegments(
      geometry,
      new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false }),
    );
    grid.renderOrder = 2;
    group.add(grid);
    return group;
  }
}
