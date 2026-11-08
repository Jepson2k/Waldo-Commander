"""Tutorial: adding keep-outs the planner steers around."""

import pytest
import waldoctl
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import marked_element
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    animating,
    arm_pixel,
    center,
    choose_item,
    menu_item,
    motion,
    object_still,
    outward,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import confirmed, probe, settled_element
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.video_compose import Rect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: The scene panel's transitions run this many times slower while recorded.
SLOW = 3
#: The page's bottom-left quarter, where the rail's lower tabs are.
BOTTOM_LEFT = Rect(0, 540, 960, 540)
#: The placement view, panned right by half the scene panel's width: the
#: boxes go down clear of the panel, which leaves the view where it is.
VIEW = [0.41, 0.359, 0.402, 0.127, 0.112, 0.15]


def test_keep_outs(demo_screen):
    screen = demo_screen
    handle = waldoctl.commander.scene

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    scene_js(screen, "S.setCameraPose(arguments[0])", VIEW)
    wait_for_scene_idle(screen)
    video = Tutorial(
        screen,
        "keep_outs",
        "Keep-outs",
        "Mark space the arm must stay out of",
    )

    def add_keepout(at: tuple[float, float], choice: str) -> None:
        video.glide(at, 1.0)
        video.context_click()
        choose_item(video, menu_item(screen, "Add keep-out…"))
        with animating(video, SLOW):
            video.click(settle=lambda: mark(choice))
        video.highlight(None)

    with motion(screen):
        tab = mark("tab-scene")
        icon = tab.find_element(By.CSS_SELECTOR, "img, .q-icon")
        video.focus(BOTTOM_LEFT, margin=0)
        video.jump(center(icon)[0] + 360, center(icon)[1] + 40)
        video.step("Open the scene panel from the rail", mark=tab)
        video.glide(center(icon), 0.8)
        with animating(video, SLOW):
            video.native_click(
                icon,
                settle=lambda: wait(screen).until(
                    lambda _: marked_element(screen, "scene-tree").is_displayed()
                ),
            )
        video.highlight(None)
        # Off the rail, whose tab's tooltip would come up.
        video.jump(center(icon)[0] + 360, center(icon)[1] - 120)
        video.hide_pointer()
        # Where the card puts a box: found with the panel open, which
        # covers some of the floor.
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        panel = settled_element(
            screen,
            lambda: screen.selenium.find_element(By.CSS_SELECTOR, ".scene-panel"),
        )
        # Room above the floor spot for a box's height and its size label.
        panel_and_floor = [panel, Rect(x - 160, y - 320, 320, 440)]
        video.focus(panel_and_floor, margin=0.05, caption="bottom")
        video.jump(*outward((x, y), arm_pixel(screen)))
        video.step("Right-click the floor: Add keep-out…")
        add_keepout((x, y), "keepout-place")
        place = mark("keepout-place")
        card = place.find_element(
            By.XPATH, "./ancestor::*[.//*[contains(text(), 'Sphere')]][1]"
        )
        video.step("A new keep-out card opens", mark=card)
        video.step("Place here puts a box on the floor", mark=place)
        video.glide(center(place), 0.7)
        # The card turns into the box's: the ring would be left on whatever
        # takes Place here's place.
        video.highlight(None)
        with animating(video, SLOW):
            video.click(
                settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1)
            )
        name = handle.shapes[0].name
        video.pause(OUTCOME_S)

        ax, ay, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", f"shape:{name}")
        )
        start = handle.shapes[0].pose
        video.step("Drag its gizmo; the panel follows")
        video.underline(mark("object-x"))
        # Along the arrow from beyond its tip, past none of the other handles.
        video.jump(ax + dx * 140, ay + dy * 140)
        video.glide((ax, ay), 0.6)
        video.press()
        video.glide(
            (ax + dx * 120, ay + dy * 120),
            2.0,
            settle=lambda: object_still(screen, f"shape:{name}"),
        )
        video.release(
            settle=lambda: confirmed(screen, lambda: handle.shapes[0].pose != start)
        )
        video.pause(CHANGE_S)

        video.highlight(None)
        box = scene_object_pixel(screen, f"shape:{name}")
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video.step("Or draw one: Add keep-out…, then Draw")
        # In from the side away from the first box, clear of its gizmo.
        video.jump(*outward((x, y), box, 220))
        add_keepout((x, y), "keepout-draw")
        draw = mark("keepout-draw")
        # The new card grows the panel up, out of the shot.
        video.hide_pointer()
        video.focus(panel, margin=0.05, caption="bottom")
        video.highlight(draw)
        video.jump(center(draw)[0] + 200, center(draw)[1])
        video.glide(center(draw), 0.7)
        video.highlight(None)
        with animating(video, SLOW):
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: probe(screen, "drawing")["active"]
                )
            )
        video.focus(panel_and_floor, margin=0.05, caption="bottom")
        begin, end = probe(screen, "drawingPlane", [[-0.06, -0.05], [0.06, 0.05]])
        video.step("Drag out its footprint")
        video.glide(tuple(begin), 0.7)
        video.press()
        video.glide(tuple(end), 2.0)
        video.release(
            settle=lambda: wait(screen).until(lambda _: probe(screen, "drawingHeight"))
        )
        hx, hy, dx, dy = probe(screen, "drawingHeight")
        video.step("Then pull up its height")
        video.glide((hx, hy), 0.7)
        video.press()
        with animating(video, SLOW):
            video.glide((hx + dx * 110, hy + dy * 110), 2.0)
            video.release(
                settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 2)
            )
        assert handle.shapes[-1].z > 0.05
        video.hide_pointer()
        video.step("Both are listed in the scene panel", mark=mark("scene-tree"))
        video.pause(CHANGE_S)
        video.finish()
