import { apply_material, THREE, STLLoader } from "nicegui-scene";

const loader = new STLLoader();

// Smoothed geometry per URL: tool changes and scene rebuilds load the same
// meshes again, and smoothing is the slow part.
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

// NiceGUI's STL object with a standard (PBR) material that casts and receives
// shadows, and crease-aware smooth normals. Each mesh owns a clone of the
// cached geometry, so disposing one leaves the others drawn.
export default class Stl {
  mesh;

  async create_mesh(url, wireframe) {
    this.mesh = new THREE.Group();
    if (wireframe) {
      const geometry = await loader.loadAsync(url);
      this.mesh.add(
        new THREE.LineSegments(new THREE.EdgesGeometry(geometry), new THREE.LineBasicMaterial({ transparent: true })),
      );
    } else {
      const mesh = new THREE.Mesh(
        (await loadSmoothed(url)).clone(),
        new THREE.MeshStandardMaterial({ transparent: true, roughness: 0.55, metalness: 0.1 }),
      );
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      this.mesh.add(mesh);
    }
    return this.mesh;
  }

  apply_material(material_info) {
    this.mesh.traverse((child) => child.material && apply_material(child.material, material_info));
  }
}
