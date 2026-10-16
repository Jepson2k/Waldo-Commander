"""Camera navigation, restoration, face alignment and orthographic editing in
the actual UI."""

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    end_target_edit,
    EDIT_PROGRAM,
    at_start,
    camera_action,
    camera_settled,
    choose,
    clear_selection,
    confirmed,
    create_shape,
    cube_view,
    object_mode,
    fill,
    hold,
    probe,
    program_previewed,
    set_projection,
)
from tests.helpers.scene_surface import (
    project_local,
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state


def _projection_shown(screen, name):
    """Whether the projection buttons show ``name``, and only it, as pressed."""
    projections = ("perspective", "orthographic")
    pressed = [
        marked_element(screen, f"camera-{p}").get_attribute("aria-pressed") == "true"
        for p in projections
    ]
    return pressed == [p == name for p in projections]


@pytest.fixture(scope="class")
def camera_screen(class_screen):
    with preserve_capture_buffers(class_screen):
        yield class_screen


@pytest.mark.browser
class TestCameraTools:
    def test_focus_projection_axis_restore_and_orthographic_edit(self, camera_screen):
        screen = camera_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        screen.selenium.execute_cdp_cmd(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-reduced-motion", "value": "no-preference"}
                ]
            },
        )
        scene = ui_state.urdf_scene
        live = waldoctl.commander.status.joints.angles.rad.copy()
        with viewport(screen, 1920, 1080), program_previewed(EDIT_PROGRAM) as targets:
            evidence = VisualEvidence(screen, "camera-tools", synchronized_frames=True)
            try:
                before = scene_js(screen, "return S.cameraPose()")
                camera_action(screen, "focus")
                camera_settled(screen)
                focused = scene_js(screen, "return S.cameraPose()")
                assert focused != pytest.approx(before)
                evidence.capture("01-focus-robot-1920")
                evidence.capture("02-camera-tools-1920")

                # Project a transverse model span before/after switching lenses.
                span = scene_js(
                    screen, "return S.project('link:L4', [[0,0,0],[0.03,0,0]])"
                )
                set_projection(screen, "Orthographic")
                ortho_span = scene_js(
                    screen, "return S.project('link:L4', [[0,0,0],[0.03,0,0]])"
                )
                assert np.linalg.norm(np.subtract(*ortho_span)) == pytest.approx(
                    np.linalg.norm(np.subtract(*span)), rel=0.25
                )
                evidence.capture("03-orthographic-1920")
                camera_action(screen, "top")
                camera_settled(screen)
                assert scene_js(screen, "return S.viewDirection()") == pytest.approx(
                    [0, 0, 1], abs=1e-5
                )
                evidence.capture("04-top-1920")
                camera_action(screen, "back")
                camera_settled(screen)
                assert scene_js(screen, "return S.cameraPose()") == pytest.approx(
                    focused, abs=1e-6
                )
                assert scene_js(screen, "return !!S.camera.isOrthographicCamera")
                # The same, from the button under the cube.
                click_marked(screen, "camera-back")
                camera_settled(screen)
                assert not scene_js(screen, "return !!S.camera.isOrthographicCamera")
                assert scene_js(screen, "return S.cameraPose()") == pytest.approx(
                    focused, abs=1e-6
                )
                evidence.capture("05-restored-1920")
                # From the right, with the front face only just on the cube, a
                # press on it turns the camera a long way round its target.
                scene_js(
                    screen,
                    """
                  const t=S.controls.target, d=S.camera.position.distanceTo(t);
                  const k=d/Math.hypot(1,0.5);
                  S.setCameraPose([t.x+k,t.y-0.5*k,t.z,...t.toArray()]);
                  window.__orbitDistances=[]; window.__watchOrbit=true;
                  const sample=()=>{if(!window.__watchOrbit)return;
                    window.__orbitDistances.push(S.camera.position.distanceTo(S.controls.target));
                    requestAnimationFrame(sample);}; sample();
                """,
                )
                camera_action(screen, "front")
                camera_settled(screen)
                assert scene_js(screen, "return S.viewDirection()") == pytest.approx(
                    [0, -1, 0], abs=1e-5
                )
                distances = scene_js(
                    screen, "window.__watchOrbit=false; return window.__orbitDistances"
                )
                assert min(distances) == pytest.approx(max(distances), rel=1e-5)
                evidence.capture("05b-front-alignment-1920")
                evidence.stop()

                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.396, 0.516, 0.513, 0, 0.17, 0.16],
                )
                set_projection(screen, "Orthographic")
                choose(
                    screen,
                    *scene_object_pixel(screen, f"targetgroup:{targets[-1]}"),
                    "Edit Target...",
                )
                wait(screen).until(lambda _: run_in_app(lambda: scene._edit_active))
                initial = run_in_app(lambda: scene._requested_edit_pose)
                with viewport(screen, 1366, 768):
                    wait_for_scene_idle(screen)
                    spot = wait(screen).until(
                        lambda _: probe(screen, "arrowTip", "tcp:ball")
                    )
                    hold(screen, spot, 20)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: scene._requested_edit_pose)
                        != initial
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("06-orthographic-drag-1366")
                    ActionChains(screen.selenium).send_keys(
                        Keys.ESCAPE
                    ).release().perform()
                    wait(screen).until(
                        lambda _: run_in_app(lambda: scene._requested_edit_pose)
                        == initial
                    )
                    end_target_edit(screen)
                    evidence.capture("07-camera-tools-1366")
                    click_marked(screen, "camera-reset")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                evidence.finish()
            finally:
                screen.selenium.execute_cdp_cmd(
                    "Emulation.setEmulatedMedia",
                    {
                        "features": [
                            {"name": "prefers-reduced-motion", "value": "reduce"}
                        ]
                    },
                )
                evidence.stop()
                if scene._edit_active:
                    run_in_app(scene.cancel_target_edit)

    def test_view_cube_restoration_recovery_and_touch(self, camera_screen):
        from tests.test_scene3d_screen import _touch

        screen = camera_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        with viewport(screen, 1024, 768, mobile=True):
            screen.selenium.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled",
                {"enabled": True, "maxTouchPoints": 1},
            )
            evidence = VisualEvidence(screen, "camera-touch", synchronized_frames=True)
            try:

                def tap(element):
                    rect = element.rect
                    _touch(
                        screen,
                        "touchStart",
                        [
                            (
                                1,
                                rect["x"] + rect["width"] / 2,
                                rect["y"] + rect["height"] / 2,
                            )
                        ],
                    )
                    _touch(screen, "touchEnd", [])

                def tap_cube(direction=None):
                    """Tap the cube where it gives ``direction``, or on the face
                    turned most toward the viewer; that direction made unit."""
                    x, y, unit = wait(screen).until(
                        lambda _: scene_js(
                            screen, "return S.cubeSpot(arguments[0])", direction
                        )
                    )
                    _touch(screen, "touchStart", [(1, x, y)])
                    _touch(screen, "touchEnd", [])
                    camera_settled(screen)
                    return unit

                def looking_from():
                    return scene_js(screen, "return S.viewDirection()")

                def orthographic():
                    return scene_js(screen, "return !!S.camera.isOrthographicCamera")

                tap(marked_element(screen, "camera-orthographic"))
                wait(screen).until(
                    lambda _: orthographic()
                    and _projection_shown(screen, "orthographic")
                )
                face = tap_cube()
                assert np.dot(looking_from(), face) > 0.999
                before = scene_js(
                    screen,
                    "return {pose:S.cameraPose(),q:S.camera.quaternion.toArray()}",
                )
                camera_action(screen, "focus")
                camera_settled(screen)
                # No keyboard on a tablet: the previous view is a button.
                tap(marked_element(screen, "camera-back"))
                camera_settled(screen)
                after = scene_js(
                    screen,
                    "return {pose:S.cameraPose(),q:S.camera.quaternion.toArray()}",
                )
                assert after["pose"] == pytest.approx(before["pose"], abs=1e-6)
                assert abs(np.dot(before["q"], after["q"])) == pytest.approx(
                    1, abs=1e-6
                )
                wait_for_scene_idle(screen)
                evidence.capture("01-cube-restored-touch-1024")

                # An edge of that face looks from half way between its two
                # faces, and a corner of the edge from among its three.
                axis = np.rint(face).astype(int)
                edge = axis + np.roll(np.abs(axis), 1)
                corner = edge + np.roll(np.abs(axis), 2)
                tap_cube(edge.tolist())
                assert looking_from() == pytest.approx(
                    edge / np.linalg.norm(edge), abs=1e-5
                )
                cube_view(screen, corner.tolist())
                camera_settled(screen)
                assert looking_from() == pytest.approx(
                    corner / np.linalg.norm(corner), abs=1e-5
                )
                evidence.capture("02-corner-view-touch-1024")
                evidence.stop()
                scene_js(screen, "S.renderer.forceContextLoss()")
                notice = screen.selenium.find_element(By.CSS_SELECTOR, ".wc-scene-lost")
                wait(screen, 10).until(
                    lambda _: notice.value_of_css_property("cursor") == "pointer"
                )
                notice.click()
                wait(screen, 15).until(
                    lambda _: screen.selenium.find_elements(
                        By.CSS_SELECTOR, ".wc-scene[data-ready]:not([data-gl])"
                    )
                )
                wait_for_scene_idle(screen)
                wait(screen).until(
                    lambda _: not orthographic()
                    and _projection_shown(screen, "perspective")
                )
                evidence.capture("03-recovered-tools-touch-1024")
                # The recovered view has no earlier one for Shift+F to go back
                # to. The projection switch after it ends any turn it began.
                recovered = scene_js(screen, "return S.cameraPose()")
                camera_action(screen, "back")
                tap(marked_element(screen, "camera-orthographic"))
                wait(screen).until(
                    lambda _: orthographic()
                    and _projection_shown(screen, "orthographic")
                )
                assert scene_js(screen, "return S.cameraPose()") == pytest.approx(
                    recovered, abs=1e-6
                )
                click_marked(screen, "camera-reset")
                camera_settled(screen)
                wait(screen).until(
                    lambda _: not orthographic()
                    and _projection_shown(screen, "perspective")
                )
                evidence.finish()
            finally:
                evidence.stop()
                screen.selenium.execute_cdp_cmd(
                    "Emulation.setTouchEmulationEnabled", {"enabled": False}
                )

    def test_face_alignment_projection_and_back(self, camera_screen):
        screen = camera_screen
        with viewport(screen, 1920, 1080):
            screen_wait_for_scene_ready(screen, timeout_s=40)
            at_start(screen)
            run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
            confirmed(screen, lambda: not waldoctl.commander.scene.shapes)
            with program_previewed(EDIT_PROGRAM):
                create_shape(screen, "box", "Tilted fixture")
                object_mode(screen, "Move")
                fill(screen, "object-rx", 25)
                confirmed(
                    screen, lambda: waldoctl.commander.scene.shapes[-1].pose[3] > 0.4
                )
                fixture = waldoctl.commander.scene.shapes[-1]
                clear_selection(screen)
                normal = Rotation.from_euler("xyz", fixture.pose[3:]).apply([0, 0, 1])
                origin = np.array(fixture.pose[:3]) + normal * fixture.z / 2
                evidence = VisualEvidence(
                    screen, "surface-camera", synchronized_frames=True
                )
                try:
                    initial = scene_js(screen, "return S.cameraPose()")
                    revision = waldoctl.commander.scene.revision
                    distance = np.linalg.norm(np.subtract(initial[:3], initial[3:]))
                    point = project_local(
                        screen, f"shape:{fixture.name}", [[0, 0, fixture.z / 2]]
                    )[0]
                    choose(screen, *point, "Align view to surface")
                    camera_settled(screen)
                    pose = scene_js(screen, "return S.cameraPose()")
                    assert scene_js(
                        screen, "return S.viewDirection()"
                    ) == pytest.approx(normal, abs=1e-5)
                    assert pose[3:] == pytest.approx(origin, abs=0.002)
                    assert np.linalg.norm(
                        np.subtract(pose[:3], pose[3:])
                    ) == pytest.approx(distance, abs=1e-5)
                    evidence.capture("01-aligned-face-1920")
                    set_projection(screen, "Orthographic")
                    evidence.capture("02-orthographic-face-1920")
                    camera_action(screen, "back")
                    camera_settled(screen)
                    assert not scene_js(
                        screen, "return !!S.camera.isOrthographicCamera"
                    )
                    camera_action(screen, "back")
                    camera_settled(screen)
                    assert scene_js(screen, "return S.cameraPose()") == pytest.approx(
                        initial, abs=1e-6
                    )
                    evidence.capture("03-restored-view-1920")
                    evidence.stop()
                    with viewport(screen, 1366, 768):
                        point = project_local(
                            screen, f"shape:{fixture.name}", [[0, 0, fixture.z / 2]]
                        )[0]
                        choose(screen, *point, "Align view to surface")
                        camera_settled(screen)
                        wait_for_scene_idle(screen)
                        evidence.capture("04-compact-face-view-1366")
                    assert waldoctl.commander.scene.shapes[-1] == fixture
                    assert waldoctl.commander.scene.revision == revision, (
                        "aligning the view wrote the world"
                    )
                    evidence.finish()
                finally:
                    evidence.stop()
