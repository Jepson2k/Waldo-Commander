"""Tutorial: writing a program, playing it, and recording jogs into it."""

import pytest
import waldoctl
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    ensure_robot_homed,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import center, jog_ended, press_mark, program_panel
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import (
    PATH_VIEW,
    at_start,
    program_previewed,
    settled_element,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

STARTER = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
"""


def test_programming_and_recording(demo_screen):
    screen = demo_screen
    ensure_robot_homed()
    at_start(screen)
    program = waldoctl.commander.programs.active

    def text() -> str:
        return run_in_app(lambda: ui_state.active_textarea.value)

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def previewed(moves: int) -> bool:
        dry = program.dry_run
        return (
            dry.commanded_revision == dry.revision
            and len(dry.targets) == moves
            and not run_in_app(lambda: dry.playback.is_active)
        )

    with program_previewed(STARTER), program_panel(screen):
        scene_js(screen, "S.setCameraPose(arguments[0])", PATH_VIEW)
        wait_for_scene_idle(screen)
        video = Tutorial(
            screen,
            "programming_and_recording",
            "Programming and recording",
            "Write a program, play it on the simulator, and record jogs into it",
        )
        recording = False
        try:
            editor = settled_element(
                screen,
                lambda: screen.selenium.find_element(
                    By.CSS_SELECTOR, ".program-panel .cm-content"
                ),
            )
            rect = editor.rect
            video.jump(rect["x"] + rect["width"] + 200, rect["y"] + 260)
            video.step("Programs are Python, in the editor on the left")
            video.glide((rect["x"] + 120, rect["y"] + 80), 1.2)
            video.click()
            video.chord(Keys.CONTROL, Keys.END)

            video.step("Start typing: autocomplete offers the robot's commands")
            video.type("rbt.mo", per_key_s=0.18)
            wait(screen).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, ".cm-tooltip-autocomplete")
            )
            video.pause(1.4)
            video.type("ve_j([90, -70, 160, 0, 20, 180], speed=0.5)", per_key_s=0.04)
            wait(screen, 30).until(lambda _: previewed(2))
            video.step("The 3D view previews the path as you type", read=1.6)

            video.step("Play runs it on the simulator")
            press_mark(
                video,
                "editor-play-btn",
                settle=lambda: wait(screen).until(
                    lambda _: run_in_app(lambda: program.dry_run.playback.is_active)
                ),
            )
            video.live(4.0)
            wait(screen, 30).until(
                lambda _: not run_in_app(lambda: program.dry_run.playback.is_active)
            )
            video.pause(0.6)

            video.step("Or teach it: press Record, then jog the arm")
            press_mark(video, "editor-record-btn", seconds=0.8)
            recording = True
            dial = center(mark("joint-dial-0"))
            video.glide(dial, 1.2)
            plus = center(mark("btn-j1-plus"))
            video.glide(plus, 0.5)
            before = text()
            video.press()
            video.live(1.4)
            video.release(settle=lambda: jog_ended(screen))
            wait(screen, 15).until(
                lambda _: text().count("move_j") > before.count("move_j")
            )
            video.step("Each jog becomes a move line in the program", read=1.8)

            video.step("Stop recording, then keep the new lines")
            press_mark(video, "editor-record-btn", seconds=0.8)
            recording = False
            press_mark(video, "staged-keep", seconds=0.8)
            wait(screen, 15).until(lambda _: previewed(3))
            video.pause(1.4)
            video.finish()
        finally:
            if recording:
                marked_element(screen, "editor-record-btn").click()
