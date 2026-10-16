"""Isolation is a reversible view mask, independent of hidden preferences and world state."""

from dataclasses import replace

import numpy as np
import pytest
import waldoctl
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from waldoctl.shapes import Box

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
    clear_selection,
    click_object,
    confirmed,
    fill,
    probe,
    program_previewed,
)
from tests.helpers.scene_surface import pointer_to, scene_js, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.services.control_lease import control_lease
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.state import ui_state


def _visibility(screen, names):
    """How each object shows: in full, faded by isolation, or hidden."""
    return scene_js(
        screen,
        "return arguments[0].map(n=>{const o=S.byName('shape:'+n);let faded=false;"
        "o?.traverse(c=>{faded||=!!c.userData.faded});"
        "return !o?.visible?'hidden':faded?'faded':'shown'})",
        names,
    )


def _isolate_shows_on(screen) -> bool:
    """Whether the isolate button shows the view isolated."""
    try:
        button = marked_element(screen, "object-isolate")
        return "text-wc-action" in button.get_attribute("class").split()
    except (NoSuchElementException, StaleElementReferenceException):
        return False


def _press_isolate(screen) -> None:
    """Press the isolate button from the keyboard."""

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


@pytest.mark.browser
class TestObjectIsolation:
    def test_isolate_edit_restore_hidden_and_external_replacement(self, class_screen):
        screen = class_screen
        with preserve_capture_buffers(screen), viewport(screen, 1920, 1080):
            screen_wait_for_scene_ready(screen, timeout_s=40)
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
                click_marked(screen, "object-isolate")
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
                    click_marked(screen, "object-isolate")
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
                    wait(screen).until(lambda _: _isolate_shows_on(screen))
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
                    click_marked(screen, "object-isolate")
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
                    click_marked(screen, "object-duplicate")
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
                        click_marked(screen, "object-isolate")
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
                    click_marked(screen, "object-isolate")
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
