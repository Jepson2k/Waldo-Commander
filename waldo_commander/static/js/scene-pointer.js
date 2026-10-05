/**
 * Pointer input the scene's own events leave out.
 *
 * Right-drag: a right-button drag pans the 3D view and must not open the
 * scene's menu the way a right-click does. The scene's click events carry no
 * pointer position, so this reports each right press, and on its release the
 * farthest the pointer got from the press. The release lands before the
 * mouseup and the contextmenu events that follow it. A menu opened another
 * way (the menu key, a long press) is reported as a release that did not move.
 *
 * Gizmo hover: a gizmo's handles reach past the object it moves, so leaving
 * the arm for one of them is not leaving the gizmo. TransformControls marks
 * the handle under a mouse as its axis; this reports whose gizmo that is
 * whenever it changes. It listens on the scene's wrapper, so the canvas's own
 * listeners have already updated the axis.
 */

(function() {
    'use strict';

    const attached = new WeakSet();
    const hoverWatched = new WeakSet();

    function attach(id) {
        const c = getElement(id);
        const canvas = c && c.renderer && c.renderer.domElement;
        if (!canvas || attached.has(canvas)) return;
        attached.add(canvas);
        watchGizmoHover(id, c.$el);
        watchRightDrag(canvas);
    }

    function watchGizmoHover(id, wrapper) {
        if (hoverWatched.has(wrapper)) return;
        hoverWatched.add(wrapper);
        let over = null;
        function report(name) {
            if (name === over) return;
            over = name;
            emitEvent('wc_gizmo_hover', { name: name });
        }
        wrapper.addEventListener('pointermove', function(e) {
            const c = getElement(id);
            if (e.pointerType === 'touch' || e.target !== c.renderer.domElement) return;
            const tc = [...c.transform_controls.values()].find(function(t) { return t.object && t.axis !== null; });
            report(tc ? tc.object.name : null);
        });
        wrapper.addEventListener('pointerleave', function() { report(null); });
    }

    function watchRightDrag(canvas) {
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

    window.ScenePointer = { attach: attach };
})();
