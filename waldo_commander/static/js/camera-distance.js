/* Reports the 3D view's camera-to-target distance so the jog step can follow the zoom. */

(function () {
    'use strict';

    const POLL_MS = 200;
    const MIN_RELATIVE_CHANGE = 0.02;

    let timer = null;
    let last = null;

    function attach(sceneId) {
        if (timer !== null) clearInterval(timer);
        last = null;
        timer = setInterval(() => {
            // move_camera can replace the controls, so they are read every tick.
            const scene = getElement(sceneId);
            if (!scene || !scene.camera || !scene.controls) return;
            const distance = scene.camera.position.distanceTo(scene.controls.target);
            if (!Number.isFinite(distance)) return;
            if (last !== null && Math.abs(distance - last) <= last * MIN_RELATIVE_CHANGE) return;
            last = distance;
            emitEvent('wc_camera_distance', distance);
        }, POLL_MS);
    }

    window.WcCameraDistance = { attach };
})();
