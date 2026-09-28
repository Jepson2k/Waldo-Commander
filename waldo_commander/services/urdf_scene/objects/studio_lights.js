import { THREE } from "nicegui-scene";

// Hemisphere ambient plus key, fill and rim lights. The key casts shadows
// over a square of half-width `radius` around the scene origin. The scene is
// z-up, so the sky comes from +z and every light sits above the floor.
export default class StudioLights {
  create_mesh(radius) {
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
}
