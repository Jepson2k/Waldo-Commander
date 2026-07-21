"""Browser-level render check for collision viz: a keep-out shape renders and
turns red when the EDITING pose collides with it (client-side checker)."""

import time

import pytest

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready

_FIND_COLOR_JS = """
const el = document.querySelector('.nicegui-scene');
if (!el) return null;
const c = getElement(el);
if (!c || !c.objects) return null;
for (const o of c.objects.values()) {
  if (o.name === arguments[0]) return o.material ? o.material.color.getHexString() : 'nomaterial';
}
return 'missing';
"""


@pytest.mark.browser
@skip_webgl_macos_ci
class TestCollisionVizScreen:
    def _poll_color(
        self, screen, name: str, want: str, timeout: float = 10.0
    ) -> str | None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = screen.selenium.execute_script(_FIND_COLOR_JS, name)
            if last == want:
                return last
            time.sleep(0.1)
        return last

    def test_shape_renders_and_turns_red_on_collision(self, class_screen) -> None:
        import waldoctl
        from waldoctl import Box
        from waldo_commander.common.theme import SceneColors
        from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
        from waldo_commander.state import ui_state

        screen_wait_for_scene_ready(class_screen)
        scene = ui_state.urdf_scene
        assert scene is not None

        try:
            # A base-encasing box collides at any pose (the base never moves),
            # so the EDITING-pose highlight is deterministic. EDITING is also
            # race-free here: the status consumer's LIVE path (which would
            # restore the tint from empty status pairs each frame) is bypassed.
            def _place_block():
                waldoctl.commander.scene.shapes = [
                    Box(
                        name="block", x=0.6, y=0.6, z=0.6, pose=(0.0, 0.0, 0.1, 0, 0, 0)
                    )
                ]

            run_in_app(_place_block)
            normal = self._poll_color(
                class_screen, "shape:block", SceneColors.SHAPE_HEX.lstrip("#")
            )
            assert normal == SceneColors.SHAPE_HEX.lstrip("#"), (
                f"shape did not render with its base color (got {normal})"
            )

            run_in_app(lambda: scene.set_appearance_mode(RobotAppearanceMode.EDITING))
            red = self._poll_color(
                class_screen, "shape:block", SceneColors.COLLISION_HEX.lstrip("#")
            )
            assert red == SceneColors.COLLISION_HEX.lstrip("#"), (
                f"shape did not turn red on collision (got {red})"
            )
            run_in_app(lambda: scene.set_appearance_mode(RobotAppearanceMode.LIVE))

            # Shapes persist on commander.scene across page loads; the rebuilt
            # scene must re-render them or the barrier turns invisible while
            # still enforced.
            class_screen.selenium.refresh()
            screen_wait_for_scene_ready(class_screen)
            after_reload = self._poll_color(
                class_screen, "shape:block", SceneColors.SHAPE_HEX.lstrip("#")
            )
            assert after_reload == SceneColors.SHAPE_HEX.lstrip("#"), (
                f"shape not re-rendered after page reload (got {after_reload})"
            )
        finally:
            # The checker is process-global — never leak shapes/mode.
            def _reset_scene():
                waldoctl.commander.scene.shapes = []
                current = ui_state.urdf_scene
                if current is not None:
                    current.set_appearance_mode(RobotAppearanceMode.LIVE)

            run_in_app(_reset_scene)
