"""Tutorial: choosing the robot's port and leaving the simulator."""

import pytest
import waldoctl
from nicegui import app as ng_app
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import marked_element, run_in_app
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.demo_steps import center, menu_item
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import settled_element
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


def test_connecting_your_robot(demo_screen, monkeypatch):
    screen = demo_screen
    status = waldoctl.commander.status

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def settings_open() -> bool:
        return run_in_app(lambda: bool(ui_state.settings_content.dialog.value))

    # Leaving the simulator opens a serial port this box does not have, so
    # the controller's side of the switch is stubbed; the app's is real.
    async def no_port(enabled: bool) -> int:
        return 1

    monkeypatch.setattr(ui_state.control_panel.client, "simulator", no_port)
    video = Tutorial(
        screen,
        "connecting_your_robot",
        "Connecting your robot",
        "Choose the robot's serial port, then switch from the simulator",
    )
    try:
        gear = center(mark("tab-settings"))
        video.jump(gear[0] + 320, gear[1] - 240)
        video.step("Settings opens from the gear at the bottom of the left rail")
        video.glide(gear, 1.2)
        video.click(settle=lambda: wait(screen).until(lambda _: settings_open()))
        video.pause(0.6)

        video.step("Connection lists the serial ports this computer has")
        video.glide(center(mark("select-serial-port")), 0.8)
        video.click()
        menu_item(screen, "/dev/")
        video.pause(0.8)
        video.step("Choose the one your robot is plugged into", read=1.6)
        video.key(Keys.ESCAPE)
        wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
        video.pause(0.4)
        video.glide(center(mark("settings-close")), 0.8)
        video.click(settle=lambda: wait(screen).until(lambda _: not settings_open()))

        video.step("Waldo, at the left of the footer, shows the connection")
        # Down the left edge and along the footer: the arm, passed over,
        # would bring up a joint's dial.
        waldo = center(mark("footer-mode"))
        video.along([video.at, (120, video.at[1]), (120, waldo[1]), waldo], 1.6)
        video.step("Amber means the simulator: no robot moves", read=1.4)

        video.step("The robot button in the control panel leaves the simulator")
        toggle = center(mark("btn-robot-toggle"))
        video.along([video.at, (toggle[0], waldo[1]), toggle], 1.4)
        video.click(
            settle=lambda: wait(screen).until(lambda _: not status.simulator_active)
        )
        video.along([video.at, (toggle[0], waldo[1]), waldo], 1.2)
        video.step("With no robot answering, Waldo looks sad: disconnected", read=2.0)

        video.step("Click it again to go back to the simulator")
        video.along([video.at, (toggle[0], waldo[1]), toggle], 1.2)
        video.click(
            settle=lambda: wait(screen).until(lambda _: status.simulator_active)
        )
        video.pause(1.0)
        video.finish()
    finally:
        if not status.simulator_active:
            marked_element(screen, "btn-robot-toggle").click()
            wait(screen).until(lambda _: status.simulator_active)
        ng_app.storage.general.pop("startup_mode", None)
