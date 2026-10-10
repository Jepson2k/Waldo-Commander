"""Review demos: one action per video, slow and smooth, captioned.

Recorded only under ``pytest --evidence`` (conftest leaves this file out
otherwise). Each demo drives the app through its real input paths and
checks the outcome it shows, so a demo of something broken fails instead of
recording it.
"""

import asyncio
import math
import time
from contextlib import contextmanager

import numpy as np
import pytest
import waldoctl
from nicegui import core
from waldoctl.shapes import Box
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_tab,
    close_panel,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_video import DemoVideo
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    PATH_PROGRAM,
    PATH_VIEW,
    TCP_VIEW,
    at_start,
    camera_kept,
    confirmed,
    gizmo,
    place_shape,
    probe,
    program_previewed,
)
from tests.helpers.scene_surface import (
    project_local,
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_par6_backend import par6_env, requires_par6  # noqa: F401
from waldo_commander.services import world_files
from waldo_commander.state import ui_state

#: The floor in front of the arm, close enough to see a target land there.
PLACEMENT_VIEW = [0.333, 0.447, 0.402, 0.05, 0.2, 0.15]


def _arm_still(timeout: float = 4.0) -> None:
    """Wait until the arm stops moving."""
    deadline = time.monotonic() + timeout
    last = waldoctl.commander.status.joints.angles.rad.copy()
    while time.monotonic() < deadline:
        time.sleep(0.06)
        now = waldoctl.commander.status.joints.angles.rad.copy()
        if np.allclose(now, last, atol=1e-5, rtol=0):
            return
        last = now


def _jog_ended(screen) -> None:
    wait(screen, 15).until(
        lambda _: ui_state.control_panel._jog_end_wait_task is None
        or ui_state.control_panel._jog_end_wait_task.done()
    )
    _arm_still()


@contextmanager
def program_panel(screen):
    """The program editor open beside the scene, so a demo shows its source."""
    click_tab(screen, "program")
    try:
        yield
    finally:
        close_panel(screen, "program-panel")


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


def _center(element) -> tuple[float, float]:
    r = element.rect
    return r["x"] + r["width"] / 2, r["y"] + r["height"] / 2


def _menu_item(screen, starts: str):
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


def _settled(screen, find):
    """``find()``'s element once it is shown and its menu has finished
    opening, so the pointer is aimed where it stays."""
    previous = None

    def ready(_):
        nonlocal previous
        # The menu is re-rendered as it fills in, so an entry can go stale.
        try:
            e = find()
            if not e.is_displayed():
                return None
            rect, previous = (e.rect, previous), e.rect
            moving = screen.selenium.execute_script(
                "const m=arguments[0].closest('.q-menu');"
                "return !!m && m.getAnimations({subtree:true})"
                ".some(a=>a.playState==='running');",
                e,
            )
        except StaleElementReferenceException:
            return None
        return e if rect[0] == rect[1] and not moving else None

    return wait(screen).until(ready)


def _outward(
    point: tuple[float, float], away: tuple[float, float], distance: float = 260
) -> tuple[float, float]:
    """A spot ``distance`` px beyond ``point``, on the side away from
    ``away``: an approach from there crosses no link, so no joint's dial
    comes up on the way in."""
    dx, dy = point[0] - away[0], point[1] - away[1]
    n = math.hypot(dx, dy) or 1.0
    return point[0] + dx / n * distance, point[1] + dy / n * distance


def _arm_pixel(screen) -> tuple[float, float]:
    """Where the middle of the arm is on screen."""
    x, y = scene_js(
        screen,
        "const c=getElement(S.root).core,v=new (c.camera.position.constructor)(0,0,0.15);"
        "return c.pointer.screen(v.project(c.camera));",
    )
    return x, y


def _bring_up_gizmo(screen, video: DemoVideo) -> None:
    """From outside the arm to the tool's badge, whose click brings up the
    gizmo, as a user would."""

    def on_top(_):
        badge = screen.selenium.find_element(
            By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
        )
        x, y = _center(badge)
        hit = screen.selenium.execute_script(
            "return document.elementFromPoint(arguments[0], arguments[1])"
            "?.getAttribute('aria-label')",
            x,
            y,
        )
        return (x, y) if hit == "Move TCP" else None

    # The badge stands aside while a dial's label would cover it.
    spot = wait(screen).until(on_top)
    video.jump(*_outward(spot, _arm_pixel(screen)))
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


def _gizmo_center(screen) -> tuple[float, float]:
    x, y = project_local(screen, "tcp:ball", [[0, 0, 0]])[0][:2]
    return x, y


@pytest.fixture
def demo_screen(class_screen):
    screen = class_screen
    screen_wait_for_scene_ready(screen, timeout_s=40)
    at_start(screen)
    with viewport(screen, 1920, 1080), camera_kept(screen):
        yield screen
    # A demo that failed keeps what it recorded, to show where it went wrong.
    if DemoVideo.current is not None and DemoVideo.current.frames:
        DemoVideo.current.save("-failed")


@pytest.mark.browser
class TestReviewDemos:
    def test_gizmo_moves_along_an_arrow(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.x, pose.y, pose.z])
        video = DemoVideo(screen, "gizmo-move-arrow")
        _bring_up_gizmo(screen, video)
        x, y, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
        )
        video.caption("Drag an arrow: the tool moves along that axis")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.press()
        video.glide((x + dx * 140, y + dy * 140), 3.0, settle=_arm_still)
        video.caption("The change rides on the arrow as you drag")
        video.pause(1.2)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        assert np.linalg.norm(np.array([pose.x, pose.y, pose.z]) - before) > 10

    def test_gizmo_moves_in_a_plane(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.x, pose.y, pose.z])
        video = DemoVideo(screen, "gizmo-move-plane")
        _bring_up_gizmo(screen, video)
        x, y, dx, dy = wait(screen).until(lambda _: probe(screen, "planeSpot", "XY"))
        video.caption("Drag a square: the tool moves in that plane")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.press()
        video.along(
            [
                (x, y),
                (x + dx * 90, y + dy * 90),
                (x + dx * 90 - dy * 70, y + dy * 90 + dx * 70),
            ],
            3.5,
            settle=_arm_still,
        )
        video.pause(1.0)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        after = np.array([pose.x, pose.y, pose.z])
        # The live gizmo's planes are the tool's, not the world's.
        assert np.linalg.norm(after - before) > 10

    def test_gizmo_turns_about_an_arc(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.rx, pose.ry, pose.rz])
        video = DemoVideo(screen, "gizmo-turn-arc")
        _bring_up_gizmo(screen, video)
        x, y, dx, dy = wait(screen).until(lambda _: probe(screen, "ringSpot", "Z"))
        cx, cy = _gizmo_center(screen)
        radius = math.hypot(x - cx, y - cy)
        start = math.atan2(y - cy, x - cx)
        # Turn the way the ring runs at the press.
        sense = 1 if (-(y - cy) * dx + (x - cx) * dy) > 0 else -1
        arc = [
            (
                cx + radius * math.cos(start + sense * t),
                cy + radius * math.sin(start + sense * t),
            )
            for t in np.linspace(0, math.radians(55), 24)
        ]
        video.caption("Drag an arc: the tool turns about that axis")
        video.glide((x, y), 1.0)
        video.pause(0.3)
        video.press()
        video.along(arc, 3.5, settle=_arm_still)
        video.pause(1.0)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        # About the tool's own Z, so any of the angles may change.
        turned = (np.array([pose.rx, pose.ry, pose.rz]) - before + 180) % 360 - 180
        assert np.abs(turned).max() > 10

    def test_gizmo_takes_a_typed_value(self, demo_screen):
        screen = demo_screen
        scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
        pose = waldoctl.commander.status.pose
        before = np.array([pose.x, pose.y, pose.z])
        moved = lambda: np.linalg.norm(np.array([pose.x, pose.y, pose.z]) - before)  # noqa: E731
        video = DemoVideo(screen, "gizmo-typed-value")
        _bring_up_gizmo(screen, video)
        x, y, _, _ = wait(screen).until(
            lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
        )
        video.caption("Click an arrow to type an exact move")
        video.glide((x, y), 1.0)
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
        video.caption("Enter applies it")
        video.key(Keys.ENTER)
        video.live(2.5)
        video.frame(
            settle=lambda: wait(screen, 15).until(
                # Along the tool's own X, which the live gizmo uses.
                lambda _: abs(moved() - 25) < 0.5
            )
        )
        video.pause(1.0)
        video.save()
        assert moved() == pytest.approx(25, abs=0.5)

    def test_editing_a_target_moves_the_target(self, demo_screen):
        screen = demo_screen
        scene = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM) as targets, program_panel(screen):
            scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
            wait_for_scene_idle(screen)
            live = waldoctl.commander.status.joints.angles.rad.copy()
            mx, my = scene_object_pixel(screen, f"targetgroup:{targets[-1]}")
            video = DemoVideo(screen, "target-edit")
            video.jump(*_outward((mx, my), _arm_pixel(screen)))
            video.caption("Right-click a target to edit it")
            video.pause(0.8)
            video.glide((mx, my), 1.0)
            video.pause(0.3)
            video.context_click()
            item = _menu_item(screen, "Edit Target")
            video.pause(0.6)
            video.glide(_center(item), 0.7)
            video.click(settle=lambda: wait(screen).until(lambda _: scene._edit_active))
            video.pause(0.8)
            initial = scene._requested_edit_pose
            x, y, dx, dy = wait(screen).until(
                lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
            )
            video.caption("Drag the gizmo: the target moves, the arm stays put")
            video.glide((x, y), 0.8)
            video.press()
            video.glide((x + dx * 110, y + dy * 110), 3.0)
            video.release(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose) != initial
                )
            )
            video.caption("Let go and it is kept: its line in the program is rewritten")
            video.pause(2.0)
            video.caption("Ctrl+Z takes it back")
            video.chord(
                Keys.CONTROL,
                "z",
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                    == EDIT_PROGRAM
                ),
            )
            video.live(1.5)
            video.pause(1.0)
            video.save()
            assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    def test_placing_a_target_on_the_floor(self, demo_screen):
        screen = demo_screen
        scene = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM), program_panel(screen):
            scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
            wait_for_scene_idle(screen)
            live = waldoctl.commander.status.joints.angles.rad.copy()
            x, y, gx, gy = wait(screen).until(lambda _: probe(screen, "floorSpot"))
            video = DemoVideo(screen, "target-place")
            video.jump(*_outward((x, y), _arm_pixel(screen)))
            video.caption("Over the floor, a ring shows where a target would go")
            video.pause(0.6)
            video.glide((x, y), 2.0)
            video.pause(0.6)
            video.context_click()
            item = _menu_item(screen, "Place Target Here")
            video.pause(0.6)
            video.glide(_center(item), 0.7)
            video.click(settle=lambda: wait(screen).until(lambda _: scene._edit_active))
            video.caption("A new target lands there, in the program at once")
            video.pause(1.5)
            tx, ty, dx, dy = wait(screen).until(
                lambda _: probe(screen, "arrowTip", "tcp:ball", "Z")
            )
            video.caption("Lift it with the Z arrow; red means out of reach")
            video.glide((tx, ty), 0.8)
            video.press()
            reached = video.along(
                [(tx, ty), (tx + dx * 260, ty + dy * 260)],
                6.0,
                until=lambda: run_in_app(
                    lambda: scene._requested_edit_pose[2] > 0.04
                    and not scene._tcp_ik_missed
                ),
            )
            video.release()
            assert reached, "no reachable height above the floor spot"
            video.caption("Let go and it is kept; Escape puts the edit away")
            video.pause(1.5)
            video.key(
                Keys.ESCAPE,
                settle=lambda: wait(screen).until(lambda _: not scene._edit_active),
            )
            video.live(2.0)
            video.pause(1.0)
            video.save()
            assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    def test_scrubbing_along_a_path(self, demo_screen):
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

            # From partway along line 7, clear of the arm, to the path's end.
            first = next(i for i, b in enumerate(record["blocks"]) if b["line"] == 7)
            points = []
            for i, block in enumerate(record["blocks"][first:]):
                pixels = scene_js(
                    screen, "return S.pathPixels(0, arguments[0])", block["command"]
                )
                if not pixels or len(pixels) < 2:
                    continue
                if i == 0:
                    pixels = pixels[len(pixels) // 10 :]
                points += [(p[0], p[1]) for p in pixels[:: max(1, len(pixels) // 40)]]
            video = DemoVideo(screen, "path-scrub")
            video.jump(*_outward(points[0], _arm_pixel(screen)))
            video.caption("Hover a path: it lights up, ready to drag")
            video.pause(0.6)
            video.glide(points[0], 1.0)
            video.pause(0.6)
            video.caption("Press and drag along it: the simulator follows")
            video.press()
            video.along(points, 9.0, settle=scrubbed)
            video.pause(1.0)
            video.release()
            video.pause(1.0)
            video.save()

    def test_joint_dial_turns_a_joint(self, demo_screen):
        screen = demo_screen
        u = 0
        scene_js(
            screen,
            "S.setCameraPose(arguments[0])",
            [0.234, -0.252, 0.37, 0, 0.06, 0.13],
        )
        wait_for_scene_idle(screen)
        before = waldoctl.commander.status.joints.angles.deg[u]
        lx, ly = scene_object_pixel(screen, f"link:L{u + 1}")
        video = DemoVideo(screen, "joint-dial")
        video.jump(*_outward((lx, ly), _arm_pixel(screen), 320))
        video.caption("Hover a link: its joint's dial appears")
        video.pause(0.6)
        video.glide((lx, ly), 1.2)
        try:
            shown = wait(screen).until(
                lambda _: scene_js(
                    screen,
                    "const r=getElement(S.root).core.pointer.ring;return r?[r.u]:null",
                )
            )[0]
        except TimeoutException:
            state = scene_js(
                screen,
                "const c=getElement(S.root).core,p=c.pointer,h=p.hover,[x,y]=arguments[0];"
                "return {shown:h.shown,target:h.target,pinned:h.pinned,grace:h.graceAt,"
                "hovered:p.hovered,pick:S.pick(x,y),rings:Object.keys(c.ix.rings||{})};",
                [lx, ly],
            )
            raise AssertionError(f"no dial on L{u + 1}: {state}") from None
        assert int(shown) == u, f"J{int(shown) + 1}'s dial came up, not J{u + 1}'s"
        dial = wait(screen).until(lambda _: probe(screen, "dial", shown))
        grab = wait(screen).until(lambda _: probe(screen, "ringGrab", shown))
        video.pause(0.8)
        at = dial["q"] + math.radians(grab)
        arc = project_local(
            screen,
            f"jog:dial:{u}",
            [
                [
                    dial["r"] * math.cos(at - math.radians(d)),
                    dial["r"] * math.sin(at - math.radians(d)),
                    0,
                ]
                for d in np.linspace(0, 35, 24)
            ],
        )
        video.caption("Drag around the dial to turn the joint")
        video.glide(tuple(arc[0][:2]), 0.8)
        video.press()
        video.along([tuple(p[:2]) for p in arc], 3.5, settle=_arm_still)
        video.pause(0.8)
        video.release(settle=lambda: _jog_ended(screen))
        video.pause(1.0)
        video.save()
        assert abs(waldoctl.commander.status.joints.angles.deg[u] - before) > 10

    def test_placing_and_moving_a_keepout(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video = DemoVideo(screen, "keepout-place-move")
        video.jump(*_outward((x, y), _arm_pixel(screen)))
        video.caption("Right-click the floor to add a keep-out there")
        video.pause(0.6)
        video.glide((x, y), 1.0)
        video.context_click()
        submenu = _menu_item(screen, "Create keep-out")
        video.pause(0.5)
        video.glide(_center(submenu), 0.7)
        video.click()
        item = _menu_item(screen, "Box here")
        video.pause(0.4)
        video.glide(_center(item), 0.6)
        video.click(settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1))
        name = handle.shapes[0].name
        video.caption("It is placed and selected; the inspector sits beside it")
        video.pause(1.8)
        ax, ay, dx, dy = wait(screen).until(
            lambda _: probe(screen, "arrowTip", f"shape:{name}")
        )
        start = handle.shapes[0].pose
        video.caption("Drag its gizmo to move it")
        video.glide((ax, ay), 0.8)
        video.press()
        video.glide((ax + dx * 120, ay + dy * 120), 3.0)
        video.release(
            settle=lambda: confirmed(screen, lambda: handle.shapes[0].pose != start)
        )
        video.pause(1.5)
        video.save()

    def test_drawing_a_box(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        video = DemoVideo(screen, "keepout-draw-box")
        video.jump(*_outward((x, y), _arm_pixel(screen)))
        video.caption("Right-click the floor, Create keep-out, Draw Box")
        video.pause(0.6)
        video.glide((x, y), 1.0)
        video.context_click()
        submenu = _menu_item(screen, "Create keep-out")
        video.pause(0.5)
        video.glide(_center(submenu), 0.7)
        video.click()
        item = _menu_item(screen, "Draw Box")
        video.pause(0.4)
        video.glide(_center(item), 0.6)
        video.click(
            settle=lambda: wait(screen).until(
                lambda _: probe(screen, "drawing")["active"]
            )
        )
        start, end = probe(screen, "drawingPlane", [[-0.06, -0.05], [0.06, 0.05]])
        video.caption("Drag out its footprint")
        video.glide(tuple(start), 0.8)
        video.press()
        video.glide(tuple(end), 2.5)
        video.release(
            settle=lambda: wait(screen).until(lambda _: probe(screen, "drawingHeight"))
        )
        hx, hy, dx, dy = probe(screen, "drawingHeight")
        video.caption("Then pull up its height")
        video.glide((hx, hy), 0.8)
        video.press()
        video.glide((hx + dx * 110, hy + dy * 110), 2.5)
        video.release(settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1))
        video.pause(1.5)
        video.save()
        assert handle.shapes[0].z > 0.05

    def test_resizing_a_box(self, demo_screen):
        screen = demo_screen
        handle = waldoctl.commander.scene
        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        scene_js(screen, "S.setCameraPose(arguments[0])", PLACEMENT_VIEW)
        wait_for_scene_idle(screen)
        x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
        place_shape(screen, "box", x, y, "Fixture")
        initial = handle.shapes[0]
        video = DemoVideo(screen, "keepout-resize")
        resize = marked_element(screen, "object-transform").find_element(
            By.XPATH, ".//button[.//*[normalize-space()='Resize']]"
        )
        video.jump(*_outward(_center(resize), _arm_pixel(screen), 160))
        video.caption("Switch the selected keep-out to Resize")
        video.pause(0.6)
        video.glide(_center(resize), 1.0)
        video.click(
            settle=lambda: wait(screen).until(lambda _: probe(screen, "resizeHandle"))
        )
        hx, hy, dx, dy, *_ = probe(screen, "resizeHandle")
        video.caption("Drag a face: the opposite face stays put")
        video.glide((hx, hy), 0.8)
        video.press()
        video.glide((hx + dx * 90, hy + dy * 90), 3.0)
        video.release(
            settle=lambda: confirmed(screen, lambda: handle.shapes[0] != initial)
        )
        video.pause(1.5)
        video.save()

    def test_isolating_objects(self, demo_screen):
        screen = demo_screen
        view = ui_state.urdf_scene
        handle = waldoctl.commander.scene
        shapes = [
            Box(name=name, x=0.065, y=0.065, z=0.09, pose=(*point, 0, 0, 0))
            for name, point in zip(
                ["Fixture A", "Fixture B", "Clamp", "Stock"],
                [
                    (0.35, -0.18, 0.045),
                    (0.4, 0.05, 0.045),
                    (0.55, -0.1, 0.045),
                    (0.55, 0.15, 0.045),
                ],
            )
        ]
        run_in_app(lambda: setattr(handle, "shapes", shapes))
        confirmed(screen, lambda: handle.shapes == shapes)
        scene_js(
            screen,
            "S.setCameraPose(arguments[0])",
            [0.685, -0.4, 0.295, 0.42, -0.05, 0.05],
        )
        wait_for_scene_idle(screen)
        a = scene_object_pixel(screen, "shape:Fixture A")
        b = scene_object_pixel(screen, "shape:Fixture B")
        video = DemoVideo(screen, "isolate")
        video.jump(*_outward(a, _arm_pixel(screen)))
        video.caption("Hovering a keep-out lights it")
        video.pause(0.6)
        video.glide(a, 1.0)
        video.pause(1.2)
        video.caption("Click it, then shift-click another")
        video.pause(0.6)
        video.click(
            settle=lambda: wait(screen).until(
                lambda _: run_in_app(lambda: len(view._selected_objects)) == 1
            )
        )
        video.pause(0.4)
        video.glide(b, 1.0)
        video.click(
            shift=True,
            settle=lambda: wait(screen).until(
                lambda _: run_in_app(lambda: len(view._selected_objects)) == 2
            ),
        )
        video.pause(0.8)

        def from_actions(marker: str, done) -> None:
            video.glide(_center(marked_element(screen, "object-actions")), 0.8)
            video.click()
            item = wait(screen).until(
                lambda _: (e := marked_element(screen, marker)).is_displayed() and e
            )
            video.pause(0.3)
            video.glide(_center(item), 0.6)
            video.click(settle=lambda: wait(screen).until(lambda _: done()))

        video.caption("Isolate them: the rest of the cell fades out")
        from_actions("object-isolate", lambda: run_in_app(lambda: view._isolated_view))
        video.pause(2.0)
        video.caption("Show all objects brings the rest back")
        from_actions(
            "object-isolate", lambda: not run_in_app(lambda: view._isolated_view)
        )
        video.pause(1.5)
        video.save()

    def test_camera_tools(self, demo_screen):
        screen = demo_screen
        with motion(screen):
            with program_previewed(EDIT_PROGRAM):
                wait_for_scene_idle(screen)
                video = DemoVideo(screen, "camera-tools")
                menu = _center(marked_element(screen, "camera-menu"))
                video.jump(menu[0] + 300, menu[1] + 200)

                def from_menu(caption: str, find, seconds: float = 1.6) -> None:
                    video.caption(caption)
                    # Some entries leave the menu open for the next.
                    if no_visible(screen, ".q-menu"):
                        video.glide(menu, 0.8)
                        video.click()
                    item = _settled(screen, find)
                    video.pause(0.3)
                    video.glide(_center(item), 0.6)
                    video.click()
                    video.live(seconds)

                from_menu(
                    "Focus the robot", lambda: marked_element(screen, "camera-focus")
                )
                from_menu(
                    "Snap to the top view", lambda: marked_element(screen, "camera-top")
                )
                from_menu(
                    "Orthographic: no perspective, true proportions",
                    lambda: marked_element(screen, "camera-projection").find_element(
                        By.XPATH, ".//button[.//*[text()='Orthographic']]"
                    ),
                    0.8,
                )
                from_menu(
                    "Back to the previous view",
                    lambda: marked_element(screen, "camera-back"),
                )
                video.pause(1.0)
                video.save()

    def test_view_cube(self, demo_screen):
        screen = demo_screen

        def spot(direction):
            x, y, _ = wait(screen).until(lambda _: probe(screen, "cubeSpot", direction))
            return x, y

        with motion(screen), program_previewed(EDIT_PROGRAM):
            wait_for_scene_idle(screen)
            video = DemoVideo(screen, "view-cube")
            corner = spot([1, -1, 1])
            video.jump(corner[0] - 320, corner[1] + 220)
            video.caption("The view cube, top right, turns with the camera")
            video.pause(0.8)
            video.glide(corner, 1.2)
            video.caption("Hover a corner, edge or face; click it to look from there")
            video.pause(1.0)
            video.click()
            video.live(1.2)
            top = spot([0, 0, 1])
            video.caption("A face looks straight on: here, from the top")
            video.glide(top, 0.8)
            video.pause(0.5)
            video.click()
            video.live(1.2)
            front = spot([0, -1, 0])
            video.caption("Drag the cube to orbit")
            video.glide(front, 0.8)
            before = probe(screen, "viewDirection")
            video.press()
            video.glide((front[0] + 70, front[1] + 20), 2.0)
            video.release()
            assert probe(screen, "viewDirection") != before
            video.pause(0.6)
            video.caption("Home puts the camera back")
            video.glide(_center(marked_element(screen, "camera-home")), 0.8)
            video.click()
            video.live(1.2)
            video.caption("The menu beside it holds the rest of the camera tools")
            video.glide(_center(marked_element(screen, "camera-menu")), 0.7)
            video.click()
            video.pause(2.0)
            video.save()

    def test_workcell_library(self, demo_screen, tmp_path, monkeypatch):
        screen = demo_screen
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
        handle = waldoctl.commander.scene
        shapes = [
            Box(name=name, x=0.065, y=0.065, z=0.09, pose=(*point, 0, 0, 0))
            for name, point in (
                ("Fixture", (0.35, -0.18, 0.045)),
                ("Stock", (0.4, 0.05, 0.045)),
            )
        ]
        run_in_app(lambda: setattr(handle, "shapes", shapes))
        confirmed(screen, lambda: handle.shapes == shapes)
        view = ui_state.urdf_scene
        with program_previewed(EDIT_PROGRAM), program_panel(screen):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.685, -0.4, 0.295, 0.42, -0.05, 0.05],
            )
            wait_for_scene_idle(screen)
            floor = wait(screen).until(lambda _: probe(screen, "floorSpot"))[:2]
            video = DemoVideo(screen, "workcell-library")
            video.jump(floor[0] + 200, floor[1] + 120)
            video.caption("Right-click the floor, Workcell library…")
            video.glide(floor, 1.0)
            video.context_click()
            item = _menu_item(screen, "Workcell library")
            video.pause(0.4)
            video.glide(_center(item), 0.6)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: marked_element(screen, "workcell-library").is_displayed()
                )
            )
            video.caption("It opens beside the scene, which stays live")
            video.pause(1.2)
            video.caption("Name the objects and save them")
            video.glide(_center(marked_element(screen, "workcell-name")), 0.8)
            video.click()
            video.type("bench")
            video.glide(_center(marked_element(screen, "workcell-save")), 0.6)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: "Saved bench"
                    in marked_element(screen, "workcell-feedback").text
                )
            )
            video.pause(1.2)
            video.caption("Delete one of them")
            video.glide(scene_object_pixel(screen, "shape:Stock"), 1.0)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: view._selected_objects)
                    == ["shape:Stock"]
                )
            )
            video.glide(_center(marked_element(screen, "object-actions")), 0.8)
            video.click()
            delete = _settled(screen, lambda: marked_element(screen, "object-delete"))
            video.pause(0.3)
            video.glide(_center(delete), 0.6)
            video.click(
                settle=lambda: confirmed(screen, lambda: len(handle.shapes) == 1)
            )
            video.pause(0.8)
            video.caption("Load bench puts the saved objects back")
            video.glide(_center(marked_element(screen, "workcell-load:bench")), 1.0)
            video.click(
                settle=lambda: confirmed(screen, lambda: handle.shapes == shapes)
            )
            video.pause(1.4)
            video.caption("Or insert them into the program as code")
            video.glide(_center(marked_element(screen, "workcell-insert")), 0.8)
            video.click(
                settle=lambda: wait(screen).until(
                    lambda _: "set_shapes("
                    in run_in_app(lambda: ui_state.active_textarea.value)
                )
            )
            video.pause(2.5)
            video.save()
            marked_element(screen, "workcell-close").click()


@requires_par6
@pytest.mark.browser
@pytest.mark.usefixtures("par6_env")
def test_comparing_plan_and_prediction(screen):
    from par6 import config

    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=60)
    assert ui_state.active_robot.backend_package == "par6"
    start = np.degrees(config.homing_ready_pose_rad()).tolist()
    asyncio.run_coroutine_threadsafe(
        ui_state.control_panel.client.teleport(start), core.loop
    ).result(10)
    waypoints = []
    for changes in (
        (-60, -15, 10),
        (60, -15, 10),
        (60, 15, -10),
        (-60, 15, -10),
        (0, 0, 0),
    ):
        target = np.array(start, dtype=float)
        target[:3] = [v + delta for v, delta in zip(start[:3], changes)]
        waypoints.append(np.round(target, 2).tolist())
    # Full speed and acceleration: the arm lags the plan by tens of mm.
    source = (
        "from par6 import RobotClient\nrbt = RobotClient()\n"
        + "\n".join(f"rbt.move_j({t}, speed=1.0, accel=1.0)" for t in waypoints)
        + "\nrbt.delay(0.5)\n"
    )
    with viewport(screen, 1920, 1080), camera_kept(screen), program_previewed(source):
        scene_js(
            screen,
            "S.setCameraPose(arguments[0])",
            [0.112, -0.192, 0.474, 0, 0.2, 0.25],
        )
        peak = wait(screen, 30).until(
            lambda _: (
                b := screen.selenium.find_element(
                    By.XPATH, "//button[normalize-space()='Peak']"
                )
            ).is_enabled()
            and b
        )
        wait_for_scene_idle(screen)
        video = DemoVideo(screen, "comparison")
        video.jump(1500, 260)
        video.caption("The plan, with the physics prediction drawn where it leaves it")
        video.pause(2.5)
        video.caption("Peak jumps to the largest deviation")
        video.glide(_center(peak), 1.2)
        video.click(
            settle=lambda: wait(screen).until(
                lambda _: scene_js(
                    screen, "return S.byName('comparison:ghost')?.visible"
                )
            )
        )
        video.caption(
            "The ghost is where physics puts the arm; the card says how far off"
        )
        video.pause(3.0)
        record = scene_js(screen, "return S.inspection().records[0]")
        points = []
        for block in record["blocks"]:
            pixels = scene_js(
                screen, "return S.pathPixels(0, arguments[0])", block["command"]
            )
            if pixels and len(pixels) > 1:
                points += [(p[0], p[1]) for p in pixels[:: max(1, len(pixels) // 30)]]
        mid = points[len(points) // 4 :]
        video.caption("Drag along the path to compare them anywhere")
        video.glide(mid[0], 1.0)
        video.press()
        video.along(mid[: len(mid) // 2], 7.0)
        video.release()
        video.pause(1.5)
        video.save()
