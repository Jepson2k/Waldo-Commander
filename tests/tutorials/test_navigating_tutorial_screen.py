"""Tutorial: moving around the 3D view."""

import numpy as np
import pytest
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import marked_element
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_steps import animating, center, motion
from tests.helpers.demo_video import OUTCOME_S, Tutorial
from tests.helpers.scene_screen import (
    camera_action,
    camera_pose,
    camera_settled,
    probe,
    settled_element,
)
from tests.helpers.video_compose import Rect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: Camera animations run this many times slower while recorded: the
#: scene draws at most ten frames a second of real time this way, every
#: frame of a 0.35 s turn once played back at speed.
SLOW = 5


def _distance(pose: list[float]) -> float:
    return float(np.linalg.norm(np.subtract(pose[:3], pose[3:])))


def test_navigating_the_3d_view(demo_screen):
    screen = demo_screen

    def facing(direction) -> bool:
        n = np.asarray(direction, float) / np.linalg.norm(direction)
        return float(np.dot(probe(screen, "viewDirection"), n)) > 0.999

    def cube_spot(direction) -> tuple[float, float]:
        x, y, _ = wait(screen).until(lambda _: probe(screen, "cubeSpot", direction))
        return x, y

    def cube() -> Rect:
        """Where the cube is drawn, not its box's empty corners."""
        return Rect(*wait(screen).until(lambda _: probe(screen, "cubeBox")))

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    # From the view Reset goes back to, so the end comes back to the start.
    camera_action(screen, "reset")
    camera_settled(screen)
    x, y = wait(screen).until(lambda _: probe(screen, "emptySpot"))
    screen.selenium.execute_cdp_cmd(
        "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}
    )
    wait(screen).until(lambda _: no_visible(screen, ".q-tooltip"))
    video = Tutorial(
        screen,
        "navigating_the_3d_view",
        "Navigating the 3D view",
        "Orbit, pan and zoom, and the view cube",
    )
    video.jump(x, y)

    video.step("Drag empty space to orbit")
    before = probe(screen, "viewDirection")
    video.mouse_hint("left")
    video.press()
    video.glide((x + 300, y + 30), 2.0)
    video.release()
    video.mouse_hint(None)
    assert not np.allclose(probe(screen, "viewDirection"), before, atol=0.05)
    video.pause(OUTCOME_S)

    video.step("Right-drag to pan")
    target = camera_pose(screen)[3:]
    video.mouse_hint("right")
    video.press("right")
    video.glide((x + 120, y + 90), 1.6)
    video.release()
    video.mouse_hint(None)
    assert not np.allclose(camera_pose(screen)[3:], target, atol=0.01)
    video.pause(OUTCOME_S)

    video.step("Scroll to zoom")
    distance = _distance(camera_pose(screen))
    video.mouse_hint("wheel")
    video.scroll(-500, 1.2)
    assert _distance(camera_pose(screen)) < distance * 0.9
    video.pause(0.5)
    video.scroll(300, 0.9)
    video.mouse_hint(None)
    video.pause(OUTCOME_S)

    # Magnified beside themselves while they are used, the whole view
    # still in sight for what they do.
    tools = settled_element(
        screen,
        lambda: screen.selenium.find_element(By.CSS_SELECTOR, ".wc-viewcube-tools"),
    )
    video.loupe([cube(), tools], zoom=2.5)
    with motion(screen):
        video.step("Click a cube face to look along it", mark=cube())
        top = cube_spot([0, 0, 1])
        # From the left: from below, the way in crosses the camera buttons.
        video.jump(top[0] - 300, top[1])
        video.glide(top, 0.8)
        with animating(video, SLOW):
            video.click()
        assert facing([0, 0, 1])
        video.pause(OUTCOME_S)

        video.step("Click a corner for a three-sided view")
        corner = cube_spot([1, -1, 1])
        video.glide(corner, 0.6)
        with animating(video, SLOW):
            video.click()
        assert facing([1, -1, 1])
        video.pause(OUTCOME_S)

        video.step("Drag the cube to orbit, too")
        corner = cube_spot([1, -1, 1])
        video.glide(corner, 0.5)
        before = probe(screen, "viewDirection")
        video.press()
        video.glide((corner[0] - 40, corner[1] + 30), 1.5)
        video.release()
        assert not np.allclose(probe(screen, "viewDirection"), before, atol=0.02)
        video.pause(OUTCOME_S)

        focus = mark("camera-focus")
        video.step("Focus frames the robot", mark=focus)
        # Along the top, above the loupe under the buttons.
        video.along([video.at, (video.at[0], 110), center(focus)], 0.7)
        with animating(video, SLOW):
            video.click()
        # Off the button, whose tooltip would stay up over the result.
        # Off the button, left of the loupe: inside it, the pointer would
        # read as a second one.
        video.glide((center(focus)[0] - 700, center(focus)[1] + 60), 0.6)
        video.pause(OUTCOME_S)

        reset = mark("camera-reset")
        video.step("Reset view goes back to the start", mark=reset)
        video.along([video.at, (video.at[0], 110), center(reset)], 0.7)
        with animating(video, SLOW):
            video.click()
        # Off the button, whose tooltip would stay up over the result.
        video.glide((center(reset)[0] - 700, center(reset)[1] + 60), 0.6)
        video.pause(OUTCOME_S)
    video.loupe(None)
    video.highlight(None)
    video.finish()
