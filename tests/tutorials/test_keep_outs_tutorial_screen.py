"""Tutorial: adding keep-outs the planner steers around."""

import pytest
import waldoctl

from tests.helpers.browser_helpers import marked_element
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    PLACEMENT_VIEW,
    arm_pixel,
    center,
    menu_item,
    open_scene_panel,
    outward,
    press_mark,
)
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import confirmed, probe, settled_element
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


def test_keep_outs(demo_screen):
    screen = demo_screen
    handle = waldoctl.commander.scene
    # The keep-out card opens in the scene panel; open, it leaves the view still.
    open_scene_panel(screen)
    scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
    wait_for_scene_idle(screen)
    x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
    video = Tutorial(
        screen,
        "keep_outs",
        "Keep-outs",
        "Mark space the arm must stay out of",
    )
    video.jump(*outward((x, y), arm_pixel(screen)))

    def add_keepout(choice: str) -> None:
        video.glide((x, y), 1.2)
        video.pause(0.3)
        video.context_click()
        item = menu_item(screen, "Add keep-out…")
        video.pause(0.6)
        video.glide(center(item), 0.8)
        video.click(
            settle=lambda: settled_element(
                screen, lambda: marked_element(screen, choice)
            )
        )

    video.step("Right-click the floor and choose Add keep-out…")
    add_keepout("keepout-place")
    video.step("The scene panel shows the new keep-out: Place here puts a box down")
    press_mark(
        video,
        "keepout-place",
        settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1),
    )
    name = handle.shapes[0].name
    video.pause(1.0)

    ax, ay, dx, dy = wait(screen).until(
        lambda _: probe(screen, "arrowTip", f"shape:{name}")
    )
    start = handle.shapes[0].pose
    video.step("Drag its gizmo to move it")
    video.glide((ax, ay), 0.8)
    video.press()
    video.glide((ax + dx * 120, ay + dy * 120), 3.0)
    video.release(
        settle=lambda: confirmed(screen, lambda: handle.shapes[0].pose != start)
    )
    video.step("Its size and position are in the scene panel too", read=1.8)

    x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
    video.step("Or draw one: Add keep-out…, then Draw")
    add_keepout("keepout-draw")
    press_mark(
        video,
        "keepout-draw",
        settle=lambda: wait(screen).until(lambda _: probe(screen, "drawing")["active"]),
    )
    begin, end = probe(screen, "drawingPlane", [[-0.06, -0.05], [0.06, 0.05]])
    video.step("Drag out its footprint")
    video.glide(tuple(begin), 0.8)
    video.press()
    video.glide(tuple(end), 2.5)
    video.release(
        settle=lambda: wait(screen).until(lambda _: probe(screen, "drawingHeight"))
    )
    hx, hy, dx, dy = probe(screen, "drawingHeight")
    video.step("Then pull up its height")
    video.glide((hx, hy), 0.8)
    video.press()
    video.glide((hx + dx * 110, hy + dy * 110), 2.5)
    video.release(settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 2))
    assert handle.shapes[-1].z > 0.05
    video.step("The arm now won't move into either", read=1.6)
    video.finish()
