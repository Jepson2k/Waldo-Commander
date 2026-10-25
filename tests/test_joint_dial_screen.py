"""Joint dial review through the real app and simulated controller."""

import asyncio
import math

import pytest
import waldoctl
from nicegui import core
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import run_in_app, viewport
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    assert_dial_readable,
    camera_kept,
    camera_pose,
    drag_dial,
    grab_ring,
    joint_deg,
    joint_settled,
    teleport_jog_pose,
)
from tests.helpers.scene_surface import (
    emits_during,
    frames,
    frames_at_rest,
    scene_js,
    wait_for_scene_idle,
)
from tests.helpers import visual_evidence
from tests.helpers.visual_evidence import VisualEvidence
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.services.urdf_scene.angle_pipeline import urdf_to_panel


@pytest.mark.browser
class TestJointDial:
    @pytest.mark.parametrize(
        "mode",
        [
            RobotAppearanceMode.SIMULATOR,
            # The grey arm runs the same flow; it is worth only its captures.
            pytest.param(
                RobotAppearanceMode.LIVE,
                marks=pytest.mark.skipif(
                    not visual_evidence.enabled(),
                    reason="only recorded with --evidence",
                ),
            ),
        ],
        ids=lambda mode: mode.value,
    )
    def test_joint_dial_appearances(self, class_screen, mode):
        """Render production live/simulator materials; all motion stays simulated."""
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        scene = ui_state.urdf_scene
        saved_mode = scene._appearance_mode
        assert waldoctl.commander.status.simulator_active
        with viewport(screen, 1920, 1080), camera_kept(screen):
            evidence = VisualEvidence(
                screen, f"joint-hover-{mode.value}", synchronized_frames=True
            )
            try:
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.234, -0.252, 0.37, 0, 0.06, 0.13],
                )
                camera = camera_pose(screen)
                teleport_jog_pose()
                run_in_app(lambda: scene.set_appearance_mode(mode))
                color = (
                    scene.config.sim_color
                    if mode == RobotAppearanceMode.SIMULATOR
                    else scene.config.material
                )
                wait(screen).until(
                    lambda _: scene_js(
                        screen,
                        "const colors = []; S.byName('link:L1').traverse(o => {if (o.isMesh) colors.push(o.material.color.getHexString());}); return colors.includes(arguments[0]);",
                        color.removeprefix("#"),
                    )
                )
                assert (
                    len(
                        screen.selenium.find_elements(
                            By.CSS_SELECTOR, ".joint-dial-svg"
                        )
                    )
                    == 6
                )
                idle_angles = waldoctl.commander.status.joints.angles.rad.copy()
                evidence.capture(f"{mode.value}-idle-1920")
                for joint in range(6):
                    if joint == 5:
                        scene_js(
                            screen,
                            "S.setCameraPose(arguments[0])",
                            [0.34, 0.34, 0.335, 0, 0, 0.2],
                        )
                    assert not emits_during(
                        screen, lambda: grab_ring(screen, f"link:L{joint + 1}", joint)
                    )
                    wait(screen).until(
                        lambda _: scene_js(
                            screen,
                            "return S.glowing(arguments[0])",
                            f"link:L{joint + 1}",
                        )
                    )
                    wait_for_scene_idle(screen)
                    assert_dial_readable(screen, joint)
                    evidence.capture(f"{mode.value}-joint-{joint + 1}-1920")
                scene_js(screen, "S.setCameraPose(arguments[0])", camera)
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    idle_angles
                )
                screen.selenium.execute_cdp_cmd(
                    "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
                )
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('joint:hover')")
                )
                wait(screen).until(lambda _: not scene_js(screen, "return S.dial(5)"))
                evidence.capture(f"{mode.value}-hover-cleared-1920")
                drag_dial(screen, -25)
                target = urdf_to_panel(0, scene_js(screen, "return S.dial(0).q"))[1]
                assert target == pytest.approx(60, abs=1.1)
                wait(screen).until(lambda _: abs(joint_deg(0) - target) < 0.2)
                assert_dial_readable(screen)
                evidence.capture(f"{mode.value}-drag-1920")
                ActionChains(screen.selenium).release().perform()
                joint_settled(0)
                assert camera_pose(screen) == pytest.approx(camera)
                with viewport(screen, 1366, 768):
                    grab_ring(screen, "link:L1", 0)
                    wait_for_scene_idle(screen)
                    assert_dial_readable(screen)
                    evidence.capture(f"{mode.value}-base-1366")
                    drag_dial(screen, 65)
                    hi = scene_js(screen, "return S.dial(0).hi")
                    limit = urdf_to_panel(0, hi)[1]
                    wait(screen).until(lambda _: abs(joint_deg(0) - limit) < 0.2)
                    assert scene_js(screen, "return S.dial(0).q") == pytest.approx(hi)
                    assert scene_js(
                        screen,
                        """
                        const points = S.byName('jog:dial:0:sweep').geometry.attributes.position;
                        for (let i = 0; i < points.count; i++) {
                            const angle = Math.atan2(points.getY(i), points.getX(i));
                            if (angle > arguments[0] + 1e-6) return false;
                        }
                        return true;
                    """,
                        hi,
                    ), "the swept sector passed the travel limit"
                    assert_dial_readable(screen)
                    evidence.capture(f"{mode.value}-limit-1366")
                    ActionChains(screen.selenium).release().perform()
                    joint_settled(0)
                    teleport_jog_pose()
                    grab_ring(screen, "link:L4", 3)
                    wait_for_scene_idle(screen)
                    assert_dial_readable(screen, 3)
                    evidence.capture(f"{mode.value}-wrist-1366")
                    for name, pose in [
                        ("close", [0.16, -0.18, 0.22, 0, 0, 0.11]),
                        ("shallow", [0.5, -0.5, 0.13, 0, 0, 0.11]),
                    ]:
                        scene_js(screen, "S.setCameraPose(arguments[0])", pose)
                        grab_ring(screen, "link:L1", 0)
                        wait_for_scene_idle(screen)
                        assert_dial_readable(screen)
                        evidence.capture(f"{mode.value}-{name}-1366")
                    screen.selenium.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": 1, "y": 1},
                    )
                    wait(screen).until(
                        lambda _: not scene_js(
                            screen, "return S.dial(0) || S.byName('joint:hover')"
                        )
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture(f"{mode.value}-hover-cleared-1366")
                    evidence.stop()
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()
                run_in_app(lambda: scene.set_appearance_mode(saved_mode))
                teleport_jog_pose()

    def test_visible_dial_handle_over_robot(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with viewport(screen, 1366, 768), camera_kept(screen):
            evidence = VisualEvidence(screen, "joint-dial-picking")
            try:
                # This view puts J1's knob over the robot at 1366 px, which is
                # what the grab below checks.
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.36, -0.42, 0.5, 0, 0.06, 0.13],
                )
                grab_ring(screen, "link:L1", 0)
                wait_for_scene_idle(screen)
                camera = camera_pose(screen)
                start = joint_deg(0)
                evidence.capture("01-visible-handle-over-robot")
                drag_dial(screen, 10, at_knob=True)
                target = urdf_to_panel(0, scene_js(screen, "return S.dial(0).q"))[1]
                assert target == pytest.approx(start + 10, abs=1.1)
                wait(screen).until(lambda _: abs(joint_deg(0) - target) < 0.2)
                assert camera_pose(screen) == pytest.approx(camera)
                evidence.capture("02-handle-dragged-over-robot")
                painted = frames_at_rest(screen, window_s=0.35)
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                wait(screen).until(lambda _: frames(screen) > painted)
                assert not scene_js(
                    screen, "return S.byName('jog:dial:0:sweep')?.visible"
                )
                evidence.capture("03-idle-cancel-cleared-sweep")
                ActionChains(screen.selenium).release().perform()
                joint_settled(0)
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()

    def test_joint_dial_views(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with viewport(screen, 1366, 768), camera_kept(screen):
            evidence = VisualEvidence(screen, "joint-dial-views")
            try:
                # Far enough out that every link up to L5 has a pixel to hover
                # at 1366 px; the views below vary the distance themselves.
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.36, -0.42, 0.5, 0, 0.06, 0.13],
                )
                wait_for_scene_idle(screen)
                for joint in range(6):
                    if joint == 5:
                        # From the first view the flange hides behind L5.
                        scene_js(
                            screen,
                            "S.setCameraPose(arguments[0])",
                            [0.5, 0.0, 0.5, 0, 0.0, 0.2],
                        )
                        wait_for_scene_idle(screen)
                    grab_ring(screen, f"link:L{joint + 1}", joint)
                    wait_for_scene_idle(screen)
                    evidence.capture(f"0{joint + 1}-joint-{joint + 1}")
                    if joint >= 2:
                        for delta in (8, -8):
                            start = joint_deg(joint)
                            _, sign, _ = ui_state.urdf_scene._panel_mapping(joint)
                            step = scene_js(
                                screen, "return S.dial(arguments[0]).step", joint
                            )
                            drag_dial(screen, delta * sign, joint)
                            target = urdf_to_panel(
                                joint,
                                scene_js(
                                    screen, "return S.dial(arguments[0]).q", joint
                                ),
                            )[1]
                            expected = start + math.floor(delta / step + 0.5) * step
                            assert target == pytest.approx(expected, abs=0.2)
                            wait(screen).until(
                                lambda _: abs(joint_deg(joint) - target) < 0.2
                            )
                            evidence.capture(
                                f"0{joint + 1}-joint-{joint + 1}-delta-{delta}"
                            )
                            ActionChains(screen.selenium).release().perform()
                            joint_settled(joint)
                for name, camera in [
                    ("06-close", [0.16, -0.18, 0.22, 0, 0, 0.11]),
                    ("07-distant", [0.75, -0.85, 1.05, 0, 0.06, 0.13]),
                    ("08-overhead", [0.03, -0.03, 0.9, 0, 0, 0.11]),
                    ("09-shallow", [0.5, -0.5, 0.13, 0, 0, 0.11]),
                    ("10-viewport-edge", [0.02, -0.76, 0.5, -0.34, -0.28, 0.13]),
                ]:
                    scene_js(screen, "S.setCameraPose(arguments[0])", camera)
                    grab_ring(screen, "link:L1", 0)
                    wait_for_scene_idle(screen)
                    assert not scene_js(
                        screen,
                        """
                        const p=getElement(S.root).core.pointer;
                        const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
                        const badges=[p.badge.element.firstChild].filter(visible);
                        const labels=[p.ring.label,...p.ring.scaleLabels].map(l=>l.element).filter(visible);
                        return badges.some(b=>labels.some(l=>{
                          const a=b.getBoundingClientRect(),c=l.getBoundingClientRect();
                          return a.left<c.right&&a.right>c.left&&a.top<c.bottom&&a.bottom>c.top;
                        }));
                        """,
                    ), name
                    evidence.capture(name)
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()

    def test_joint_dial_recovery(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        speed = waldoctl.commander.settings.jog.speed
        with viewport(screen, 1366, 768), camera_kept(screen):
            evidence = VisualEvidence(screen, "joint-dial-recovery")
            try:
                ActionChains(screen.selenium).send_keys(
                    "[" * (int(speed / 10) - 1)
                ).perform()
                wait(screen).until(
                    lambda _: waldoctl.commander.settings.jog.speed == 10
                )
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.234, -0.252, 0.37, 0, 0.06, 0.13],
                )
                wait_for_scene_idle(screen)
                # Far enough that the arm, at 10 %, is still on its way when
                # the target is read, on a page that delivers a move a frame.
                drag_dial(screen, -100, duration=5, steps=3)
                target = urdf_to_panel(0, scene_js(screen, "return S.dial(0).q"))[1]
                assert abs(joint_deg(0) - target) > 5
                evidence.capture("01-target-ahead-of-robot")
                ActionChains(screen.selenium).release().perform()
                grab_ring(screen, "link:L1", 0)
                wait(screen).until(
                    lambda _: "actual"
                    in scene_js(screen, "return S.label('jog:dial:0:label')")
                )
                evidence.capture("02-released-following-actual")
                wait(screen).until(lambda _: abs(joint_deg(0) - target) < 0.2)
                evidence.capture("03-release-settled")

                drag_dial(screen, 100, duration=5, steps=3)
                cancelled = urdf_to_panel(0, scene_js(screen, "return S.dial(0).q"))[1]
                assert abs(joint_deg(0) - cancelled) > 5
                evidence.capture("04-before-escape")
                assert abs(joint_deg(0) - cancelled) > 5
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                wait(screen).until(
                    lambda _: not scene_js(
                        screen, "return S.byName('jog:dial:0:sweep')?.visible"
                    )
                )
                evidence.capture("05-escape-returned-to-actual")
                ActionChains(screen.selenium).release().perform()
                joint_settled(0)
                grab_ring(screen, "link:L1", 0)
                evidence.capture("06-cancel-settled")

                for joint, start, deltas in [(0, -115, [-20, 10]), (2, 175, [10, -10])]:
                    pose = [85, -85, 135, 10, 45, 170]
                    pose[joint] = start
                    # The last release would otherwise keep driving the arm.
                    run_in_app(ui_state.control_panel.cancel_drags)
                    asyncio.run_coroutine_threadsafe(
                        ui_state.control_panel.client.teleport(pose), core.loop
                    ).result(10)
                    wait(screen).until(lambda _: abs(joint_deg(joint) - start) < 0.2)
                    wait_for_scene_idle(screen)
                    for delta in deltas:
                        drag_dial(screen, delta, joint)
                        q = scene_js(screen, "return S.dial(arguments[0]).q", joint)
                        accepted = urdf_to_panel(joint, q)[1]
                        expected = (
                            max(
                                urdf_to_panel(
                                    joint,
                                    scene_js(
                                        screen, "return S.dial(arguments[0]).lo", joint
                                    ),
                                )[1],
                                start + delta,
                            )
                            if joint == 0
                            else start + delta
                        )
                        assert accepted == pytest.approx(expected, abs=1.1)
                        wait(screen).until(
                            lambda _: abs(joint_deg(joint) - accepted) < 0.2
                        )
                        evidence.capture(f"07-joint-{joint + 1}-to-{round(accepted)}")
                        ActionChains(screen.selenium).release().perform()
                        joint_settled(joint)
                        start = accepted
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()
                ActionChains(screen.selenium).send_keys(
                    "]" * (int(speed / 10) - 1)
                ).perform()
                wait(screen).until(
                    lambda _: waldoctl.commander.settings.jog.speed == speed
                )
                teleport_jog_pose()

    def test_joint_dial_review(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        teleport_jog_pose()
        with viewport(screen, 1920, 1080), camera_kept(screen):
            scene_js(
                screen,
                "S.setCameraPose(arguments[0])",
                [0.234, -0.252, 0.37, 0, 0.06, 0.13],
            )
            wait_for_scene_idle(screen)
            evidence = VisualEvidence(screen, "joint-dial-review")
            try:
                grab_ring(screen, "link:L1", 0)
                evidence.capture("01-travel-scale-1920")
                start = joint_settled(0)
                camera = camera_pose(screen)

                assert_dial_readable(screen)

                _, sign, _ = ui_state.urdf_scene._panel_mapping(0)
                drag_dial(screen, -25 * sign)
                target = scene_js(screen, "return S.dial(0)")
                target_deg = urdf_to_panel(0, target["q"])[1]
                assert target_deg == pytest.approx(start - 25, abs=1.1)
                evidence.capture("02a-while-catching-up-1920")
                assert (
                    "target"
                    in scene_js(screen, "return S.label('jog:dial:0:label')").lower()
                )
                wait(screen).until(
                    lambda _: abs(joint_deg(0) - target_deg) < 0.2,
                    message=f"start={start}, sign={sign}, dial={target}",
                )
                evidence.capture("02-start-current-and-delta-1920")
                assert camera_pose(screen) == pytest.approx(camera)
                label = scene_js(screen, "return S.label('jog:dial:0:label')")
                assert f"Δ −{start - target_deg:.1f}°" in label, label
                ActionChains(screen.selenium).release().perform()
                assert joint_settled(0) == pytest.approx(target_deg, abs=0.2)

                drag_dial(screen, 15 * sign)
                second_deg = urdf_to_panel(
                    0, scene_js(screen, "return S.dial(0)")["q"]
                )[1]
                assert second_deg == pytest.approx(target_deg + 15, abs=1.1)
                wait(screen).until(lambda _: abs(joint_deg(0) - second_deg) < 0.2)
                evidence.capture("03-positive-delta-1920")
                label = scene_js(screen, "return S.label('jog:dial:0:label')")
                assert f"Δ +{second_deg - target_deg:.1f}°" in label, label
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                wait(screen).until(
                    lambda _: not scene_js(
                        screen, "return S.byName('jog:dial:0:sweep')?.visible"
                    )
                )
                ActionChains(screen.selenium).release().perform()
                assert camera_pose(screen) == pytest.approx(camera)

                teleport_jog_pose()
                wait_for_scene_idle(screen)
                _, shoulder_sign, _ = ui_state.urdf_scene._panel_mapping(1)
                drag_dial(screen, 8 * shoulder_sign, joint=1)
                shoulder = urdf_to_panel(1, scene_js(screen, "return S.dial(1).q"))[1]
                wait(screen).until(lambda _: abs(joint_deg(1) - shoulder) < 0.2)
                evidence.capture("05-shoulder-dial-1920")
                ActionChains(screen.selenium).release().perform()
                joint_settled(1)
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()
