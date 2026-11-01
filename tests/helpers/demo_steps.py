"""Steps the recorded videos share: the review demos and the tutorials.

Each moves the pointer the way a user would (from outside the arm, waiting
for menus to finish opening) so a recording shows a real interaction.
"""

import math
import time
from contextlib import contextmanager

import numpy as np
import waldoctl
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_video import DemoVideo
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


def outward(
    point: tuple[float, float], away: tuple[float, float], distance: float = 260
) -> tuple[float, float]:
    """A spot ``distance`` px beyond ``point``, on the side away from
    ``away``: an approach from there crosses no link, so no joint's dial
    comes up on the way in."""
    dx, dy = point[0] - away[0], point[1] - away[1]
    n = math.hypot(dx, dy) or 1.0
    return point[0] + dx / n * distance, point[1] + dy / n * distance


def arm_pixel(screen) -> tuple[float, float]:
    """Where the middle of the arm is on screen."""
    x, y = scene_js(
        screen,
        "const c=getElement(S.root).core,v=new (c.camera.position.constructor)(0,0,0.15);"
        "return c.pointer.screen(v.project(c.camera));",
    )
    return x, y


def bring_up_gizmo(screen, video: DemoVideo) -> None:
    """From outside the arm to the tool's badge, whose click brings up the
    gizmo, as a user would."""

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

    # The badge stands aside while a dial's label would cover it.
    spot = wait(screen).until(on_top)
    video.jump(*outward(spot, arm_pixel(screen)))
    video.caption("Click the tool's badge to bring up the gizmo")
    video.pause(0.8)
    video.glide(spot, 1.2)

    def shown() -> None:
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

    video.click(settle=shown)
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


def frame_choice(video, choice, label):
    screen = video.screen
    badge = settled_element(
        screen, lambda: screen.selenium.find_element(By.CSS_SELECTOR, ".wc-gizmo-frame")
    )
    video.jump(*outward(center(badge), arm_pixel(screen), 100))
    video.glide(center(badge), 0.4)
    video.click()
    press_mark(video, f"scene-gizmo-{choice}")
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
