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
    object_box,
    approach,
    center,
    choose_item,
    menu_item,
    outward,
    program_panel,
    text_rect,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.review_scenes import FRAME_STORE_VIEW
from tests.helpers.scene_screen import (
    gizmo,
    program_previewed,
    reset_target_edit,
    settled_element,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from tests.helpers.video_compose import Rect
from waldo_commander.services.scene_source import target_binding
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: The program's code, top left, at twice the size: its stored line runs
#: long.
CODE = Rect(0, 0, 960, 540)

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


def _around(x: float, y: float, w: float = 960, h: float = 600) -> Rect:
    return Rect(x - w / 2, y - h / 2, w, h)


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

    def line(containing: str):
        return next(
            e
            for e in screen.selenium.find_elements(
                By.CSS_SELECTOR, ".program-panel .cm-line"
            )
            if containing in e.text
        )

    def target_spot() -> Rect:
        """Where the program's last target is: its id follows its line, which
        moves when an edit adds an import above it."""
        last = run_in_app(
            lambda: waldoctl.commander.programs.active.dry_run.targets[-1].id
        )
        return object_box(screen, f"targetgroup:{last}")

    with program_previewed(source), program_panel(screen):
        scene_js(screen, "S.setCameraPose(arguments[0])", FRAME_STORE_VIEW)
        wait_for_scene_idle(screen)
        video = Tutorial(
            screen,
            "setup_frames",
            "Setup frames",
            "Store a target relative to a fixture, so it moves when the fixture does",
        )
        try:
            video.focus(CODE, margin=0, caption="bottom")
            video.step("This program loads the bench setup")
            video.underline(text_rect(screen, line("load_setup(")))
            video.pause(CHANGE_S)

            spot = target_spot()
            video.highlight(None)
            scene = _around(spot.x + 36, spot.y + 36)
            video.focus(scene, margin=0)
            video.step("Right-click its target: Edit Target", mark=spot)
            approach(video, (spot.x + 36, spot.y + 36), 0.7)
            video.context_click()
            choose_item(video, menu_item(screen, "Edit Target"))
            video.click(lambda: wait(screen).until(lambda _: gizmo(screen, "tcp:ball")))
            wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
            video.highlight(None)
            video.pause(OUTCOME_S)

            badge = settled_element(
                screen,
                lambda: screen.selenium.find_element(
                    By.CSS_SELECTOR, ".wc-gizmo-frame"
                ),
            )
            video.step("Click the gizmo's World badge", mark=badge)
            approach(video, center(badge), 0.7)
            # Its menu opens just under it, where the ring would be.
            video.highlight(None)
            video.click()
            store = settled_element(
                screen,
                lambda: marked_element(screen, "scene-target-store-frame:fixture"),
            )
            video.step("Then choose Store in › fixture")
            choose_item(video, store)
            video.click()
            wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
            stored = wait(screen).until(lambda _: (code := text()) != source and code)
            local = stored_pose(stored)
            video.highlight(None)
            video.glide(outward(center(badge), (spot.x + 36, spot.y + 36), 160), 0.5)
            # Escape puts the badge's menu away with the gizmo.
            video.step("Escape finishes editing", keys=[["Esc"]])
            video.key(
                Keys.ESCAPE,
                lambda: wait(screen).until(
                    lambda _: not run_in_app(lambda: ui_state.urdf_scene._edit_active)
                ),
            )
            video.keys()
            video.hide_pointer()
            video.step("The target stays put", mark=target_spot())
            video.pause(OUTCOME_S)

            video.hide_pointer()
            video.focus(CODE, margin=0, caption="bottom")
            video.step("Its line now reads in fixture")
            video.underline(text_rect(screen, line("pose=")))
            video.pause(CHANGE_S)

            # Up into open space: across, the target would land on the tool.
            moved = setup.with_frame("fixture", Frame((35, 240, 270, 15, 20, 35)))
            expected = moved.resolve(local).as_list()
            video.highlight(None)
            video.focus(scene, margin=0)
            # Read before the target moves, so the next caption lands on it.
            video.step("When the fixture is re-taught…", mark=target_spot(), read=2.0)
            run_in_app(lambda: SetupStore(tmp_path).save("bench", moved))

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
            wait_for_scene_idle(screen)
            video.frame()
            video.move_highlight(target_spot())
            video.step("The target moves with it")
            video.pause(CHANGE_S)

            video.highlight(None)
            video.focus(CODE, margin=0, caption="bottom")
            video.step("And the program's numbers don't change")
            video.underline(text_rect(screen, line("pose=")))
            assert stored_pose(text()).as_list() == pytest.approx(local.as_list())
            video.pause(CHANGE_S)
            video.finish()
        finally:
            if video.pressed:
                video.release()
            reset_target_edit(screen)
