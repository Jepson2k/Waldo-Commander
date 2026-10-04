
/**
 * Scene framing: keeps the camera centred on the part of the 3D view that the
 * program column, bottom panel and status footer leave uncovered, and reports the
 * camera's distance from its orbit target, which sets how far the scene's
 * rings and gizmo snap.
 *
 * PanelResize publishes what covers the view in a `wc:layout` event. The
 * fork's ui.scene resets the camera aspect on every window resize, so the
 * view offset is re-applied a frame later.
 *
 * The scene redraws every frame whether or not anything changed. Behind a
 * modal dialog's backdrop it is dimmed and mostly hidden, so it redraws a few
 * times a second instead: an arm moving behind a dialog still shows, without
 * the frames competing with the dialog for a software-rendered browser.
 */

(function() {
    'use strict';

    let sceneId = null;
    let listening = false;
    let inset = { left: 0, bottom: 0 };
    let distanceTimer = null;
    let lastDistance = 0;
    const COVERED_FRAME_MS = 250;

    function component() {
        return sceneId === null ? null : getElement(sceneId);
    }

    function apply() {
        const c = component();
        if (!c || !c.camera || !c.camera.isPerspectiveCamera) return;
        const cam = c.camera;
        const W = c.$el.clientWidth;
        const H = c.$el.clientHeight;
        if (!W || !H) return;
        const { left, bottom } = inset;
        if (left <= 0 && bottom <= 0) {
            if (cam.view && cam.view.enabled) cam.clearViewOffset();
            cam.aspect = W / H;
        } else {
            cam.aspect = (W + left) / (H + bottom);
            cam.setViewOffset(W + left, H + bottom, 0, bottom, W, H);
        }
        cam.updateProjectionMatrix();
        if (c.request_render) c.request_render();
    }

    function follow(layout) {
        inset = { left: Math.max(0, layout.columnRight), bottom: Math.max(0, layout.bottomCover) };
        apply();
    }

    function pollDistance() {
        const c = component();
        // move_camera can recreate the controls, so they are looked up on every tick.
        if (!c || !c.camera || !c.controls || !c.controls.target) return;
        const d = c.camera.position.distanceTo(c.controls.target);
        if (lastDistance === 0 || Math.abs(d - lastDistance) / lastDistance > 0.02) {
            lastDistance = d;
            emitEvent('wc_camera_distance', { distance: d });
        }
    }

    function throttleWhileCovered(c) {
        if (!c || !c.renderer || c.renderer.wcThrottled) return;
        c.renderer.wcThrottled = true;
        // A frame's draw calls (scene, labels, view helper) land within
        // microseconds; the first decides for the rest of that frame.
        let decidedAt = -Infinity;
        let draw = true;
        let lastDrawn = 0;
        const drawNow = function() {
            const now = performance.now();
            if (now - decidedAt > 5) {
                decidedAt = now;
                draw = !document.querySelector('.q-dialog__backdrop')
                    || now - lastDrawn >= COVERED_FRAME_MS;
                if (draw) lastDrawn = now;
            }
            return draw;
        };
        for (const r of [c.renderer, c.text_renderer, c.text3d_renderer]) {
            if (!r) continue;
            const render = r.render.bind(r);
            r.render = function(scene, camera) {
                if (drawNow()) render(scene, camera);
            };
        }
    }

    function attach(id) {
        sceneId = id;
        throttleWhileCovered(component());
        if (!listening) {
            listening = true;
            window.addEventListener('resize', function() { requestAnimationFrame(apply); });
            window.addEventListener('wc:layout', function(e) { follow(e.detail); });
        }
        if (window.PanelResize) follow(PanelResize.layout());
        // Scene init resizes the view after attaching, resetting the aspect.
        requestAnimationFrame(apply);
        if (distanceTimer) clearInterval(distanceTimer);
        lastDistance = 0;
        distanceTimer = setInterval(pollDistance, 200);
    }

    window.SceneFraming = {
        attach: attach,
        getInset: function() { return { ...inset }; },
        camera: function() { const c = component(); return c ? c.camera : null; }
    };
})();
