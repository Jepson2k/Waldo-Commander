"""Tutorial: jogging from the control panel and the keyboard."""

import pytest
import waldoctl

from tests.helpers.browser_helpers import (
    defocus_editor,
    ensure_robot_homed,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import center, jog_ended, press_mark
from tests.helpers.demo_video import Tutorial
from tests.helpers.scene_screen import at_start, settled_element
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(600)]


def test_basic_controls(demo_screen, monkeypatch):
    screen = demo_screen
    ensure_robot_homed()
    at_start(screen)
    # The app tells a click from a hold by when the release reaches it. The
    # recording browser, drawing the scene in software, delivers a quick
    # click's release a few hundred milliseconds late, so the threshold is
    # stretched the way DemoVideo stretches the hover grace.
    panel = ui_state.control_panel
    for handler in (panel._joint_click_hold, panel._cart_click_hold):
        monkeypatch.setattr(handler, "_threshold_s", 0.6)
    status = waldoctl.commander.status
    jog = waldoctl.commander.settings.jog
    speed = jog.speed

    def joint() -> float:
        return float(status.joints.angles.deg[0])

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def jog_allowed() -> None:
        """A press the app would refuse (the arm still settling, the lease
        not yet held) starts no jog."""
        wait(screen, 15).until(
            lambda _: run_in_app(
                lambda: ui_state.control_panel._movement_allowed(notify=False)
            )
        )

    video = Tutorial(
        screen,
        "basic_controls",
        "Basic controls",
        "Jog the arm from the control panel and the keyboard",
    )
    try:
        dial = center(mark("joint-dial-0"))
        video.jump(dial[0] - 320, dial[1] - 220)
        video.step(
            "Each joint has a dial in the control panel: rest the pointer on one"
        )
        video.glide(dial, 1.2)
        plus = center(mark("btn-j1-plus"))
        video.pause(0.6)

        before = joint()
        video.step("Click + to move the joint one step")
        video.glide(plus, 0.6)
        jog_allowed()
        video.quick_click(settle=lambda: jog_ended(screen))
        video.live(0.8)
        assert 0.5 < joint() - before < 2

        before = joint()
        video.step("Hold it to keep the joint moving")
        jog_allowed()
        video.press()
        video.live(1.6)
        video.release(settle=lambda: jog_ended(screen))
        assert joint() - before > 3
        video.pause(0.6)

        video.step("The Cartesian tab moves the tool along X, Y and Z")
        press_mark(video, "tab-cartesian", seconds=0.8)
        up = center(mark("axis-zplus"))
        z = status.pose.z
        video.glide(up, 0.8)
        jog_allowed()
        video.press()
        video.live(1.4)
        video.release(settle=lambda: jog_ended(screen))
        assert status.pose.z > z + 5
        video.pause(0.6)

        defocus_editor(screen)
        video.step("Keys jog too: W A S D move in X and Y, Q and E in Z")
        z = status.pose.z
        jog_allowed()
        video.hold_key("e", 1.2)
        jog_ended(screen)
        assert status.pose.z > z + 3
        moved = (status.pose.x, status.pose.y)
        jog_allowed()
        video.hold_key("a", 1.2)
        jog_ended(screen)
        assert (status.pose.x, status.pose.y) != moved
        video.pause(0.6)

        video.step("] speeds jogging up and [ slows it down: watch the speed chip")
        video.key("]")
        wait(screen).until(lambda _: jog.speed == speed + 10)
        video.pause(1.0)
        video.key("[")
        wait(screen).until(lambda _: jog.speed == speed)
        video.pause(0.6)
        video.finish()
    finally:
        jog.speed = speed
        marked_element(screen, "tab-joint").click()
