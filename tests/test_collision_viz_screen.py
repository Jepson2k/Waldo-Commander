"""Browser-level render check for collision viz: a keep-out shape renders and
turns red when the controller reports it colliding."""

import time

import pytest

from tests.conftest import skip_webgl_macos_ci
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
    def _poll_color(self, screen, name: str, timeout: float = 4.0) -> str | None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = screen.selenium.execute_script(_FIND_COLOR_JS, name)
            if last not in (None, "missing"):
                return last
            time.sleep(0.1)
        return last

    def test_shape_renders_and_turns_red_on_collision(self, class_screen) -> None:
        from waldoctl import Box
        from waldo_commander.common.theme import SceneColors
        from waldo_commander.state import ui_state

        screen_wait_for_scene_ready(class_screen)
        scene = ui_state.urdf_scene
        assert scene is not None

        scene.render_shapes(
            [Box(name="wall", x=0.2, y=0.2, z=0.2, pose=(0.3, 0.0, 0.3, 0, 0, 0))]
        )
        normal = self._poll_color(class_screen, "shape:wall")
        assert normal == SceneColors.SHAPE_HEX.lstrip("#"), (
            f"shape did not render with its base color (got {normal})"
        )

        # Drive the highlight directly: the live status consumer would otherwise
        # race us and overwrite commander.status.collision back to empty. The
        # consumer -> highlight path is covered by the user-fixture test.
        link = next(n for n, m in scene._link_to_meshes.items() if m)
        scene.set_colliding([(f"{link}_0", "shape:wall")])

        red = self._poll_color(class_screen, "shape:wall")
        assert red == SceneColors.COLLISION_HEX.lstrip("#"), (
            f"shape did not turn red on collision (got {red})"
        )
