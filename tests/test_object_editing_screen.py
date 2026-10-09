"""Pointer-driven object authoring and contextual controls in the running app."""

import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    choose,
    click_object,
    confirmed,
    fill,
    hold,
    object_mode,
    probe,
    program_previewed,
)
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state


@pytest.fixture(scope="class")
def object_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        yield class_screen


@pytest.mark.browser
class TestObjectEditing:
    def test_select_drag_cancel_undo_and_inspector(self, object_screen):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        handle = waldoctl.commander.scene
        view = ui_state.urdf_scene
        live = waldoctl.commander.status.joints.angles.rad.copy()

        run_in_app(lambda: setattr(handle, "shapes", []))
        confirmed(screen, lambda: not handle.shapes)
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            evidence = VisualEvidence(
                screen, "review-corrections/objects", synchronized_frames=True
            )
            try:
                x, y, *_ = wait(screen).until(lambda _: probe(screen, "floorSpot"))
                choose(screen, x, y, "Box Here...")
                fill(screen, "shape-dialog-name", "Fixture")
                fill(screen, "shape-dialog-dim-x", 180)
                fill(screen, "shape-dialog-dim-y", 130)
                click_marked(screen, "shape-dialog-save")
                confirmed(
                    screen, lambda: any(s.name == "Fixture" for s in handle.shapes)
                )
                click_marked(screen, "object-close")
                px, py = scene_object_pixel(screen, "shape:Fixture")
                choose(screen, px, py, "Edit object")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture"]
                    )
                )
                wait_for_scene_idle(screen)
                card = marked_element(screen, "object-inspector").rect
                distance = max(card["x"] - px, 0, px - card["x"] - card["width"])
                vertical = max(card["y"] - py, 0, py - card["y"] - card["height"])
                assert (distance**2 + vertical**2) ** 0.5 <= 160, (card, px, py)
                handles = scene_js(
                    screen,
                    """
                    const c=getElement(S.root).core, helper=c.gizmos.shape.tc.helper;
                    const r=S.canvas.getBoundingClientRect();
                    return [[0,0,0],[.85,0,0],[0,.85,0],[0,0,.85]].map(v=>{
                        const p=helper.localToWorld(helper.position.clone().fromArray(v)).project(c.camera);
                        return [r.left+(p.x+1)*r.width/2,r.top+(1-p.y)*r.height/2];
                    });
                """,
                )
                assert all(
                    not (
                        card["x"] <= x <= card["x"] + card["width"]
                        and card["y"] <= y <= card["y"] + card["height"]
                    )
                    for x, y in handles
                ), (card, handles)
                evidence.capture("01-right-click-inspector-1920")
                initial = handle.shapes[0]
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "shape:Fixture")
                )
                hold(screen, spot, 35, kind="shape")
                evidence.capture("02-translation-preview-1920")
                assert handle.shapes[0] == initial
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait_for_scene_idle(screen)
                assert handle.shapes[0] == initial
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "shape:Fixture")
                )
                hold(screen, spot, 35, kind="shape")
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: handle.shapes[0].pose != initial.pose)
                evidence.capture("03-move-committed-1920")
                fill(screen, "object-rz", 30)
                confirmed(screen, lambda: handle.shapes[0].pose[5] > 0.5)
                object_mode(screen, "Resize")
                fill(screen, "object-dim-x", 240)
                confirmed(screen, lambda: handle.shapes[0].x == 0.24)
                evidence.capture("04-rotated-resized-1920")
                click_marked(screen, "object-duplicate")
                confirmed(screen, lambda: len(handle.shapes) == 2)
                object_mode(screen, "Move")
                fill(screen, "object-x", 450)
                confirmed(screen, lambda: handle.shapes[-1].pose[0] == 0.45)
                evidence.capture("05-duplicate-1920")
                click_marked(screen, "object-actions")
                click_marked(screen, "object-hide")
                click_marked(screen, "object-close")
                evidence.capture("06-hidden-recovery-1920")
                click_marked(screen, "object-show-hidden")
                click_object(screen, "shape:Fixture")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture"]
                    )
                )
                evidence.stop()
                with viewport(screen, 1366, 768):
                    wait_for_scene_idle(screen)
                    evidence.capture("07-inspector-1366")
                evidence.finish()
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
