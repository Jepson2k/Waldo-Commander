"""Precision movement through the real scene and simulated controller."""

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import run_in_app, viewport
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    at_start,
    camera_kept,
    gizmo,
    hold,
    probe,
    tcp_mm,
    teleport_jog_pose,
    zoom,
)
from tests.helpers.scene_surface import (
    scene_js,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.state import ui_state


def _reveal(screen):
    if gizmo(screen, "tcp:ball"):
        return
    badge = screen.selenium.find_element(
        By.CSS_SELECTOR, '.wc-tcp-affordance button[aria-label="Move TCP"]'
    )
    badge.click()
    wait(screen).until(lambda _: gizmo(screen, "tcp:ball"))


@pytest.mark.browser
class TestPrecisionGizmo:
    @pytest.mark.parametrize(
        "mode",
        [RobotAppearanceMode.SIMULATOR, RobotAppearanceMode.LIVE],
        ids=lambda m: m.value,
    )
    def test_translation_rotation_and_exact_entry(self, class_screen, mode):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        scene = ui_state.urdf_scene
        saved_mode = scene._appearance_mode
        assert waldoctl.commander.status.simulator_active
        with viewport(screen, 1920, 1080), camera_kept(screen):
            evidence = VisualEvidence(
                screen, f"combined-transform/{mode.value}", synchronized_frames=True
            )
            try:
                run_in_app(lambda: scene.set_appearance_mode(mode))
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.55, 0.65, 0.65, 0, 0.17, 0.16],
                )
                _reveal(screen)
                wait_for_scene_idle(screen)
                far_step = gizmo(screen, "tcp:ball")["translationSnap"]
                evidence.capture("01-translation-ready-1920")
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, spot)
                wait_for_scene_idle(screen)
                value = screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-gizmo-value"
                ).text
                assert "mm" in value and "X" in value
                evidence.capture("02-translation-drag-1920")
                zoom(screen, 0.5)
                wait_for_scene_idle(screen)
                assert gizmo(screen, "tcp:ball")["translationSnap"] == far_step
                evidence.capture("03-frozen-step-zoom-1920")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('tcp:ticks')")
                )
                wait(screen).until(
                    lambda _: gizmo(screen, "tcp:ball")["translationSnap"] < far_step
                )

                at_start(screen)
                zoom(screen, 0.8)
                _reveal(screen)
                wait_for_scene_idle(screen)
                before = tcp_mm()
                screen.selenium.find_element(By.CSS_SELECTOR, ".wc-gizmo-value").click()
                entry = screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-gizmo-entry input"
                )
                entry.send_keys("3")
                evidence.capture("04-exact-entry-1920")
                entry.send_keys(Keys.ENTER)
                wait(screen, 15).until(
                    lambda _: np.linalg.norm(tcp_mm() - before) > 2.7
                )
                assert np.linalg.norm(tcp_mm() - before) < 3.5
                wait(screen, 15).until(
                    lambda _: ui_state.control_panel._jog_end_wait_task is None
                    or ui_state.control_panel._jog_end_wait_task.done()
                )
                at_start(screen)
                screen.selenium.find_element(By.CSS_SELECTOR, ".wc-gizmo-value").click()
                screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-gizmo-entry input"
                ).send_keys("30")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                _reveal(screen)
                wait(screen).until(
                    lambda _: not screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-gizmo-entry"
                    ).is_displayed()
                )
                spot = wait(screen).until(
                    lambda _: next(
                        (s for a in "ZXY" if (s := probe(screen, "ringSpot", a))), None
                    )
                )
                hold(screen, spot, 15)
                wait_for_scene_idle(screen)
                assert (
                    "°"
                    in screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-gizmo-value"
                    ).text
                )
                evidence.capture("05-rotation-drag-1920")
                with viewport(screen, 1366, 768):
                    wait_for_scene_idle(screen)
                    evidence.capture("06-rotation-drag-1366")
                wait_for_scene_idle(screen)
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('tcp:ticks')")
                )
                wait_for_scene_idle(screen)
                evidence.capture("07-released-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                run_in_app(lambda: scene.set_appearance_mode(saved_mode))
                teleport_jog_pose()

    def test_axis_rotation_keeps_its_sign(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        with viewport(screen, 1920, 1080), camera_kept(screen):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.55, 0.65, 0.65, 0, 0.17, 0.16],
            )
            try:
                values = []
                for direction in (-1, 1):
                    at_start(screen)
                    if direction == 1:
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
                    _reveal(screen)
                    spot = wait(screen).until(lambda _: probe(screen, "ringSpot", "Z"))
                    hold(screen, spot, direction * 20)
                    text = screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-gizmo-value"
                    ).text
                    assert text.startswith("Z ")
                    values.append(
                        float(text.split()[1].removesuffix("°").replace("−", "-"))
                    )
                    ActionChains(screen.selenium).send_keys(
                        Keys.ESCAPE
                    ).release().perform()
                assert values[0] * values[1] < 0, values
            finally:
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                at_start(screen)
