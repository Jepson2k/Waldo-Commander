"""Tutorial: inspecting a program's previewed path."""

import numpy as np
import pytest
import waldoctl

from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import approach, arm_pixel, outward, press_mark
from tests.helpers.demo_video import Tutorial
from tests.helpers.review_scenes import SWEEP_VIEW, swept_preview
from tests.helpers.scene_screen import (
    PATH_PROGRAM,
    PATH_VIEW,
    path_point,
    probe,
    program_previewed,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


def test_inspecting_a_path(demo_screen):
    screen = demo_screen
    with program_previewed(PATH_PROGRAM):
        scene_js(screen, "S.setCameraPose(arguments[0])", PATH_VIEW)
        wait_for_scene_idle(screen)
        record = scene_js(screen, "return S.inspection().records[0]")
        dry = waldoctl.commander.programs.active.dry_run

        def scrubbed() -> None:
            def matches(_) -> bool:
                state = scene_js(screen, "return S.inspection()")
                return bool(state["current"]) and np.allclose(
                    waldoctl.commander.status.joints.angles.rad,
                    dry.commanded.joints_rad[state["current"]["row"]],
                    atol=np.radians(0.05),
                    rtol=0,
                )

            wait(screen, 10).until(matches)

        # From partway along line 7, where nothing covers the path, to its end.
        first = next(i for i, b in enumerate(record["blocks"]) if b["line"] == 7)
        grab = wait(screen).until(
            lambda _: scene_js(
                screen,
                "return S.pathGrab(0, arguments[0], 0.25)",
                record["blocks"][first]["command"],
            )
        )
        points = [(grab[0], grab[1])]
        for i, block in enumerate(record["blocks"][first:]):
            pixels = scene_js(
                screen, "return S.pathPixels(0, arguments[0])", block["command"]
            )
            if not pixels or len(pixels) < 2:
                continue
            if i == 0:
                pixels = [p for p in pixels if p[2] > grab[2]]
            points += [(p[0], p[1]) for p in pixels[:: max(1, len(pixels) // 40)]]
        video = Tutorial(
            screen,
            "inspecting_a_path",
            "Inspecting a path",
            "Scrub through a program's preview, and see what a move sweeps",
        )
        video.jump(*outward(points[0], arm_pixel(screen)))
        video.step("The editor's program is previewed as a path in the view")
        video.glide(points[0], 1.2)
        video.step("Hover the path: it lights up, ready to drag", read=1.0)
        video.step("Press and drag along it: the simulator follows", read=0.6)
        video.press()
        video.along(points, 9.0, settle=scrubbed)
        video.pause(0.8)
        video.release()
        video.pause(1.0)

    with swept_preview(screen) as move:
        scene_js(screen, "S.setCameraPose(arguments[0])", SWEEP_VIEW)
        wait_for_scene_idle(screen)
        video.step("This program's last move runs into a keep-out")
        point = tuple(path_point(screen, move["command"], 0.3)[:2])
        approach(video, point)
        video.step("Right-click the move and choose Show swept volume", read=0.6)
        video.context_click()
        press_mark(video, "scene-toggle-swept-volume")
        video.live(1)
        sweep = wait(screen).until(lambda _: scene_js(screen, "return S.sweptVolume()"))
        assert any(s["colliding"] for s in sweep["samples"])
        video.step(
            "Everything the arm passes through is drawn; the hit is red", read=2.0
        )
        direction = probe(screen, "viewDirection")
        x, y, _ = wait(screen).until(lambda _: probe(screen, "cubeSpot"))
        video.step("Orbit to look at it from another side")
        video.glide((x, y), 0.6)
        video.press()
        video.glide((x + 35, y + 22), 2.0)
        video.release()
        assert probe(screen, "viewDirection") != direction
        video.pause(1.2)
        point = tuple(path_point(screen, move["command"], 0.3)[:2])
        video.step("The same menu hides it again", read=0.6)
        video.glide(point, 0.8)
        video.context_click()
        press_mark(
            video,
            "scene-toggle-swept-volume",
            lambda: wait(screen).until(
                lambda _: scene_js(screen, "return S.sweptVolume()") is None
            ),
        )
        video.finish()
