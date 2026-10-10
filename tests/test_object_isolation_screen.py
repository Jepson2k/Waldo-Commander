"""Isolation is a reversible view mask, independent of hidden preferences and world state."""

from dataclasses import replace

import numpy as np
import pytest
import waldoctl
from selenium.common.exceptions import StaleElementReferenceException
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
    camera_menu,
    click_object,
    confirmed,
    fill,
    menu_fits,
    program_previewed,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.services.control_lease import control_lease
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.state import ui_state


def _visibility(screen, names):
    return scene_js(
        screen, "return arguments[0].map(n=>!!S.byName('shape:'+n)?.visible)", names
    )


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
                click_marked(screen, "object-actions")

                def isolate_enabled(_) -> list[bool] | None:
                    try:
                        return [marked_element(screen, "object-isolate").is_enabled()]
                    except StaleElementReferenceException:
                        return None

                (hidden_can_isolate,) = wait(screen).until(isolate_enabled)
                click_marked(screen, "object-close")
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
                    click_marked(screen, "object-actions")
                    click_marked(screen, "object-isolate")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._isolated_view)
                    )
                    assert _visibility(screen, names) == [True, True, False, False]
                    evidence.capture("02-isolated-pair-1920")
                    assert tuple(waldoctl.commander.scene.shapes) == before
                    assert ui_state.active_textarea.value == source
                    assert len(view.object_history.undo_stack) == history
                    assert control_lease.generation == lease
                    np.testing.assert_allclose(
                        waldoctl.commander.status.joints.angles.rad, live, atol=1e-6
                    )
                    # Isolating rebuilds the inspector; its actions menu is
                    # still closing over it until it is seated again.
                    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                    wait(screen).until(
                        lambda _: screen.selenium.execute_script(
                            "const c=document.querySelector('.wc-object-inspector');"
                            "return !!c && getComputedStyle(c).visibility==='visible'"
                        )
                    )
                    click_marked(screen, "object-close")
                    wait_for_scene_idle(screen)
                    evidence.capture("03-clear-selection-restore-1920")
                    marked_element(screen, "object-isolate").send_keys(Keys.SPACE)
                    wait(screen).until(
                        lambda _: not run_in_app(lambda: view._isolated_view)
                    )
                    assert _visibility(screen, names) == [True, True, False, True]
                    click_object(screen, "shape:Fixture A")
                    click_marked(screen, "object-actions")
                    click_marked(screen, "object-isolate")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: view._isolated_view)
                    )
                    fill(screen, "object-x", 370)
                    confirmed(
                        screen,
                        lambda: waldoctl.commander.scene.shapes[0].pose[0] == 0.37,
                    )
                    assert _visibility(screen, names) == [True, False, False, False]
                    fill(screen, "object-name", "Renamed fixture")
                    confirmed(
                        screen,
                        lambda: waldoctl.commander.scene.shapes[0].name
                        == "Renamed fixture",
                    )
                    names[0] = "Renamed fixture"
                    assert run_in_app(lambda: view._isolated_view)
                    assert _visibility(screen, names) == [True, False, False, False]
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
                    assert _visibility(screen, names) == [True, False, False, False]
                    assert not hidden_can_isolate
                    click_marked(screen, "object-duplicate")
                    confirmed(screen, lambda: len(waldoctl.commander.scene.shapes) == 5)
                    assert _visibility(
                        screen,
                        ["Renamed fixture", "Renamed fixture-copy-1", "Fixture B"],
                    ) == [True, True, False]
                    run_in_app(
                        lambda: view.set_appearance_mode(RobotAppearanceMode.LIVE)
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("04-duplicate-isolated-1920")
                    evidence.stop()
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("05-compact-isolated-1366")
                        click_marked(screen, "object-close")
                        camera_menu(screen)
                        menu_fits(screen)
                        evidence.capture("06-camera-restore-1366")
                        click_marked(screen, "camera-restore-objects")
                        wait(screen).until(
                            lambda _: not run_in_app(lambda: view._isolated_view)
                        )
                        assert _visibility(screen, names) == [True, True, False, True]
                    click_object(screen, "shape:Fixture B")
                    click_marked(screen, "object-actions")
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
                    assert _visibility(screen, [replacement.name]) == [True]
                    evidence.capture("07-external-replacement-restores-1920")
                    evidence.finish()
                finally:
                    evidence.stop()
                    run_in_app(lambda: view.set_appearance_mode(appearance))
                    ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
