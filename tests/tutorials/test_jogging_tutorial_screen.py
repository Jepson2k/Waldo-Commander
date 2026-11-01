"""Tutorial: jogging the arm from the 3D view."""

import math

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    arm_pixel,
    arm_still,
    bring_up_gizmo,
    jog_ended,
    outward,
)
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import TCP_VIEW, at_start, probe
from tests.helpers.scene_surface import (
    project_local,
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: Close on the base and shoulder, so the base's dial fills the view.
DIAL_VIEW = [0.234, -0.252, 0.37, 0, 0.06, 0.13]


def test_jogging_in_the_3d_view(demo_screen):
    screen = demo_screen
    scene_js(screen, "S.setCameraPose(arguments[0])", DIAL_VIEW)
    wait_for_scene_idle(screen)
    joint = 0
    before = waldoctl.commander.status.joints.angles.deg[joint]
    lx, ly = scene_object_pixel(screen, f"link:L{joint + 1}")
    video = Tutorial(
        screen,
        "jogging_in_the_3d_view",
        "Jogging in the 3D view",
        "Turn a joint on its dial, or move the tool with the gizmo",
    )
    video.jump(*outward((lx, ly), arm_pixel(screen), 320))

    video.step("Rest the pointer on a link: its joint's dial appears")
    video.glide((lx, ly), 1.4)
    shown = wait(screen).until(
        lambda _: scene_js(
            screen, "const r=getElement(S.root).core.pointer.ring;return r?[r.u]:null"
        )
    )[0]
    assert int(shown) == joint
    dial = wait(screen).until(lambda _: probe(screen, "dial", shown))
    grab = wait(screen).until(lambda _: probe(screen, "ringGrab", shown))
    video.pause(0.8)
    at = dial["q"] + math.radians(grab)
    arc = project_local(
        screen,
        f"jog:dial:{joint}",
        [
            [
                dial["r"] * math.cos(at - math.radians(d)),
                dial["r"] * math.sin(at - math.radians(d)),
                0,
            ]
            for d in np.linspace(0, 35, 24)
        ],
    )
    video.step("Drag around the dial to turn the joint, in whole steps")
    video.glide(tuple(arc[0][:2]), 0.8)
    video.press()
    video.along([tuple(p[:2]) for p in arc], 4.0, settle=arm_still)
    video.pause(0.8)
    video.release(settle=lambda: jog_ended(screen))
    assert abs(waldoctl.commander.status.joints.angles.deg[joint] - before) > 10
    video.pause(0.8)

    at_start(screen)
    scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
    wait_for_scene_idle(screen)
    pose = waldoctl.commander.status.pose
    start = np.array([pose.x, pose.y, pose.z])
    bring_up_gizmo(screen, video)
    x, y, dx, dy = wait(screen).until(
        lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
    )
    video.step("Drag an arrow to move the tool along it")
    video.glide((x, y), 1.0)
    video.pause(0.3)
    video.press()
    video.glide((x + dx * 140, y + dy * 140), 3.0, settle=arm_still)
    video.pause(0.8)
    video.release(settle=lambda: jog_ended(screen))
    moved = np.array([pose.x, pose.y, pose.z])
    assert np.linalg.norm(moved - start) > 10
    video.pause(0.8)

    x, y, _, _ = wait(screen).until(
        lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
    )
    video.step("Or click an arrow and type an exact distance")
    video.glide((x, y), 0.8)
    video.pause(0.3)
    video.click()
    wait(screen).until(
        lambda _: scene_js(
            screen, "return !!getElement(S.root).core.gizmos.precision.entry"
        )
    )
    video.pause(0.6)
    video.type("25")
    video.pause(0.5)
    video.step("Enter moves the tool by that much", read=0.6)
    video.key(Keys.ENTER)
    video.live(2.5)
    video.frame(
        settle=lambda: wait(screen, 15).until(
            lambda _: (
                abs(np.linalg.norm(np.array([pose.x, pose.y, pose.z]) - moved) - 25)
                < 0.5
            )
        )
    )
    video.finish()
