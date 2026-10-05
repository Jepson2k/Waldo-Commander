// The three.js object for each kind of scene node, and how a node's material
// is painted onto it.

import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const GEOMETRIES = {
  box: (a) => new THREE.BoxGeometry(...a),
  sphere: (a) => new THREE.SphereGeometry(...a),
  cylinder: (a) => new THREE.CylinderGeometry(...a),
  capsule: (a) => new THREE.CapsuleGeometry(...a),
  lathe: ([points, ...rest]) =>
    new THREE.LatheGeometry(points.map(([x, y]) => new THREE.Vector2(x, y)), ...rest),
};

export function build(kind, args, wireframe) {
  const geometry = GEOMETRIES[kind];
  if (geometry) {
    const g = geometry(args);
    if (wireframe) {
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineBasicMaterial({ transparent: true }));
      g.dispose();
      return edges;
    }
    return new THREE.Mesh(g, new THREE.MeshPhongMaterial({ transparent: true }));
  }
  switch (kind) {
    case "group":
    case "joint":
    case "stl":
      return new THREE.Group();
    case "line": {
      const [start, end] = args;
      const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(...start), new THREE.Vector3(...end)]);
      return new THREE.Line(g, new THREE.LineBasicMaterial({ transparent: true }));
    }
    case "polyline":
      return polyline(...args);
    case "floor":
      return floor(...args);
    case "lights":
      return lights(...args);
    default:
      throw new Error(`unknown scene object kind ${kind}`);
  }
}

// Vertex colours are taken as given, so a colour already linearised and one
// still in sRGB both draw as their sender meant.
function polyline(points, colors, dashed, dashSize, gapSize) {
  const g = new THREE.BufferGeometry().setFromPoints(points.map((p) => new THREE.Vector3(p[0], p[1], p[2])));
  const vertexColors = !!colors;
  if (vertexColors) g.setAttribute("color", new THREE.Float32BufferAttribute(colors.flat(), 3));
  if (dashed) {
    const line = new THREE.Line(
      g,
      new THREE.LineDashedMaterial({ transparent: true, dashSize, gapSize, vertexColors }),
    );
    line.computeLineDistances();
    return line;
  }
  return new THREE.Line(g, new THREE.LineBasicMaterial({ transparent: true, vertexColors }));
}

// The materials of a node's drawable: the mesh or line that shows it.
export function materials(drawable) {
  const m = drawable.material;
  return Array.isArray(m) ? m : [m];
}

export function paint(drawable, material) {
  const [color, opacity, side] = material;
  const vertexColors = color === null;
  for (const mat of materials(drawable)) {
    if (vertexColors) mat.color.setRGB(1, 1, 1);
    else mat.color.set(color);
    if (mat.vertexColors !== vertexColors) {
      mat.vertexColors = vertexColors;
      mat.needsUpdate = true;
    }
    mat.opacity = opacity;
    mat.side = side === "back" ? THREE.BackSide : side === "both" ? THREE.DoubleSide : THREE.FrontSide;
  }
}

export function clip(drawable, planes) {
  const clipping = planes.map(([nx, ny, nz, d]) => new THREE.Plane(new THREE.Vector3(nx, ny, nz).normalize(), d));
  for (const mat of materials(drawable)) {
    mat.clippingPlanes = clipping;
    mat.clipIntersection = false;
    mat.needsUpdate = true;
  }
}

// ---- STL meshes ----------------------------------------------------------

const loader = new STLLoader();

// Smoothed geometry per URL: tool changes and remounts load the same meshes
// again, and smoothing is the slow part. A file that changes in place is
// sent under a new URL.
const smoothed = new Map();

// STL files carry one flat normal per triangle, so curved surfaces render as
// facets. Average the normals of the triangles that meet at each vertex,
// area-weighted, but only across triangles within the crease angle, so
// machined edges stay sharp while cylinders and fillets shade smoothly.
function smoothNormals(geometry, creaseDegrees) {
  const pos = geometry.attributes.position;
  const count = pos.count;
  const faces = count / 3;
  const faceNormal = new Float32Array(faces * 3);
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const cb = new THREE.Vector3();
  for (let f = 0; f < faces; f++) {
    a.fromBufferAttribute(pos, f * 3);
    b.fromBufferAttribute(pos, f * 3 + 1);
    c.fromBufferAttribute(pos, f * 3 + 2);
    cb.subVectors(c, b);
    ab.subVectors(a, b);
    cb.cross(ab); // length is twice the face area: the weight
    faceNormal[f * 3] = cb.x;
    faceNormal[f * 3 + 1] = cb.y;
    faceNormal[f * 3 + 2] = cb.z;
  }
  const buckets = new Map();
  for (let i = 0; i < count; i++) {
    const key = `${Math.round(pos.getX(i) * 1e5)},${Math.round(pos.getY(i) * 1e5)},${Math.round(pos.getZ(i) * 1e5)}`;
    let bucket = buckets.get(key);
    if (!bucket) buckets.set(key, (bucket = []));
    bucket.push(i);
  }
  const cosCrease = Math.cos((creaseDegrees * Math.PI) / 180);
  const normals = new Float32Array(count * 3);
  const unit = (f) => {
    const x = faceNormal[f * 3];
    const y = faceNormal[f * 3 + 1];
    const z = faceNormal[f * 3 + 2];
    const len = Math.hypot(x, y, z) || 1;
    return [x / len, y / len, z / len];
  };
  for (const bucket of buckets.values()) {
    const units = bucket.map((i) => unit((i / 3) | 0));
    for (let k = 0; k < bucket.length; k++) {
      const [nx, ny, nz] = units[k];
      let sx = 0;
      let sy = 0;
      let sz = 0;
      for (let m = 0; m < bucket.length; m++) {
        const [gx, gy, gz] = units[m];
        if (nx * gx + ny * gy + nz * gz >= cosCrease) {
          const g = (bucket[m] / 3) | 0;
          sx += faceNormal[g * 3];
          sy += faceNormal[g * 3 + 1];
          sz += faceNormal[g * 3 + 2];
        }
      }
      const len = Math.hypot(sx, sy, sz) || 1;
      const i = bucket[k];
      normals[i * 3] = sx / len;
      normals[i * 3 + 1] = sy / len;
      normals[i * 3 + 2] = sz / len;
    }
  }
  geometry.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
}

function loadSmoothed(url) {
  let geometry = smoothed.get(url);
  if (!geometry) {
    geometry = loader.loadAsync(url).then((loaded) => {
      smoothNormals(loaded, 32);
      return loaded;
    });
    geometry.catch(() => smoothed.delete(url));
    smoothed.set(url, geometry);
  }
  return geometry;
}

// The STL's drawable: a shaded mesh that casts and receives shadows, on its
// own copy of the cached geometry, or the mesh's edges.
export async function loadStl(url, wireframe) {
  if (wireframe) {
    const geometry = await loader.loadAsync(url);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), new THREE.LineBasicMaterial({ transparent: true }));
    geometry.dispose();
    return edges;
  }
  const mesh = new THREE.Mesh(
    (await loadSmoothed(url)).clone(),
    new THREE.MeshStandardMaterial({ transparent: true, roughness: 0.55, metalness: 0.1 }),
  );
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

// ---- floor ---------------------------------------------------------------

const smoothstep = (e0, e1, x) => {
  const t = Math.min(Math.max((x - e0) / (e1 - e0), 0), 1);
  return t * t * (3 - 2 * t);
};

// The floor under the robot, in the XY plane (the scene is z-up). Three
// coplanar layers: an unlit tint that dissolves into the background past the
// reach, a shadow catcher, and a polar grid whose lines fade out with radius.
function floor(reach, sectors, rings, color, gridColor) {
  const group = new THREE.Group();
  const outer = reach * 1.5;

  // Tint alpha in units of the reach: 0.85 under the robot, easing out
  // between 0.45 and 1.3 reach. The disc's UV square maps its radius to the
  // canvas's half-width; the stops sample the easing along that radius.
  const size = 512;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const fade = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  const from = (0.45 * reach) / outer;
  const to = (1.3 * reach) / outer;
  const stops = 16;
  for (let s = 0; s <= stops; s++) {
    const t = from + ((to - from) * s) / stops;
    const level = Math.round(255 * 0.85 * (1 - smoothstep(0.45, 1.3, (t * outer) / reach)));
    const hex = level.toString(16).padStart(2, "0");
    fade.addColorStop(t, `#${hex}${hex}${hex}`);
  }
  ctx.fillStyle = fade;
  ctx.fillRect(0, 0, size, size);
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

// ---- lights --------------------------------------------------------------

// Hemisphere ambient plus key, fill and rim lights. The key casts shadows
// over a square of half-width `radius` around the scene origin. The scene is
// z-up, so the sky comes from +z and every light sits above the floor.
function lights(radius) {
  const group = new THREE.Group();
  const sky = new THREE.HemisphereLight(0xffffff, 0x1b2430, 0.6 * Math.PI);
  sky.position.set(0, 0, 1);
  group.add(sky);

  const key = new THREE.DirectionalLight(0xffffff, 0.8 * Math.PI);
  key.position.set(4, -6, 9);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.radius = 3;
  key.shadow.bias = -0.0005;
  key.shadow.normalBias = 0.02;
  const cam = key.shadow.camera;
  cam.left = cam.bottom = -radius;
  cam.right = cam.top = radius;
  cam.near = 0.5;
  cam.far = 40;
  cam.updateProjectionMatrix();
  group.add(key, key.target);

  const fill = new THREE.DirectionalLight(0xd6e4ff, 0.25 * Math.PI);
  fill.position.set(-7, 3, 4);
  group.add(fill, fill.target);

  const rim = new THREE.DirectionalLight(0xffffff, 0.35 * Math.PI);
  rim.position.set(1, 8, 5);
  group.add(rim, rim.target);
  return group;
}

// Free what a node's own object holds; geometry shared through the STL cache
// is never the object's own, since each mesh draws a clone of it.
export function dispose(object) {
  object.traverse((child) => {
    if (child.geometry) child.geometry.dispose();
    const m = child.material;
    if (!m) return;
    for (const mat of Array.isArray(m) ? m : [m]) {
      if (mat.alphaMap) mat.alphaMap.dispose();
      mat.dispose();
    }
  });
}
