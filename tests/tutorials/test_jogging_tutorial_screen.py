"""Tutorial: jogging the arm from the 3D view."""

import math

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    arm_pixel,
    arm_still,
    camera_to,
    gizmo_center,
    gizmo_shown,
    jog_ended,
    outward,
    slowed_jogs,
    tcp_badge,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import TCP_VIEW, probe, settled_element
from tests.helpers.scene_surface import (
    project_local,
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.video_compose import Rect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: Moves run this many times slower while recorded, then play back at speed.
SLOW = 3
#: Close on the base and shoulder, so the base's ring fills the view.
DIAL_VIEW = [0.234, -0.252, 0.37, 0, 0.06, 0.13]


def _turned(view: list[float], degrees: float) -> list[float]:
    """``view`` carried round the base's axis by ``degrees``, as a turn of
    the base carries the tool."""
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    out = list(view)
    for i in (0, 3):
        x, y = view[i], view[i + 1]
        out[i], out[i + 1] = x * c - y * s, x * s + y * c
    return out


def test_jogging_in_the_3d_view(demo_screen):
    screen = demo_screen
    status = waldoctl.commander.status
    scene_js(screen, "S.setCameraPose(arguments[0])", DIAL_VIEW)
    wait_for_scene_idle(screen)
    joint = 0
    start = float(status.joints.angles.deg[joint])
    lx, ly = scene_object_pixel(screen, f"link:L{joint + 1}")
    video = Tutorial(
        screen,
        "jogging_in_the_3d_view",
        "Jogging in the 3D view",
        "Turn a joint on its ring, or move the tool with the gizmo",
    )
    video.jump(*outward((lx, ly), arm_pixel(screen), 320))

    video.step("Rest the pointer on a link")
    video.glide((lx, ly), 1.2)
    shown = wait(screen).until(
        lambda _: scene_js(
            screen, "const r=getElement(S.root).core.pointer.ring;return r?[r.u]:null"
        )
    )[0]
    assert int(shown) == joint
    dial = wait(screen).until(lambda _: probe(screen, "dial", shown))
    grab = wait(screen).until(lambda _: probe(screen, "ringGrab", shown))
    ring = project_local(
        screen,
        f"jog:dial:{joint}",
        [
            [dial["r"] * math.cos(t), dial["r"] * math.sin(t), 0]
            for t in np.linspace(0, 2 * math.pi, 16, endpoint=False)
        ],
    )
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    video.focus(
        Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)),
        margin=0.4,
        caption="bottom",
    )
    video.pause(OUTCOME_S)
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
            for d in np.linspace(0, 32, 24)
        ],
    )
    video.step("Drag the ring to turn the joint")
    video.glide(tuple(arc[0][:2]), 0.6)
    video.press()
    video.along([tuple(p[:2]) for p in arc], 1.2, settle=arm_still)
    video.release(settle=lambda: jog_ended(screen))
    turned = float(status.joints.angles.deg[joint]) - start
    assert abs(turned) > 10
    video.pause(CHANGE_S)

    # Off the arm, so the ring puts itself away before the view moves on.
    video.glide(outward((lx, ly), arm_pixel(screen), 420), 0.6)
    # Out of the way: on the floor, its hover ring would show where it rests.
    video.jump(1000, 150)
    wait(screen).until(
        lambda _: scene_js(screen, "return !getElement(S.root).core.pointer.ring")
    )
    video.hide_pointer()
    video.focus(None)
    video.step("Now the tool and its gizmo")
    camera_to(video, _turned(TCP_VIEW, turned), seconds=1.4, factor=5)

    spot = tcp_badge(screen)
    tcp = gizmo_center(screen)
    # The tool low in the shot: the drag and the typed move both go up its
    # X arrow, and the typed value and the result ride on the arrow's tip.
    video.focus(Rect(tcp[0] - 380, tcp[1] - 470, 760, 670), margin=0)
    video.jump(*outward(spot, arm_pixel(screen)))
    badge = screen.selenium.find_element(
        By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
    )
    video.step("Click the TCP badge for the gizmo", mark=badge)
    video.glide(spot, 0.9)
    video.click(settle=lambda: gizmo_shown(screen, spot))
    video.highlight(None)
    video.pause(OUTCOME_S)

    pose = status.pose
    start_xyz = np.array([pose.x, pose.y, pose.z])
    x, y, dx, dy = wait(screen).until(
        lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
    )
    video.step("Drag an arrow to move the tool")
    video.glide((x, y), 0.8)
    video.press()
    video.glide((x + dx * 140, y + dy * 140), 2.0, settle=arm_still)
    video.release(settle=lambda: jog_ended(screen))
    moved = np.array([pose.x, pose.y, pose.z])
    assert np.linalg.norm(moved - start_xyz) > 10
    video.pause(OUTCOME_S)

    x, y, _, _ = wait(screen).until(
        lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
    )
    video.step("Or click an arrow, type a distance")
    video.glide((x, y), 0.7)
    video.click()
    field = settled_element(
        screen,
        lambda: screen.selenium.find_element(
            By.CSS_SELECTOR, ".wc-gizmo-field:not([hidden])"
        ),
    )
    video.glide((x + 60, y + 90), 0.4)
    video.keys(["2", "5", "Enter"])
    video.type("25", per_key_s=0.25)
    video.underline(field)
    video.pause(OUTCOME_S)

    video.step("Enter moves the tool that far")
    # The field closes on Enter: its underline would float over nothing.
    video.highlight(None)
    with slowed_jogs(SLOW), video.recording(SLOW):
        video.key(Keys.ENTER)
        wait(screen, 30).until(
            lambda _: (
                abs(np.linalg.norm(np.array([pose.x, pose.y, pose.z]) - moved) - 25)
                < 0.5
            )
        )
        arm_still()
    video.pause(OUTCOME_S)
    video.finish()
