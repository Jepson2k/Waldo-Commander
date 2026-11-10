"""Object authoring in the running app: pointer edits and contextual
controls, shared selection handles, isolation, surface teaching and the
workcell library."""

import math
from contextlib import nullcontext
from dataclasses import replace

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation
from waldoctl.shapes import Box, Cylinder
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    focus_editor,
    marked_element,
    run_in_app,
    tap,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.review_scenes import FIXTURE_VIEW, bracket_stl, clearance_view
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    camera_action,
    camera_kept,
    camera_settled,
    choose,
    clearance_button,
    clearance_endpoint_pixels,
    clearance_line,
    clear_selection,
    click_object,
    click_spot,
    confirmed,
    create_shape,
    field_number,
    fill,
    frame_mm,
    gizmo_field,
    hold,
    menu_choice,
    object_mode,
    park_scene_pointer,
    place_shape,
    probe,
    program_previewed,
    settled_element,
    settled_pixel,
)
from tests.helpers.scene_surface import (
    pointer_to,
    project_local,
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import (
    VisualEvidence,
    enabled,
    preserve_capture_buffers,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.helpers.scene_objects import fixture_stl, teaching_fixture
from waldo_commander.common.theme import SceneColors
from waldo_commander.services import world_files
from waldo_commander.services.control_lease import control_lease
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.state import ui_state


def in_scene_panel(screen, marker) -> bool:
    """Whether the element marked ``marker`` shows in the scene panel."""
    try:
        element = marked_element(screen, marker)
        return element.is_displayed() and screen.selenium.execute_script(
            "return !!arguments[0].closest('.scene-panel')", element
        )
    except (NoSuchElementException, StaleElementReferenceException):
        return False


def _leave_scene_empty(screen) -> None:
    """Hand the next test in the class what a fresh page gives it: an empty
    world, which also ends an isolation, nothing selected, and the scene and
    program panels shut."""
    view, handle = ui_state.urdf_scene, waldoctl.commander.scene
    run_in_app(lambda: view._select_objects([]))
    run_in_app(lambda: setattr(handle, "shapes", []))
    confirmed(screen, lambda: not handle.shapes)
    if run_in_app(lambda: ui_state.program_panel_visible):
        click_marked(screen, "program-panel-close")
    if run_in_app(lambda: ui_state.scene_panel.is_open):
        click_marked(screen, "scene-close")
    wait(screen).until(
        lambda _: no_visible(screen, ".scene-panel")
        and not run_in_app(
            lambda: ui_state.program_panel_visible or view._isolated_view
        )
    )


def _teach(screen, fixture):
    wait_for_scene_idle(screen)
    x, y = project_local(screen, f"shape:{fixture.name}", [[0.025, 0, fixture.z / 2]])[
        0
    ]
    choose(screen, x, y, "Teach on this surface…")
    wait(screen).until(
        lambda _: run_in_app(lambda: ui_state.urdf_scene._teaching is not None)
    )


def _visibility(screen, names):
    """How each object shows: in full, faded by isolation, or hidden."""
    return scene_js(
        screen,
        "return arguments[0].map(n=>{const o=S.byName('shape:'+n);let faded=false;"
        "o?.traverse(c=>{faded||=!!c.userData.faded});"
        "return !o?.visible?'hidden':faded?'faded':'shown'})",
        names,
    )


def _isolate_shows_on(view) -> bool:
    """Whether the More menu's isolate item offers to show all, as it does
    while the view is isolated."""

    def offered() -> bool:
        item = view._inspector_controls.isolate_text
        return item is not None and item.text == "Show all"

    return run_in_app(offered)


def _press_isolate(screen) -> None:
    """Choose the isolate item from the More menu with the keyboard."""
    click_marked(screen, "object-actions")

    def pressed(_) -> bool:
        try:
            marked_element(screen, "object-isolate").send_keys(Keys.SPACE)
        except (
            ElementNotInteractableException,
            NoSuchElementException,
            StaleElementReferenceException,
        ):
            return False
        return True

    wait(screen).until(pressed)
    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))


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


def _library(screen):
    point = wait(screen).until(lambda _: probe(screen, "floorSpot"))
    choose(screen, *point[:2], "Workcell library…")
    wait(screen).until(lambda _: in_scene_panel(screen, "workcell-library"))


@pytest.fixture(scope="class")
def object_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        yield class_screen


@pytest.mark.browser
class TestObjectEditing:
    def test_face_standoff_twist_validity_sequence_undo(self, object_screen):
        screen = object_screen
        with viewport(screen, 1920, 1080):
            screen_wait_for_scene_ready(screen, timeout_s=40)
            at_start(screen)
            view = ui_state.urdf_scene
            fixture, _, _ = run_in_app(lambda: teaching_fixture(view))
            run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", [fixture]))
            confirmed(screen, lambda: waldoctl.commander.scene.shapes == [fixture])
            with program_previewed(EDIT_PROGRAM):
                evidence = VisualEvidence(
                    screen, "surface-teaching", synchronized_frames=True
                )
                try:
                    _teach(screen, fixture)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._teaching.valid)
                    )
                    evidence.capture("01-picked-surface-1920")
                    fill(screen, "surface-standoff", 35)
                    fill(screen, "surface-twist", 10)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._teaching.valid)
                    )
                    evidence.capture("02-standoff-twist-1920")
                    fill(screen, "surface-standoff", 600)
                    wait(screen).until(
                        lambda _: not run_in_app(lambda: view._teaching.valid)
                    )
                    assert not marked_element(screen, "surface-insert").is_enabled()
                    evidence.capture("03-unreachable-request-1920")
                    fill(screen, "surface-standoff", 30)
                    fill(screen, "surface-twist", 0)
                    click_marked(screen, "surface-sequence")
                    fill(screen, "surface-clearance", 5)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._teaching.valid)
                    )
                    evidence.capture("04-approach-retract-1920")
                    click_marked(screen, "surface-insert")
                    wait(screen).until(
                        lambda _: run_in_app(
                            lambda: view._teaching is None
                            and "surface_task" in ui_state.active_textarea.value
                        )
                    )
                    source = run_in_app(lambda: ui_state.active_textarea.value)
                    click_tab(screen, "program")
                    wait_for_scene_idle(screen)
                    evidence.capture("05-inserted-operation-1920")
                    editor = focus_editor(screen)
                    editor.send_keys(Keys.CONTROL, "z", Keys.NULL)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        == EDIT_PROGRAM
                    )
                    editor.send_keys(Keys.CONTROL, Keys.SHIFT, "z", Keys.NULL)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        == source
                    )
                    evidence.stop()
                    close_panel(screen, "program-panel")
                    with viewport(screen, 1366, 768):
                        _teach(screen, fixture)
                        wait(screen).until(
                            lambda _: run_in_app(lambda: view._teaching.valid)
                        )
                        evidence.capture("06-compact-teaching-1366")
                        click_tab(screen, "program")
                        wait_for_scene_idle(screen)
                        card = marked_element(screen, "surface-teaching")
                        panel = screen.selenium.find_element(
                            By.CSS_SELECTOR, ".program-panel"
                        )
                        assert card.rect["x"] >= panel.rect["x"] + panel.rect["width"]
                        evidence.capture("07-open-program-teaching-1366")
                        marked_element(screen, "surface-standoff").click()
                        ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                        wait(screen).until(
                            lambda _: run_in_app(lambda: view._teaching is None)
                        )
                    evidence.finish()
                finally:
                    evidence.stop()
                    run_in_app(view.cancel_target_edit)
                    _leave_scene_empty(screen)

    def test_isolate_edit_restore_hidden_and_external_replacement(self, object_screen):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        with viewport(screen, 1920, 1080), camera_kept(screen):
            at_start(screen)
            shapes = [
                Box(name=name, x=0.065, y=0.065, z=0.09, pose=(*point, 0, 0, 0))
                for name, point in zip(
                    ["Fixture A", "Fixture B", "Hidden fixture", "Surrounding fixture"],
                    [
                        (0.35, -0.18, 0.045),
                        (0.4, 0.05, 0.045),
                        (0.55, -0.1, 0.045),
                        (0.55, 0.15, 0.045),
                    ],
                )
            ]
            run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", shapes))
            confirmed(screen, lambda: waldoctl.commander.scene.shapes == shapes)
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.685, -0.4, 0.295, 0.42, -0.05, 0.05],
            )
            with program_previewed(EDIT_PROGRAM):
                view = ui_state.urdf_scene
                appearance = view._appearance_mode
                click_object(screen, "shape:Hidden fixture")
                click_marked(screen, "object-actions")
                click_marked(screen, "object-hide")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: "shape:Hidden fixture" in view._hidden_objects
                    )
                )
                wait_for_scene_idle(screen)
                # Nothing of a hidden selection is in view to isolate.
                wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                click_marked(screen, "object-actions")
                item = wait(screen).until(
                    lambda _: marked_element(screen, "object-isolate")
                )
                assert item.get_attribute("aria-disabled") == "true"
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                wait_for_scene_idle(screen)
                assert not run_in_app(lambda: view._isolated_view)
                assert "faded" not in _visibility(screen, [s.name for s in shapes])
                clear_selection(screen)
                click_object(screen, "shape:Fixture A")
                click_object(screen, "shape:Fixture B", shift=True)
                wait(screen).until(
                    lambda _: run_in_app(lambda: len(view._selected_objects)) == 2
                )
                before = tuple(waldoctl.commander.scene.shapes)
                source = ui_state.active_textarea.value
                history = len(view.object_history.undo_stack)
                lease = control_lease.generation
                live = waldoctl.commander.status.joints.angles.rad.copy()
                names = [shape.name for shape in shapes]
                evidence = VisualEvidence(
                    screen, "object-isolation", synchronized_frames=True
                )
                try:
                    evidence.capture("01-selected-workcell-1920")
                    menu_choice(screen, "object-actions", "object-isolate")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._isolated_view)
                    )
                    assert _visibility(screen, names) == [
                        "shown",
                        "shown",
                        "hidden",
                        "faded",
                    ]
                    evidence.capture("02-isolated-pair-1920")
                    assert tuple(waldoctl.commander.scene.shapes) == before
                    assert ui_state.active_textarea.value == source
                    assert len(view.object_history.undo_stack) == history
                    assert control_lease.generation == lease
                    np.testing.assert_allclose(
                        waldoctl.commander.status.joints.angles.rad, live, atol=1e-6
                    )
                    wait(screen).until(lambda _: _isolate_shows_on(view))
                    # A faded object is context, not something to pick: a
                    # click on one is a click on nothing.
                    x, y = probe(screen, "pixelOf", "shape:Surrounding fixture")
                    actions = ActionChains(screen.selenium)
                    pointer_to(screen, x, y, actions)
                    actions.click().perform()
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._selected_objects) == []
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("03-clear-selection-1920")
                    # Isolate again shows every object.
                    click_object(screen, "shape:Fixture A")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._selected_objects)
                        == ["shape:Fixture A"]
                    )
                    assert run_in_app(lambda: view._isolated_view)
                    _press_isolate(screen)
                    wait(screen).until(
                        lambda _: not run_in_app(lambda: view._isolated_view)
                    )
                    assert _visibility(screen, names) == [
                        "shown",
                        "shown",
                        "hidden",
                        "shown",
                    ]
                    menu_choice(screen, "object-actions", "object-isolate")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._isolated_view)
                    )
                    fill(screen, "object-x", 370)
                    confirmed(
                        screen,
                        lambda: waldoctl.commander.scene.shapes[0].pose[0] == 0.37,
                    )
                    assert _visibility(screen, names) == [
                        "shown",
                        "faded",
                        "hidden",
                        "faded",
                    ]
                    fill(screen, "object-name", "Renamed fixture")
                    confirmed(
                        screen,
                        lambda: waldoctl.commander.scene.shapes[0].name
                        == "Renamed fixture",
                    )
                    names[0] = "Renamed fixture"
                    assert run_in_app(lambda: view._isolated_view)
                    assert _visibility(screen, names) == [
                        "shown",
                        "faded",
                        "hidden",
                        "faded",
                    ]
                    click_marked(screen, "object-undo")
                    confirmed(
                        screen,
                        lambda: waldoctl.commander.scene.shapes[0].name == "Fixture A",
                    )
                    assert run_in_app(lambda: view._isolated_view)
                    click_marked(screen, "object-redo")
                    confirmed(
                        screen,
                        lambda: waldoctl.commander.scene.shapes[0].name
                        == "Renamed fixture",
                    )
                    assert _visibility(screen, names) == [
                        "shown",
                        "faded",
                        "hidden",
                        "faded",
                    ]
                    menu_choice(screen, "object-actions", "object-duplicate")
                    confirmed(screen, lambda: len(waldoctl.commander.scene.shapes) == 5)
                    assert _visibility(
                        screen,
                        ["Renamed fixture", "Renamed fixture-copy-1", "Fixture B"],
                    ) == ["shown", "shown", "faded"]
                    run_in_app(
                        lambda: view.set_appearance_mode(RobotAppearanceMode.LIVE)
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("04-duplicate-isolated-1920")
                    evidence.stop()
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("05-compact-isolated-1366")
                        menu_choice(screen, "object-actions", "object-isolate")
                        wait(screen).until(
                            lambda _: not run_in_app(lambda: view._isolated_view)
                        )
                        assert _visibility(screen, names) == [
                            "shown",
                            "shown",
                            "hidden",
                            "shown",
                        ]
                        evidence.capture("06-show-all-1366")
                    click_object(screen, "shape:Fixture B")
                    menu_choice(screen, "object-actions", "object-isolate")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._isolated_view)
                    )
                    replacement = replace(shapes[3], pose=(0.5, 0.15, 0.045, 0, 0, 0))
                    run_in_app(
                        lambda: setattr(
                            waldoctl.commander.scene, "shapes", [replacement]
                        )
                    )
                    confirmed(
                        screen, lambda: waldoctl.commander.scene.shapes == [replacement]
                    )
                    wait(screen).until(
                        lambda _: not run_in_app(lambda: view._isolated_view)
                    )
                    assert _visibility(screen, [replacement.name]) == ["shown"]
                    evidence.capture("07-external-replacement-restores-1920")
                    evidence.finish()
                finally:
                    evidence.stop()
                    run_in_app(lambda: view.set_appearance_mode(appearance))
                    ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                    _leave_scene_empty(screen)

    def test_move_rotate_selection_and_history(self, object_screen):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
        confirmed(screen)
        at_start(screen)
        handle, view = waldoctl.commander.scene, ui_state.urdf_scene
        live = waldoctl.commander.status.joints.angles.rad.copy()
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(EDIT_PROGRAM),
        ):
            point = wait(screen).until(lambda _: probe(screen, "floorSpot"))
            place_shape(screen, "box", *point[:2], "Fixture A")
            confirmed(screen, lambda: len(handle.shapes) == 1)
            fill(screen, "object-rz", 20)
            confirmed(screen, lambda: handle.shapes[0].pose[5] > 0.3)
            menu_choice(screen, "object-actions", "object-duplicate")
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
                _leave_scene_empty(screen)

    def test_late_rejection_restores_every_selected_primitive(
        self, object_screen, monkeypatch
    ):
        screen = object_screen
        screen_wait_for_scene_ready(screen)
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
            try:
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
                    wait(screen).until(
                        lambda _: any(a.get("t") == "end" for a in queued)
                    )
                    assert not np.allclose(
                        scene_js(screen, read_geometry, names), geometry
                    )

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
            finally:
                ActionChains(screen.selenium).release().perform()
                _leave_scene_empty(screen)

    def test_workcell_save_reload_and_source_undo(
        self, object_screen, tmp_path, monkeypatch
    ):
        screen = object_screen
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
        with viewport(screen, 1920, 1080):
            screen_wait_for_scene_ready(screen, timeout_s=40)
            at_start(screen)
            run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
            confirmed(screen, lambda: not waldoctl.commander.scene.shapes)
            handle = waldoctl.commander.scene
            with program_previewed(EDIT_PROGRAM):
                create_shape(screen, "box", "Fixture")
                fill(screen, "object-dim-x", 180)
                confirmed(screen, lambda: handle.shapes[-1].x == 0.18)
                create_shape(screen, "cylinder", "Stock")
                object_mode(screen, "Move")
                fill(screen, "object-x", 400)
                confirmed(screen, lambda: handle.shapes[-1].pose[0] == 0.4)
                fill(screen, "object-y", -0.18 * 1000)
                confirmed(screen, lambda: handle.shapes[-1].pose[1] == -0.18)
                original = list(handle.shapes)
                evidence = VisualEvidence(
                    screen, "workcell-library", synchronized_frames=True
                )
                try:
                    _library(screen)
                    # Enter saves, as the Save button beside the name does.
                    fill(screen, "workcell-name", "bench-cell")
                    wait(screen).until(
                        lambda _: "Saved bench-cell"
                        in marked_element(screen, "workcell-feedback").text
                    )
                    wait(screen).until(
                        lambda _: "Fixture, Stock"
                        in marked_element(screen, "workcell-preview:bench-cell").text
                    )
                    evidence.capture("01-saved-workcell-1920")
                    # The library is no dialog: the scene beside it stays live.
                    click_object(screen, "shape:Fixture")
                    fill(screen, "object-x", 550)
                    confirmed(screen, lambda: handle.shapes[0].pose[0] == 0.55)
                    evidence.capture("02-edited-beside-library-1920")
                    click_marked(screen, "workcell-load:bench-cell")
                    confirmed(screen, lambda: handle.shapes == original)
                    wait(screen).until(
                        lambda _: "Loaded"
                        in marked_element(screen, "workcell-note:bench-cell").text
                    )
                    evidence.capture("03-reloaded-world-1920")
                    click_tab(screen, "program")
                    editor = focus_editor(screen)
                    editor.send_keys(
                        Keys.CONTROL, Keys.HOME, Keys.NULL, Keys.ARROW_DOWN
                    )
                    wait(screen).until(
                        lambda _: run_in_app(
                            lambda: waldoctl.commander.programs.active.dry_run.playback.active_cursor_line
                        )
                        == 2
                    )
                    click_marked(screen, "workcell-insert")
                    wait(screen).until(
                        lambda _: "set_shapes("
                        in run_in_app(lambda: ui_state.active_textarea.value)
                    )
                    updated = run_in_app(lambda: ui_state.active_textarea.value)
                    assert updated.index("set_shapes(") < updated.index("rbt.move_j(")
                    assert (
                        updated.splitlines()[1] == "from waldoctl import Box, Cylinder"
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("04-inserted-source-1920")
                    editor = focus_editor(screen)
                    editor.send_keys(Keys.CONTROL, "z")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        == EDIT_PROGRAM
                    )
                    editor.send_keys(Keys.CONTROL, Keys.SHIFT, "z", Keys.NULL)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        == updated
                    )
                    evidence.stop()
                    close_panel(screen, "program-panel")
                    if evidence.enabled:
                        with viewport(screen, 1366, 768):
                            evidence.capture("05-library-1366")
                            click_marked(screen, "workcell-close")
                            click_object(screen, "shape:Fixture")
                            evidence.capture("06-reloaded-inspector-1366")
                    else:
                        click_marked(screen, "workcell-close")
                    evidence.finish()
                finally:
                    evidence.stop()
                    if in_scene_panel(screen, "workcell-library"):
                        click_marked(screen, "workcell-close")
                    _leave_scene_empty(screen)

    def test_clearance_lines_follow_keep_out_moves(self, object_screen):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        view, handle, robot = (
            ui_state.urdf_scene,
            waldoctl.commander.scene,
            ui_state.active_robot,
        )
        original = list(handle.shapes)
        q = run_in_app(lambda: view._shown_q.copy())
        tcp = frame_mm(screen) / 1000
        box = Box(
            name="Clearance box",
            x=0.06,
            y=0.06,
            z=0.06,
            pose=(tcp[0] + 0.18, tcp[1], tcp[2] + 0.04, 0, 0, 0),
        )
        obstacle = f"shape:{box.name}"

        def expected():
            return next(c for c in robot.clearances(q) if c.obstacle == obstacle)

        def matches(clearance):
            line = clearance_line(screen, obstacle)
            return (
                line
                if line and line["label"] == f"{round(clearance.distance * 1000)} mm"
                else False
            )

        def touch_toggle():
            button = clearance_button(screen)
            rect = button.rect
            tap(screen, rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)

        with (
            viewport(screen, 1920, 1080) if enabled() else nullcontext(),
            camera_kept(screen),
        ):
            evidence = VisualEvidence(screen, "clearance")
            try:
                run_in_app(lambda: setattr(handle, "shapes", [box]))
                confirmed(screen, lambda: handle.shapes == [box])
                if run_in_app(lambda: ui_state.scene_panel.is_open):
                    click_marked(screen, "scene-close")
                wait(screen).until(lambda _: no_visible(screen, ".scene-panel"))
                assert not scene_js(screen, "return S.clearanceLines()")
                initial = expected()
                assert 0.02 < initial.distance < 0.3
                touch_toggle()
                line = wait(screen).until(lambda _: matches(initial))
                assert no_visible(screen, ".scene-panel")
                assert clearance_button(screen).get_attribute("aria-pressed") == "true"
                assert np.asarray(line["endpoints"]) == pytest.approx(
                    np.asarray([initial.part_point, initial.obstacle_point]), abs=1e-6
                )
                line_id, resets = line["id"], scene_js(screen, "return S.resets")

                click_marked(screen, "tab-scene")
                click_marked(screen, "scene-row-Clearance-box")
                object_mode(screen, "Move")
                delta = np.subtract(initial.part_point, initial.obstacle_point)
                delta *= 0.03 / np.linalg.norm(delta)
                for axis, change in enumerate(delta):
                    if abs(change) < 1e-6:
                        continue
                    mm = round((box.pose[axis] + change) * 1000, 3)
                    fill(screen, f"object-{'xyz'[axis]}", mm)
                    confirmed(
                        screen,
                        lambda a=axis, value=mm: abs(
                            handle.shapes[0].pose[a] * 1000 - value
                        )
                        < 0.001,
                    )
                closer = expected()
                assert round(closer.distance * 1000) < round(initial.distance * 1000)
                assert 0.06 <= closer.distance <= 0.12
                assert closer.part_point[2] > 0.1
                line = wait(screen).until(lambda _: matches(closer))
                assert line["id"] == line_id
                assert scene_js(screen, "return S.resets") == resets
                clear_selection(screen)
                click_marked(screen, "scene-eye-Clearance-box")
                wait(screen).until(lambda _: clearance_line(screen, obstacle) is None)
                click_marked(screen, "scene-eye-Clearance-box")
                wait(screen).until(lambda _: matches(closer))
                screen.selenium.execute_script(
                    "PanelResize.resizePanel('scene', {width:1})"
                )
                close = settled_element(
                    screen, lambda: marked_element(screen, "scene-close")
                )
                assert screen.selenium.execute_script(
                    """
                    const close = arguments[0], panel = close.closest('.scene-panel');
                    const rect = panel.getBoundingClientRect();
                    const heading = panel.querySelector('.panel-heading');
                    const tools = panel.querySelector('.scene-panel-tools');
                    const fits = element => {
                        const r = element.getBoundingClientRect();
                        return r.width > 0 && r.left >= rect.left && r.right <= rect.right;
                    };
                    const r = close.getBoundingClientRect();
                    return [heading, tools, close].every(fits)
                        && tools.scrollWidth <= tools.clientWidth + 1
                        && close.contains(document.elementFromPoint(r.x+r.width/2, r.y+r.height/2));
                    """,
                    close,
                )
                click_marked(screen, "scene-close")
                wait(screen).until(lambda _: no_visible(screen, ".scene-panel"))
                scene_js(
                    screen, "S.setCameraPose(arguments[0])", clearance_view(closer)
                )
                park_scene_pointer(screen)
                wait_for_scene_idle(screen)
                color = [int(line["color"][i : i + 2], 16) for i in (1, 3, 5)]
                assert np.asarray(
                    clearance_endpoint_pixels(screen, obstacle)
                ) == pytest.approx(np.asarray([color, color]), abs=4)
                if evidence.enabled:
                    evidence.capture("01-lines-1920")
                touch_toggle()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return S.clearanceLines()")
                )
                assert clearance_button(screen).get_attribute("aria-pressed") == "false"
                assert not screen.selenium.find_elements(
                    By.CSS_SELECTOR, ".wc-clearance-label"
                )
                evidence.finish()
            finally:
                evidence.stop()
                if run_in_app(lambda: view.clearance_overlay.enabled):
                    run_in_app(view.clearance_overlay.toggle)
                run_in_app(lambda: view._set_hidden([obstacle], False))
                run_in_app(lambda: setattr(handle, "shapes", original))
                confirmed(screen, lambda: handle.shapes == original)
                if run_in_app(lambda: ui_state.scene_panel.is_open):
                    click_marked(screen, "scene-close")

    def test_imported_fixture_is_visible_at_scaled_size(
        self, object_screen, tmp_path, monkeypatch
    ):
        screen = object_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path / "worlds")
        source = (bracket_stl if enabled() else fixture_stl)(
            tmp_path / "Imported-fixture.stl"
        )
        view = ui_state.urdf_scene
        before = view.fixture_meshes
        with (
            viewport(screen, 1920, 1080) if enabled() else nullcontext(),
            camera_kept(screen) if enabled() else nullcontext(),
        ):
            evidence = VisualEvidence(screen, "stl-import")
            try:
                click_marked(screen, "tab-scene")
                menu_choice(screen, "scene-add-menu", "scene-import-mesh")
                upload = settled_element(
                    screen, lambda: marked_element(screen, "mesh-upload")
                )
                upload.find_element(By.CSS_SELECTOR, 'input[type="file"]').send_keys(
                    str(source)
                )
                size = "120 × 80 × 60 mm" if evidence.enabled else "12 × 8 × 4 mm"
                wait(screen).until(
                    lambda _: marked_element(screen, "mesh-size").text == size
                )
                click_marked(screen, "mesh-units-cm")
                if evidence.enabled:
                    wait(screen).until(
                        lambda _: marked_element(screen, "mesh-size").text
                        == "1200 × 800 × 600 mm"
                    )
                    click_marked(screen, "mesh-units-mm")
                    wait(screen).until(
                        lambda _: marked_element(screen, "mesh-size").text == size
                    )
                    scene_js(screen, "S.setCameraPose(arguments[0])", FIXTURE_VIEW)
                    wait_for_scene_idle(screen)
                    evidence.capture("01-importer-size-1920")
                click_marked(screen, "mesh-add")
                confirmed(screen, lambda: len(view.fixture_meshes) == len(before) + 1)
                fixture = run_in_app(lambda: view.fixture_meshes[-1])
                drawn = wait(screen).until(
                    lambda _: scene_js(
                        screen,
                        """
                    const o = S.byName(arguments[0]);
                    const mesh = o?.children.find(c => c.isMesh);
                    if (!mesh) return null;
                    mesh.geometry.computeBoundingBox();
                    const box = mesh.geometry.boundingBox;
                    const size = box.max.clone().sub(box.min).multiply(o.scale);
                    return {visible: o.visible && mesh.visible, size: size.toArray()};
                    """,
                        fixture.key,
                    )
                )
                assert drawn["visible"] and drawn["size"] == pytest.approx(
                    [0.12, 0.08, 0.06] if evidence.enabled else [0.12, 0.08, 0.04]
                )
                wait_for_scene_idle(screen)
                proxy = scene_js(
                    screen,
                    """
                    const o = S.byName(arguments[0]);
                    const edges = o.getObjectByName('fixture:edges');
                    return {
                        fill: o.material.opacity, depthWrite: o.material.depthWrite,
                        edge: edges?.material.opacity,
                        color: edges && '#' + edges.material.color.getHexString(),
                    };
                    """,
                    f"shape:{fixture.proxy_names[0]}",
                )
                assert proxy["fill"] <= 0.1 and not proxy["depthWrite"]
                assert proxy["edge"] >= 0.5 and proxy["color"] == SceneColors.SHAPE_HEX
                if evidence.enabled:
                    camera_action(screen, "focus")
                    camera_settled(screen)
                    markers = [
                        f"scene-mesh-{fixture.name.replace(' ', '-')}",
                        *(
                            f"scene-row-{name.replace(' ', '-')}"
                            for name in fixture.proxy_names
                        ),
                    ]
                    wait(screen).until(
                        lambda _: all(
                            marked_element(screen, marker).is_displayed()
                            for marker in markers
                        )
                    )
                wait_for_scene_idle(screen)
                evidence.capture("02-fixture-group-tree-1920")
                evidence.capture("08-imported-fixture")
                evidence.finish()
            finally:
                evidence.stop()
                if view.fixture_meshes != before:
                    click_marked(screen, "object-undo")
                    confirmed(screen, lambda: view.fixture_meshes == before)
                    wait_for_scene_idle(screen)
                    assert not any(
                        url.startswith("/fixture-assets/")
                        for url in scene_js(screen, "return S.cachedStls()")
                    )

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
                place_shape(screen, "box", x, y, "Fixture")
                object_mode(screen, "Resize")
                fill(screen, "object-dim-x", 180)
                confirmed(screen, lambda: handle.shapes[0].x == 0.18)
                fill(screen, "object-dim-y", 130)
                confirmed(screen, lambda: handle.shapes[0].y == 0.13)
                object_mode(screen, "Move")
                clear_selection(screen)
                click_marked(screen, "scene-close")
                wait(screen).until(
                    lambda _: run_in_app(lambda: not ui_state.scene_panel.is_open)
                )
                px, py = scene_object_pixel(screen, "shape:Fixture")
                choose(screen, px, py, "Edit object")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture"]
                    )
                )
                # Selecting the object opens the scene panel on its properties.
                wait(screen).until(lambda _: in_scene_panel(screen, "object-inspector"))
                wait_for_scene_idle(screen)
                plain = scene_js(
                    screen,
                    "const o=S.byName('shape:Fixture'); return "
                    "[o.material.opacity, !!o.getObjectByName('fixture:edges')];",
                )
                assert plain == [0.35, False]
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
                menu_choice(screen, "object-actions", "object-duplicate")
                confirmed(screen, lambda: len(handle.shapes) == 2)
                object_mode(screen, "Move")
                fill(screen, "object-x", 450)
                confirmed(screen, lambda: handle.shapes[-1].pose[0] == 0.45)
                evidence.capture("05-duplicate-1920")
                copy = handle.shapes[-1].name
                click_marked(screen, "object-actions")
                click_marked(screen, "object-hide")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._hidden_objects == {f"shape:{copy}"}
                    )
                )
                wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                clear_selection(screen)
                assert len(handle.shapes) == 2
                evidence.capture("06-hidden-recovery-1920")
                click_marked(screen, f"scene-eye-{copy}")
                wait(screen).until(
                    lambda _: run_in_app(lambda: not view._hidden_objects)
                )
                click_object(screen, "shape:Fixture")
                wait(screen).until(
                    lambda _: run_in_app(
                        lambda: view._selected_objects == ["shape:Fixture"]
                    )
                )
                evidence.stop()
                if evidence.enabled:
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
