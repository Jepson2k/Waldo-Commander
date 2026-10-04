/* Client-side animations for the 3D scene (driven by scene_fx.py).
 *
 * Python names the objects; the browser animates them on its own frame
 * clock, so an effect costs one websocket message instead of a stream of
 * transforms. Every effect restores the object's own scale/material when it
 * ends, and nothing runs under prefers-reduced-motion.
 *
 * The scene draws only when asked, so while a one-shot effect runs its
 * scene is redrawn on every animation frame, and whatever changes an object
 * outside that (a ripple, a restore) asks for a frame. A ripple runs a few
 * waves and stops, so the scene goes back to drawing nothing.
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
  const pulses = new Map(); // scene element id -> { items: [{ mesh, base, phase }], start, until }
  const rushUntil = new Map(); // scene element id -> when its one-shot effects end
  let raf = 0;

  // A ripple wave: sin(t / RIPPLE_RATE), staggered RIPPLE_STAGGER per object.
  const RIPPLE_RATE = 260;
  const RIPPLE_STAGGER = 0.55;
  const RIPPLE_WAVES = 2;

  /* An animated property's own value, kept while any effect on it runs: an
   * effect starting part-way through another returns to this value, not to
   * the other's in-between one. */
  const rests = new WeakMap(); // object -> { property: { value, holds } }

  function hold(obj, property, read) {
    let held = rests.get(obj);
    if (!held) rests.set(obj, (held = {}));
    const entry = held[property] || (held[property] = { value: read(), holds: 0 });
    entry.holds++;
    return entry.value;
  }

  function release(obj, property) {
    const held = rests.get(obj);
    const entry = held && held[property];
    if (entry && --entry.holds === 0) delete held[property];
  }

  function shown(mesh) {
    for (let o = mesh; o; o = o.parent) if (!o.visible) return false;
    return true;
  }

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

  /* The scene's own draw, as its loop makes it: views of the scene and its
   * hover effects follow, and the loop does not draw the frame again. Not
   * the controls or the camera tween: the loop steps those on its clock. */
  function renderNow(comp) {
    if (!comp || !comp.renderer || !comp.camera) return;
    comp.render_requested = false;
    if (typeof comp.scene_version === 'number') comp.scene_version++;
    if (comp._syncEffectsIfDirty) comp._syncEffectsIfDirty();
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
    for (const [sceneId, pulse] of pulses) {
      if (now >= pulse.until) {
        stopPulse(sceneId);
        continue;
      }
      let drawn = false;
      for (const p of pulse.items) {
        if (!p.mesh.parent) continue;
        // From a trough, so each object starts and ends at its own size.
        const a = (now - pulse.start) / RIPPLE_RATE - p.phase;
        const s = a < 0 || a > RIPPLE_WAVES * 2 * Math.PI ? -1 : Math.sin(a - Math.PI / 2);
        p.mesh.scale.copy(p.base).multiplyScalar(1 + 0.45 * Math.max(0, s) ** 3);
        drawn = drawn || shown(p.mesh);
      }
      if (drawn) requestRender(sceneId);
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
    const base = hold(mesh, 'scale', () => mesh.scale.clone());
    tween(sceneId, delayMs, ms,
      t => mesh.scale.copy(base).multiplyScalar(Math.max(1e-3, easeOutBack(t))),
      () => {
        mesh.scale.copy(base);
        release(mesh, 'scale');
      });
  }

  function emissives(mesh) {
    const mats = [];
    mesh.traverse(child => {
      const m = child.material;
      if (m && m.emissive) mats.push({ m, emissive: hold(m, 'emissive', () => m.emissive.clone()) });
    });
    return mats;
  }

  function restoreEmissives(mats) {
    for (const { m, emissive } of mats) {
      m.emissive.copy(emissive);
      release(m, 'emissive');
    }
  }

  function glowPop(sceneId, mesh) {
    const base = hold(mesh, 'scale', () => mesh.scale.clone());
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
      release(mesh, 'scale');
      restoreEmissives(mats);
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
    }, () => restoreEmissives(mats));
  }

  function fadeIn(sceneId, mesh, ms) {
    const mats = [];
    mesh.traverse(child => {
      const m = child.material;
      if (m && typeof m.opacity === 'number') {
        mats.push({
          m,
          opacity: hold(m, 'opacity', () => m.opacity),
          transparent: hold(m, 'transparent', () => m.transparent),
        });
      }
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
        release(m, 'opacity');
        release(m, 'transparent');
      }
    });
  }

  function stopPulse(sceneId) {
    const pulse = pulses.get(sceneId);
    if (!pulse) return;
    for (const p of pulse.items) {
      p.mesh.scale.copy(p.base);
      release(p.mesh, 'scale');
    }
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
        restoreEmissives(mats);
      });
    },

    /**
     * Ripple a set of objects in order, a few waves travelling along them;
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
          const base = hold(mesh, 'scale', () => mesh.scale.clone());
          items.push({ mesh, base, phase: i * RIPPLE_STAGGER });
        }
      });
      if (items.length) {
        const start = performance.now();
        const span = RIPPLE_WAVES * 2 * Math.PI + (items.length - 1) * RIPPLE_STAGGER;
        pulses.set(sceneId, { items, start, until: start + span * RIPPLE_RATE });
        wake();
      }
    },

    /** Ease the camera move started just before this call; the scene draws
     * it on its own clock. */
    easeCamera(sceneId) {
      const comp = getElement(sceneId);
      const tw = comp ? comp.camera_tween : null;
      if (tw && typeof tw.easing === 'function') {
        tw.easing(reducedMotion() ? () => 1 : easeInOutCubic);
      }
    },
  };
})();
