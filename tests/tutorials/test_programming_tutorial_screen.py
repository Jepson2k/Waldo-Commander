"""Tutorial: writing a program, playing it, and recording jogs into it."""

import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    defocus_editor,
    ensure_robot_homed,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    arm_pixel,
    arm_still,
    center,
    jog_ended,
    outward,
    program_panel,
    ring_arc,
    ring_shown,
    slowed_preview,
    text_rect,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import (
    PATH_VIEW,
    at_start,
    program_previewed,
    settled_element,
)
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.video_compose import Rect
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: Motion runs this many times slower while recorded, then plays back at speed.
SLOW = 3
#: The program's code, top left, a little over twice the size: clear of
#: the recording indicator at the top of the page.
CODE = Rect(0, 0, 872, 490)
#: The code and the arm beside it, where the path is previewed.
CODE_AND_ARM = Rect(0, 0, 1440, 810)
#: The arm, and the playback bar under the code with Record on it.
PLAY_BAR = Rect(240, 320, 1280, 720)

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

    def last_line():
        lines = [
            e
            for e in screen.selenium.find_elements(
                By.CSS_SELECTOR, ".program-panel .cm-line"
            )
            if e.text.strip()
        ]
        return lines[-1]

    def playing() -> bool:
        return run_in_app(lambda: program.dry_run.playback.is_active)

    with program_previewed(STARTER), program_panel(screen):
        scene_js(screen, "S.setCameraPose(arguments[0])", PATH_VIEW)
        wait_for_scene_idle(screen)
        video = Tutorial(
            screen,
            "programming_and_recording",
            "Programming and recording",
            "Write a program, play it, and record jogs into it",
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
            video.jump(rect["x"] + rect["width"] + 200, rect["y"] + 300)
            video.focus(CODE, zoom=2.2, margin=0, caption="bottom")
            # The lines with their numbers: round the code alone, a ring would
            # run between the two.
            gutters = screen.selenium.find_element(
                By.CSS_SELECTOR, ".program-panel .cm-gutters"
            )
            video.step("Programs are Python", mark=[gutters, editor])
            video.glide((rect["x"] + 160, rect["y"] + 70), 1.0)
            video.click()
            video.chord(Keys.CONTROL, Keys.END)
            # Off the line being typed, which the pointer would cover.
            video.glide((rect["x"] + 420, rect["y"] + 300), 0.5)
            video.highlight(None)

            video.step("Type: autocomplete offers the robot's commands")
            video.type("rbt.mo", per_key_s=0.2)
            wait(screen).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, ".cm-tooltip-autocomplete")
            )
            video.pause(CHANGE_S)
            video.step("Pick move_j from the list", keys=[["↓", "Enter"]])
            video.key(Keys.DOWN)
            video.key(Keys.ENTER)
            wait(screen).until(lambda _: "move_j" in text().rstrip().splitlines()[-1])
            video.keys()
            # The rest, after whatever the completion put in.
            arguments = "[90, -70, 160, 0, 20, 180], speed=0.5"
            typed = text().rstrip().splitlines()[-1]
            if typed.endswith("move_j()"):
                rest = arguments
            elif typed.endswith("move_j("):
                rest = arguments + ")"
            else:
                rest = "(" + arguments + ")"
            video.step("Then type its arguments")
            video.type(rest, per_key_s=0.035)
            # The argument list's own completions would stay open over the
            # path the next step shows.
            ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
            video.underline(text_rect(screen, last_line()))

            video.focus(CODE_AND_ARM, margin=0, caption="bottom")
            video.step("The 3D view previews the new path")
            video.frame(settle=lambda: wait(screen, 30).until(lambda _: previewed(2)))
            video.pause(CHANGE_S)

            video.highlight(None)
            record = mark("editor-record-btn")
            video.focus(record, zoom=2.5, caption="top-left")
            video.step("Press Record", mark=record)
            video.glide(center(record), 0.7)
            video.click()
            recording = True
            video.highlight(None)
            # Up into the editor, off the button, whose tooltip would stay up.
            video.glide((center(record)[0] - 150, center(record)[1] - 200), 0.5)
            video.focus(PLAY_BAR, margin=0, caption="top-left")
            video.step("Then jog the arm")
            before = text().count("move_")
            # A joint's ring, from outside the arm: a frame per step of the
            # drag, so it plays smoothly at the jog speed as set.
            lx, ly = scene_object_pixel(screen, "link:L1")
            video.along([video.at, outward((lx, ly), arm_pixel(screen), 300)], 0.6)
            video.glide((lx, ly), 0.8)
            arc = ring_arc(screen, int(ring_shown(screen)), 30)
            video.glide(arc[0], 0.5)
            video.press()
            # About a 1° step a frame: the ring snaps to whole steps.
            video.along(arc, 0.9, settle=arm_still)
            video.release(settle=lambda: jog_ended(screen))
            video.frame(
                settle=lambda: wait(screen, 15).until(
                    lambda _: text().count("move_") > before
                )
            )
            # Off the arm without crossing its links, which light up.
            video.jump(*outward((lx, ly), arm_pixel(screen), 360))
            video.hide_pointer()
            video.pause(OUTCOME_S)

            video.focus(record, zoom=2.5, caption="top-left")
            video.step("Stop recording when you are done", mark=record)
            video.glide(center(record), 0.7)
            video.click()
            recording = False
            # Off the button, whose tooltip would stay up.
            video.jump(*outward((lx, ly), arm_pixel(screen), 360))
            video.hide_pointer()
            keep = mark("staged-keep")
            video.highlight(None)
            video.focus(CODE, zoom=2.2, margin=0, caption="bottom")
            video.step("Recorded: the start, then each jog")
            video.underline(text_rect(screen, last_line()))
            video.pause(CHANGE_S)

            video.step("Keep adds them to the program", mark=keep)
            kx, ky = center(keep)
            video.glide((kx, ky), 0.8)
            video.click()
            moves = text().count("move_")
            wait(screen, 15).until(lambda _: previewed(moves))
            # Off where Keep was: the ring and the pointer would sit on the
            # button that takes its place.
            video.highlight(None)
            video.glide((kx - 200, ky + 260), 0.5)
            video.pause(OUTCOME_S)

            video.highlight(None)
            video.hide_pointer()
            video.focus(CODE_AND_ARM, margin=0, caption="bottom")
            defocus_editor(screen)
            video.step("Space plays it on the simulator", keys=[["Space"]])
            with slowed_preview(SLOW), video.recording(SLOW):
                video.key(
                    Keys.SPACE, settle=lambda: wait(screen).until(lambda _: playing())
                )
                wait(screen, 30).until(lambda _: not playing())
            video.frame()
            video.keys()
            video.pause(OUTCOME_S)
            video.finish()
        finally:
            if recording:
                marked_element(screen, "editor-record-btn").click()
