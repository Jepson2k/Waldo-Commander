"""Tutorial: moving around the 3D view."""

import numpy as np
import pytest

from tests.helpers.browser_helpers import marked_element
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import center, motion
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import camera_pose, probe

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


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

    video = Tutorial(
        screen,
        "navigating_the_3d_view",
        "Navigating the 3D view",
        "Orbit, pan and zoom, and the view cube",
    )
    x, y = wait(screen).until(lambda _: probe(screen, "emptySpot"))
    video.jump(x, y)

    video.step("Drag empty space to orbit around the robot")
    before = probe(screen, "viewDirection")
    video.press()
    video.glide((x + 300, y + 30), 2.5)
    video.release()
    assert not np.allclose(probe(screen, "viewDirection"), before, atol=0.05)
    video.pause(0.6)

    video.step("Right-drag to pan")
    target = camera_pose(screen)[3:]
    video.press("right")
    video.glide((x + 120, y + 90), 2.0)
    video.release()
    assert not np.allclose(camera_pose(screen)[3:], target, atol=0.01)
    video.pause(0.6)

    video.step("Scroll to zoom in and out")
    distance = _distance(camera_pose(screen))
    video.scroll(-500, 1.5)
    assert _distance(camera_pose(screen)) < distance * 0.9
    video.pause(0.4)
    video.scroll(300, 1.0)
    video.pause(0.6)

    with motion(screen):
        video.step("The view cube turns with the camera: click a face to look along it")
        top = cube_spot([0, 0, 1])
        video.glide(top, 1.2)
        video.pause(0.4)
        video.click()
        video.live(1.2)
        assert facing([0, 0, 1])

        video.step("Click a corner for a view from three sides at once")
        corner = cube_spot([1, -1, 1])
        video.glide(corner, 0.9)
        video.pause(0.4)
        video.click()
        video.live(1.2)
        assert facing([1, -1, 1])

        video.step("Drag the cube to orbit too")
        corner = cube_spot([1, -1, 1])
        video.glide(corner, 0.6)
        before = probe(screen, "viewDirection")
        video.press()
        video.glide((corner[0] - 40, corner[1] + 30), 1.8)
        video.release()
        assert not np.allclose(probe(screen, "viewDirection"), before, atol=0.02)
        video.pause(0.6)

        video.step("Focus frames the robot")
        video.glide(center(marked_element(screen, "camera-focus")), 1.0)
        video.pause(0.3)
        video.click()
        video.live(1.4)

        video.step("Reset view puts the camera back where it started")
        video.glide(center(marked_element(screen, "camera-reset")), 0.8)
        video.pause(0.3)
        video.click()
        video.live(1.4)
    video.finish()
