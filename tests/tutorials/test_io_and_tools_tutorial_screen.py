"""Tutorial: attaching a tool, driving the gripper, and switching outputs."""

import asyncio
import time

import pytest
import waldoctl
from nicegui import core
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import (
    close_panel,
    ensure_robot_homed,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_steps import (
    animating,
    center,
    choose_item,
    dialog_card,
    menu_item,
    motion,
    place_arm,
    press_mark,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import TCP_VIEW, at_start, settled_element
from tests.helpers.scene_surface import (
    project_local,
    scene_js,
    wait_for_scene_idle,
)
from tests.helpers.video_compose import Rect
from waldo_commander.state import robot_state, ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: Dialog transitions run this many times slower while recorded.
SLOW = 3
#: The tool's and I/O's panels, top left, at twice the size.
TOP_LEFT = Rect(0, 0, 960, 540)
#: Where the pointer goes off a button it clicked: empty sky.
AWAY = (1000, 150)
#: The page's bottom-left quarter, where the gear is.
BOTTOM_LEFT = Rect(0, 540, 960, 540)
#: Where the tool stands: beside the control panel.
TOOL_AT = (1180, 720)


def _client(call) -> None:
    async def run() -> None:
        client = waldoctl.commander.client
        assert await client.wait_command(await call(client), timeout=10)

    asyncio.run_coroutine_threadsafe(run(), core.loop).result(20)


def test_io_and_tool_control(demo_screen):
    screen = demo_screen
    ensure_robot_homed()
    at_start(screen)
    status = waldoctl.commander.status
    settings = ui_state.settings_content

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def settings_open() -> bool:
        return run_in_app(lambda: bool(settings.dialog.value))

    def rail_icon(name: str):
        """A rail tab's icon: the tab itself reaches past the rail."""
        return mark(name).find_element(By.CSS_SELECTOR, "img, .q-icon")

    def open_rail_tab(icon) -> None:
        # A synthetic press and release on the rail does not always switch
        # its tabs while the recording browser is busy drawing the scene; a
        # native click on the icon does.
        icon.click()
        video.frame()

    def output_button(label: str):
        return settled_element(
            screen,
            lambda: next(
                b
                for b in screen.selenium.find_elements(By.CSS_SELECTOR, ".q-btn")
                if b.is_displayed() and b.text.strip() == label
            ),
        )

    def jaws() -> list[float]:
        return list(status.tool.positions or [0.0])

    def jaws_still() -> None:
        """Record the jaws until they stop."""
        seen = [jaws()]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.2)
            seen.append(jaws())
            if len(seen) > 3 and seen[-1] == seen[-2] == seen[-3]:
                return
        raise AssertionError(f"the jaws never stopped: {seen[-4:]}")

    scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
    wait_for_scene_idle(screen)
    # The tool beside the control panel, so a shot shows the gripper and its
    # buttons together.
    place_arm(screen, *TOOL_AT, 1.3)
    video = Tutorial(
        screen,
        "io_and_tools",
        "I/O and tool control",
        "Attach a tool, drive the gripper, and switch digital outputs",
    )
    try:
        gear = mark("tab-settings")
        video.focus(BOTTOM_LEFT, margin=0)
        video.jump(center(gear)[0] + 320, center(gear)[1] - 240)
        video.step("Tools are chosen in Settings, under Tool", mark=gear)
        video.glide(center(gear), 1.0)
        with motion(screen), animating(video, SLOW, settings.dialog):
            video.click(settle=lambda: wait(screen).until(lambda _: settings_open()))
        video.highlight(None)
        category = mark("settings-cat-tool")
        r = category.rect
        # The Tool category and, right of it, the tool list it shows.
        video.focus(
            Rect(r["x"], r["y"] - 40, 760, 260), zoom=3.0, margin=0, caption="bottom"
        )
        press_mark(video, "settings-cat-tool", seconds=0.7)
        select = mark("select-tool")
        video.step("Pick the SSG-48 gripper", mark=select)
        video.glide(center(select), 0.7)
        video.click()
        choose_item(video, menu_item(screen, "SSG-48"))
        video.click(
            settle=lambda: wait(screen, 20).until(lambda _: status.tool.key == "SSG-48")
        )
        video.highlight(select)
        video.pause(OUTCOME_S)
        close = mark("settings-close")
        video.highlight(None)
        # The whole dialog, so it is seen to close rather than leaving a
        # close-up empty.
        video.focus(dialog_card(screen), margin=0.04)
        video.glide(center(close), 0.7)
        with motion(screen), animating(video, SLOW, settings.dialog):
            video.click(
                settle=lambda: wait(screen).until(lambda _: not settings_open())
            )
        video.jump(*AWAY)
        video.hide_pointer()
        wait_for_scene_idle(screen)
        calibrate, grip = mark("btn-tool-action-r"), mark("btn-tool-action-l")
        # The wrist, under the tool that now hides it.
        jx, jy = project_local(screen, "link:L6", [[0, 0, 0]])[0][:2]
        video.focus([calibrate, grip, Rect(jx - 220, jy - 180, 440, 360)], zoom=2.0)
        video.step("The gripper and its buttons appear", mark=[calibrate, grip])
        video.pause(OUTCOME_S)

        video.step("Calibrate it once with the wrench", mark=calibrate)
        video.glide(center(calibrate), 0.8)
        with video.recording():
            video.click()
            # Off the button at once: its tooltip would come up while it works.
            video.glide((center(calibrate)[0], center(calibrate)[1] + 90), 0.4)
            wait(screen, 20).until(lambda _: robot_state.gripper_calibrated)
            jaws_still()

        video.step("The button beside it closes the jaws", mark=grip)
        before = jaws()
        video.glide(center(grip), 0.6)
        with video.recording():
            video.click()
            video.glide((center(grip)[0], center(grip)[1] + 90), 0.4)
            wait(screen, 15).until(lambda _: jaws() != before)
            jaws_still()
        video.pause(OUTCOME_S)
        video.step("Click it again to open them")
        closed = jaws()
        video.glide(center(grip), 0.4)
        with video.recording():
            video.click()
            video.glide((center(grip)[0], center(grip)[1] + 90), 0.4)
            wait(screen, 15).until(lambda _: jaws() != closed)
            jaws_still()
        video.pause(OUTCOME_S)

        gripper = rail_icon("tab-gripper")
        video.highlight(None)
        video.hide_pointer()
        video.focus(TOP_LEFT, margin=0, caption="bottom")
        # In from the right, clear of the arm, whose links would bring up
        # their dials on the way past.
        spot = center(gripper)
        video.jump(spot[0] + 360, spot[1])
        video.step("The gripper tab: sliders and a chart", mark=gripper)
        video.glide(spot, 0.8)
        open_rail_tab(gripper)
        video.highlight(None)
        # Off the rail, whose icons' tooltips would come up.
        video.jump(spot[0] + 360, spot[1] + 120)
        video.hide_pointer()
        # The panel is rebuilt for each tool, so take the slider that shows.
        shown = wait(screen).until(
            lambda _: next(
                (
                    e
                    for e in screen.selenium.find_elements(
                        By.CSS_SELECTOR, ".top-panels-container .q-slider"
                    )
                    if e.is_displayed()
                ),
                None,
            )
        )
        slider = settled_element(screen, lambda: shown)
        thumb = slider.find_element(By.CSS_SELECTOR, ".q-slider__thumb")
        tx, ty = center(thumb)
        rect = slider.rect
        video.step("Drag Pos to move the jaws", mark=slider)
        # The jaws, out of this shot, beside the slider that moves them.
        video.loupe(Rect(jx - 220, jy - 180, 440, 360), zoom=1.0, place=(1400, 200))
        video.glide((tx, ty), 0.7)
        before = jaws()
        video.press()
        video.glide((rect["x"] + rect["width"] * 0.5, ty), 1.4)
        with video.recording():
            video.release()
            wait(screen, 15).until(lambda _: jaws() != before)
            jaws_still()
        video.pause(OUTCOME_S)
        video.loupe(None)

        io = rail_icon("tab-io")
        video.step("Digital I/O has its own tab", mark=io)
        # Right of the rail and down, then in: across the rail's icons their
        # tooltips would come up.
        x, y = center(io)
        video.along([video.at, (x + 90, video.at[1]), (x + 90, y), (x, y)], 1.0)
        open_rail_tab(io)
        high = output_button("HIGH")
        readout = settled_element(
            screen,
            lambda: next(
                e
                for e in screen.selenium.find_elements(
                    By.XPATH, "//*[starts-with(normalize-space(text()), 'OUTPUT 1:')]"
                )
                if e.is_displayed()
            ),
        )
        video.jump(center(io)[0] + 300, center(io)[1] + 160)
        video.hide_pointer()
        video.focus([readout, high], zoom=3.0, caption="bottom")
        video.step("Set an output high", mark=high)
        video.glide(center(high), 0.7)
        video.click(
            settle=lambda: wait(screen).until(lambda _: status.io.outputs[0] == 1)
        )
        video.glide((center(high)[0] + 260, center(high)[1] + 220), 0.5)
        video.highlight(None)
        video.step("Its state reads 1 now")
        video.underline(readout)
        video.pause(CHANGE_S)
        video.finish()
    finally:
        if status.io.outputs and status.io.outputs[0]:
            _client(lambda c: c.write_io(0, 0))
        if not no_visible(screen, ".top-panels-container .q-tab-panel"):
            close_panel(screen, "top-panels-container")
        _client(lambda c: c.select_tool("NONE"))
        wait(screen, 20).until(lambda _: status.tool.key == "NONE")
