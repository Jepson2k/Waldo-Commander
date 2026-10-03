"""Browser-level checks for the client-side animations.

The readout face and the scene effects are JavaScript: whether Python asked
for a reaction says nothing about what the browser shows. These drive real
events — a page load, jog keys, a digital E-STOP, a fresh path — and read
the resulting DOM and three.js state.

The test browser runs with prefers-reduced-motion forced on, which keeps
every other browser test deterministic; the scene test turns it off for
itself through CDP, since reduced motion skips the scene effects entirely.
"""

import time

import pytest
import waldoctl

from tests.helpers.browser_helpers import (
    click_tab,
    defocus_editor,
    dismiss_dialogs,
    ensure_robot_homed,
    marked_element,
    run_in_app,
    send_global_key,
)

# Eye radius, left pupil offset and visible mouth of the face under the
# selector arguments[0].
_FACE_JS = """
const svg = document.querySelector(arguments[0] + ' svg[data-mood]');
if (!svg) return null;
const eye = svg.querySelector('.eye-white');
const pupil = svg.querySelector('.pupil');
const m = (pupil.style.transform || '').match(/translate\\((-?[\\d.]+)px, (-?[\\d.]+)px\\)/);
const mouth = [...svg.querySelectorAll('[data-state]')]
  .find(el => el.getAttribute('opacity') !== '0');
return {
  eyeR: parseFloat(eye.getAttribute('r')),
  pupilX: m ? parseFloat(m[1]) : 0,
  mouth: mouth ? mouth.dataset.state : null,
};
"""
_FOOTER = ".footer-mode"

# Whether the run-bar Waldo sits wholly below its clip, out of sight.
_PEEK_HIDDEN_JS = """
const root = document.querySelector('.waldo-peek');
if (!root) return null;
const body = root.firstElementChild.getBoundingClientRect();
return body.top >= root.getBoundingClientRect().bottom - 0.5;
"""

# Records the run-bar Waldo's mouths while it peeks, and whether it has
# ducked back out of sight since.
_WATCH_PEEK_JS = (
    """
const root = document.querySelector('.waldo-peek');
const hidden = () => { """
    + _PEEK_HIDDEN_JS.replace("return null", "return false")
    + """ };
window.__peek = {mouths: [], peeking: false, hidden: hidden()};
(function sample() {
  const peeking = root.classList.contains('waldo-peeking');
  window.__peek.peeking = peeking;
  window.__peek.hidden = hidden();
  if (peeking) {
    const mouth = [...root.querySelectorAll('[data-state]')]
      .find(el => el.getAttribute('opacity') !== '0');
    if (mouth && !window.__peek.mouths.includes(mouth.dataset.state)) {
      window.__peek.mouths.push(mouth.dataset.state);
    }
  }
  requestAnimationFrame(sample);
})();
"""
)

# Records whether the face blinks: the blink overlay turning opaque.
_WATCH_BLINK_JS = """
window.__faceBlinked = false;
const blink = document.querySelector('.footer-mode svg [data-part="blink"]');
new MutationObserver(() => {
  if (blink.getAttribute('opacity') === '1') window.__faceBlinked = true;
}).observe(blink, {attributes: true, attributeFilter: ['opacity']});
"""


def _arrow_direction(key: str) -> int:
    """-1/+1: whether the pad arrow that jog key *key* presses points left or right."""

    def _find() -> int:
        from waldo_commander.services.keybindings import keybindings_manager
        from waldo_commander.state import ui_state

        axis = keybindings_manager._bindings[key].description.removeprefix("Jog ")
        panel = ui_state.control_panel
        for slot_id, direction in (("lr_neg", -1), ("lr_pos", 1)):
            meta = panel._cart_slot_meta[slot_id]
            slot_axis = panel._axis_string_for(
                meta["assign_key"], meta["sign"], meta["rotation"]
            )
            if slot_axis == axis:
                return direction
        raise AssertionError(f"{key!r} drives no left/right arrow ({axis})")

    return run_in_app(_find)


def _teleport_to_jog_pose() -> None:
    import asyncio

    from nicegui import core

    from tests.helpers.wait import teleport_to_jog_pose
    from waldo_commander.state import ui_state

    assert core.loop is not None, "app event loop not running"
    asyncio.run_coroutine_threadsafe(
        teleport_to_jog_pose(ui_state.control_panel.client), core.loop
    ).result(15)


def _poll(screen, script: str, predicate, timeout: float, what: str, *args):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = screen.selenium.execute_script(script, *args)
        if value is not None and predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError(f"{what}; last value: {value!r}")


@pytest.mark.browser
class TestAnimations:
    def test_face_idles_follows_the_jog_and_startles_on_estop(
        self, class_screen
    ) -> None:
        screen = class_screen
        dismiss_dialogs(screen)
        # Jogs are refused until the arm is homed, and the homed pose is a
        # wrist singularity where a cartesian step can be refused too.
        ensure_robot_homed()
        _teleport_to_jog_pose()
        face = _FACE_JS
        _poll(screen, face, bool, 15, "no face", _FOOTER)

        # Idle behaviours start with the page, not only after a mood change:
        # a reload once the connection state has settled builds the face in
        # its final mood, so no mood change follows to start them.
        screen.selenium.refresh()
        rest = _poll(screen, face, bool, 30, "no face after reload", _FOOTER)
        screen.selenium.execute_script(_WATCH_BLINK_JS)
        _poll(
            screen,
            "return window.__faceBlinked",
            bool,
            15,
            "the face never blinked after page load",
        )

        # A jog tap turns the eyes the way the pad arrow it drives points.
        # The pair also returns the arm to where it started.
        for key in ("d", "a"):
            sign = _arrow_direction(key)
            defocus_editor(screen)
            send_global_key(screen, key)
            _poll(
                screen,
                face,
                lambda f, s=sign: f["pupilX"] * s > 0.3,
                3,
                f"eyes did not follow the {key!r} jog",
                _FOOTER,
            )
            _poll(
                screen,
                face,
                lambda f: f["pupilX"] == 0,
                3,
                f"eyes did not recenter after the {key!r} jog",
                _FOOTER,
            )

        # Digital E-STOP: wide eyes and an open mouth until reset, on the
        # footer face and on the dialog's own Waldo, each driving its own SVG.
        defocus_editor(screen)
        send_global_key(screen, "")  # Escape
        for root in (_FOOTER, ".estop-card .waldo-guest"):
            _poll(
                screen,
                face,
                lambda f: f["eyeR"] > rest["eyeR"] and f["mouth"] == "o",
                5,
                f"{root} face did not startle on E-STOP",
                root,
            )
        screen.click("Reset")
        _poll(
            screen,
            face,
            lambda f: f["eyeR"] == rest["eyeR"] and f["mouth"] == rest["mouth"],
            5,
            "face did not settle after the E-STOP reset",
            _FOOTER,
        )
        screen.selenium.execute_script(_WATCH_BLINK_JS)
        _poll(
            screen,
            "return window.__faceBlinked",
            bool,
            15,
            "the footer face stopped idling once the dialog's Waldo was gone",
        )

    def test_a_finished_run_raises_waldo_over_the_run_bar(self, class_screen) -> None:
        screen = class_screen
        dismiss_dialogs(screen)
        click_tab(screen, "program")
        assert _poll(screen, _PEEK_HIDDEN_JS, lambda v: True, 15, "no run-bar Waldo"), (
            "the run-bar Waldo shows before any run"
        )

        def _load() -> None:
            from waldo_commander.state import ui_state

            assert ui_state.active_textarea is not None
            ui_state.active_textarea.value = "print('done', flush=True)\n"
            program = waldoctl.commander.programs.active
            assert program is not None
            program.source = ui_state.active_textarea.value

        run_in_app(_load)
        screen.selenium.execute_script(_WATCH_PEEK_JS)
        marked_element(screen, "editor-play-btn").click()
        _poll(
            screen,
            "return window.__peek",
            lambda p: "grin" in p["mouths"] and not p["peeking"] and p["hidden"],
            20,
            "Waldo did not rise grinning over the run bar and duck back down "
            "after a clean run",
        )

    def test_a_new_path_draws_in_and_settles(self, class_screen) -> None:
        screen = class_screen
        dismiss_dialogs(screen)
        driver = screen.selenium
        driver.execute_cdp_cmd(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-reduced-motion", "value": "no-preference"}
                ]
            },
        )
        # Sample the path group every frame: did any line draw partially, and
        # how small did any mesh get while it popped in?
        driver.execute_script("""
            const canvas = document.querySelector('canvas');
            const comp = getElement(canvas.closest('[id^="c"]').id.slice(1));
            let paths = null;
            comp.scene.traverse(o => { if (o.name === 'simulation:paths') paths = o; });
            window.__fx = {partial: false, minScale: Infinity, lines: 0, meshes: 0, run: true};
            (function sample() {
              if (!window.__fx.run) return;
              paths.traverse(o => {
                if (o === paths) return;
                if (o.isLine && o.geometry.drawRange.count !== Infinity) window.__fx.partial = true;
                if (o.isMesh) window.__fx.minScale = Math.min(window.__fx.minScale, o.scale.x);
              });
              requestAnimationFrame(sample);
            })();
        """)
        saved_paths_visible = waldoctl.commander.settings.view.paths_visible

        def _populate() -> None:
            from waldo_commander.state import PathSegment, simulation_state

            waldoctl.commander.settings.view.paths_visible = True
            program = waldoctl.commander.programs.active
            assert program is not None
            program.dry_run.path_segments = [
                PathSegment(
                    points=[[0.20 + 0.004 * i, 0.10, 0.25] for i in range(40)],
                    color="#2196f3",
                    is_valid=True,
                    line_number=1,
                    estimated_duration=1.0,
                )
            ]
            program.dry_run.total_steps = 1
            simulation_state.notify_changed()

        def _restore() -> None:
            from waldo_commander.components.playback import playback
            from waldo_commander.state import simulation_state

            waldoctl.commander.settings.view.paths_visible = saved_paths_visible
            program = waldoctl.commander.programs.active
            if program is not None:
                program.dry_run.path_segments = []
                program.dry_run.total_steps = 0
            playback.invalidate_timeline()
            simulation_state.notify_changed()

        run_in_app(_populate)
        try:
            # Settled: every line fully drawn and every mesh back to its own
            # (unit) scale, after the reveal visibly ran.
            settled = """
                const canvas = document.querySelector('canvas');
                const comp = getElement(canvas.closest('[id^="c"]').id.slice(1));
                let paths = null;
                comp.scene.traverse(o => { if (o.name === 'simulation:paths') paths = o; });
                let lines = 0, meshes = 0, done = true;
                paths.traverse(o => {
                  if (o === paths) return;
                  if (o.isLine) { lines++; if (o.geometry.drawRange.count !== Infinity) done = false; }
                  if (o.isMesh) { meshes++; if (Math.abs(o.scale.x - 1) > 1e-6) done = false; }
                });
                return {lines, meshes, done, sampled: window.__fx};
            """
            state = _poll(
                screen,
                settled,
                lambda s: s["lines"] > 0 and s["meshes"] > 0 and s["done"],
                10,
                "path never settled fully drawn at full size",
            )
            assert state["sampled"]["partial"], "the path appeared without drawing in"
            assert state["sampled"]["minScale"] < 0.5, (
                "markers appeared without popping in"
            )
        finally:
            driver.execute_script("window.__fx.run = false;")
            driver.execute_cdp_cmd(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )
            run_in_app(_restore)
