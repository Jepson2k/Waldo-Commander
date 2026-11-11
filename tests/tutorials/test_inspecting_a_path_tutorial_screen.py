"""Tutorial: inspecting a program's previewed path."""

import numpy as np
import pytest
import waldoctl

from tests.helpers.browser_helpers import marked_element
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    animating,
    approach,
    choose_item,
    arm_pixel,
    motion,
    outward,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.review_scenes import SWEEP_VIEW, swept_preview
from tests.helpers.scene_screen import (
    PATH_PROGRAM,
    PATH_VIEW,
    path_point,
    probe,
    program_previewed,
    settled_element,
)
from tests.helpers.scene_surface import project_local, scene_js, wait_for_scene_idle
from tests.helpers.video_compose import W, Rect, framing

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: The swept volume's reveal runs this many times slower while recorded.
SLOW = 3
#: The control panel, bottom right.
PANEL = Rect(1470, 770, 450, 290)
#: Where the pointer goes out of the shot: empty sky.
AWAY = (1000, 150)


def _bounds(points, pad: float = 80) -> Rect:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return Rect(
        min(xs) - pad,
        min(ys) - pad,
        max(xs) - min(xs) + 2 * pad,
        max(ys) - min(ys) + 2 * pad,
    )


def test_inspecting_a_path(demo_screen):
    screen = demo_screen

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def hit() -> Rect:
        """Where the move's sweep is inside the keep-out, on screen."""
        x, y, w, h = scene_js(
            screen,
            """
            const c = getElement(S.root).core, V = c.camera.position.constructor;
            let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
            for (const s of c.inspection.sweep.samples) {
              if (!s.colliding) continue;
              s.object.traverse((o) => {
                if (!o.isMesh) return;
                o.geometry.computeBoundingBox();
                const b = o.geometry.boundingBox;
                for (const X of [b.min.x, b.max.x]) for (const Y of [b.min.y, b.max.y])
                  for (const Z of [b.min.z, b.max.z]) {
                    const v = new V(X, Y, Z).applyMatrix4(o.matrixWorld).project(c.camera);
                    const [px, py] = c.pointer.screen(v);
                    x0 = Math.min(x0, px); y0 = Math.min(y0, py);
                    x1 = Math.max(x1, px); y1 = Math.max(y1, py);
                  }
              });
            }
            return [x0, y0, x1 - x0, y1 - y0];
            """,
        )
        box = keep_out()
        x0, y0 = max(x, box.x), max(y, box.y)
        x1, y1 = min(x + w, box.x + box.w), min(y + h, box.y + box.h)
        assert x1 > x0 and y1 > y0, "the sweep doesn't reach the keep-out on screen"
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def keep_out() -> Rect:
        """Where the keep-out the move hits is on screen."""
        hx, hy, hz = 0.0225, 0.0225, 0.08
        corners = project_local(
            screen,
            "shape:Sweep keep-out",
            [[x, y, z] for x in (-hx, hx) for y in (-hy, hy) for z in (-hz, hz)],
        )
        return _bounds([(c[0], c[1]) for c in corners], 10)

    def empty_in(view: Rect) -> tuple[float, float]:
        """A spot in ``view`` where a drag orbits: nothing there to pick."""
        for fy in (0.8, 0.7, 0.88, 0.6):
            for fx in (0.12, 0.22, 0.32, 0.78, 0.88):
                x, y = view.x + view.w * fx, view.y + view.h * fy
                hit = scene_js(
                    screen, "return S.pick(arguments[0], arguments[1])", x, y
                )
                if not hit["covered"] and not hit["names"] and not hit["handles"]:
                    return x, y
        raise AssertionError(f"nothing empty to drag in {view}")

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
        video.focus([_bounds(points), _bounds([arm_pixel(screen)], 260)], zoom=1.6)
        video.jump(*outward(points[0], arm_pixel(screen)))
        video.step("Rest the pointer on the path: it lights up")
        video.glide(points[0], 1.0)
        video.pause(OUTCOME_S)
        video.step("Press: the simulator goes there")
        video.press()
        video.pause(OUTCOME_S)
        video.step("Drag along it: the simulator follows")
        video.along(points, 5.0, settle=scrubbed)
        video.release()
        video.pause(OUTCOME_S)
        video.caption(None)

        video.cover("A program that hits a keep-out")

    with swept_preview(screen) as move:
        scene_js(screen, "S.setCameraPose(arguments[0])", SWEEP_VIEW)
        wait_for_scene_idle(screen)
        # Near the move's start, short of the keep-out: on the keep-out, a
        # right-click is the keep-out's.
        point = tuple(path_point(screen, move["command"], 0.08)[:2])
        # Framed under the card: the arm, the move and the keep-out it hits,
        # clear of the control panel, a sliver of which would show.
        shot = framing(
            _bounds([point], 220).union(_bounds([arm_pixel(screen)], 260)), 2.0
        )
        if shot.x + shot.w > PANEL.x - 24 and shot.y + shot.h > PANEL.y - 24:
            shot = Rect(max(PANEL.x - 24 - shot.w, 0), shot.y, shot.w, shot.h)
        video.focus(shot, zoom=W / shot.w, margin=0, seconds=0.01)
        video.uncover()

        video.step("Its last move hits the red keep-out")
        approach(video, point)
        video.step("Right-click its target: Show swept volume")
        video.context_click()
        choose_item(video, mark("scene-toggle-swept-volume"))
        with motion(screen), animating(video, SLOW):
            video.click()
        sweep = wait(screen).until(lambda _: scene_js(screen, "return S.sweptVolume()"))
        assert any(s["colliding"] for s in sweep["samples"])
        video.highlight(None)
        # Out of the shot: over the sweep, its hover label would come up.
        video.jump(*AWAY)
        video.hide_pointer()
        video.step("All the arm passes through is drawn")
        video.pause(OUTCOME_S)

        direction = probe(screen, "viewDirection")
        # Empty space in the shot, low down (the view cube is out of it),
        # dragged away from the arm, whose links would bring up their rings.
        x, y = empty_in(video.view())
        video.step("Drag to see it from another side")
        video.jump(x + 120, y)
        video.glide((x, y), 0.6)
        video.mouse_hint("left")
        video.press()
        video.glide((x - 200, y + 10), 1.6)
        video.release()
        video.mouse_hint(None)
        assert probe(screen, "viewDirection") != direction
        # Off whatever the turn brought under the pointer (a link whose ring
        # would come up) and off the floor, whose hover ring would show.
        video.jump(*AWAY)
        video.hide_pointer()
        video.frame(
            settle=lambda: wait(screen).until(
                lambda _: scene_js(
                    screen, "return !getElement(S.root).core.pointer.ring"
                )
            )
        )
        video.step("The sweep runs into the keep-out", mark=hit())
        video.pause(CHANGE_S)
        video.finish()
