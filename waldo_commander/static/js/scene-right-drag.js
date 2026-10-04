/**
 * Right-drag: a right-button drag pans the 3D view and must not open the
 * scene's menu the way a right-click does. The scene's click events carry no
 * pointer position, so this reports each right press, and on its release the
 * farthest the pointer got from the press. The release lands before the
 * mouseup and the contextmenu events that follow it. A menu opened another
 * way (the menu key, a long press) is reported as a release that did not move.
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
        let farthest = 0;
        // Set by a right release whose contextmenu may still follow (Windows).
        let released = false;
        canvas.addEventListener('pointerdown', function(e) {
            if (e.button !== 2) return;
            start = [e.clientX, e.clientY];
            farthest = 0;
            emitEvent('wc_right_press', {});
        });
        canvas.addEventListener('pointermove', function(e) {
            if (start === null) return;
            farthest = Math.max(farthest, Math.hypot(e.clientX - start[0], e.clientY - start[1]));
        });
        // The camera controls capture the pointer, so the release arrives here
        // even when it happens off the canvas.
        canvas.addEventListener('pointerup', function(e) {
            if (e.button !== 2 || start === null) return;
            const moved = Math.max(farthest, Math.hypot(e.clientX - start[0], e.clientY - start[1]));
            start = null;
            emitEvent('wc_right_release', { moved: moved });
            released = true;
            setTimeout(function() { released = false; });
        });
        canvas.addEventListener('pointercancel', function() { start = null; });
        // Runs before the scene's own contextmenu handler reports the hits.
        canvas.addEventListener('contextmenu', function() {
            if (start === null && !released) emitEvent('wc_right_release', { moved: 0 });
            released = false;
        });
    }

    window.SceneRightDrag = { attach: attach };
})();
