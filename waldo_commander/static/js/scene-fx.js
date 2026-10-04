/* Client-side animations for the 3D scene (driven by scene_fx.py).
 *
 * Python names the objects; the browser animates them on its own frame
 * clock, so an effect costs one websocket message instead of a stream of
 * transforms. Every effect restores the object's own scale/material when it
 * ends, and nothing runs under prefers-reduced-motion.
 *
 * The scene draws only when asked, so while a one-shot effect runs its
 * scene is redrawn on every animation frame, and whatever changes an object
 * outside that (a ripple, a restore) asks for a frame.
 */
(function () {
  const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const nextFrame = () => new Promise(r => requestAnimationFrame(r));

  const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
  const easeInOutCubic = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const easeOutBack = t => {
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  };

  /* ---- One frame loop for every scene ---- */
  const tweens = new Set();
  const pulses = new Map(); // scene element id -> [{ mesh, base, phase }]
  const restScale = new WeakMap(); // mesh -> its own scale while a pop-in runs
  const rushUntil = new Map(); // scene element id -> when its one-shot effects end
  let raf = 0;

  function wake() {
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function rush(sceneId, until) {
    if (until > (rushUntil.get(sceneId) || 0)) rushUntil.set(sceneId, until);
    wake();
  }

  function requestRender(sceneId) {
    const comp = getElement(sceneId);
    if (comp && comp.request_render) comp.request_render();
  }

  function tween(sceneId, delayMs, ms, step, done) {
    const tw = { sceneId, start: performance.now() + delayMs, ms, step, done };
    step(0);
    tweens.add(tw);
    rush(sceneId, tw.start + ms + 50);
    return tw;
  }

  /* The scene's own render pass, minus the controls update (its damping
   * steps per call, so extra calls would speed it up). */
  function renderNow(comp) {
    if (!comp || !comp.renderer || !comp.camera) return;
    if (comp.camera_tween) comp.camera_tween.update();
    comp.renderer.render(comp.scene, comp.camera);
    if (comp.text_renderer) comp.text_renderer.render(comp.scene, comp.camera);
    if (comp.text3d_renderer) comp.text3d_renderer.render(comp.scene, comp.camera);
    if (comp.viewHelper) {
      const autoClear = comp.renderer.autoClear;
      comp.renderer.autoClear = false;
      comp.viewHelper.render(comp.renderer);
      comp.renderer.autoClear = autoClear;
    }
  }

  function tick(now) {
    for (const tw of tweens) {
      if (now < tw.start) continue;
      const t = Math.min(1, (now - tw.start) / tw.ms);
      tw.step(t);
      if (t >= 1) {
        tweens.delete(tw);
        if (tw.done) tw.done();
        requestRender(tw.sceneId);
      }
    }
    for (const [sceneId, items] of pulses) {
      const live = items.filter(p => p.mesh.parent);
      if (live.length !== items.length) pulses.set(sceneId, live);
      for (const p of live) {
        const s = Math.sin(now / 260 - p.phase);
        p.mesh.scale.copy(p.base).multiplyScalar(1 + 0.45 * Math.max(0, s) ** 3);
      }
      requestRender(sceneId);
    }
    for (const [sceneId, until] of rushUntil) {
      if (now > until) rushUntil.delete(sceneId);
      else renderNow(getElement(sceneId));
    }
    raf = tweens.size || pulses.size || rushUntil.size ? requestAnimationFrame(tick) : 0;
  }

  /* ---- Scene object lookup ----
   * Objects are created asynchronously (their module is imported first), and
   * a batched update may land after this call, so poll briefly. */
  async function meshOf(sceneId, objectId) {
    for (let i = 0; i < 40; i++) {
      const comp = getElement(sceneId);
      const rec = comp && comp.objects ? comp.objects.get(objectId) : undefined;
      if (rec) {
        if (rec.ready_promise && !(await rec.ready_promise.then(() => true, () => false))) {
          return null;
        }
        const ready = comp.objects.get(objectId);
        return ready && ready.mesh ? ready.mesh : null;
      }
      await new Promise(r => setTimeout(r, 25));
    }
    return null;
  }

  /* ---- Effects ---- */
  function drawIn(sceneId, mesh, delayMs, ms) {
    const g = mesh.geometry;
    const n = g && g.attributes && g.attributes.position ? g.attributes.position.count : 0;
    if (!n) return;
    tween(sceneId, delayMs, ms,
      t => g.setDrawRange(0, t <= 0 ? 0 : Math.max(2, Math.ceil(n * easeOutCubic(t)))),
      () => g.setDrawRange(0, Infinity));
  }

  function popIn(sceneId, mesh, delayMs, ms) {
    const base = (restScale.get(mesh) || mesh.scale).clone();
    restScale.set(mesh, base);
    tween(sceneId, delayMs, ms,
      t => mesh.scale.copy(base).multiplyScalar(Math.max(1e-3, easeOutBack(t))),
      () => {
        mesh.scale.copy(base);
        restScale.delete(mesh);
      });
  }

  function emissives(mesh) {
    const mats = [];
    mesh.traverse(child => {
      const m = child.material;
      if (m && m.emissive) mats.push({ m, emissive: m.emissive.clone() });
    });
    return mats;
  }

  function glowPop(sceneId, mesh) {
    const base = mesh.scale.clone();
    const mats = emissives(mesh);
    tween(sceneId, 0, 750, t => {
      const k = t < 0.3 ? 0.88 + 0.2 * easeOutCubic(t / 0.3) : 1.08 - 0.08 * easeOutCubic((t - 0.3) / 0.7);
      mesh.scale.copy(base).multiplyScalar(k);
      const glow = 0.55 * (1 - easeOutCubic(t));
      for (const { m, emissive } of mats) {
        m.emissive.copy(emissive).addScalar(glow);
      }
    }, () => {
      mesh.scale.copy(base);
      for (const { m, emissive } of mats) m.emissive.copy(emissive);
    });
  }

  /* Two hard emissive flashes in *color*, e.g. on geometry that just collided. */
  function alarm(sceneId, mesh, color) {
    const mats = emissives(mesh);
    if (!mats.length) return;
    const hot = mats[0].emissive.clone().set(color);
    tween(sceneId, 0, 700, t => {
      const k = Math.max(0, Math.sin(t * 2 * Math.PI * 2 - Math.PI / 2) * 0.5 + 0.5) * (1 - t);
      for (const { m, emissive } of mats) m.emissive.copy(emissive).lerp(hot, k);
    }, () => {
      for (const { m, emissive } of mats) m.emissive.copy(emissive);
    });
  }

  function fadeIn(sceneId, mesh, ms) {
    const mats = [];
    mesh.traverse(child => {
      const m = child.material;
      if (m && typeof m.opacity === 'number') mats.push({ m, opacity: m.opacity, transparent: m.transparent });
    });
    if (!mats.length) return;
    // Blending is compiled into the material's program, so flipping
    // transparency needs a recompile.
    const setTransparent = (m, on) => {
      if (m.transparent !== on) {
        m.transparent = on;
        m.needsUpdate = true;
      }
    };
    for (const { m } of mats) setTransparent(m, true);
    tween(sceneId, 0, ms, t => {
      const k = easeOutCubic(t);
      for (const { m, opacity } of mats) m.opacity = opacity * k;
    }, () => {
      for (const { m, opacity, transparent } of mats) {
        m.opacity = opacity;
        setTransparent(m, transparent);
      }
    });
  }

  function stopPulse(sceneId) {
    const items = pulses.get(sceneId);
    if (!items) return;
    for (const p of items) p.mesh.scale.copy(p.base);
    pulses.delete(sceneId);
    requestRender(sceneId);
  }

  window.SceneFx = {
    /**
     * Reveal freshly created path and marker objects.
     * @param {number} sceneId - the ui.scene element id
     * @param {string[][]} segments - per path segment, in path order: the
     *   polyline id first, then its direction cones
     * @param {string[]} markers - waypoint/target marker ids to pop in
     */
    async reveal(sceneId, segments, markers) {
      if (reducedMotion()) return;
      const lineMs = 380;
      segments.forEach(async (ids, k) => {
        const delay = Math.min(k * 70, 700);
        const meshes = await Promise.all(ids.map(id => meshOf(sceneId, id)));
        const [line, ...cones] = meshes;
        if (line) drawIn(sceneId, line, delay, lineMs);
        cones.forEach((cone, j) => {
          if (cone) popIn(sceneId, cone, delay + ((j + 1) / (cones.length + 1)) * lineMs, 260);
        });
      });
      markers.forEach(async (id, k) => {
        const mesh = await meshOf(sceneId, id);
        // A scale queued behind the creation (an ellipsoid's radii) is the
        // size to pop to.
        await nextFrame();
        if (mesh) popIn(sceneId, mesh, Math.min(k * 50, 500) + 150, 420);
      });
    },

    /** Glow-and-pop meshes that just appeared, e.g. a newly mounted tool. */
    async flash(sceneId, ids) {
      if (reducedMotion()) return;
      const meshes = await Promise.all(ids.map(id => meshOf(sceneId, id)));
      // Let any scale/material calls queued behind creation land first.
      await nextFrame();
      for (const mesh of meshes) if (mesh) glowPop(sceneId, mesh);
    },

    /** Flash meshes that just started colliding in *color*. */
    async alarm(sceneId, ids, color) {
      if (reducedMotion()) return;
      const meshes = await Promise.all(ids.map(id => meshOf(sceneId, id)));
      await nextFrame();
      for (const mesh of meshes) if (mesh) alarm(sceneId, mesh, color);
    },

    /** Fade meshes that were just shown up to their own opacity. */
    async fadeIn(sceneId, ids, ms = 450) {
      if (reducedMotion()) return;
      const meshes = await Promise.all(ids.map(id => meshOf(sceneId, id)));
      await nextFrame();
      for (const mesh of meshes) if (mesh) fadeIn(sceneId, mesh, ms);
    },

    /**
     * Spring a dragged handle back from where it was let go, *from* in its
     * parent's frame, to where Python just put it. A *missColor* tints it
     * on the way: the drag asked for a pose the arm could not reach.
     */
    async springBack(sceneId, id, from, missColor) {
      if (reducedMotion()) return;
      const mesh = await meshOf(sceneId, id);
      if (!mesh) return;
      await nextFrame();
      const rest = mesh.position.clone();
      const start = rest.clone().set(from[0], from[1], from[2]);
      const mats = missColor ? emissives(mesh) : [];
      const hot = mats.length ? mats[0].emissive.clone().set(missColor) : null;
      tween(sceneId, 0, 520, t => {
        mesh.position.lerpVectors(start, rest, easeOutBack(t));
        const k = Math.max(0, Math.sin(t * Math.PI * 3)) * (1 - t);
        for (const { m, emissive } of mats) m.emissive.copy(emissive).lerp(hot, k);
      }, () => {
        mesh.position.copy(rest);
        for (const { m, emissive } of mats) m.emissive.copy(emissive);
      });
    },

    /**
     * Ripple a set of objects in order, a wave travelling along them, until
     * the next call for this scene replaces the set (an empty list stops it).
     */
    async pulse(sceneId, ids) {
      stopPulse(sceneId);
      if (reducedMotion() || !ids.length) return;
      const meshes = await Promise.all(ids.map(id => meshOf(sceneId, id)));
      stopPulse(sceneId);
      const items = [];
      meshes.forEach((mesh, i) => {
        if (mesh) {
          const base = (restScale.get(mesh) || mesh.scale).clone();
          items.push({ mesh, base, phase: i * 0.55 });
        }
      });
      if (items.length) {
        pulses.set(sceneId, items);
        wake();
      }
    },

    /** Ease the camera move of *ms* started just before this call. */
    easeCamera(sceneId, ms) {
      const comp = getElement(sceneId);
      const tw = comp ? comp.camera_tween : null;
      if (tw && typeof tw.easing === 'function') {
        tw.easing(reducedMotion() ? () => 1 : easeInOutCubic);
        if (!reducedMotion()) rush(sceneId, performance.now() + ms + 50);
      }
    },
  };
})();
