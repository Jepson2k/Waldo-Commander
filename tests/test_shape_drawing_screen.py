"""Draw keep-outs on the floor and a tilted primitive face with actual pointers."""

import numpy as np
import pytest
import waldoctl
from contextlib import suppress
from scipy.spatial.transform import Rotation
from selenium.common.exceptions import InvalidArgumentException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    camera_action,
    camera_settled,
    choose,
    clear_selection,
    confirmed,
    create_shape,
    object_mode,
    fill,
    probe,
    program_previewed,
    set_projection,
)
from tests.helpers.scene_surface import (
    pointer_to,
    project_local,
    scene_js,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state


# A free floor point whose surrounding 10 cm square is inside the view with a
# margin, so a footprint drawn around it stays on screen at any camera.
_ROOMY_FLOOR_SPOT = """
const c = getElement(S.root).core, r = c.canvas.getBoundingClientRect();
const v = new (c.camera.position.constructor)();
const at = (x, y) => c.pointer.screen(v.set(x, y, 0).project(c.camera));
const inside = ([sx, sy]) =>
  sx > r.left + 40 && sx < r.right - 40 && sy > r.top + 40 && sy < r.bottom - 40;
for (let rad = 0.2; rad <= 0.45; rad += 0.05) {
  for (let a = 0; a < 360; a += 15) {
    const t = (a * Math.PI) / 180, x = rad * Math.cos(t), y = rad * Math.sin(t);
    const square = [[-0.1, -0.1], [0.1, -0.1], [-0.1, 0.1], [0.1, 0.1]];
    if (!square.every(([dx, dy]) => inside(at(x + dx, y + dy)))) continue;
    const [px, py] = at(x, y).map(Math.round);
    const hit = S.pick(px, py);
    if (!hit.covered && !hit.names.length && !hit.handles.length) return [px, py];
  }
}
return null;
"""


def _draw_floor(screen, kind):
    wait_for_scene_idle(screen)
    point = wait(screen).until(lambda _: scene_js(screen, _ROOMY_FLOOR_SPOT))
    choose(screen, *point[:2], f"Draw {kind.capitalize()}…")
    wait(screen).until(lambda _: probe(screen, "drawing")["active"])


def _footprint(screen, points):
    start, end = probe(screen, "drawingPlane", points)
    action = ActionChains(screen.selenium, duration=40)
    pointer_to(screen, *start, action)
    action.click_and_hold()
    for i in range(1, 11):
        pointer_to(
            screen,
            start[0] + (end[0] - start[0]) * i / 10,
            start[1] + (end[1] - start[1]) * i / 10,
            action,
        )
    action.perform()
    wait(screen).until(lambda _: probe(screen, "drawing")["width"] > 0)


def _hide_and_deselect(screen):
    """Hide the selected object and clear the selection, so neither it nor
    its handles take a press meant for the floor."""
    click_marked(screen, "object-actions")
    click_marked(screen, "object-hide")
    # A closing menu still covers the view and would swallow the click.
    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
    clear_selection(screen)


def _readout_clear_of_box(screen):
    points = project_local(
        screen,
        "draw:box",
        [[x, y, z] for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)],
    )
    rect = screen.selenium.find_element(By.CSS_SELECTOR, ".wc-draw-readout").rect
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    assert (
        rect["x"] + rect["width"] <= x0
        or rect["x"] >= x1
        or rect["y"] + rect["height"] <= y0
        or rect["y"] >= y1
    ), (rect, [x0, y0, x1, y1])


@pytest.fixture(scope="class")
def drawing_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        screen_wait_for_scene_ready(class_screen, timeout_s=40)
        run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
        confirmed(class_screen, lambda: not waldoctl.commander.scene.shapes)
        yield class_screen


@pytest.mark.browser
class TestShapeDrawing:
    def test_press_without_a_footprint_keeps_preview_hidden(self, drawing_screen):
        screen = drawing_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        try:
            for repeated in (False, True):
                _draw_floor(screen, "box")
                start = probe(screen, "drawingPlane", [[0, 0]])[0]
                action = ActionChains(screen.selenium)
                pointer_to(screen, *start, action)
                action.click_and_hold().perform()
                wait_for_scene_idle(screen)
                assert not scene_js(
                    screen, "return S.byName('draw:preview').visible"
                ), repeated
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(lambda _: not probe(screen, "drawing")["active"])
                if not repeated:
                    _draw_floor(screen, "box")
                    _footprint(screen, [[-0.06, -0.05], [0.06, 0.05]])
                    ActionChains(screen.selenium).send_keys(
                        Keys.ESCAPE
                    ).release().perform()
                    wait(screen).until(lambda _: not probe(screen, "drawing")["active"])
        finally:
            ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()

    def test_rejected_drawing_clears_armed_server_state(
        self, drawing_screen, monkeypatch
    ):
        screen = drawing_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        view = ui_state.urdf_scene
        active_client = ui_state.active_client_id
        shapes = list(waldoctl.commander.scene.shapes)
        handle = view.scene.gestures.handle

        def deliver_after_takeover(args):
            if args.get("kind") == "draw" and args.get("t") == "begin":
                ui_state.active_client_id = "another-tab"
                try:
                    handle(args)
                finally:
                    ui_state.active_client_id = active_client
            else:
                handle(args)

        try:
            _draw_floor(screen, "box")
            start = probe(screen, "drawingPlane", [[0, 0]])[0]
            monkeypatch.setattr(view.scene.gestures, "handle", deliver_after_takeover)
            action = ActionChains(screen.selenium)
            pointer_to(screen, *start, action)
            action.click_and_hold().perform()
            wait(screen).until(lambda _: not probe(screen, "drawing")["active"])
            wait(screen, 5).until(lambda _: run_in_app(lambda: view._drawing is None))
            assert not view._hover_suspended
            assert waldoctl.commander.scene.shapes == shapes
            ActionChains(screen.selenium).release().perform()
            queued = []

            def delay_draw(args):
                if args.get("kind") == "draw":
                    queued.append(args)
                else:
                    handle(args)

            monkeypatch.setattr(view.scene.gestures, "handle", delay_draw)
            _draw_floor(screen, "sphere")
            _footprint(screen, [[0, 0], [0.05, 0]])
            ActionChains(screen.selenium).release().perform()
            wait(screen).until(lambda _: any(a.get("t") == "end" for a in queued))

            def deliver_rejected_release():
                ui_state.active_client_id = "another-tab"
                try:
                    for args in queued:
                        handle(args)
                finally:
                    ui_state.active_client_id = active_client

            run_in_app(deliver_rejected_release)
            wait(screen, 5).until(lambda _: run_in_app(lambda: view._drawing is None))
            assert not view._hover_suspended
            assert waldoctl.commander.scene.shapes == shapes
        finally:
            run_in_app(lambda: setattr(ui_state, "active_client_id", active_client))
            ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
            run_in_app(view._cancel_shape_drawing)

    def test_box_footprint_height_and_cancel(self, drawing_screen):
        screen = drawing_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        handle = waldoctl.commander.scene
        live = waldoctl.commander.status.joints.angles.rad.copy()
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "shape-drawing/box", synchronized_frames=True
            )
            try:
                _draw_floor(screen, "box")
                _footprint(screen, [[-0.06, -0.05], [0.06, 0.05]])
                evidence.capture("01-footprint-preview-1920")
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: probe(screen, "drawing")["phase"] == "height"
                )
                assert not handle.shapes
                evidence.capture("02-height-stage-1920")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                wait(screen).until(lambda _: not probe(screen, "drawing")["active"])
                assert not handle.shapes
                _draw_floor(screen, "box")
                _footprint(screen, [[-0.06, -0.05], [0.06, 0.05]])
                ActionChains(screen.selenium).release().perform()
                x, y, dx, dy = wait(screen).until(
                    lambda _: probe(screen, "drawingHeight")
                )
                action = ActionChains(screen.selenium, duration=40)
                pointer_to(screen, x, y, action)
                action.click_and_hold()
                for i in range(1, 11):
                    pointer_to(screen, x + dx * i * 2, y + dy * i * 2, action)
                action.perform()
                evidence.capture("03-pulled-height-1920")
                assert not handle.shapes
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: len(handle.shapes) == 1)
                shape = handle.shapes[0]
                assert shape.x == pytest.approx(0.12, abs=0.005)
                assert shape.y == pytest.approx(0.1, abs=0.005)
                assert shape.z > 0.1
                assert shape.pose[2] == pytest.approx(shape.z / 2)
                evidence.capture("04-created-resize-handles-1920")
                evidence.stop()
                if evidence.enabled:
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("05-created-1366")
                evidence.finish()
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()

    def test_draw_on_tilted_face_and_sphere_radius(self, drawing_screen):
        screen = drawing_screen
        screen_wait_for_scene_ready(screen)
        at_start(screen)
        handle = waldoctl.commander.scene
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "shape-drawing/face", synchronized_frames=True
            )
            try:
                if run_in_app(lambda: bool(ui_state.urdf_scene._selected_objects)):
                    _hide_and_deselect(screen)
                create_shape(screen, "box", "Tilted fixture")
                object_mode(screen, "Move")
                fill(screen, "object-rx", 25)
                confirmed(screen, lambda: handle.shapes[-1].pose[3] > 0.4)
                fixture = handle.shapes[-1]
                # Selected, its own handles would take the press on its face.
                clear_selection(screen)
                x, y = project_local(
                    screen, f"shape:{fixture.name}", [[0, 0, fixture.z / 2]]
                )[0]
                choose(screen, x, y, "Draw Cylinder…")
                # The card follows the drawing the app sends, a frame later.
                wait(screen).until(
                    lambda _: probe(screen, "drawing")["active"]
                    and "Face of Tilted fixture"
                    in screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-draw-readout"
                    ).text
                )
                _footprint(screen, [[0, 0], [0.04, 0]])
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: probe(screen, "drawing")["phase"] == "height"
                )
                height = screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-draw-readout input"
                )
                height.send_keys(Keys.CONTROL, "a", Keys.NULL, "60")
                evidence.capture("01-face-cylinder-height-1920")
                count = len(handle.shapes)
                height.send_keys(Keys.ENTER)
                confirmed(screen, lambda: len(handle.shapes) == count + 1)
                cylinder = handle.shapes[-1]
                normal = Rotation.from_euler("xyz", fixture.pose[3:]).apply([0, 0, 1])
                assert cylinder.length == pytest.approx(0.06)
                assert Rotation.from_euler("xyz", cylinder.pose[3:]).apply(
                    [0, 0, 1]
                ) == pytest.approx(normal)
                distance = np.dot(
                    np.array(cylinder.pose[:3]) - fixture.pose[:3], normal
                )
                assert distance == pytest.approx(fixture.z / 2 + 0.03, abs=0.002)
                evidence.capture("02-face-cylinder-created-1920")
                _hide_and_deselect(screen)
                _draw_floor(screen, "sphere")
                _footprint(screen, [[0, 0], [0.05, 0]])
                evidence.capture("03-sphere-radius-preview-1920")
                count = len(handle.shapes)
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: len(handle.shapes) == count + 1)
                assert handle.shapes[-1].radius == pytest.approx(0.05, abs=0.005)
                assert handle.shapes[-1].pose[2] == pytest.approx(
                    handle.shapes[-1].radius
                )
                evidence.capture("04-sphere-created-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()

    def test_compact_orthographic_and_touch_drawing(self, drawing_screen):
        from tests.test_scene3d_screen import _touch

        screen = drawing_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        handle = waldoctl.commander.scene
        with viewport(screen, 1366, 768), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "shape-drawing/compact", synchronized_frames=True
            )
            try:
                set_projection(screen, "Orthographic")
                camera_action(screen, "top")
                camera_settled(screen)
                _draw_floor(screen, "box")
                _footprint(screen, [[-0.05, -0.04], [0.05, 0.04]])
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: probe(screen, "drawing")["phase"] == "height"
                )
                height = screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-draw-readout input"
                )
                height.send_keys(Keys.CONTROL, "a", Keys.NULL, "75")
                evidence.capture("01-top-view-numeric-height-1366")
                count = len(handle.shapes)
                height.send_keys(Keys.ENTER)
                confirmed(screen, lambda: len(handle.shapes) == count + 1)
                assert handle.shapes[-1].z == pytest.approx(0.075)
                evidence.capture("02-top-view-created-1366")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
            _hide_and_deselect(screen)
            set_projection(screen, "Perspective")
            camera_action(screen, "reset")
            camera_settled(screen)
        with viewport(screen, 1024, 768, mobile=True), program_previewed(EDIT_PROGRAM):
            screen.selenium.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": True, "maxTouchPoints": 1},
            )
            evidence = VisualEvidence(
                screen, "shape-drawing/touch", synchronized_frames=True
            )
            try:
                wait_for_scene_idle(screen)
                point = wait(screen).until(lambda _: probe(screen, "floorSpot"))
                _touch(screen, "touchStart", [(1, *point[:2])])
                wait(screen).until(
                    lambda _: marked_element(
                        screen, "scene-create-keepout"
                    ).is_displayed()
                )
                _touch(screen, "touchEnd", [])
                for touch, marker in (
                    (2, "scene-create-keepout"),
                    (3, "scene-draw-box"),
                ):
                    item = wait(screen).until(
                        lambda _, m=marker: (
                            e := marked_element(screen, m)
                        ).is_displayed()
                        and e.rect
                    )
                    _touch(
                        screen,
                        "touchStart",
                        [
                            (
                                touch,
                                item["x"] + item["width"] / 2,
                                item["y"] + item["height"] / 2,
                            )
                        ],
                    )
                    _touch(screen, "touchEnd", [])
                wait(screen).until(lambda _: probe(screen, "drawing")["active"])
                wait(screen).until(
                    lambda _: not screen.selenium.find_elements(
                        By.CSS_SELECTOR, ".q-menu"
                    )
                )
                start, end = probe(
                    screen, "drawingPlane", [[-0.06, -0.05], [0.06, 0.05]]
                )
                _touch(screen, "touchStart", [(3, *start)])
                for i in range(1, 9):
                    _touch(
                        screen,
                        "touchMove",
                        [
                            (
                                3,
                                start[0] + (end[0] - start[0]) * i / 8,
                                start[1] + (end[1] - start[1]) * i / 8,
                            )
                        ],
                    )
                wait_for_scene_idle(screen)
                _readout_clear_of_box(screen)
                evidence.capture("01-touch-footprint-1024")
                _touch(screen, "touchEnd", [])
                x, y, dx, dy = wait(screen).until(
                    lambda _: probe(screen, "drawingHeight")
                )
                _touch(screen, "touchStart", [(4, x, y)])
                for i in range(1, 9):
                    _touch(screen, "touchMove", [(4, x + dx * i * 3, y + dy * i * 3)])
                wait_for_scene_idle(screen)
                _readout_clear_of_box(screen)
                evidence.capture("02-touch-height-1024")
                count = len(handle.shapes)
                _touch(screen, "touchEnd", [])
                confirmed(screen, lambda: len(handle.shapes) == count + 1)
                assert handle.shapes[-1].z > 0.1
                evidence.capture("03-touch-created-1024")
                evidence.finish()
            finally:
                evidence.stop()
                with suppress(InvalidArgumentException):
                    _touch(screen, "touchCancel", [])
                screen.selenium.execute_cdp_cmd(
                    "Emulation.setTouchEmulationEnabled", {"enabled": False}
                )
