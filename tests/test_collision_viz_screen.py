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
  const mesh = o.mesh;
  if (mesh && mesh.name === arguments[0]) {
    return mesh.material ? mesh.material.color.getHexString() : 'nomaterial';
  }
}
return 'missing';
"""


@pytest.mark.browser
@skip_webgl_macos_ci
class TestCollisionVizScreen:
    def test_held_object_follows_flange_and_attachment_dialog_renders(
        self, class_screen
    ):
        import asyncio
        import os
        from pathlib import Path

        import numpy as np
        import waldoctl
        from nicegui import core
        from selenium.webdriver.support.ui import WebDriverWait
        from waldoctl import Box

        import parol6.PAROL6_ROBOT as model
        from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
        from waldo_commander.state import ui_state

        screen_wait_for_scene_ready(class_screen)
        assert core.loop is not None
        local = (0.03, 0.02, 0.25, 0.2, -0.3, 0.4)

        async def prepare():
            client = waldoctl.commander.client
            assert await client.simulator(True) == 1
            assert await client.reset() == 1
            index = await client.home()
            assert await client.wait_command(index, timeout=15)
            index = await client.select_tool("NONE")
            assert await client.wait_command(index, timeout=5)
            world = await client.shapes()
            assert world is not None
            part = Box(name="held-part", x=0.045, y=0.07, z=0.1).attach(
                flange_pose=local,
                epoch=world.attachment_epoch,
            )
            assert await client.set_shapes([part]) == 1
            await waldoctl.commander.scene.refresh_from_backend()
            return np.radians(await client.angles())

        joints = asyncio.run_coroutine_threadsafe(prepare(), core.loop).result(30)
        scene = ui_state.urdf_scene
        assert scene is not None

        def at_pose(q):
            scene.set_appearance_mode(RobotAppearanceMode.EDITING)
            scene.set_editing_angles(q.tolist())

        script = """
        const el = document.querySelector('.nicegui-scene');
        const c = el && getElement(el);
        if (!c || !c.objects) return null;
        for (const o of c.objects.values()) {
          if (o.mesh && o.mesh.name === 'shape:held-part') {
            o.mesh.updateWorldMatrix(true, false);
            return o.mesh.matrixWorld.elements;
          }
        }
        return null;
        """
        try:
            for turn in (0, 0.3):
                q = joints.copy()
                q[0] += turn
                expected = model.robot.fkine(q) @ model._pose_to_matrix(local)
                run_in_app(lambda: at_pose(q))

                def matches(driver):
                    actual = driver.execute_script(script)
                    return actual is not None and np.allclose(
                        np.asarray(actual).reshape((4, 4), order="F"),
                        expected,
                        atol=1e-6,
                    )

                WebDriverWait(class_screen.selenium, 20).until(matches)

            def show_dialog():
                with scene.scene:
                    scene._show_attachment_dialog("held-part")

            run_in_app(show_dialog)
            class_screen.should_contain("Attach / reconcile held-part")
            if directory := os.environ.get("WALDO_REVIEW_IMAGE_DIR"):
                destination = Path(directory) / "held-object-attachment.png"
                destination.parent.mkdir(parents=True, exist_ok=True)
                class_screen.selenium.save_screenshot(str(destination))
            class_screen.click("Cancel")
        finally:
            asyncio.run_coroutine_threadsafe(
                waldoctl.commander.client.set_shapes([]), core.loop
            ).result(10)
            run_in_app(lambda: scene.set_appearance_mode(RobotAppearanceMode.LIVE))

    def _poll_color(
        self, screen, name: str, want: str, timeout: float = 30.0
    ) -> str | None:
        # Generous deadline: on loaded CI runners the app process can stall
        # for many seconds (e.g. an in-process numba JIT compile) before the
        # material update reaches the browser; the poll returns on match.
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = screen.selenium.execute_script(_FIND_COLOR_JS, name)
            if last == want:
                return last
            time.sleep(0.1)
        return last

    def _enter_editing_and_poll_red(self, screen, want: str) -> str | None:
        """Enter EDITING on the *current* scene and poll for the red tint.

        A long event-loop stall (observed on loaded CI runners) can drop the
        socket.io connection mid-test; the reconnect rebuilds the page with a
        fresh ``ui_state.urdf_scene`` in its restored (non-EDITING) mode,
        silently discarding an EDITING switch made on the old scene object.
        Re-entering EDITING on whatever scene is live makes the check converge
        instead of latching on that lost switch.
        """
        from tests.helpers.browser_helpers import run_in_app
        from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
        from waldo_commander.state import ui_state

        def _enter() -> None:
            us = ui_state.urdf_scene
            if us is not None:
                us.set_appearance_mode(RobotAppearanceMode.EDITING)

        last = None
        for _ in range(3):
            run_in_app(_enter)
            last = self._poll_color(screen, "shape:block", want, timeout=10.0)
            if last == want:
                return last
        return last

    def test_installation_floor_replaces_the_placeholder_disc(
        self, class_screen
    ) -> None:
        """The backend's floor height renders as the ground plane in the
        browser; the screenshot is the sign-off artifact for that change."""
        from waldo_commander.state import ui_state

        screen_wait_for_scene_ready(class_screen)
        assert ui_state.urdf_scene is not None
        from waldo_commander.common.theme import SceneColors

        want = SceneColors.SHAPE_INSTALL_HEX.lstrip("#")

        def _draw_floor() -> None:
            from waldoctl import Box, Physical

            scene = ui_state.urdf_scene
            if scene is not None:
                scene.render_shapes(
                    [],
                    installation=[
                        Box(
                            name="floor",
                            x=6.0,
                            y=6.0,
                            z=0.2,
                            pose=(0.0, 0.0, -0.1, 0, 0, 0),
                            physics=Physical(),
                        )
                    ],
                )

        run_in_app(_draw_floor)
        got = self._poll_color(class_screen, "install:floor", want)
        assert got == want, f"the floor did not render in the browser (got {got})"
        class_screen.shot("installation_floor", failed=False)

        def _drop_floor() -> None:
            scene = ui_state.urdf_scene
            if scene is not None:
                scene.render_shapes([])

        run_in_app(_drop_floor)
        gone = self._poll_color(class_screen, "install:floor", "missing")
        assert gone == "missing", f"the floor object outlived its readback (got {gone})"

    def test_installation_proposal_renders_as_a_ghost(self, class_screen) -> None:
        """A proposed installation shape reaches the browser in its own
        colour; the screenshot is the sign-off artifact."""
        import waldoctl
        from waldoctl import Box
        from waldo_commander.common.theme import SceneColors

        screen_wait_for_scene_ready(class_screen)
        want = SceneColors.SHAPE_PROPOSED_HEX.lstrip("#")

        def _propose() -> None:
            handle = waldoctl.commander.scene
            handle.shapes = [
                Box(name="fence", x=0.6, y=0.05, z=0.4, pose=(0.0, -0.35, 0.2, 0, 0, 0))
            ]
            handle.propose_installation(["fence"])

        run_in_app(_propose)
        try:
            got = self._poll_color(class_screen, "draft:fence", want)
            assert got == want, f"the proposal did not render in its colour (got {got})"
            time.sleep(0.5)  # let three.js draw the frame the shot captures
            class_screen.shot("installation_proposal", failed=False)
        finally:

            def _clear() -> None:
                waldoctl.commander.scene.discard_installation_draft()

            run_in_app(_clear)

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

            red = self._enter_editing_and_poll_red(
                class_screen, SceneColors.COLLISION_HEX.lstrip("#")
            )
            assert red == SceneColors.COLLISION_HEX.lstrip("#"), (
                f"shape did not turn red on collision (got {red})"
            )

            def _back_to_live() -> None:
                current = ui_state.urdf_scene
                if current is not None:
                    current.set_appearance_mode(RobotAppearanceMode.LIVE)

            run_in_app(_back_to_live)

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
