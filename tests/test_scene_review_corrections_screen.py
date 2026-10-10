"""Ground placement and native movement controls through the actual workspace."""

import json

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_tab,
    close_panel,
    click_marked,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    PATH_PROGRAM,
    PATH_VIEW,
    at_start,
    camera_kept,
    choose,
    click_spot,
    frame_mm,
    gizmo_field,
    grab_ring,
    hold,
    path_point,
    probe,
    program_previewed,
    zoom,
)
from tests.helpers.scene_surface import pointer_to, scene_js, wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode
from waldo_commander.state import ui_state


def _marker_missed(screen) -> bool:
    """Whether the edited target's marker is drawn in the miss colour."""
    return scene_js(
        screen,
        "const c=getElement(S.root).core,g=c.gizmos,color=g.ball.material.color;"
        "return g.miss && color.getHex() === color.clone().set(c.ix.colors.miss).getHex();",
    )


@pytest.mark.browser
class TestReviewCorrections:
    def test_ground_preview_native_handles_and_gpu(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        scene = ui_state.urdf_scene
        live = waldoctl.commander.status.joints.angles.rad.copy()
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(EDIT_PROGRAM),
        ):
            evidence = VisualEvidence(
                screen, "combined-transform/placement", synchronized_frames=True
            )
            try:
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.396, 0.516, 0.513, 0, 0.17, 0.16],
                )
                wait_for_scene_idle(screen)
                assert (
                    len(
                        screen.selenium.find_elements(
                            By.CSS_SELECTOR, ".joint-dial-svg"
                        )
                    )
                    == 6
                )
                grab_ring(screen, "link:L1", 0)
                wait(screen).until(lambda _: scene_js(screen, "return !!S.dial(0)"))
                evidence.capture("01-discoverable-joints-1920")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                x, y, gx, gy = wait(screen).until(lambda _: probe(screen, "floorSpot"))
                action = ActionChains(screen.selenium)
                pointer_to(screen, x, y, action)
                action.perform()
                wait(screen).until(
                    lambda _: scene_js(
                        screen, "return S.byName('placement:ground').visible"
                    )
                )
                # The recorded pointer would cover the ring; it exists only
                # when evidence is captured.
                screen.selenium.execute_script(
                    "const p=window.__sceneEvidencePointer;if(p)p.hidden=true"
                )
                evidence.capture("02-ground-indicator-1920")
                screen.selenium.execute_script(
                    "const p=window.__sceneEvidencePointer;if(p)p.hidden=false"
                )
                choose(
                    screen,
                    x,
                    y,
                    f"Place Target Here ({gx * 1000:.0f}, {gy * 1000:.0f})...",
                )
                wait(screen).until(lambda _: run_in_app(lambda: scene._edit_active))
                # The target starts on the floor and its gizmo sits there; the
                # arm stays where it is.
                assert run_in_app(
                    lambda: scene._requested_edit_pose[:3]
                ) == pytest.approx([gx, gy, 0], abs=0.00001)
                wait(screen).until(
                    lambda _: np.linalg.norm(
                        frame_mm(screen) - np.array([gx, gy, 0]) * 1000
                    )
                    < 0.5
                )
                assert scene._appearance_mode != RobotAppearanceMode.EDITING
                wait_for_scene_idle(screen)
                evidence.capture("03-ground-target-1920")

                before_plane = run_in_app(lambda: scene._requested_edit_pose)
                plane_axis, plane = wait(screen).until(
                    lambda _: next(
                        (
                            (a, p)
                            for a in ("XY", "XZ", "YZ")
                            if (p := probe(screen, "planeSpot", a))
                        ),
                        None,
                    )
                )
                hold(screen, plane, 18)
                wait(screen).until(
                    lambda _: not np.allclose(
                        run_in_app(lambda: scene._requested_edit_pose[:3]),
                        before_plane[:3],
                    )
                )
                evidence.capture("03b-plane-translation-1920")
                delta = (
                    np.asarray(run_in_app(lambda: scene._requested_edit_pose[:3]))
                    - before_plane[:3]
                )
                for i, axis in enumerate("XYZ"):
                    if axis in plane_axis:
                        assert abs(delta[i]) > 0.002
                    else:
                        assert abs(delta[i]) < 0.001
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose)
                    == before_plane
                )
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, spot, 20)
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose)
                    != before_plane
                )
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                evidence.capture("04-translation-1920")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose)
                    == before_plane
                )
                far = scene_js(screen, "return S.gizmo('tcp:ball').translationSnap")
                view = scene_js(screen, "return S.cameraPose()")
                zoom(screen, 0.25)
                wait(screen).until(
                    lambda _: scene_js(
                        screen, "return S.gizmo('tcp:ball').translationSnap"
                    )
                    < far
                )
                scene_js(screen, "S.setCameraPose(arguments[0])", view)
                wait_for_scene_idle(screen)
                # Home moves the target to the arm's home pose.
                click_marked(screen, "btn-home")
                home = run_in_app(
                    lambda: tuple(
                        float(v)
                        for v in scene._ik_solver.forward_kinematics(
                            ui_state.active_robot.joints.home.rad.tolist()
                        )[:6]
                    )
                )
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose)
                    == pytest.approx(home, abs=1e-6)
                )
                spot = wait(screen).until(
                    lambda _: next(
                        (p for a in "ZYX" if (p := probe(screen, "ringSpot", a))), None
                    )
                )
                hold(screen, spot, 40)
                wait(screen).until(
                    lambda _: (f := gizmo_field(screen)) and f["unit"] == "°"
                )
                evidence.capture("05-rotation-1920")
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('tcp:ticks')")
                )

                # A typed turn about an arc turns the target in place.
                before_turn = run_in_app(lambda: scene._requested_edit_pose)
                ring = wait(screen).until(
                    lambda _: next(
                        (p for a in "ZYX" if (p := probe(screen, "ringSpot", a))), None
                    )
                )
                click_spot(screen, ring)
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                ActionChains(screen.selenium).send_keys("5", Keys.ENTER).perform()
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose)
                    != before_turn
                )
                after_turn = run_in_app(lambda: scene._requested_edit_pose)
                turned = (
                    Rotation.from_euler("XYZ", after_turn[3:])
                    * Rotation.from_euler("XYZ", before_turn[3:]).inv()
                ).magnitude()
                assert np.degrees(turned) == pytest.approx(5, abs=0.2)
                assert after_turn[:3] == pytest.approx(before_turn[:3], abs=0.001)

                # Typed far out of reach: the marker turns the miss colour and
                # the target cannot be saved until it comes back.
                arrow = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                click_spot(screen, arrow)
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                ActionChains(screen.selenium).send_keys("2000", Keys.ENTER).perform()
                wait(screen).until(lambda _: run_in_app(lambda: scene._tcp_ik_missed))
                wait(screen).until(lambda _: _marker_missed(screen))
                wait(screen).until(
                    lambda _: not marked_element(
                        screen, "edit-bar-confirm"
                    ).is_enabled()
                    and marked_element(screen, "edit-bar-unreachable").is_displayed()
                )
                click_marked(screen, "btn-home")
                wait(screen).until(lambda _: not _marker_missed(screen))
                wait(screen).until(
                    lambda _: marked_element(screen, "edit-bar-confirm").is_enabled()
                    and not marked_element(
                        screen, "edit-bar-unreachable"
                    ).is_displayed()
                )
                if evidence.enabled:
                    with viewport(screen, 1366, 768):
                        zoom(screen, 0.6)
                        wait_for_scene_idle(screen)
                        evidence.capture("06-compact-1366")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                gpu = scene_js(
                    screen,
                    "const gl=S.renderer.getContext(),ext=gl.getExtension('WEBGL_debug_renderer_info');return {renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER),attributes:gl.getContextAttributes()};",
                )
                if evidence.enabled:
                    (evidence.root / "gpu.json").write_text(json.dumps(gpu, indent=2))
                assert gpu["attributes"]["powerPreference"] == "high-performance"
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()
                if run_in_app(lambda: scene._edit_active):
                    click_marked(screen, "edit-bar-cancel")

    def test_path_uses_the_simulator_and_shared_timeline(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(PATH_PROGRAM),
        ):
            evidence = VisualEvidence(screen, "review-corrections/scrubbing")
            try:
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    PATH_VIEW,
                )
                wait_for_scene_idle(screen)
                blocks = scene_js(screen, "return S.inspection().records[0].blocks")
                block = next(b for b in blocks if b["line"] == 7)
                x, y, _ = path_point(screen, block["command"], 0.7)
                before = waldoctl.commander.status.joints.angles.rad.copy()
                action = ActionChains(screen.selenium)
                pointer_to(screen, x, y, action)
                action.perform()
                wait_for_scene_idle(screen)
                assert not scene_js(screen, "return S.inspection().current")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    before
                )
                evidence.capture("01-hover-does-not-preview-1920")
                ActionChains(screen.selenium).click().perform()
                wait(screen).until(
                    lambda _: not np.allclose(
                        waldoctl.commander.status.joints.angles.rad, before
                    )
                )
                assert not screen.selenium.find_elements(
                    By.CSS_SELECTOR, ".wc-path-card"
                )
                state = scene_js(screen, "return S.inspection()")
                assert run_in_app(
                    lambda: waldoctl.commander.programs.active.dry_run.playback.playback_time
                ) == pytest.approx(state["time"])
                evidence.capture("02-simulator-scrub-1920")
                click_tab(screen, "program")
                timeline = marked_element(screen, "editor-scrub-slider")
                action = ActionChains(screen.selenium, duration=0)
                action.move_to_element_with_offset(
                    timeline, -timeline.size["width"] / 2 + 3, 0
                ).click().perform()

                # The pinned point follows the scrub bar back to near the start.
                def followed(_) -> bool:
                    shown = scene_js(screen, "return S.inspection().time")
                    playback = run_in_app(
                        lambda: waldoctl.commander.programs.active.dry_run.playback.playback_time
                    )
                    return (
                        shown is not None
                        and shown < state["time"]
                        and abs(shown - playback) < 0.05
                    )

                wait(screen).until(followed)
                evidence.capture("03-shared-timeline-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).perform()
                # A failure before the panel opened must not hide behind this.
                if screen.selenium.find_elements(By.CSS_SELECTOR, ".program-panel"):
                    close_panel(screen, "program-panel")
