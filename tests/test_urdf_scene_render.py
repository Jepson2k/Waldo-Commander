"""Regressions for the URDF scene's render loop."""

from typing import TYPE_CHECKING

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


@pytest.mark.browser
@skip_webgl_macos_ci
class TestUrdfSceneRender:
    """End-to-end render-correctness tests for WC's URDF scene."""

    def test_axes_inset_preserves_main_scene_render(
        self, class_screen: "Screen"
    ) -> None:
        """``viewHelper.render()`` clears the framebuffer when ``renderer.autoClear`` is true;
        without the scene-loop guard, WC's URDF scene (which always enables ``set_axes_inset``)
        gets wiped each frame and the user sees a blank canvas.
        """
        screen_wait_for_scene_ready(class_screen)

        class_screen.selenium.execute_script(
            'const div = document.querySelector(".nicegui-scene");'
            'if (!div) throw new Error("scene element not mounted");'
            "const comp = getElement(div);"
            'if (!comp || !comp.viewHelper) throw new Error("axes inset not active");'
            "const orig = comp.viewHelper.render.bind(comp.viewHelper);"
            "window.__autoClearLog = [];"
            "comp.viewHelper.render = function (renderer) {"
            "  window.__autoClearLog.push(renderer.autoClear);"
            "  return orig(renderer);"
            "};"
        )
        # Let the rAF loop run a few frames.
        import time

        time.sleep(0.3)
        log = class_screen.selenium.execute_script("return window.__autoClearLog")
        assert log and len(log) >= 2, (
            f"expected multiple viewHelper.render calls, got {log}"
        )
        assert all(v is False for v in log), (
            f"renderer.autoClear must be false during viewHelper.render; got {log}"
        )

    def test_zoomed_out_the_fog_starts_beyond_the_robot(
        self, class_screen: "Screen"
    ) -> None:
        """A fog fixed to the reach swallowed the arm, its paths and targets
        once the camera pulled back past a couple of metres."""
        screen_wait_for_scene_ready(class_screen)
        reach = run_in_app(lambda: ui_state.urdf_scene._chain_reach())
        read = (
            "const view = getElement(document.querySelector('.nicegui-scene'));"
            "if (!view.scene.fog) return null;"
            "view.camera.position.set(0, -6, 6); view.controls.update();"
            "return {near: view.scene.fog.near, d: view.camera.position.length()};"
        )
        try:
            fog = WebDriverWait(class_screen.selenium, 5).until(
                lambda _: (m := class_screen.selenium.execute_script(read))
                and m["near"] > m["d"] + reach
                and m
            )
        except TimeoutException:
            fog = class_screen.selenium.execute_script(read)
        assert fog and fog["near"] > fog["d"] + reach, (fog, reach)
