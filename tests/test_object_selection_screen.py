"""Shared translation and rotation handles on a real pointer selection."""

import math

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from waldoctl.shapes import Box, Cylinder

from tests.helpers.browser_helpers import (
    click_marked,
    marked_element,
    run_in_app,
    tap,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    camera_action,
    camera_settled,
    clear_selection,
    click_object,
    click_spot,
    confirmed,
    field_number,
    fill,
    gizmo_field,
    hold,
    place_shape,
    probe,
    program_previewed,
    settled_pixel,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode


def _focused(screen):
    """Focus the selection from the scene panel, as a user whose panel
    covers it would, and wait until the camera has framed it."""
    click_marked(screen, "object-focus")
    camera_settled(screen)


def _assert_handle_clearance(screen):
    overlap = screen.selenium.execute_script("""
        const card=document.querySelector('.scene-panel').getBoundingClientRect();
        return [...document.querySelectorAll('.wc-axis-label')]
          .filter(e=>e.getClientRects().length)
          .filter(e=>{const b=e.getBoundingClientRect(); return b.left<card.right && b.right>card.left && b.top<card.bottom && b.bottom>card.top;})
          .map(e=>e.textContent);
    """)
    assert not overlap, f"The scene panel covers movement labels: {overlap}"


def _assert_coordinate_readability(screen):
    clipped = screen.selenium.execute_script("""
        const ctx=document.createElement('canvas').getContext('2d');
        return [...document.querySelectorAll('.scene-properties input[type=number]')]
          .filter(e=>e.getClientRects().length)
          .map(e=>{
            const style=getComputedStyle(e);
            ctx.font=`${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
            return {value:e.value,needed:ctx.measureText(e.value).width,
              available:e.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)};
          }).filter(e=>e.needed>e.available+1);
    """)
    assert not clipped, f"The properties clip coordinate values: {clipped}"


def _field_value(screen, marker) -> float | None:
    """The number the field marked ``marker`` shows."""
    try:
        element = marked_element(screen, marker)
        if element.tag_name != "input":
            element = element.find_element(By.CSS_SELECTOR, "input")
        text = element.get_attribute("value")
    except (NoSuchElementException, StaleElementReferenceException):
        return None
    return float(text) if text else None


@pytest.fixture(scope="class")
def selection_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        screen_wait_for_scene_ready(class_screen, timeout_s=40)
        run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
        confirmed(class_screen)
        yield class_screen


@pytest.mark.browser
class TestSelectionTransform:
    def test_move_rotate_selection_and_history(self, selection_screen):
        screen = selection_screen
        at_start(screen)
        handle, view = waldoctl.commander.scene, ui_state.urdf_scene
        live = waldoctl.commander.status.joints.angles.rad.copy()
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM):
            point = wait(screen).until(lambda _: probe(screen, "floorSpot"))
            place_shape(screen, "box", *point[:2], "Fixture A")
            confirmed(screen, lambda: len(handle.shapes) == 1)
            fill(screen, "object-rz", 20)
            confirmed(screen, lambda: handle.shapes[0].pose[5] > 0.3)
            click_marked(screen, "object-duplicate")
            confirmed(screen, lambda: len(handle.shapes) == 2)
            fill(screen, "object-name", "Fixture B")
            confirmed(screen, lambda: handle.shapes[1].name == "Fixture B")
            fill(screen, "object-y", -180)
            confirmed(screen, lambda: handle.shapes[1].pose[1] == -0.18)
            clear_selection(screen)
            evidence = VisualEvidence(
                screen, "object-selection", synchronized_frames=True
            )
            try:
                if evidence.enabled:
                    click_marked(screen, "scene-multi-select")
                    wait(screen).until(lambda _: run_in_app(lambda: view._multi_select))
                    for count, name in enumerate(("Fixture A", "Fixture B"), 1):
                        tap(screen, *settled_pixel(screen, f"shape:{name}"))
                        wait(screen).until(
                            lambda _, count=count: run_in_app(
                                lambda: len(view._selected_objects)
                            )
                            == count
                        )
                else:
                    click_object(screen, "shape:Fixture A")
                    click_object(screen, "shape:Fixture B", shift=True)
                wait(screen).until(
                    lambda _: run_in_app(lambda: len(view._selected_objects) == 2)
                )
                before = tuple(handle.shapes)
                history = len(view.object_history.undo_stack)
                _focused(screen)
                evidence.capture("01-selected-pair-1920")
                if evidence.enabled:
                    evidence.capture("08-select-several-header-1920")
                    click_marked(screen, "scene-multi-select")
                    wait(screen).until(
                        lambda _: not run_in_app(lambda: view._multi_select)
                    )
                _assert_handle_clearance(screen)
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "selection:center")
                )
                hold(screen, spot, 40, kind="shape")
                evidence.capture("02-pair-translation-preview-1920")
                preview = scene_js(
                    screen,
                    "return ['Fixture A','Fixture B'].map(n=>S.byName('shape:'+n).position.toArray())",
                )
                deltas = np.asarray(preview) - np.asarray([s.pose[:3] for s in before])
                assert np.linalg.norm(deltas[0]) > 0.005
                # The members move together; positions carry float noise.
                assert deltas[0] == pytest.approx(deltas[1], abs=1e-6)
                assert tuple(handle.shapes) == before
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait_for_scene_idle(screen)
                restored = scene_js(
                    screen,
                    "return ['Fixture A','Fixture B'].map(n=>S.byName('shape:'+n).position.toArray())",
                )
                np.testing.assert_allclose(
                    restored, [s.pose[:3] for s in before], rtol=0, atol=1e-6
                )
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "selection:center")
                )
                hold(screen, spot, 40, kind="shape")
                ActionChains(screen.selenium).release().perform()
                confirmed(
                    screen, lambda: len(view.object_history.undo_stack) == history + 1
                )
                moved = tuple(handle.shapes)
                spot = wait(screen).until(
                    lambda _: probe(screen, "ringSpot", "Z", "selection:center")
                )
                # A click on the arc opens a field for an exact turn about Z.
                click_spot(screen, spot)
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                assert gizmo_field(screen)["label"].startswith("Rz "), gizmo_field(
                    screen
                )
                ActionChains(screen.selenium).send_keys("30", Keys.ENTER).perform()
                confirmed(
                    screen, lambda: len(view.object_history.undo_stack) == history + 2
                )
                center = np.mean([s.pose[:3] for s in moved], axis=0)
                rotated = tuple(handle.shapes)
                evidence.capture("03-pair-rotated-1920")
                spot = wait(screen).until(
                    lambda _: probe(screen, "ringSpot", "Z", "selection:center")
                )
                hold(screen, spot, 32, kind="shape")
                evidence.capture("04-pair-rotation-preview-1920")
                assert tuple(handle.shapes) == rotated
                preview = np.asarray(
                    scene_js(
                        screen,
                        "return ['Fixture A','Fixture B'].map(n=>S.byName('shape:'+n).position.toArray())",
                    )
                )
                assert preview.mean(axis=0) == pytest.approx(center)
                assert np.linalg.norm(preview[1] - preview[0]) == pytest.approx(
                    np.linalg.norm(
                        np.array(rotated[1].pose[:3]) - np.array(rotated[0].pose[:3])
                    )
                )
                assert not np.allclose(preview, [s.pose[:3] for s in rotated])
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait_for_scene_idle(screen)
                evidence.stop()
                with viewport(screen, 1366, 768):
                    _focused(screen)
                    evidence.capture("05-selection-controls-1366")
                    _assert_handle_clearance(screen)
                clear_selection(screen)
                click_object(screen, "shape:Fixture A")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture A"]
                    )
                )
                before_turn = handle.shapes[0]
                spot = wait(screen).until(
                    lambda _: probe(screen, "ringSpot", "X", "shape:Fixture A")
                )
                hold(screen, spot, 25, kind="shape")
                field = gizmo_field(screen)
                assert field["label"].startswith("Rx "), field
                angle = field_number(field["value"])
                assert abs(angle) > 1
                ActionChains(screen.selenium).release().perform()
                confirmed(screen, lambda: handle.shapes[0] != before_turn)
                expected = Rotation.from_euler(
                    "x", angle, degrees=True
                ) * Rotation.from_euler("xyz", before_turn.pose[3:])
                assert Rotation.from_euler(
                    "xyz", handle.shapes[0].pose[3:]
                ).as_matrix() == pytest.approx(expected.as_matrix())
                _focused(screen)
                evidence.capture("06-single-object-world-rotation-1920")
                _assert_handle_clearance(screen)
                # The rotation fields follow a turn made with the handles.
                turned = math.degrees(handle.shapes[0].pose[3])
                wait(screen).until(
                    lambda _: _field_value(screen, "object-rx")
                    == pytest.approx(turned, abs=2e-3)
                )
                _assert_coordinate_readability(screen)
                with viewport(screen, 1366, 768):
                    _focused(screen)
                    evidence.capture("06b-single-object-world-rotation-1366")
                    _assert_handle_clearance(screen)
                    _assert_coordinate_readability(screen)
                click_object(screen, "shape:Fixture B", shift=True)
                wait(screen).until(
                    lambda _: run_in_app(lambda: len(view._selected_objects) == 2)
                )
                run_in_app(lambda: view.set_appearance_mode(RobotAppearanceMode.LIVE))
                evidence.capture("07-grey-robot-selection-1920")
                run_in_app(
                    lambda: view.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
                )
                evidence.finish()
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
            finally:
                if evidence.enabled and run_in_app(lambda: view._multi_select):
                    click_marked(screen, "scene-multi-select")
                run_in_app(
                    lambda: view.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
                )
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()

    def test_late_rejection_restores_every_selected_primitive(
        self, selection_screen, monkeypatch
    ):
        screen = selection_screen
        handle, view = waldoctl.commander.scene, ui_state.urdf_scene
        originals = [
            Box(
                name="Pending box",
                x=0.1,
                y=0.1,
                z=0.1,
                pose=(0.25, 0.05, 0.05, 0.2, 0.3, 0.4),
            ),
            Cylinder(
                name="Pending cylinder",
                radius=0.05,
                length=0.1,
                pose=(0.25, -0.18, 0.05, 0.2, 0.3, 0.4),
            ),
        ]
        run_in_app(lambda: setattr(handle, "shapes", originals))
        confirmed(screen, lambda: handle.shapes == originals)
        with viewport(screen, 1920, 1080):
            # From the home view, not wherever the last test left the camera.
            camera_action(screen, "reset")
            camera_settled(screen)
            click_object(screen, "shape:Pending box")
            click_object(screen, "shape:Pending cylinder", shift=True)
            wait(screen).until(
                lambda _: run_in_app(lambda: len(view._selected_objects) == 2)
            )
            names = ["shape:" + s.name for s in originals]
            read_geometry = "return arguments[0].map(n=>{const o=S.byName(n);return [...o.position.toArray(),...o.quaternion.toArray()]})"
            geometry = np.asarray(scene_js(screen, read_geometry, names))
            queued = []
            dispatch = view.scene.gestures.handle

            def delayed(args):
                if args.get("kind") == "shape":
                    queued.append(args)
                else:
                    dispatch(args)

            with monkeypatch.context() as patch:
                patch.setattr(view.scene.gestures, "handle", delayed)
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "selection:center")
                )
                hold(screen, spot, 40, kind="shape")
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(lambda _: any(a.get("t") == "end" for a in queued))
                assert not np.allclose(scene_js(screen, read_geometry, names), geometry)

                def deliver_stale_events():
                    view.scene.gestures.bump()
                    for args in queued:
                        dispatch(args)

                run_in_app(deliver_stale_events)
                wait(screen).until(
                    lambda _: np.allclose(
                        scene_js(screen, read_geometry, names), geometry
                    )
                )
                assert handle.shapes == originals
