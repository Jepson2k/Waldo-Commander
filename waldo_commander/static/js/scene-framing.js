
/**
 * Scene framing: keeps the camera centred on the part of the 3D view that the
 * program column, the status footer and the bottom panel leave uncovered.
 *
 * PanelResize publishes what covers the view in a `wc:layout` event. The
 * fork's ui.scene resets the camera aspect on every window resize, so the
 * view offset is re-applied a frame later.
 */

(function() {
    'use strict';

    let sceneId = null;
    let listening = false;
    let inset = { left: 0, bottom: 0 };

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
    }

    function follow(layout) {
        inset = { left: Math.max(0, layout.columnRight), bottom: Math.max(0, layout.bottomCover) };
        apply();
    }

    function attach(id) {
        sceneId = id;
        if (!listening) {
            listening = true;
            window.addEventListener('resize', function() { requestAnimationFrame(apply); });
            window.addEventListener('wc:layout', function(e) { follow(e.detail); });
        }
        if (window.PanelResize) follow(PanelResize.layout());
        // Scene init resizes the view after attaching, resetting the aspect.
        requestAnimationFrame(apply);
    }

    window.SceneFraming = {
        attach: attach,
        getInset: function() { return { ...inset }; },
        camera: function() { const c = component(); return c ? c.camera : null; }
    };
})();
