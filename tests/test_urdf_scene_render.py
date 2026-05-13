"""Regressions for the URDF scene's render loop."""

from typing import TYPE_CHECKING

import pytest

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.wait import screen_wait_for_scene_ready

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
