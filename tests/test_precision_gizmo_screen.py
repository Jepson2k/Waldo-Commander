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
    TCP_VIEW,
    at_start,
    camera_kept,
    click_spot,
    field_number,
    gizmo,
    gizmo_field,
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


def _jog_settled(screen):
    """Wait until the jog a release or a typed change started has ended."""
    wait(screen, 15).until(
        lambda _: ui_state.control_panel._jog_end_wait_task is None
        or ui_state.control_panel._jog_end_wait_task.done()
    )


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
                    TCP_VIEW,
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
                # The change rides on the dragged arrow, named for its axis.
                field = gizmo_field(screen)
                assert field and not field["typing"], field
                assert field["unit"] == "mm" and field["label"].startswith("X "), field
                evidence.capture("02-translation-drag-1920")
                # Close enough that the step would be finer if it followed the zoom.
                zoom(screen, 0.25)
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

                # A click on the arrow opens a field for an exact change; Tab
                # moves it to the next axis and back, Enter applies it.
                at_start(screen)
                zoom(screen, 0.45)
                _reveal(screen)
                wait_for_scene_idle(screen)
                before = tcp_mm()
                arrow = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                click_spot(screen, arrow)
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                assert gizmo_field(screen)["label"].startswith("X "), gizmo_field(
                    screen
                )
                ActionChains(screen.selenium).send_keys(Keys.TAB).perform()
                wait(screen).until(
                    lambda _: gizmo_field(screen)["label"].startswith("Y ")
                )
                ActionChains(screen.selenium).key_down(Keys.SHIFT).send_keys(
                    Keys.TAB
                ).key_up(Keys.SHIFT).perform()
                wait(screen).until(
                    lambda _: gizmo_field(screen)["label"].startswith("X ")
                )
                assert tcp_mm() == pytest.approx(before, abs=0.05), "typing moved"
                ActionChains(screen.selenium).send_keys("3").perform()
                evidence.capture("04-exact-entry-1920")
                ActionChains(screen.selenium).send_keys(Keys.ENTER).perform()
                wait(screen, 15).until(
                    lambda _: np.linalg.norm(tcp_mm() - before) > 2.7
                )
                assert np.linalg.norm(tcp_mm() - before) < 3.5
                _jog_settled(screen)

                # Escape puts the field away and nothing moves.
                at_start(screen)
                _reveal(screen)
                before = tcp_mm()
                arrow = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                click_spot(screen, arrow)
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                ActionChains(screen.selenium).send_keys("30").send_keys(
                    Keys.ESCAPE
                ).perform()
                wait(screen).until(
                    lambda _: not (gizmo_field(screen) or {}).get("typing")
                )
                assert tcp_mm() == pytest.approx(before, abs=0.05), "Escape moved"

                # After a drag its value can be retyped: what is typed replaces
                # what the drag did along that axis.
                _reveal(screen)
                arrow = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, arrow)
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('tcp:ticks')")
                )
                _jog_settled(screen)
                dragged = gizmo_field(screen)
                assert dragged and not dragged["typing"], dragged
                after = tcp_mm()
                base = field_number(dragged["value"])
                screen.selenium.find_element(
                    By.CSS_SELECTOR,
                    ".wc-gizmo-field:not([hidden]) .wc-gizmo-field-value",
                ).click()
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                assert field_number(gizmo_field(screen)["input"]) == pytest.approx(base)
                ActionChains(screen.selenium).key_down(Keys.CONTROL).send_keys(
                    "a"
                ).key_up(Keys.CONTROL).send_keys(f"{base + 3:g}", Keys.ENTER).perform()
                wait(screen, 15).until(lambda _: np.linalg.norm(tcp_mm() - after) > 2.7)
                assert np.linalg.norm(tcp_mm() - after) < 3.5
                _jog_settled(screen)

                at_start(screen)
                _reveal(screen)
                spot = wait(screen).until(
                    lambda _: next(
                        (s for a in "ZXY" if (s := probe(screen, "ringSpot", a))), None
                    )
                )
                hold(screen, spot, 15)
                wait_for_scene_idle(screen)
                assert gizmo_field(screen)["unit"] == "°", gizmo_field(screen)
                evidence.capture("05-rotation-drag-1920")
                if evidence.enabled:
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
                [0.396, 0.516, 0.513, 0, 0.17, 0.16],
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
                    field = gizmo_field(screen)
                    assert field["label"].startswith("Rz "), field
                    values.append(field_number(field["value"]))
                    ActionChains(screen.selenium).send_keys(
                        Keys.ESCAPE
                    ).release().perform()
                assert values[0] * values[1] < 0, values
            finally:
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                at_start(screen)
