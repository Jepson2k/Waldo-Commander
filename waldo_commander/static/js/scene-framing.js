/**
 * Scene framing: keeps the camera centred on the part of the 3D view that the
 * program column and the status footer leave uncovered.
 *
 * The fork's ui.scene resets the camera aspect on every window resize, so the
 * view offset is re-applied a frame later and again on every `wc:layout`
 * event PanelResize dispatches.
 */

(function() {
    'use strict';

    let sceneId = null;
    let listening = false;
    let inset = { left: 0, bottom: 0 };

    function component() {
        return sceneId === null ? null : getElement(sceneId);
    }

    function measure() {
        let left = 0;
        const wrap = document.querySelector('.panels-wrap');
        if (wrap && wrap.classList.contains('column-open')) {
            left = parseFloat(document.documentElement.style.getPropertyValue('--wc-column-right')) || 0;
        }
        let bottom = 0;
        const footer = document.querySelector('.status-footer');
        if (footer && footer.offsetParent !== null) {
            bottom = Math.max(0, Math.round(window.innerHeight - footer.getBoundingClientRect().top));
        }
        return { left: Math.max(0, left), bottom: bottom };
    }

    function setInset(left, bottom) {
        inset = { left: left, bottom: bottom };
        const c = component();
        if (!c || !c.camera || !c.camera.isPerspectiveCamera) return;
        const cam = c.camera;
        const W = c.$el.clientWidth;
        const H = c.$el.clientHeight;
        if (!W || !H) return;
        if (left <= 0 && bottom <= 0) {
            if (cam.view && cam.view.enabled) cam.clearViewOffset();
            cam.aspect = W / H;
        } else {
            cam.aspect = (W + left) / (H + bottom);
            cam.setViewOffset(W + left, H + bottom, 0, bottom, W, H);
        }
        cam.updateProjectionMatrix();
    }

    function refresh() {
        const m = measure();
        setInset(m.left, m.bottom);
    }

    function attach(id) {
        sceneId = id;
        if (!listening) {
            listening = true;
            window.addEventListener('resize', function() { requestAnimationFrame(refresh); });
            window.addEventListener('wc:layout', refresh);
        }
        refresh();
    }

    window.SceneFraming = {
        attach: attach,
        setInset: setInset,
        refresh: refresh,
        getInset: function() { return { ...inset }; },
        camera: function() { const c = component(); return c ? c.camera : null; }
    };
})();
