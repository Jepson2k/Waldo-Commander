"""Recorded-path inspection through real picking and playback controls."""

import numpy as np
import pytest
import waldoctl
from nicegui import app as ng_app
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    PATH_PROGRAM,
    PATH_VIEW,
    camera_kept,
    camera_pose,
    path_point,
    program_previewed,
    teleport_jog_pose,
)
from tests.helpers.scene_surface import (
    emits_during,
    pointer_to,
    scene_js,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_scene3d_screen import _touch
from waldo_commander.components.playback import playback
from waldo_commander.services.control_lease import control_lease
from waldo_commander.state import ui_state


TOOL_PROGRAM = """from parol6 import RobotClient
import time
rbt = RobotClient()
rbt.select_tool("SSG-48", "pinch"); rbt.tool.calibrate()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
rbt.tool.close()
time.sleep(0.8)
rbt.tool.open()
rbt.select_tool("NONE")
time.sleep(0.8)
rbt.select_tool("SSG-48", "pinch")
rbt.select_tool("NONE")
"""


def _event(screen, line):
    marker = screen.selenium.find_element(
        By.CSS_SELECTOR, f'.wc-path-event[data-lines~="{line}"]:not([hidden])'
    )
    marker.click()
    choices = screen.selenium.find_elements(
        By.CSS_SELECTOR, f'.wc-path-choices button[aria-label$="line {line}"]'
    )
    if choices and choices[0].is_displayed():
        choices[0].click()


def _selected_line(screen):
    return scene_js(
        screen,
        "const s=S.inspection();return s.current?s.records[s.current.record].blocks[s.current.block].line:null",
    )


def _seek_timeline(screen, fraction):
    timeline = marked_element(screen, "editor-scrub-slider")
    if fraction == 1:
        ActionChains(screen.selenium).move_to_element_with_offset(
            timeline, timeline.size["width"] / 2 - 2, 0
        ).click_and_hold().move_by_offset(20, 0).release().perform()
        return
    ActionChains(screen.selenium).move_to_element_with_offset(
        timeline,
        -timeline.size["width"] / 2 + max(1, fraction * timeline.size["width"]),
        0,
    ).click().perform()


def _matches_record(screen):
    state = scene_js(screen, "return S.inspection()")
    # Teleport readback is quantized to controller steps.
    return state["current"] and np.allclose(
        waldoctl.commander.status.joints.angles.rad,
        waldoctl.commander.programs.active.dry_run.commanded.joints_rad[
            state["current"]["row"]
        ],
        atol=np.radians(0.02),
        rtol=0,
    )


@pytest.mark.browser
class TestPathInspection:
    def test_recorded_tools_timeline_and_invalidation(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(TOOL_PROGRAM),
        ):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.396, 0.516, 0.513, 0, 0.17, 0.16],
            )
            click_tab(screen, "program")
            wait_for_scene_idle(screen)
            revision = scene_js(screen, "return S.inspection().revision")
            authored_tool = run_in_app(
                lambda: ng_app.storage.general.get("selected_tool")
            )
            evidence = VisualEvidence(screen, "path-tools")
            try:
                _event(screen, 7)
                wait(screen).until(lambda _: _selected_line(screen) == 7)
                wait(screen).until(
                    lambda _: waldoctl.commander.status.tool.position > 0.9
                )
                assert not screen.selenium.find_elements(
                    By.CSS_SELECTOR, ".wc-path-card"
                )
                evidence.capture("01-recorded-gripper-1920")
                _event(screen, 9)
                wait(screen).until(
                    lambda _: waldoctl.commander.status.tool.key in (None, "NONE")
                )
                evidence.capture("02-recorded-tool-change-1920")
                click_marked(screen, "editor-step-next")
                wait(screen).until(lambda _: _selected_line(screen) == 10)
                click_marked(screen, "editor-step-prev")
                wait(screen).until(lambda _: _selected_line(screen) == 9)
                for line, tool in ((11, "SSG-48"), (12, "NONE")):
                    _event(screen, line)
                    wait(screen).until(lambda _: _selected_line(screen) == line)
                    wait(screen).until(
                        lambda _: waldoctl.commander.status.tool.key == tool
                    )
                    event = next(
                        b
                        for b in scene_js(
                            screen, "return S.inspection().records[0].blocks"
                        )
                        if b["line"] == line
                    )
                    wait(screen).until(
                        lambda _: run_in_app(lambda: playback._scrub_slider.value)
                        == pytest.approx(event["at"])
                    )
                assert not marked_element(screen, "editor-step-next").is_enabled()
                for button, line, tool in (
                    ("editor-step-prev", 11, "SSG-48"),
                    ("editor-step-prev", 10, "NONE"),
                    ("editor-step-next", 11, "SSG-48"),
                    ("editor-step-next", 12, "NONE"),
                ):
                    click_marked(screen, button)
                    wait(screen).until(lambda _: _selected_line(screen) == line)
                    wait(screen).until(
                        lambda _: waldoctl.commander.status.tool.key == tool
                    )
                assert (
                    run_in_app(lambda: ng_app.storage.general.get("selected_tool"))
                    == authored_tool
                )
                assert scene_js(screen, "return S.inspection().revision") == revision
                _seek_timeline(screen, 0)
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().current.row") == 0
                )
                click_marked(screen, "editor-play-btn")
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().current.row") > 3
                )
                _event(screen, 7)
                wait(screen).until(
                    lambda _: not run_in_app(
                        lambda: waldoctl.commander.programs.active.dry_run.playback.is_active
                    )
                )
                evidence.capture("03-event-and-timeline-1920")
                close_panel(screen, "program-panel")
                screen.selenium.execute_cdp_cmd(
                    "Emulation.setTouchEmulationEnabled",
                    {"enabled": True, "maxTouchPoints": 1},
                )
                try:
                    with viewport(screen, 1024, 768, mobile=True):
                        wait_for_scene_idle(screen)
                        screen.selenium.find_element(
                            By.CSS_SELECTOR,
                            '.wc-path-event[data-lines~="7"]:not([hidden])',
                        ).click()
                        wait(screen).until(
                            lambda _: screen.selenium.find_element(
                                By.CSS_SELECTOR, ".wc-path-choices"
                            ).is_displayed()
                        )
                        evidence.capture("04-event-chooser-touch-1024")
                        assert screen.selenium.execute_script(
                            "return [...document.querySelectorAll('.wc-path-choices button')].every(e=>e.getBoundingClientRect().height>=44)"
                        )
                        _event(screen, 12)
                finally:
                    screen.selenium.execute_cdp_cmd(
                        "Emulation.setTouchEmulationEnabled", {"enabled": False}
                    )
                run_in_app(
                    lambda: setattr(
                        ui_state.active_textarea,
                        "value",
                        TOOL_PROGRAM + "\n# Updated preview\n",
                    )
                )
                wait(screen).until(
                    lambda _: not scene_js(screen, "return S.inspection().current")
                )
                evidence.finish()
            finally:
                evidence.stop()
                run_in_app(playback._pause_sim_playback)
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                if screen.selenium.find_elements(By.CSS_SELECTOR, ".program-panel"):
                    close_panel(screen, "program-panel")

    def test_pointer_ownership_context_restore_and_remount(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(PATH_PROGRAM),
        ):
            wait(screen, 20).until(
                lambda _: scene_js(
                    screen, "return S.inspection().records?.[0].blocks.length > 4"
                )
            )
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                PATH_VIEW,
            )
            wait_for_scene_idle(screen)
            command = next(
                b["command"]
                for b in scene_js(screen, "return S.inspection().records[0].blocks")
                if b["line"] == 7
            )
            x, y, row = path_point(screen, command, 0.2)
            target_x, target_y, target_row = path_point(screen, command, 0.7)
            touching = False
            try:
                _touch(screen, "touchStart", [(1, x, y)])
                touching = True
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().dragging")
                )
                _touch(screen, "touchStart", [(1, x, y), (2, 1500, 160)])
                _touch(screen, "touchMove", [(1, target_x, target_y), (2, 1500, 160)])
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().current.row")
                    >= target_row - 2
                )
                _touch(screen, "touchMove", [(1, target_x, target_y)])
                assert scene_js(screen, "return S.inspection().dragging")
                _touch(screen, "touchEnd", [])
                touching = False
                wait(screen).until(
                    lambda _: not scene_js(screen, "return S.inspection().dragging")
                )
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().pinned")
                )
                scene_js(screen, "S.renderer.forceContextLoss()")
                wait(screen).until(
                    lambda _: not scene_js(screen, "return S.inspection().pinned")
                )
                scene_js(screen, "S.renderer.forceContextRestore()")
                wait(screen).until(
                    lambda _: not screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-scene"
                    ).get_attribute("data-gl")
                )
                action = ActionChains(screen.selenium, duration=0)
                pointer_to(screen, x, y, action)
                action.click().perform()
                try:
                    wait(screen).until(
                        lambda _: scene_js(screen, "return S.inspection().pinned")
                    )
                except TimeoutException:
                    state = scene_js(
                        screen,
                        "const c=getElement(S.root).core;return {inspection:S.inspection(),hit:c.pointer.hit(arguments[0],arguments[1])?.source,handles:c.pointer.handlesAt(arguments[0],arguments[1]).map(h=>h.axis),shown:c.pointer.hover.shown,blocked:c.gestures.blocked,active:c.gestures.active,picks:c.inspection.pick(arguments[0],arguments[1]),camera:S.cameraPose()}",
                        x,
                        y,
                    )
                    raise AssertionError(state)
                # Mounted again, the view has the same paths from its snapshot.
                revision = scene_js(screen, "return S.inspection().revision")
                resets = scene_js(screen, "return S.resets")
                scene_js(screen, "getElement(S.root).$emit('init')")
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.resets") > resets
                    and scene_js(screen, "return S.inspection().revision") == revision
                    and not scene_js(screen, "return S.inspection().pinned")
                )
                wait_for_scene_idle(screen)
                x, y, row = path_point(screen, command, 0.3)
                action = ActionChains(screen.selenium, duration=0)
                pointer_to(screen, x, y, action)
                action.click().perform()
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().pinned")
                )
            finally:
                if touching:
                    _touch(screen, "touchCancel", [])
                ActionChains(screen.selenium).release().send_keys(Keys.ESCAPE).perform()

    def test_click_scrub_crossing_and_stationary_events(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(PATH_PROGRAM),
        ):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                PATH_VIEW,
            )
            click_tab(screen, "program")
            wait_for_scene_idle(screen)
            record = scene_js(screen, "return S.inspection().records[0]")
            moving = next(b for b in record["blocks"] if b["line"] == 7)
            lease = control_lease.generation
            before = waldoctl.commander.status.joints.angles.rad.copy()
            evidence = VisualEvidence(
                screen, "path-inspection", synchronized_frames=True
            )
            try:
                row = path_point(screen, moving["command"], 0.3)[2]
                action = ActionChains(screen.selenium, duration=0)
                for fraction in np.linspace(0.1, 0.3, 6):
                    px, py, _ = path_point(screen, moving["command"], float(fraction))
                    pointer_to(screen, px, py, action)
                events = emits_during(screen, action.perform)
                wait_for_scene_idle(screen)
                assert not events
                assert not scene_js(screen, "return S.inspection().current")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    before
                )
                assert not screen.selenium.find_elements(
                    By.CSS_SELECTOR, ".wc-path-card"
                )
                evidence.capture("01-hover-unchanged-1920")
                camera = camera_pose(screen)
                action = ActionChains(screen.selenium, duration=80)
                action.click_and_hold()
                for fraction in np.linspace(0.3, 0.8, 14):
                    px, py, _ = path_point(screen, moving["command"], float(fraction))
                    pointer_to(screen, px, py, action)
                action.release().perform()
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().current.row")
                    > row
                )
                state = scene_js(screen, "return S.inspection()")
                assert record["blocks"][state["current"]["block"]]["line"] == 7
                wait(screen).until(lambda _: _matches_record(screen))
                wait(screen).until(
                    lambda _: run_in_app(lambda: playback._scrub_slider.value)
                    == pytest.approx(state["time"])
                )
                assert camera_pose(screen) == pytest.approx(camera)
                assert control_lease.generation == lease
                evidence.capture("02-crossing-path-scrub-1920")
                _event(screen, 8)
                wait(screen).until(lambda _: _selected_line(screen) == 8)
                evidence.capture("03-dwell-on-shared-timeline-1920")
                _event(screen, 11)
                wait(screen).until(lambda _: _selected_line(screen) == 11)
                wait(screen).until(lambda _: _matches_record(screen))
                _seek_timeline(screen, 1)
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.inspection().current.row")
                    == record["rows"] - 1
                )
                wait(screen).until(lambda _: _matches_record(screen))
                evidence.capture("04-stationary-rotation-1920")
                if evidence.enabled:
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("05-shared-readouts-1366")
                wait_for_scene_idle(screen)
                evidence.capture("06-shared-readouts-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().send_keys(Keys.ESCAPE).perform()
                close_panel(screen, "program-panel")
