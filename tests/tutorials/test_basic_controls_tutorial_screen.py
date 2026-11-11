"""Tutorial: jogging from the control panel and the keyboard."""

import pytest
import waldoctl
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    defocus_editor,
    ensure_robot_homed,
    marked_element,
    run_in_app,
)
from tests.helpers.browser_session import wait
from tests.helpers.demo_steps import (
    animating,
    center,
    jog_ended,
    motion,
    place_arm,
    slowed_jogs,
)
from tests.helpers.demo_video import CHANGE_S, OUTCOME_S, Tutorial
from tests.helpers.scene_screen import at_start, settled_element
from tests.helpers.video_compose import Rect
from waldo_commander.services.keybindings import _JOG_KEY_SLOTS
from waldo_commander.state import ui_state

pytestmark = [pytest.mark.browser, pytest.mark.timeout(900)]

#: Jogs run this many times slower while recorded, then play back at speed.
SLOW = 3
#: Where the arm stands: beside the control panel, so a zoom shows both.
ARM_AT = (1160, 770)
#: The page's bottom-right quarter, the panel and the arm, at twice the size,
#: down to the footer, whose last-action text would show the slowed jogs.
PANEL_CORNER = Rect(960, 496, 960, 540)
#: Where the pointer waits while keys do the work: on the panel, off the
#: floor, which would mark where it rests.
ASIDE = (1720, 805)
#: Where the pointer goes out of the way: empty sky, out of the shot.
AWAY = (1000, 150)


def test_basic_controls(demo_screen, monkeypatch):
    screen = demo_screen
    ensure_robot_homed()
    at_start(screen)
    # The app tells a click from a hold by when the release reaches it. The
    # recording browser delivers a quick click's release a few hundred
    # milliseconds late, so the threshold is stretched the way DemoVideo
    # stretches the hover grace.
    panel = ui_state.control_panel
    for handler in (panel._joint_click_hold, panel._cart_click_hold):
        monkeypatch.setattr(handler, "_threshold_s", 0.6)
    status = waldoctl.commander.status
    jog = waldoctl.commander.settings.jog
    speed, step_deg = jog.speed, jog.joint_step_deg
    place_arm(screen, *ARM_AT, 1.6)

    def joint() -> float:
        return float(status.joints.angles.deg[0])

    def mark(name: str):
        return settled_element(screen, lambda: marked_element(screen, name))

    def pad_arrow(key: str):
        """The pad arrow a jog key presses."""
        return mark(panel._axis_marker(panel.pad_axis(_JOG_KEY_SLOTS[key])))

    def jog_allowed() -> None:
        """A press the app would refuse (the arm still settling, the lease
        not yet held) starts no jog."""
        wait(screen, 15).until(
            lambda _: run_in_app(
                lambda: ui_state.control_panel._movement_allowed(notify=False)
            )
        )

    def settled() -> None:
        jog_ended(screen)

    video = Tutorial(
        screen,
        "basic_controls",
        "Basic controls",
        "Jog the arm from the control panel and the keyboard",
    )
    try:
        video.focus(PANEL_CORNER, margin=0)

        dial = mark("joint-dial-0")
        video.jump(1300, 1000)
        video.step("Rest the pointer on a dial", mark=dial)
        video.glide(center(dial), 0.8)
        video.pause(OUTCOME_S)

        step = mark("step-input")
        video.step("The step: how far one click turns", mark=step)
        x, y = center(step)
        # In from its right, along its row: from below the pointer would pass
        # the tabs or the dials and light one up.
        video.jump(x + 170, y)
        video.glide((x, y), 0.5)
        video.click()
        # Out of the way while keys do the work: on the panel, a tab or the
        # field's tooltip would light up under it.
        video.jump(*AWAY)
        video.hide_pointer()
        video.keys("10")
        video.chord(Keys.CONTROL, "a")
        video.type("10", per_key_s=0.25)
        video.key(Keys.ENTER)
        screen.selenium.execute_script("document.activeElement.blur()")
        wait(screen).until(lambda _: jog.joint_step_deg == 10)
        video.underline(step)
        video.pause(CHANGE_S)
        video.keys()

        video.jump(1545, 912)
        video.glide(center(dial), 0.6)
        plus = mark("btn-j1-plus")
        before = joint()
        video.step("Click + to turn the joint one step", mark=plus)
        video.glide(center(plus), 0.5)
        video.underline(mark("joint-readout-0"))
        jog_allowed()
        with slowed_jogs(SLOW), video.recording(SLOW):
            video.quick_click(settle=settled)
        assert 9 < joint() - before < 11
        video.pause(CHANGE_S)

        before = joint()
        video.step("Hold + to keep it turning")
        jog_allowed()
        with slowed_jogs(SLOW):
            video.hold(1.2, settle=settled, dilation=SLOW)
        assert joint() - before > 12
        video.pause(OUTCOME_S)

        tab = mark("tab-cartesian")
        video.step("Cartesian Jog: move and turn the tool", mark=tab)
        video.glide(center(tab), 0.7)
        # The panel grows as it switches: a ring or the pointer left where the
        # tab was would land on the arrows and the dials.
        video.highlight(None)
        with motion(screen), animating(video, SLOW):
            video.click()
            video.hide_pointer()
            video.jump(*AWAY)
        pad = [
            mark(f"axis-{axis}{sign}")
            for axis in ("x", "y", "z", "rx", "ry", "rz")
            for sign in ("plus", "minus")
        ]
        video.highlight(pad)
        video.pause(OUTCOME_S)

        up = mark("axis-zplus")
        z = status.pose.z
        video.step("Hold Z+ to raise the tool", mark=up)
        video.jump(*ASIDE)
        video.glide(center(up), 0.6)
        jog_allowed()
        with slowed_jogs(SLOW):
            video.hold(1.4, settle=settled, dilation=SLOW)
        assert status.pose.z > z + 5
        video.pause(OUTCOME_S)

        defocus_editor(screen)
        video.jump(*ASIDE)
        video.hide_pointer()
        video.keys("QWE", "ASD")
        video.step("E raises the tool, Q lowers it", mark=pad_arrow("e"))
        z = status.pose.z
        jog_allowed()
        with slowed_jogs(SLOW):
            video.hold_key("e", 1.2, settle=settled, dilation=SLOW)
        assert status.pose.z > z + 3
        video.pause(OUTCOME_S)

        moved = (status.pose.x, status.pose.y)
        video.step("W, A, S, D: across the floor", mark=pad_arrow("a"))
        jog_allowed()
        with slowed_jogs(SLOW):
            video.hold_key("a", 1.2, settle=settled, dilation=SLOW)
        assert (status.pose.x, status.pose.y) != moved
        video.pause(OUTCOME_S)

        chip = mark("chip-jog-speed")
        video.highlight(None)
        video.keys()
        # The panel's top row, out to the E-Stop, rather than half a button.
        video.focus([chip, mark("btn-estop")], zoom=3.0)
        video.keys("[]")
        video.step("] jogs faster, [ slower", mark=chip)
        video.key("]")
        wait(screen).until(lambda _: jog.speed == speed + 10)
        video.underline(chip)
        video.pause(CHANGE_S)
        video.key("[")
        wait(screen).until(lambda _: jog.speed == speed)
        video.pause(OUTCOME_S)
        video.keys()
        video.step("E-Stop stops the arm at once", mark=mark("btn-estop"))
        video.pause(OUTCOME_S)

        video.caption(None)
        video.highlight(None)
        video.keys()
        video.focus(PANEL_CORNER, margin=0)
        video.finish(wide=False)
    finally:
        jog.speed = speed
        jog.joint_step_deg = step_deg
        marked_element(screen, "tab-joint").click()
