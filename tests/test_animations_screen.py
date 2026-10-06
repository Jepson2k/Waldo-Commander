"""Browser-level checks for the client-side animations.

Waldo and the scene effects are JavaScript: whether Python asked for a
reaction says nothing about what the browser shows. These drive real
events — a page load, taps on the jog pad, a digital E-STOP, a fresh path,
an AI session taking control — and read the resulting DOM and three.js
state.

The test browser runs with prefers-reduced-motion forced on, which keeps
every other browser test deterministic; the scene test turns it off for
itself through CDP, since reduced motion skips the scene effects entirely.
"""

import asyncio
import time
from io import BytesIO

import pytest
import waldoctl
from PIL import Image
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import (
    click_tab,
    dismiss_dialogs,
    ensure_robot_homed,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import window_size
from tests.helpers.scene_surface import frames, with_surface

# Eye scale, left pupil offset, visible mouth and sweat drop of the Waldo at
# the selector arguments[0].
_FACE_JS = """
const root = document.querySelector(arguments[0]);
if (!root) return null;
const scale = (root.querySelector('.waldo-eye').style.transform || '')
  .match(/scale\\((-?[\\d.]+)\\)/);
const pupil = root.querySelector('.waldo-pupil');
const m = (pupil.style.transform || '').match(/translate\\((-?[\\d.]+)px, (-?[\\d.]+)px\\)/);
const mouth = [...root.querySelectorAll('[data-mouth]')]
  .find(el => getComputedStyle(el).opacity === '1');
return {
  eyeScale: scale ? parseFloat(scale[1]) : 1,
  pupilX: m ? parseFloat(m[1]) : 0,
  mouth: mouth ? mouth.dataset.mouth : null,
  sweat: !!root.querySelector('.waldo-drop'),
};
"""
_CHIP = ".footer-mode .waldo"
_FOOTER = ".footer-mode"

# What the status chip shows of an AI session: the antenna tips lit, the
# pupils in the mode's colour rather than the body's, the mode's label, the
# connection word and the Take control button.
_AI_JS = """
const chip = document.querySelector(arguments[0]);
const root = chip && chip.querySelector('.waldo');
if (!root) return null;
const shown = el => !!el && el.getClientRects().length > 0;
const mode = chip.querySelector('.footer-ai-mode');
const word = chip.querySelector('.footer-mode-word');
const fill = el => getComputedStyle(el).fill;
return {
  tips: getComputedStyle(root.querySelector('.waldo-tip')).opacity,
  driving: fill(root.querySelector('.waldo-pupil circle'))
    !== fill(root.querySelector('g[mask] > rect')),
  mode: shown(mode) ? mode.textContent.trim() : '',
  word: shown(word) ? word.textContent.trim() : '',
  take: shown(chip.querySelector('.btn-take-control')),
};
"""

# The footer buttons, other than Take control, that a click at their
# centre would reach.
_FOOTER_BUTTONS_HIT_JS = """
return [...document.querySelectorAll('.status-footer .q-btn:not(.btn-take-control)')]
  .filter(b => b.getClientRects().length)
  .filter(b => {
    const r = b.getBoundingClientRect();
    return b.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
  })
  .map(b => b.textContent.trim() || b.className);
"""

# One small triangle, for a model the scene has to download.
_TRIANGLE_STL = (
    b"solid t\nfacet normal 0 0 1\nouter loop\n"
    b"vertex 0 0 0\nvertex 0.02 0 0\nvertex 0 0.02 0\n"
    b"endloop\nendfacet\nendsolid t\n"
)

# Whether everything the status chip shows stays inside it, and the chip
# clear of the footer cells beside it.
_CHIP_FITS_JS = """
const footer = document.querySelector('.status-footer');
const chip = footer.querySelector('.footer-mode').getBoundingClientRect();
const inside = r => r.width === 0 || (r.left >= chip.left - 0.5
  && r.right <= chip.right + 0.5 && r.top >= chip.top - 0.5
  && r.bottom <= chip.bottom + 0.5);
const parts = [...footer.querySelectorAll(
  '.footer-mode .footer-ai-mode, .footer-mode .footer-mode-word, '
  + '.footer-mode .btn-take-control .q-btn__content')];
const beside = ['.readout-robot-name', '.footer-tool', '.footer-empty-tool']
  .map(s => footer.querySelector(s))
  .filter(e => e && e.getClientRects().length > 0)
  .map(e => e.getBoundingClientRect());
// The text's own extent: a squeezed label keeps its box and spills its text.
const extent = e => {
  const range = document.createRange();
  range.selectNodeContents(e);
  return range.getBoundingClientRect();
};
return {
  spilled: parts.filter(e => !inside(extent(e))).map(e => e.className),
  overlapped: beside.filter(r => r.left < chip.right && r.right > chip.left).length,
};
"""

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
    const mouth = [...root.querySelectorAll('[data-mouth]')]
      .find(el => getComputedStyle(el).opacity === '1');
    if (mouth && !window.__peek.mouths.includes(mouth.dataset.mouth)) {
      window.__peek.mouths.push(mouth.dataset.mouth);
    }
  }
  requestAnimationFrame(sample);
})();
"""
)

# Records how far the chip Waldo's left pupil swings either way, every
# frame: a tap holds the look for under half a second, which a loaded
# runner's WebDriver round trips can step right over.
_WATCH_LOOK_JS = """
const pupil = document.querySelector('.footer-mode .waldo .waldo-pupil');
window.__look = {min: 0, max: 0};
(function sample() {
  const m = (pupil.style.transform || '').match(/translate\\((-?[\\d.]+)px/);
  const x = m ? parseFloat(m[1]) : 0;
  window.__look.min = Math.min(window.__look.min, x);
  window.__look.max = Math.max(window.__look.max, x);
  if (pupil.isConnected) requestAnimationFrame(sample);
})();
"""

# Records whether the chip Waldo blinks: its lid dropping over the eye.
_WATCH_BLINK_JS = """
window.__faceBlinked = false;
const lid = document.querySelector('.footer-mode .waldo .waldo-lid');
new MutationObserver(() => {
  const m = (lid.style.transform || '').match(/translateY\\((-?[\\d.]+)px\\)/);
  if (m && parseFloat(m[1]) > 1.5) window.__faceBlinked = true;
}).observe(lid, {attributes: true, attributeFilter: ['style']});
"""


def _waldo_pixels(screen, css: str) -> dict[str, tuple[int, ...]]:
    """Colours drawn inside the Waldo at *css*: beside the left pupil (in the
    eye), on the lower-left of the head, and in the element's top corner,
    which Waldo leaves bare."""
    element = screen.selenium.find_element(By.CSS_SELECTOR, css)
    image = Image.open(BytesIO(element.screenshot_as_png)).convert("RGB")
    side = image.width
    # The 18 x 16 viewBox from (3, 3) is fitted to the square and centred.
    unit = side / 18
    top = (side - 16 * unit) / 2

    def at(x: float, y: float) -> tuple[int, ...]:
        return image.getpixel((round((x - 3) * unit), round(top + (y - 3) * unit)))

    return {
        "eye": at(6.8, 12),
        "head": at(5.0, 17.5),
        "outside": image.getpixel((2, 2)),
    }


def _close(a: tuple[int, ...], b: tuple[int, ...], tolerance: int = 12) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def _pad_arrow(screen, slot_id: str):
    """The cartesian pad's arrow in the fixed slot *slot_id* ("lr_neg" is the left arrow)."""
    from waldo_commander.state import ui_state

    element_id = run_in_app(lambda: ui_state.control_panel._cart_slot_elems[slot_id].id)
    return screen.selenium.find_element(By.ID, f"c{element_id}")


def _wait_until_jog_allowed(timeout: float = 15.0) -> None:
    """Block until the app would let a jog through: a tap refused for the
    lease or a busy guard never begins, so the eyes would have nothing to
    follow."""
    from waldo_commander.state import ui_state

    deadline = time.time() + timeout
    while not run_in_app(
        lambda: ui_state.control_panel._movement_allowed(notify=False)
    ):
        if time.time() > deadline:
            raise AssertionError("the app never allowed a jog")
        time.sleep(0.1)


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


def _held(
    screen, script: str, predicate, hold_s: float, timeout: float, what: str, *args
):
    """The script's value once *predicate* has held for *hold_s*."""
    deadline = time.time() + timeout
    since = None
    value = None
    while time.time() < deadline:
        value = screen.selenium.execute_script(script, *args)
        if value is not None and predicate(value):
            since = since or time.time()
            if time.time() - since >= hold_s:
                return value
        else:
            since = None
        time.sleep(0.05)
    raise AssertionError(f"{what}; last value: {value!r}")


@pytest.mark.browser
class TestAnimations:
    def test_waldo_blinks_follows_the_jog_and_startles_on_estop(
        self, class_screen
    ) -> None:
        screen = class_screen
        dismiss_dialogs(screen)
        # Jogs are refused until the arm is homed, and the homed pose is a
        # wrist singularity where a cartesian step can be refused too.
        ensure_robot_homed()
        _teleport_to_jog_pose()
        face = _FACE_JS
        _poll(screen, face, bool, 15, "no Waldo", _CHIP)

        # Blinking starts with the page, not only after a mood change: a
        # reload once the connection state has settled builds Waldo in its
        # final mood, so no mood change follows to start it. The simulator's
        # resting face is a flat mouth and unscaled eyes, once the greeting
        # is over.
        screen.selenium.refresh()
        _poll(
            screen,
            face,
            lambda f: f["mouth"] == "flat" and f["eyeScale"] == 1,
            30,
            "Waldo never came to rest after the reload",
            _CHIP,
        )
        # The first blink is watched for through the jogs below: a look
        # never blinks, so any blink by then is the idle loop's.
        screen.selenium.execute_script(_WATCH_BLINK_JS)

        # A tap on a pad arrow turns the eyes the way the arrow points. The
        # pair also returns the arm to where it started.
        marked_element(screen, "tab-cartesian").click()
        _wait_until_jog_allowed()
        for slot_id, sign in (("lr_pos", 1), ("lr_neg", -1)):
            arrow = _pad_arrow(screen, slot_id)
            _poll(
                screen,
                "return arguments[0].offsetParent !== null",
                bool,
                5,
                f"the {slot_id} arrow never showed",
                arrow,
            )
            screen.selenium.execute_script(_WATCH_LOOK_JS)
            arrow.click()
            _poll(
                screen,
                "return window.__look",
                lambda look, s=sign: (look["max"] if s > 0 else -look["min"]) > 0.3,
                3,
                f"eyes did not follow the {slot_id} arrow",
            )
            _poll(
                screen,
                face,
                lambda f: f["pupilX"] == 0,
                5,
                f"eyes did not recenter after the {slot_id} arrow",
                _CHIP,
            )

        _poll(
            screen,
            "return window.__faceBlinked",
            bool,
            15,
            "Waldo never blinked after page load",
        )

        # Digital E-STOP: wide eyes, an open mouth and a bead of sweat until
        # reset, on the chip's Waldo and on the dialog's own.
        marked_element(screen, "btn-estop").click()
        for root in (_CHIP, ".estop-card .waldo"):
            _poll(
                screen,
                face,
                lambda f: f["eyeScale"] > 1 and f["mouth"] == "o" and f["sweat"],
                5,
                f"{root} did not startle on E-STOP",
                root,
            )
        # The eyes are holes: the card shows through them, not a fill.
        pixels = _waldo_pixels(screen, ".estop-card .waldo")
        assert _close(pixels["eye"], pixels["outside"]), pixels
        assert not _close(pixels["eye"], pixels["head"], 40), pixels
        screen.click("Reset")
        _poll(
            screen,
            face,
            lambda f: f["eyeScale"] == 1 and f["mouth"] == "flat" and not f["sweat"],
            8,
            "Waldo did not settle after the E-STOP reset",
            _CHIP,
        )

        # Every E-STOP builds its own dialog; a closed one goes with its Waldo.
        def estop_cards() -> int:
            from nicegui import Client

            return sum(
                "estop-card" in element.classes
                for client in Client.instances.values()
                for element in list(client.elements.values())
            )

        deadline = time.monotonic() + 5
        while run_in_app(estop_cards) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert run_in_app(estop_cards) == 0, "the closed E-STOP dialog was kept"
        screen.selenium.execute_script(_WATCH_BLINK_JS)
        _poll(
            screen,
            "return window.__faceBlinked",
            bool,
            15,
            "the chip Waldo stopped blinking once the dialog's was gone",
        )

    def test_an_ai_session_takes_over_the_status_chip(self, class_screen) -> None:
        """An MCP client around lights Waldo's antenna tips and puts its
        control mode beside the connection; one that takes control turns its
        eyes to the mode's colour and offers Take control, which hands the
        chip back."""
        from waldo_commander.services.control_lease import (
            BROWSER,
            MCP,
            control_lease,
            mcp_touch,
        )
        from waldo_commander.state import ui_state

        screen = class_screen
        dismiss_dialogs(screen)
        try:
            run_in_app(lambda: mcp_touch("anim-mcp"))
            around = _poll(
                screen,
                _AI_JS,
                lambda v: v["tips"] == "1" and v["mode"] == "Inspect",
                10,
                "an MCP client did not light the antenna tips or show its mode",
                _FOOTER,
            )
            assert not around["driving"] and not around["take"], around
            # The wrapped footer gives the chip room for the session, in each
            # mode a click on it cycles to.
            with window_size(screen, 900, 900):
                for step, mode in enumerate(
                    ("Inspect", "Auto-edits", "Autopilot", "Inspect")
                ):
                    if step:
                        screen.selenium.find_element(
                            By.CSS_SELECTOR, ".footer-ai-mode"
                        ).click()
                    _poll(
                        screen,
                        _AI_JS,
                        lambda v, mode=mode: v["mode"] == mode,
                        5,
                        f"clicking the mode did not cycle to {mode}",
                        _FOOTER,
                    )
                    _poll(
                        screen,
                        _CHIP_FITS_JS,
                        lambda f: not f["spilled"] and not f["overlapped"],
                        5,
                        f"the wrapped chip spilled {mode}",
                    )
            run_in_app(
                lambda: control_lease.seize(MCP, "anim-mcp", "MCP session anim-mc")
            )
            driving = _poll(
                screen,
                _AI_JS,
                lambda v: v["driving"] and v["take"],
                10,
                "an AI holding control did not take the eyes or offer Take control",
                _FOOTER,
            )
            # Whether the AI moves the arm or the simulator stays in words.
            assert driving["word"] == "Simulator", driving
            with window_size(screen, 900, 900):
                _poll(
                    screen,
                    _CHIP_FITS_JS,
                    lambda f: not f["spilled"] and not f["overlapped"],
                    5,
                    "the wrapped chip spilled Take control",
                )
            # Reachable over an open dialog, as the capsule it replaced was:
            # Selenium refuses a click a backdrop would take. The rest of the
            # footer is drawn over the dialog too, but answers no clicks.
            marked_element(screen, "tab-settings").click()
            _poll(
                screen,
                "return !!document.querySelector('.q-dialog__backdrop')",
                bool,
                10,
                "Settings did not open",
            )
            assert screen.selenium.execute_script(_FOOTER_BUTTONS_HIT_JS) == [], (
                "the footer's other buttons answer clicks over an open dialog"
            )
            screen.selenium.find_element(By.CSS_SELECTOR, ".btn-take-control").click()
            _poll(
                screen,
                _AI_JS,
                lambda v: not v["driving"] and not v["take"],
                10,
                "Take control did not hand the chip back",
                _FOOTER,
            )
            assert run_in_app(
                lambda: control_lease.held_by(BROWSER, ui_state.active_client_id or "")
            )
        finally:
            run_in_app(control_lease.reset)
            dismiss_dialogs(screen)

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
        driver.execute_script(
            with_surface("""
            const paths = S.byName('simulation:paths');
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
        )
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
            settled = with_surface("""
                const paths = S.byName('simulation:paths');
                let lines = 0, meshes = 0, done = true;
                paths.traverse(o => {
                  if (o === paths) return;
                  if (o.isLine) { lines++; if (o.geometry.drawRange.count !== Infinity) done = false; }
                  if (o.isMesh) { meshes++; if (Math.abs(o.scale.x - 1) > 1e-6) done = false; }
                });
                return {lines, meshes, done, sampled: window.__fx};
            """)
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

            # The editor cursor on the path's line ripples its cones for a few
            # waves, and then the scene stops drawing again.
            driver.execute_script("window.__fx.maxScale = 0;")
            driver.execute_script(
                with_surface("""
                const paths = S.byName('simulation:paths');
                (function sample() {
                  if (!window.__fx.run) return;
                  paths.traverse(o => {
                    if (o !== paths && o.isMesh)
                      window.__fx.maxScale = Math.max(window.__fx.maxScale, o.scale.x);
                  });
                  requestAnimationFrame(sample);
                })();
            """)
            )

            def _cursor_on_path() -> None:
                from waldo_commander.state import ui_state

                program = waldoctl.commander.programs.active
                assert program is not None and ui_state.urdf_scene is not None
                program.dry_run.playback.active_cursor_line = 1
                ui_state.urdf_scene.update_cursor_line_highlight()

            run_in_app(_cursor_on_path)
            _poll(
                screen,
                "return window.__fx.maxScale",
                lambda m: m > 1.2,
                5,
                "the cursor's line did not ripple",
            )
            deadline = time.monotonic() + 10
            count, since = frames(screen), time.monotonic()
            while time.monotonic() - since < 1.0:
                assert time.monotonic() < deadline, (
                    f"the scene kept drawing after the ripple ({count} frames)"
                )
                time.sleep(0.1)
                now = frames(screen)
                if now != count:
                    count, since = now, time.monotonic()
        finally:
            driver.execute_script("window.__fx.run = false;")
            driver.execute_cdp_cmd(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )
            run_in_app(_restore)

    def test_scene_effects_keep_what_python_sets_during_them(
        self, class_screen
    ) -> None:
        """An effect ends on the scale or opacity Python set while it ran, not
        the one it started from: Python sends only changes, so one undone in
        the browser stays undone. A ripple asked for after another is the one
        that runs, though the earlier one finishes waiting for its objects
        last."""
        from nicegui import app as ng_app
        from starlette.responses import Response

        from waldo_commander.state import ui_state

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

        def make(name: str):
            def build():
                urdf = ui_state.urdf_scene
                assert urdf is not None
                return (
                    urdf.scene.sphere(0.02)
                    .material("#888888", opacity=1.0)
                    .move(0.3, 0.3, 0.3)
                    .with_name(name)
                )

            return build

        dot = run_in_app(make("fx-dot"))
        read = with_surface("""
            const found = S.byName(arguments[0]);
            return found && {scale: found.scale.x, opacity: found.material.opacity};
        """)
        try:
            _poll(screen, read, bool, 10, "the sphere was never drawn", "fx-dot")
            scene = ui_state.urdf_scene.scene

            run_in_app(lambda: scene.fx.fade_in([dot]))
            time.sleep(0.15)
            run_in_app(lambda: dot.material("#888888", opacity=0.4))
            # Mid-fade the opacity is below what it fades to, so it is
            # Python's only once the fade has ended on it.
            _held(
                screen,
                read,
                lambda v: v["opacity"] == pytest.approx(0.4),
                0.3,
                5,
                "the fade never ended on Python's opacity 0.4",
                "fx-dot",
            )

            run_in_app(lambda: scene.fx.pulse([dot]))
            time.sleep(0.3)
            run_in_app(lambda: dot.scale(2.0))
            # Between waves the scale rests on its own for about 0.8 s, so
            # one held longer than that is where the ripple ended.
            _held(
                screen,
                read,
                lambda v: v["scale"] == pytest.approx(2.0),
                1.2,
                10,
                "the ripple never ended on Python's scale 2.0",
                "fx-dot",
            )

            # The first ripple waits for a model still downloading; the
            # second, asked for meanwhile, runs past its arrival.
            async def slow_stl() -> Response:
                await asyncio.sleep(0.6)
                return Response(_TRIANGLE_STL, media_type="model/stl")

            ng_app.add_api_route("/test/slow.stl", slow_stl)
            driver.execute_script(
                with_surface("""
                const dot = S.byName('fx-dot');
                // From the first swell (0.4 s into the ripple): the earlier
                // call gives up about 0.6 s later, and the second wave peaks
                // 2 s later.
                const s = window.__ripple = {
                  start: performance.now(), first: null, late: 0, run: true,
                };
                (function sample() {
                  if (!s.run) return;
                  const t = s.t = performance.now() - s.start;
                  const v = dot.scale.x / 2;
                  if (s.first === null && v > 1.001) s.first = t;
                  if (s.first !== null && t > s.first + 900 && t < s.first + 2500) {
                    s.late = Math.max(s.late, v);
                  }
                  requestAnimationFrame(sample);
                })();
            """)
            )

            def ripple_twice() -> None:
                model = scene.stl("/test/slow.stl").with_name("fx-model")
                scene.fx.pulse([model])
                scene.fx.pulse([dot])

            run_in_app(ripple_twice)
            ripple = _poll(
                screen,
                "return window.__ripple",
                lambda r: r["first"] is not None and r["t"] > r["first"] + 2600,
                10,
                "the later ripple never started",
            )
            assert ripple["late"] > 1.05, (
                "a ripple asked for earlier stopped the later one when it finished "
                f"waiting (largest scale in its second wave: {ripple['late']:.3f})"
            )
        finally:
            driver.execute_script("if (window.__ripple) window.__ripple.run = false;")

            def remove() -> None:
                for obj in list(ui_state.urdf_scene.scene.objects.values()):
                    if obj.name in ("fx-dot", "fx-model"):
                        obj.delete()

            run_in_app(remove)
            driver.execute_cdp_cmd(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )

    def test_a_keep_out_pops_in_once_and_keeps_its_size_when_redrawn(
        self, class_screen
    ) -> None:
        """A new keep-out pops in. Resized, it is redrawn as a new mesh, but it
        was on screen all along: no shrinking to nothing and popping back."""
        from waldoctl import Box

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
        # Every frame: the box's mesh, and the smallest scale it was drawn at.
        driver.execute_script(
            with_surface("""
            window.__box = {uuid: null, scale: null, minScale: Infinity, run: true};
            (function sample() {
              if (!window.__box.run) return;
              const o = S.byName('shape:fx-box');
              if (o) {
                window.__box.uuid = o.uuid;
                window.__box.scale = o.scale.x;
                window.__box.minScale = Math.min(window.__box.minScale, o.scale.x);
              }
              requestAnimationFrame(sample);
            })();
        """)
        )
        pose = (0.5, 0.4, 0.1, 0.0, 0.0, 0.0)

        def _declare(size: float):
            def assign() -> None:
                scene = waldoctl.commander.scene
                assert scene is not None
                scene.shapes = [Box(name="fx-box", x=size, y=0.1, z=0.1, pose=pose)]

            return assign

        def _clear() -> None:
            scene = waldoctl.commander.scene
            if scene is not None:
                scene.shapes = []

        run_in_app(_declare(0.1))
        try:
            first = _poll(
                screen,
                "return window.__box",
                lambda b: b["uuid"] and b["minScale"] < 0.5 and b["scale"] == 1,
                10,
                "the new keep-out never popped in and settled",
            )
            driver.execute_script("window.__box.minScale = Infinity;")
            run_in_app(_declare(0.15))
            _poll(
                screen,
                "return window.__box",
                lambda b: b["uuid"] != first["uuid"],
                10,
                "the resized keep-out was never redrawn",
            )
            # Longer than a pop-in, which is where a redrawn mesh shrank.
            time.sleep(1.0)
            redrawn = driver.execute_script("return window.__box")
            assert redrawn["minScale"] > 0.99, (
                f"the resized keep-out shrank to {redrawn['minScale']:.3f} and "
                "popped back in"
            )

            # A collision flash starting part-way through another still ends
            # on the box's own glow, not the first flash's in-between red.
            # The glow is sampled every frame and read once it has held still
            # for a few frames past both flashes: a loaded runner draws slowly
            # enough that a fixed wait can land inside the second flash, and a
            # glow left stuck holds still on the wrong colour.
            driver.execute_script(
                with_surface("""
                const box = S.byName('shape:fx-box');
                const glow = () => box.material.emissive.getHex();
                const s = window.__glow = {
                  own: glow(), last: glow(), still: 0, start: performance.now(), run: true,
                };
                (function sample() {
                  if (!s.run) return;
                  const now = glow();
                  s.still = now === s.last ? s.still + 1 : 0;
                  s.last = now;
                  s.elapsed = performance.now() - s.start;
                  requestAnimationFrame(sample);
                })();
                S.fx.alarm(['shape:fx-box'], '#ff0000');
                setTimeout(() => S.fx.alarm(['shape:fx-box'], '#ff0000'), 200);
            """)
            )
            glow = _poll(
                screen,
                "return window.__glow",
                lambda g: g["elapsed"] > 1000 and g["still"] >= 5,
                15,
                "the keep-out's glow never settled after the flashes",
            )
            driver.execute_script("window.__glow.run = false;")
            assert glow["last"] == glow["own"], (
                f"overlapping flashes left the keep-out glowing #{glow['last']:06x}, "
                f"not its own #{glow['own']:06x}"
            )
        finally:
            driver.execute_script("window.__box.run = false;")
            driver.execute_cdp_cmd(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )
            run_in_app(_clear)
