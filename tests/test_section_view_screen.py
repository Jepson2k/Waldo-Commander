"""Section controls change the view and let picking pass through removed faces."""

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from waldoctl.shapes import Box, Sphere

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
    camera_menu,
    confirmed,
    fill,
    menu_fits,
    probe,
    program_previewed,
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


def _click_center(screen, name):
    x, y = project_local(screen, name, [[0, 0, 0]])[0]
    action = ActionChains(screen.selenium)
    pointer_to(screen, x, y, action)
    action.click().perform()


def _section_menu(screen):
    camera_menu(screen)
    expansion = marked_element(screen, "camera-section-options")
    if "q-expansion-item--expanded" not in expansion.get_attribute("class"):
        expansion.find_element(By.CSS_SELECTOR, ".q-item").click()
    wait(screen).until(
        lambda _: marked_element(screen, "camera-section-enabled").is_displayed()
    )


def _section_settled(screen, enabled):
    previous = None

    def settled(_):
        nonlocal previous
        switch = marked_element(screen, "camera-section-enabled")
        rect = switch.rect
        done = (
            rect == previous
            and switch.get_attribute("aria-checked") == str(enabled).lower()
        )
        previous = rect
        return done and probe(screen, "section")["section"] == enabled

    wait(screen).until(settled)


@pytest.mark.browser
class TestSectionView:
    def test_section_slider_numeric_flip_picking_and_restore(self, class_screen):
        screen = class_screen
        with preserve_capture_buffers(screen), viewport(screen, 1920, 1080):
            screen_wait_for_scene_ready(screen, timeout_s=40)
            at_start(screen)
            center = np.array([0.38, 0.05, 0.2])
            camera = np.array([0.9, -0.6, 0.6])
            normal = (center - camera) / np.linalg.norm(center - camera)
            part = center + normal * 0.07
            shapes = [
                Box(
                    name="Fixture shell",
                    x=0.3,
                    y=0.3,
                    z=0.3,
                    pose=(*center, 0, 0, 0),
                    collision=False,
                ),
                Sphere(
                    name="Internal part",
                    radius=0.025,
                    pose=(*part, 0, 0, 0),
                    collision=False,
                ),
            ]
            run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", shapes))
            confirmed(screen, lambda: waldoctl.commander.scene.shapes == shapes)
            scene_js(screen, "S.setCameraPose(arguments[0])", [*camera, *center])
            with program_previewed(EDIT_PROGRAM):
                evidence = VisualEvidence(
                    screen, "section-view", synchronized_frames=True
                )
                try:
                    _click_center(screen, "shape:Internal part")
                    wait(screen).until(
                        lambda _: run_in_app(
                            lambda: ui_state.urdf_scene._selected_objects
                        )
                        == ["shape:Fixture shell"]
                    )
                    click_marked(screen, "object-close")
                    evidence.capture("01-before-section-1920")
                    _section_menu(screen)
                    click_marked(screen, "camera-section-enabled")
                    wait(screen).until(lambda _: probe(screen, "section")["section"])
                    menu_fits(screen)
                    evidence.capture("02-section-controls-1920")
                    click_marked(screen, "camera-menu")
                    _click_center(screen, "shape:Internal part")
                    wait(screen).until(
                        lambda _: run_in_app(
                            lambda: ui_state.urdf_scene._selected_objects
                        )
                        == ["shape:Internal part"]
                    )
                    evidence.capture("03-pick-through-cut-face-1920")
                    click_marked(screen, "object-close")
                    _section_menu(screen)
                    slider = marked_element(screen, "camera-section-slider")
                    thumb = slider.find_element(By.CSS_SELECTOR, ".q-slider__thumb")
                    ActionChains(screen.selenium).move_to_element(
                        thumb
                    ).click_and_hold().move_by_offset(6, 0).move_by_offset(
                        12, 0
                    ).move_by_offset(12, 0).release().perform()
                    wait(screen).until(
                        lambda _: probe(screen, "section")["sectionOffset"] > 10
                    )
                    wait(screen).until(
                        lambda _: float(
                            marked_element(
                                screen, "camera-section-offset"
                            ).get_attribute("value")
                        )
                        == pytest.approx(
                            probe(screen, "section")["sectionOffset"], abs=0.1
                        )
                    )
                    evidence.capture("04a-slider-section-1920")
                    fill(screen, "camera-section-offset", 1000)
                    wait(screen).until(
                        lambda _: float(
                            marked_element(
                                screen, "camera-section-offset"
                            ).get_attribute("value")
                        )
                        == probe(screen, "section")["sectionOffset"]
                        == probe(screen, "section")["sectionRange"]
                    )
                    fill(screen, "camera-section-offset", 20)
                    wait(screen).until(
                        lambda _: abs(probe(screen, "section")["sectionOffset"] - 20)
                        < 0.01
                    )
                    evidence.capture("04-offset-section-1920")
                    click_marked(screen, "camera-section-flip")
                    click_marked(screen, "camera-menu")
                    x, y = project_local(screen, "shape:Internal part", [[0, 0, 0]])[0]
                    hover = ActionChains(screen.selenium)
                    pointer_to(screen, x, y, hover)
                    hover.perform()
                    wait_for_scene_idle(screen)
                    evidence.capture("05a-flipped-hover-1920")
                    _click_center(screen, "shape:Internal part")
                    wait(screen).until(
                        lambda _: run_in_app(
                            lambda: ui_state.urdf_scene._selected_objects
                        )
                        == ["shape:Fixture shell"]
                    )
                    click_marked(screen, "object-close")
                    evidence.capture("05-flipped-side-1920")
                    _section_menu(screen)
                    click_marked(screen, "camera-section-enabled")
                    wait(screen).until(
                        lambda _: not probe(screen, "section")["section"]
                        and probe(screen, "section")["materials"] == 0
                    )
                    click_marked(screen, "camera-menu")
                    evidence.capture("06-restored-1920")
                    assert waldoctl.commander.scene.shapes == shapes
                    evidence.stop()
                    with viewport(screen, 1366, 768):
                        _section_menu(screen)
                        click_marked(screen, "camera-section-enabled")
                        wait(screen).until(
                            lambda _: probe(screen, "section")["section"]
                        )
                        menu_fits(screen)
                        axis = marked_element(screen, "camera-section-axis")
                        axis.find_element(
                            By.XPATH, ".//button[.//*[text()='X']]"
                        ).click()
                        wait(screen).until(
                            lambda _: probe(screen, "section")["plane"][:3] == [1, 0, 0]
                        )
                        wait_for_scene_idle(screen)
                        evidence.capture("07-compact-section-1366")
                        baseline = scene_js(
                            screen, "return S.renderer.info.memory.geometries"
                        )
                        for _ in range(3):
                            click_marked(screen, "camera-section-enabled")
                            _section_settled(screen, False)
                            click_marked(screen, "camera-section-enabled")
                            _section_settled(screen, True)
                        assert (
                            scene_js(screen, "return S.renderer.info.memory.geometries")
                            == baseline
                        )
                        plane = probe(screen, "section")["plane"]
                        scene_js(screen, "S.renderer.forceContextLoss()")
                        wait(screen).until(
                            lambda _: scene_js(
                                screen, "return S.root.hasAttribute('data-gl')"
                            )
                        )
                        scene_js(screen, "S.renderer.forceContextRestore()")
                        wait(screen).until(
                            lambda _: scene_js(
                                screen, "return !S.root.hasAttribute('data-gl')"
                            )
                        )
                        wait_for_scene_idle(screen)
                        assert probe(screen, "section")["plane"] == pytest.approx(plane)
                        axis.find_element(
                            By.XPATH, ".//button[.//*[text()='Y']]"
                        ).click()
                        wait(screen).until(
                            lambda _: probe(screen, "section")["plane"][:3] == [0, 1, 0]
                        )
                        click_marked(screen, "camera-section-enabled")
                        click_marked(screen, "camera-menu")
                    evidence.finish()
                finally:
                    evidence.stop()
