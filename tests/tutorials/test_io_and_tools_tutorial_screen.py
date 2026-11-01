"""Tutorial: attaching a tool, driving the gripper, and switching outputs."""

import asyncio

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
from tests.helpers.demo_steps import center, menu_item, press_mark
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import TCP_VIEW, at_start, settled_element
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from waldo_commander.state import robot_state, ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


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

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def settings_open() -> bool:
        return run_in_app(lambda: bool(ui_state.settings_content.dialog.value))

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

    scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
    wait_for_scene_idle(screen)
    video = Tutorial(
        screen,
        "io_and_tools",
        "I/O and tool control",
        "Attach a tool, drive the gripper, and switch digital outputs",
    )
    try:
        gear = center(mark("tab-settings"))
        video.jump(gear[0] + 320, gear[1] - 240)
        video.step("Tools are chosen in Settings, under Tool")
        video.glide(gear, 1.2)
        video.click(settle=lambda: wait(screen).until(lambda _: settings_open()))
        press_mark(video, "settings-cat-tool", seconds=0.8)
        video.glide(center(mark("select-tool")), 0.8)
        video.click()
        option = menu_item(screen, "SSG-48")
        video.pause(0.6)
        video.glide(center(option), 0.8)
        video.click(
            settle=lambda: wait(screen, 20).until(lambda _: status.tool.key == "SSG-48")
        )
        video.pause(0.8)
        video.glide(center(mark("settings-close")), 0.8)
        video.click(settle=lambda: wait(screen).until(lambda _: not settings_open()))
        wait_for_scene_idle(screen)
        video.step(
            "The gripper is drawn on the arm, and its buttons join the control panel",
            read=2.0,
        )

        video.step("An electric gripper calibrates once: the wrench button")
        press_mark(
            video,
            "btn-tool-action-r",
            settle=lambda: wait(screen, 20).until(
                lambda _: robot_state.gripper_calibrated
            ),
            seconds=1.0,
        )
        video.live(1.5)
        video.step("The button beside it closes and opens the jaws")
        closed = status.tool.positions[0] if status.tool.positions else 0.0
        press_mark(video, "btn-tool-action-l", seconds=0.8)
        video.live(2.0)
        wait(screen, 15).until(
            lambda _: status.tool.positions
            and abs(status.tool.positions[0] - closed) > 0.2
        )

        video.step(
            "The gripper tab has sliders for position and current, and a live chart"
        )
        # Over the top of the view, clear of the arm, whose tool would
        # bring up its gizmo on the way past.
        gripper = rail_icon("tab-gripper")
        spot = center(gripper)
        video.along([video.at, (video.at[0], 140), (spot[0] + 120, 140), spot], 1.6)
        open_rail_tab(gripper)
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
        video.glide((tx, ty), 0.8)
        video.press()
        video.glide((rect["x"] + rect["width"] * 0.5, ty), 1.6)
        video.release()
        video.live(2.0)
        video.pause(0.6)

        video.step("Digital I/O has its own tab: set an output high or low")
        io = rail_icon("tab-io")
        video.glide(center(io), 1.0)
        open_rail_tab(io)
        high = output_button("HIGH")
        video.glide(center(high), 0.8)
        video.click(
            settle=lambda: wait(screen).until(lambda _: status.io.outputs[0] == 1)
        )
        video.step("The footer's dots show each line's state", read=1.0)
        video.glide(center(mark("io-dots")), 1.0)
        video.pause(1.4)
        low = output_button("LOW")
        video.glide(center(low), 0.8)
        video.click(
            settle=lambda: wait(screen).until(lambda _: status.io.outputs[0] == 0)
        )
        video.pause(0.8)
        video.finish()
    finally:
        if status.io.outputs and status.io.outputs[0]:
            _client(lambda c: c.write_io(0, 0))
        if not no_visible(screen, ".top-panels-container .q-tab-panel"):
            close_panel(screen, "top-panels-container")
        _client(lambda c: c.select_tool("NONE"))
        wait(screen, 20).until(lambda _: status.tool.key == "NONE")
