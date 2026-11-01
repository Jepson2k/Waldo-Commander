"""Teach from a real picked face, inspect validity and undo a source sequence."""

import pytest
import waldoctl
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
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_objects import teaching_fixture
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    choose,
    confirmed,
    fill,
    program_previewed,
)
from tests.helpers.scene_surface import project_local, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state


def _teach(screen, fixture):
    wait_for_scene_idle(screen)
    x, y = project_local(screen, f"shape:{fixture.name}", [[0.025, 0, fixture.z / 2]])[
        0
    ]
    choose(screen, x, y, "Teach on this surface…")
    wait(screen).until(
        lambda _: run_in_app(lambda: ui_state.urdf_scene._teaching is not None)
    )


@pytest.mark.browser
class TestSurfaceTeaching:
    def test_face_standoff_twist_validity_sequence_undo(self, class_screen):
        screen = class_screen
        with preserve_capture_buffers(screen), viewport(screen, 1920, 1080):
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
