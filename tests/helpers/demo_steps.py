"""Steps the recorded videos share: the review demos and the tutorials.

Each moves the pointer the way a user would (from outside the arm, waiting
for menus to finish opening) so a recording shows a real interaction.
"""

import asyncio
import math
import time
from contextlib import contextmanager

import numpy as np
import waldoctl
from nicegui import core
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_video import DemoVideo
from tests.helpers.video_compose import Rect
from tests.helpers.scene_screen import (
    ball_mm,
    camera_action,
    camera_pose,
    camera_settled,
    gizmo,
    probe,
    settled_element,
    settled_pixel,
    zoom,
)
from tests.helpers.scene_surface import project_local, scene_js, wait_for_scene_idle
from waldo_commander.state import ui_state


#: The floor in front of the arm, close enough to see a target land there.
PLACEMENT_VIEW = [0.333, 0.447, 0.402, 0.05, 0.2, 0.15]


@contextmanager
def slowed_jogs(factor: float):
    """Jogs run ``factor`` times slower, with acceleration to match. Recorded
    with ``DemoVideo.recording(dilation=factor)``, they play back at the
    speed the panel shows, with ``factor`` times the frames."""
    from waldo_commander.components import control

    speed, accel = control._norm_speed, control._norm_accel
    control._norm_speed = lambda: speed() / factor
    control._norm_accel = lambda: accel() / factor**2
    try:
        yield
    finally:
        control._norm_speed, control._norm_accel = speed, accel


@contextmanager
def slowed_jog_settings(factor: float):
    """Jogs run ``factor`` times slower through the jog speed and
    acceleration settings, for a shot that leaves out the speed chip (which
    shows them); see ``slowed_jogs``. Yields the call that puts them back:
    a recorded jog's line takes the settings as they are when it ends."""
    jog = waldoctl.commander.settings.jog
    speed, accel = jog.speed, jog.accel

    def restore() -> None:
        jog.speed, jog.accel = speed, accel

    jog.speed = max(1, round(speed / factor))
    jog.accel = max(1, round(accel / factor**2))
    try:
        yield restore
    finally:
        restore()


@contextmanager
def slowed_moves(factor: float):
    """Queued moves (a program, Home, a typed move) play ``factor`` times
    slower, through the controller's execution speed, for
    ``DemoVideo.recording(dilation=factor)``; the playback bar shows the
    speed as written."""
    from waldo_commander.components.playback import playback

    def scale(value: float) -> None:
        sent = asyncio.run_coroutine_threadsafe(
            waldoctl.commander.client.set_execution_speed(value, timeout=3.0),
            core.loop,
        ).result(5)
        assert sent > 0, f"execution speed {value} was not confirmed"

    show = playback._show_speed
    playback._show_speed = lambda value: show(value * factor)
    scale(1 / factor)
    try:
        yield
    finally:
        scale(1.0)
        playback._show_speed = show


@contextmanager
def slowed_preview(factor: float):
    """The preview of the active program plays ``factor`` times slower, for
    ``DemoVideo.recording(dilation=factor)``; the playback bar shows the
    speed as written."""
    from waldo_commander.components.playback import playback

    preview = waldoctl.commander.programs.active.dry_run.playback
    speed = preview.playback_speed
    show = playback._show_speed
    playback._show_speed = lambda value: show(value * factor)
    preview.playback_speed = speed / factor
    try:
        yield
    finally:
        preview.playback_speed = speed
        playback._show_speed = show


#: The page's clock and its stylesheets' durations, run ``arguments[0]``
#: times slower; 1 puts them back.
_SLOW_PAGE_JS = """
const k = arguments[0];
let c = window.__demoClock;
if (!c) {
  const now = performance.now.bind(performance);
  const raf = window.requestAnimationFrame.bind(window);
  c = window.__demoClock = {k: 1, base: 0, real: 0, now, rules: []};
  c.map = (t) => c.base + (t - c.real) / c.k;
  performance.now = () => c.map(now());
  window.requestAnimationFrame = (cb) => raf((t) => cb(c.map(t)));
}
for (const [style, prop, value, priority] of c.rules) style.setProperty(prop, value, priority);
c.rules = [];
if (k === 1) {
  Object.assign(c, {k: 1, base: 0, real: 0});
  return;
}
Object.assign(c, {base: c.map(c.now()), real: c.now(), k});
const times = (v) => v.split(",").map((t) => {
  const s = t.trim(), n = parseFloat(s);
  return s.endsWith("ms") ? `${n * k}ms` : `${n * k}s`;
}).join(", ");
const walk = (rules) => {
  for (const rule of rules) {
    if (rule.cssRules) walk(rule.cssRules);
    const style = rule.style;
    if (!style) continue;
    for (const prop of style) {
      if (!/(^|-)(duration|delay)$/.test(prop)) continue;
      const v = style.getPropertyValue(prop).trim();
      if (!/^[\\d.]+m?s(\\s*,\\s*[\\d.]+m?s)*$/.test(v)) continue;
      const priority = style.getPropertyPriority(prop);
      c.rules.push([style, prop, v, priority]);
      style.setProperty(prop, times(v), priority);
    }
  }
};
for (const sheet of document.styleSheets) {
  try { walk(sheet.cssRules); } catch (e) { if (e.name !== "SecurityError") throw e; }
}
"""

#: Quasar's own transition length, which its dialogs and menus set inline.
_QUASAR_TRANSITION_MS = 300


@contextmanager
def slowed_page(screen, factor: float, *quasar):
    """What the page animates (camera moves, transitions) runs ``factor``
    times slower, for ``DemoVideo.recording(dilation=factor)``; ``quasar``
    are dialogs or menus whose transitions slow down too."""

    def length(ms: int) -> None:
        def apply() -> None:
            for element in quasar:
                element.props(f"transition-duration={ms}")

        run_in_app(apply)

    screen.selenium.execute_script(_SLOW_PAGE_JS, factor)
    length(round(_QUASAR_TRANSITION_MS * factor))
    try:
        yield
    finally:
        length(_QUASAR_TRANSITION_MS)
        screen.selenium.execute_script(_SLOW_PAGE_JS, 1)


#: What the page is still animating: the scene's camera and effects, and
#: transitions with an end (an idle loop, as Waldo's, has none).
_ANIMATING_JS = """
const c = getElement(S.root).core;
const busy = [];
if (c.cameraMove.active) busy.push("camera");
if (c.fx.active) busy.push("fx");
for (const a of document.getAnimations()) {
  const end = a.effect && a.effect.getComputedTiming().endTime;
  if (a.playState === "running" && Number.isFinite(end)) {
    const t = a.effect.target;
    busy.push(`${a.constructor.name} ${a.transitionProperty || a.animationName || ""} on ${t && (t.className || t.tagName)}`);
  }
}
return busy;
"""


def object_box(screen, name: str) -> Rect:
    """Where the scene object ``name`` is drawn, once the view is still."""
    last: list = []

    def still(_):
        wait_for_scene_idle(screen)
        box = probe(screen, "screenBox", name)
        settled = box is not None and bool(last) and last[-1] == box
        last.append(box)
        return box if settled else None

    return Rect(*wait(screen, 10).until(still))


def dialog_card(screen):
    """The card of the Settings dialog."""
    return screen.selenium.find_element(By.CSS_SELECTOR, ".settings-dialog-card")


def page_still(screen) -> None:
    """Wait for what the page animates (transitions, camera moves) to end."""
    try:
        wait(screen, 15).until(lambda _: not scene_js(screen, _ANIMATING_JS))
    except TimeoutException:
        raise AssertionError(
            f"still animating: {scene_js(screen, _ANIMATING_JS)}"
        ) from None


@contextmanager
def animating(video: DemoVideo, factor: float, *quasar):
    """Record the block, and on until the page is still, from the browser's
    frames, with what the page animates slowed ``factor`` times and played
    back at speed; ``quasar`` as for ``slowed_page``."""
    screen = video.screen
    with slowed_page(screen, factor, *quasar), video.recording(factor):
        yield
        # What the block set off reaches the page after a round trip to the
        # server; until it starts, a still page says nothing.
        try:
            WebDriverWait(screen.selenium, 3, poll_frequency=0.05).until(
                lambda _: scene_js(screen, _ANIMATING_JS)
            )
        except TimeoutException:
            pass
        page_still(screen)
        time.sleep(0.15 * factor)
    video.frame()


def camera_to(
    video: DemoVideo, pose: list[float], seconds: float = 1.2, factor: float = 3
) -> None:
    """Move the 3D view's camera to ``pose`` (where it is, then the point it
    looks at) over ``seconds``, eased, recorded as it moves."""
    with motion(video.screen), animating(video, factor):
        run_in_app(
            lambda: ui_state.urdf_scene.scene.move_camera(
                *pose, duration=seconds, ease=True
            )
        )


def place_arm(screen, x: float, y: float, distance: float) -> None:
    """Move the camera, keeping its direction, so the point it looks at
    lands at page pixel (x, y), ``distance`` times as far away as now."""
    scene_js(
        screen,
        """
        const [x, y, back] = arguments;
        const core = getElement(S.root).core, cam = core.camera, ctl = core.controls;
        const rect = core.renderer.domElement.getBoundingClientRect();
        const nx = (x - rect.left) / rect.width * 2 - 1;
        const ny = 1 - (y - rect.top) / rect.height * 2;
        const target = ctl.target.clone();
        const away = cam.position.clone().sub(target).multiplyScalar(back);
        cam.position.copy(target).add(away);
        cam.updateMatrixWorld();
        const h = Math.tan(cam.fov * Math.PI / 360) * away.length();
        const right = target.clone().setFromMatrixColumn(cam.matrixWorld, 0);
        const up = target.clone().setFromMatrixColumn(cam.matrixWorld, 1);
        const shift = right.multiplyScalar(-nx * h * cam.aspect).add(up.multiplyScalar(-ny * h));
        cam.position.add(shift);
        ctl.target.copy(target).add(shift);
        ctl.update();
        core.requestRender();
        """,
        x,
        y,
        distance,
    )
    wait_for_scene_idle(screen)


def focus_subject(screen):
    """Fit the selection with room for the handles and their drag travel."""
    camera_action(screen, "focus")
    camera_settled(screen)
    pose = camera_pose(screen)
    zoom(screen, float(np.linalg.norm(np.subtract(pose[:3], pose[3:]))) * 1.2)
    camera_settled(screen)


def arm_still(timeout: float = 4.0) -> None:
    """Wait until the arm stops moving."""
    deadline = time.monotonic() + timeout
    last = waldoctl.commander.status.joints.angles.rad.copy()
    while time.monotonic() < deadline:
        time.sleep(0.06)
        now = waldoctl.commander.status.joints.angles.rad.copy()
        if np.allclose(now, last, atol=1e-5, rtol=0):
            return
        last = now


def jog_ended(screen) -> None:
    wait(screen, 15).until(
        lambda _: ui_state.control_panel._jog_end_wait_task is None
        or ui_state.control_panel._jog_end_wait_task.done()
    )
    arm_still()


@contextmanager
def program_panel(screen):
    """The program editor open beside the scene, so a demo shows its source."""
    click_tab(screen, "program")
    try:
        yield
    finally:
        # The panel slides out under the pointer left on its close button; a
        # button passing under it shows a tooltip that only a move puts away.
        def away() -> None:
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 960, "y": 540}
            )

        away()
        close_panel(screen, "program-panel")
        wait(screen).until(lambda _: no_visible(screen, ".program-panel"))
        away()


@contextmanager
def motion(screen):
    """Animations on, as a user without reduced motion sees them."""

    def emulate(value: str) -> None:
        screen.selenium.execute_cdp_cmd(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-reduced-motion", "value": value}]},
        )

    emulate("no-preference")
    try:
        yield
    finally:
        emulate("reduce")


def text_rect(screen, element) -> Rect:
    """Where ``element``'s text is drawn: a line of code's element spans the
    editor, its text only as far as it goes."""
    x, y, w, h = screen.selenium.execute_script(
        "const r=document.createRange();r.selectNodeContents(arguments[0]);"
        "const b=r.getBoundingClientRect();return [b.x,b.y,b.width,b.height];",
        element,
    )
    return Rect(x, y, w, h)


def center(element) -> tuple[float, float]:
    r = element.rect
    return r["x"] + r["width"] / 2, r["y"] + r["height"] / 2


def menu_item(screen, starts: str):
    """The menu entry starting ``starts``, once the menu has finished
    opening, so the pointer is aimed where the entry stays."""
    previous = None

    def settled(_):
        nonlocal previous
        for e in screen.selenium.find_elements(By.CSS_SELECTOR, ".q-menu .q-item"):
            if e.is_displayed() and e.text.strip().startswith(starts):
                rect, previous = (e.rect, previous), e.rect
                moving = screen.selenium.execute_script(
                    "const m=arguments[0].closest('.q-menu');"
                    "return !m || m.getAnimations({subtree:true}).some(a=>a.playState==='running');",
                    e,
                )
                return e if rect[0] == rect[1] and not moving else None
        return None

    return wait(screen).until(settled)


def choose_item(video: DemoVideo, item, hold: float = 0.8) -> None:
    """Point at menu entry ``item`` the way it is read: ring it and hold,
    then go down the menu's outside edge and in, past none of the others.
    The caller clicks."""
    video.frame()
    video.highlight(item)
    video.pause(hold)
    menu = item.find_element(By.XPATH, "./ancestor::*[contains(@class, 'q-menu')]")
    left = menu.rect["x"] - 12
    r = item.rect
    y = r["y"] + r["height"] / 2
    video.along(
        [
            video.at,
            (left, video.at[1]),
            (left, y),
            (r["x"] + min(70, r["width"] / 3), y),
        ],
        0.7,
    )
    # The pointer marks it now; the menu closes on the click, and the ring
    # would be left on nothing.
    video.highlight(None)
    video.pause(0.25)


def outward(
    point: tuple[float, float], away: tuple[float, float], distance: float = 260
) -> tuple[float, float]:
    """A spot ``distance`` px beyond ``point``, on the side away from
    ``away``: an approach from there crosses no link, so no joint's dial
    comes up on the way in."""
    dx, dy = point[0] - away[0], point[1] - away[1]
    n = math.hypot(dx, dy) or 1.0
    return point[0] + dx / n * distance, point[1] + dy / n * distance


def ring_shown(screen) -> int:
    """Wait for a joint's ring, which resting the pointer on its link brings
    up, and say whose it is."""
    return wait(screen).until(
        lambda _: scene_js(
            screen,
            "const r=getElement(S.root).core.pointer.ring;return r ? [r.u] : null;",
        )
    )[0]


def ring_arc(screen, joint: int, degrees: float, steps: int = 24) -> list:
    """Pixels round ``joint``'s ring from where it is grabbed, ``degrees``
    on; the ring is up."""
    dial = wait(screen).until(lambda _: probe(screen, "dial", joint))
    # A grab at 0° is an answer too.
    grab = wait(screen).until(
        lambda _: (g := probe(screen, "ringGrab", joint)) is not None and [g]
    )[0]
    at = dial["q"] + math.radians(grab)
    points = project_local(
        screen,
        f"jog:dial:{joint}",
        [
            [
                dial["r"] * math.cos(at - math.radians(d)),
                dial["r"] * math.sin(at - math.radians(d)),
                0,
            ]
            for d in np.linspace(0, degrees, steps)
        ],
    )
    return [(p[0], p[1]) for p in points]


def arm_pixel(screen) -> tuple[float, float]:
    """Where the middle of the arm is on screen."""
    x, y = scene_js(
        screen,
        "const c=getElement(S.root).core,v=new (c.camera.position.constructor)(0,0,0.15);"
        "return c.pointer.screen(v.project(c.camera));",
    )
    return x, y


def tcp_badge(screen) -> tuple[float, float]:
    """Where the tool's badge is, once nothing covers it: it stands aside
    while a dial's label would."""

    def on_top(_):
        badge = screen.selenium.find_element(
            By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
        )
        x, y = center(badge)
        hit = screen.selenium.execute_script(
            "return document.elementFromPoint(arguments[0], arguments[1])"
            "?.getAttribute('aria-label')",
            x,
            y,
        )
        return (x, y) if hit == "Move TCP" else None

    return wait(screen).until(on_top)


def gizmo_shown(screen, spot: tuple[float, float]) -> None:
    """Wait for the gizmo a click on the badge at ``spot`` brings up."""
    try:
        wait(screen, 5).until(lambda _: gizmo(screen, "tcp:ball"))
    except TimeoutException:
        state = scene_js(
            screen,
            "const c=getElement(S.root).core,h=c.pointer.hover,[x,y]=arguments[0];"
            "const e=document.elementFromPoint(x,y);"
            "return {shown:h.shown,target:h.target,pinned:h.pinned,rules:c.ix.rules,"
            "edit:!!c.ix.edit?.active,at:e&&(e.getAttribute('aria-label')||e.className)};",
            list(spot),
        )
        raise AssertionError(f"no gizmo after the badge click: {state}") from None


def bring_up_gizmo(screen, video: DemoVideo) -> None:
    """From outside the arm to the tool's badge, whose click brings up the
    gizmo, as a user would."""
    spot = tcp_badge(screen)
    video.jump(*outward(spot, arm_pixel(screen)))
    video.caption("Click the tool's badge to bring up the gizmo")
    video.pause(0.8)
    video.glide(spot, 1.2)
    video.click(settle=lambda: gizmo_shown(screen, spot))
    video.pause(0.6)


def gizmo_center(screen) -> tuple[float, float]:
    x, y = project_local(screen, "tcp:ball", [[0, 0, 0]])[0][:2]
    return x, y


def press_mark(video, marker, settle=None, seconds=0.45):
    element = settled_element(
        video.screen, lambda: marked_element(video.screen, marker)
    )
    video.glide(center(element), seconds)
    video.click(settle)


def open_scene_panel(screen):
    if no_visible(screen, ".scene-panel"):
        click_marked(screen, "tab-scene")
    wait(screen).until(lambda _: marked_element(screen, "scene-tree").is_displayed())


def frame_choice(video, choice, label, seconds=0.45):
    """Pick ``choice`` from the gizmo badge's menu, gliding ``seconds`` to
    each."""
    screen = video.screen
    badge = settled_element(
        screen, lambda: screen.selenium.find_element(By.CSS_SELECTOR, ".wc-gizmo-frame")
    )
    video.jump(*outward(center(badge), arm_pixel(screen), 100))
    video.glide(center(badge), max(0.4, seconds))
    video.click()
    press_mark(video, f"scene-gizmo-{choice}", seconds=seconds)
    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
    if label is not None:
        wait(screen).until(lambda _: gizmo(screen, "tcp:ball")["frame"] == label)
    video.frame()


def open_target_edit(screen, target):
    from tests.helpers.scene_screen import choose

    choose(screen, *settled_pixel(screen, f"targetgroup:{target}"), "Edit Target...")
    wait(screen).until(lambda _: gizmo(screen, "tcp:ball"))
    wait_for_scene_idle(screen)


def approach(video, point, seconds=0.5):
    video.jump(*outward(point, arm_pixel(video.screen), 100))
    video.glide(point, seconds)


def drag_arrow(video, name, length=55, seconds=1.8, settle=None):
    screen = video.screen
    x, y, dx, dy = wait(screen).until(lambda _: probe(screen, "arrowTip", name, "X"))
    approach(video, (x, y))
    video.press()
    video.glide((x + dx * length, y + dy * length), seconds, settle)


def object_still(screen, name: str) -> None:
    """Wait until the named scene object has stopped moving: what a drag
    sets off reaches the view a round trip after the pointer that made it."""
    seen: list = []

    def still(_) -> bool:
        seen.append(
            scene_js(
                screen,
                "const o=S.byName(arguments[0]);"
                "return o && o.matrixWorld.elements.map((v) => +v.toFixed(5));",
                name,
            )
        )
        return len(seen) >= 3 and seen[-1] == seen[-2] == seen[-3]

    WebDriverWait(screen.selenium, 5, poll_frequency=0.03).until(still)


def target_caught_up(screen):
    wait(screen).until(
        lambda _: np.linalg.norm(
            ball_mm(screen)
            - np.asarray(
                run_in_app(lambda: ui_state.urdf_scene._requested_edit_pose[:3])
            )
            * 1000
        )
        < 0.5
    )
