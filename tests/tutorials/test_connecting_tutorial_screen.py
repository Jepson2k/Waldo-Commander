"""Tutorial: choosing the robot's port and leaving the simulator."""

import logging
from pathlib import Path

import pytest
import waldoctl
from nicegui import app as ng_app
from parol6.config import load_com_port
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import marked_element, run_in_app
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_steps import (
    animating,
    center,
    choose_item,
    dialog_card,
    menu_item,
    motion,
    place_arm,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import settled_element
from tests.helpers.video_compose import Rect
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]

#: Animations run this many times slower while recorded, then play back at speed.
SLOW = 3
#: Where the arm stands: beside the control panel, so a zoom shows both.
ARM_AT = (1160, 770)
#: The page's bottom-right quarter: the panel and the arm beside it.
PANEL_CORNER = Rect(960, 496, 960, 540)
#: The page's bottom-left quarter, where the gear is.
BOTTOM_LEFT = Rect(0, 540, 960, 540)
#: Where the pointer goes off the button it clicked, whose tooltip it
#: would otherwise keep up: empty sky, out of the panel's way.
AWAY = (1000, 150)


class _NoPort(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "could not open port" not in record.getMessage()


def test_connecting_your_robot(demo_screen, monkeypatch):
    screen = demo_screen
    status = waldoctl.commander.status
    settings = ui_state.settings_content

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def banner_gone() -> None:
        try:
            wait(screen, 10).until(lambda _: no_visible(screen, ".q-notification"))
        except TimeoutException:
            shown = [
                e.text
                for e in screen.selenium.find_elements(
                    By.CSS_SELECTOR, ".q-notification"
                )
            ]
            raise AssertionError(f"notifications still up: {shown}") from None

    def below(element) -> None:
        """Up into ``element`` from just under it: on the way in from
        anywhere else the pointer would pass its neighbours and bring up
        their tooltips."""
        x, y = center(element)
        video.jump(x, y + 45)
        video.glide((x, y), 0.4)

    def settings_open() -> bool:
        return run_in_app(lambda: bool(settings.dialog.value))

    # Choosing a port saves it for the controller, in the user's own file:
    # the controller's side of that is stubbed, the app's is real. Leaving
    # the simulator is real: with nothing on the saved port, no robot
    # answers.
    async def saved(*_) -> int:
        return 1

    assert not Path(load_com_port() or "/nonexistent").exists(), (
        "a device is on the saved serial port; record this with no robot attached"
    )
    monkeypatch.setattr(settings.client, "connect_hardware", saved)
    # Its failed attempt to open the port is what the controller logs at
    # ERROR, the one ERROR this video sets out to cause.
    serial_log = logging.getLogger("parol6.server.transports.serial_transport")
    no_port = _NoPort()
    serial_log.addFilter(no_port)
    port = ng_app.storage.general.get("com_port")
    chosen = run_in_app(lambda: settings._port_select.value)
    place_arm(screen, *ARM_AT, 1.6)
    video = Tutorial(
        screen,
        "connecting_your_robot",
        "Connecting your robot",
        "Choose the robot's port, then leave the simulator",
    )
    try:
        gear = mark("tab-settings")
        video.focus(BOTTOM_LEFT, margin=0)
        video.jump(560, 760)
        video.step("Settings opens from the gear", mark=gear)
        video.glide(center(gear), 1.0)
        with motion(screen), animating(video, SLOW, settings.dialog):
            video.click(settle=lambda: wait(screen).until(lambda _: settings_open()))
        video.highlight(None)
        select = mark("select-serial-port")
        r = select.rect
        # The row, its label at the left, and room under it for the list.
        video.focus(
            # Clear of the category list's last letters, left of the row.
            Rect(r["x"] - 346, r["y"] - 30, r["width"] + 376, 230),
            zoom=3.0,
            margin=0,
            caption="bottom",
        )
        video.step("Serial port lists this computer's ports", mark=select)
        video.glide(center(select), 0.8)
        video.click()
        item = menu_item(screen, "/dev/")
        video.step("Choose your robot's port")
        choose_item(video, item)
        video.click(
            settle=lambda: wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
        )
        assert run_in_app(lambda: settings._port_select.value)
        video.highlight(select)
        video.underline(select)
        video.pause(CHANGE_S)

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
        video.focus(PANEL_CORNER, margin=0)

        toggle = mark("btn-robot-toggle")
        video.step("The Robot/Simulator button switches to the robot", mark=toggle)
        below(toggle)
        video.click(
            settle=lambda: wait(screen).until(lambda _: not status.simulator_active)
        )
        video.jump(*AWAY)
        video.hide_pointer()
        video.pause(OUTCOME_S)

        waldo = mark("footer-mode")
        # The banner saying the robot needs a connection, beside Waldo.
        banner = wait(screen).until(
            lambda d: next(
                (
                    e
                    for e in d.find_elements(By.CSS_SELECTOR, ".q-notification")
                    if e.is_displayed()
                ),
                None,
            )
        )
        video.highlight(None)
        video.hide_pointer()
        video.focus([waldo, banner], zoom=3.0, margin=0.04)
        video.step("No robot answers: Waldo shows Disconnected", mark=waldo)
        video.pause(CHANGE_S)

        video.highlight(None)
        video.focus(PANEL_CORNER, margin=0)
        video.step("Click again for the simulator", mark=toggle)
        below(toggle)
        video.click(
            settle=lambda: wait(screen).until(lambda _: status.simulator_active)
        )
        video.jump(*AWAY)
        video.hide_pointer()
        # The banner that said no robot answered goes once the simulator is back.
        video.frame(settle=banner_gone)
        video.pause(OUTCOME_S)
        video.finish()
    finally:
        if not status.simulator_active:
            marked_element(screen, "btn-robot-toggle").click()
            wait(screen).until(lambda _: status.simulator_active)
        run_in_app(lambda: settings._port_select.set_value(chosen))
        if port is None:
            ng_app.storage.general.pop("com_port", None)
        else:
            ng_app.storage.general["com_port"] = port
        ng_app.storage.general.pop("startup_mode", None)
        serial_log.removeFilter(no_port)
