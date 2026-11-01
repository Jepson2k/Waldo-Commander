"""Tutorial: storing a target in a setup frame, so it follows the fixture."""

import ast

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import marked_element, run_in_app
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_steps import (
    approach,
    center,
    drag_arrow,
    frame_choice,
    menu_item,
    press_mark,
    program_panel,
    target_caught_up,
)
from tests.helpers.demo_video import Tutorial
from tests.helpers.review_scenes import FRAME_STORE_VIEW
from tests.helpers.scene_screen import (
    gizmo,
    program_previewed,
    reset_target_edit,
    settled_element,
    settled_pixel,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from waldo_commander.services.scene_source import target_binding
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

SOURCE = """from waldo_commander.setup import load_setup
setup = load_setup('bench')
from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
rbt.move_l(
    pose=[0, 280, 250, 90, 0, 90],
    speed=0.5,
)
"""


def test_setup_frames(demo_screen, tmp_path, monkeypatch):
    from waldoctl.setup import Frame, Pose, SetupSnapshot

    from waldo_commander.setup import SetupStore

    screen = demo_screen
    # The program loads the setup by name alone, as a user's would.
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    setup = SetupSnapshot(frames={"fixture": Frame((35, 240, 210, 15, 20, 35))})
    run_in_app(lambda: SetupStore(tmp_path).save("bench", setup))
    source = SOURCE

    def text() -> str:
        return run_in_app(lambda: ui_state.active_textarea.value)

    def stored_pose(code: str) -> Pose:
        binding = target_binding(code, ast.parse(code).body[-1].lineno, 6)
        assert binding.storage.name == "fixture"
        return Pose(tuple(ast.literal_eval(binding.vector)), "fixture")

    with program_previewed(source) as targets, program_panel(screen):
        scene_js(screen, "S.setCameraPose(arguments[0])", FRAME_STORE_VIEW)
        wait_for_scene_idle(screen)
        video = Tutorial(
            screen,
            "setup_frames",
            "Setup frames",
            "Store a target relative to a fixture, so it moves when the fixture does",
        )

        def edit_target(target: str) -> None:
            approach(video, settled_pixel(screen, f"targetgroup:{target}"), 0.6)
            video.context_click()
            item = menu_item(screen, "Edit Target")
            video.glide(center(item), 0.6)
            video.click(lambda: wait(screen).until(lambda _: gizmo(screen, "tcp:ball")))
            wait(screen).until(lambda _: no_visible(screen, ".q-menu"))

        try:
            video.step("This program loads a setup with a frame called fixture")
            video.step("Its target is stored in world coordinates", read=1.2)
            edit_target(targets[-1])

            video.step("Edit it, then choose Store in › fixture from the gizmo's badge")
            badge = settled_element(
                screen,
                lambda: screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-gizmo-frame"
                ),
            )
            approach(video, center(badge), 0.6)
            video.click()
            settled_element(
                screen,
                lambda: marked_element(screen, "scene-target-store-frame:fixture"),
            )
            video.pause(0.8)
            press_mark(video, "scene-target-store-frame:fixture")
            wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
            stored = wait(screen).until(lambda _: (code := text()) != source and code)
            local = stored_pose(stored)
            video.step(
                "The target stays put; its line now reads relative to fixture", read=2.4
            )

            video.step(
                "Align the gizmo to the fixture and nudge it along the fixture's X"
            )
            frame_choice(video, "align-frame:fixture", "fixture")
            drag_arrow(
                video,
                "tcp:ball",
                length=24,
                seconds=1.6,
                settle=lambda: target_caught_up(screen),
            )
            video.release(lambda: wait(screen).until(lambda _: text() != stored))
            dragged = stored_pose(text())
            assert np.subtract(dragged.as_list(), local.as_list())[0] > 1
            video.pause(1.0)

            moved = setup.with_frame("fixture", Frame((95, 240, 210, 15, 20, 50)))
            expected = moved.resolve(dragged).as_list()
            video.key(
                Keys.ESCAPE,
                lambda: wait(screen).until(
                    lambda _: not run_in_app(lambda: ui_state.urdf_scene._edit_active)
                ),
            )
            video.step(
                "Now the fixture is re-taught: 60 mm over and turned 15°", read=1.4
            )
            run_in_app(lambda: SetupStore(tmp_path).save("bench", moved))
            video.live(3.0)

            def relocated(_):
                dry = waldoctl.commander.programs.active.dry_run
                target = run_in_app(
                    lambda: dry.targets[-1]
                    if dry.commanded_revision == dry.revision and dry.targets
                    else None
                )
                return (
                    target
                    if target
                    and np.allclose(
                        target.pose[:3], np.asarray(expected[:3]) / 1000, atol=0.0001
                    )
                    else None
                )

            wait(screen, 30).until(relocated)
            video.frame()
            video.step(
                "The target moves with it; the program's numbers don't change", read=2.4
            )
            assert stored_pose(text()).as_list() == pytest.approx(dragged.as_list())
            video.finish()
        finally:
            if video.pressed:
                video.release()
            reset_target_edit(screen)
