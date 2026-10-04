/**
 * Right-drag: a right-button drag pans the 3D view and must not open the
 * scene's menu the way a right-click does. The scene's click events carry no
 * pointer position, so this reports each right press, and on its release how
 * far it moved. The release lands before the mouseup and the contextmenu
 * events that follow it.
 */

(function() {
    'use strict';

    const attached = new WeakSet();

    function attach(id) {
        const c = getElement(id);
        const canvas = c && c.renderer && c.renderer.domElement;
        if (!canvas || attached.has(canvas)) return;
        attached.add(canvas);
        let start = null;
        canvas.addEventListener('pointerdown', function(e) {
            if (e.button !== 2) return;
            start = [e.clientX, e.clientY];
            emitEvent('wc_right_press', {});
        });
        // The camera controls capture the pointer, so the release arrives here
        // even when it happens off the canvas.
        canvas.addEventListener('pointerup', function(e) {
            if (e.button !== 2 || start === null) return;
            const moved = Math.hypot(e.clientX - start[0], e.clientY - start[1]);
            start = null;
            emitEvent('wc_right_release', { moved: moved });
        });
    }

    window.SceneRightDrag = { attach: attach };
})();
