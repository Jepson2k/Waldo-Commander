"""Target preview ownership and structural source editing in the real workspace."""

import ast

import numpy as np
import pytest
import waldoctl
from scipy.spatial.transform import Rotation
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    focus_editor,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import no_visible, wait
from tests.helpers.review_scenes import framed_program
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    end_target_edit,
    marker_mm,
    TCP_VIEW,
    at_start,
    camera_kept,
    choose,
    field_number,
    ball_mm,
    gizmo_field,
    gizmo_choice,
    gizmo,
    hold,
    probe,
    program_previewed,
    reset_target_edit,
    settled_element,
)
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence, enabled
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state
from waldo_commander.services.scene_source import target_binding


@pytest.mark.browser
class TestSceneEditing:
    def test_tool_aligned_target_drag_writes_world_pose(self, class_screen, request):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        scene = ui_state.urdf_scene
        source = (
            framed_program(request.getfixturevalue("tmp_path"))
            if enabled()
            else EDIT_PROGRAM
        )
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(source) as targets,
        ):
            evidence = VisualEvidence(screen, "gizmo-frames")
            try:
                scene_js(screen, "S.setCameraPose(arguments[0])", TCP_VIEW)
                wait_for_scene_idle(screen)
                choose(
                    screen,
                    *scene_object_pixel(screen, f"targetgroup:{targets[-1]}"),
                    "Edit Target...",
                )
                wait(screen).until(lambda _: gizmo(screen, "tcp:ball"))
                if evidence.enabled:
                    screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-gizmo-frame"
                    ).click()
                    settled_element(
                        screen,
                        lambda: marked_element(
                            screen, "scene-gizmo-align-frame:Tilted-nest"
                        ),
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("01-frame-menu-1920")
                    click_marked(screen, "scene-gizmo-align-tool")
                    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                else:
                    handle = wait(screen).until(
                        lambda _: probe(screen, "arrowTip", "tcp:ball")
                    )
                    gizmo_choice(screen, "align-tool", at=handle)
                initial = np.array(run_in_app(lambda: scene._requested_edit_pose))
                tool_x = Rotation.from_euler("XYZ", initial[3:]).as_matrix()[:, 0]
                assert abs(tool_x[0]) < 0.9
                wait(screen).until(
                    lambda _: gizmo(screen, "tcp:ball")["frame"] == "Tool"
                )
                wait_for_scene_idle(screen)
                assert run_in_app(lambda: ui_state.active_textarea.value) == source
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball", "X")
                )
                hold(screen, spot, 20)
                delta = ball_mm(screen) / 1000 - initial[:3]
                assert np.linalg.norm(delta) > 0.001
                assert np.linalg.norm(np.cross(delta, tool_x)) < 0.0002
                assert (
                    abs(
                        field_number(gizmo_field(screen)["value"]) / 1000
                        - np.dot(delta, tool_x)
                    )
                    < 0.0002
                )
                ActionChains(screen.selenium).release().perform()
                updated = wait(screen).until(
                    lambda _: (
                        value := run_in_app(lambda: ui_state.active_textarea.value)
                    )
                    != source
                    and value
                )
                line = run_in_app(lambda: scene._edit_line)
                binding = target_binding(updated, line, 6)
                values = np.array([ast.literal_eval(e) for e in binding.vector.elts])
                assert values[:3] / 1000 == pytest.approx(
                    initial[:3] + delta, abs=0.0002
                )
                assert Rotation.from_euler(
                    "XYZ", values[3:], degrees=True
                ).as_matrix() == pytest.approx(
                    Rotation.from_euler("XYZ", initial[3:]).as_matrix(), abs=1e-4
                )
                if evidence.enabled:
                    screen.selenium.find_element(
                        By.CSS_SELECTOR, ".wc-gizmo-frame"
                    ).click()
                    settled_element(
                        screen,
                        lambda: marked_element(
                            screen, "scene-gizmo-align-frame:Tilted-nest"
                        ),
                    ).click()
                    wait(screen).until(
                        lambda _: gizmo(screen, "tcp:ball")["frame"] == "Tilted-nest"
                    )
                    wait(screen).until(
                        lambda _: scene_js(
                            screen, "return S.byName('gizmo:aligned-frame')?.visible"
                        )
                    )
                    wait_for_scene_idle(screen)
                    wait(screen).until(lambda _: no_visible(screen, ".q-menu"))
                    evidence.capture("02-named-frame-axes-1920")
                    gizmo_choice(screen, "align-world")
                    evidence.finish()
                end_target_edit(screen)
            finally:
                evidence.stop()
                reset_target_edit(screen)

    def test_keep_tool_vertical_rewrites_once_and_undo_restores(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        scene = ui_state.urdf_scene
        # A near-vertical tool with wrist travel left for the snap to vertical.
        pose = run_in_app(
            lambda: scene._ensure_ik_solver()
            .forward_kinematics(np.radians([90, -75, 210, 0, 70, 180]))
            .copy()
        )
        literal = [float(v) for v in pose[:3] * 1000] + [
            float(v) for v in np.degrees(pose[3:])
        ]
        source = (
            "from parol6 import RobotClient\n"
            "rbt = RobotClient()\n"
            "rbt.move_j([90, -85, 210, 0, 65, 180], speed=0.5)\n"
            f"rbt.move_j(pose={literal!r}, speed=0.5)\n"
        )
        view = [*(np.array(TCP_VIEW[:3]) - TCP_VIEW[3:] + pose[:3]), *pose[:3]]
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(source) as targets,
        ):
            evidence = VisualEvidence(screen, "gizmo-alignment")
            try:
                reason = run_in_app(lambda: scene._target_preview_reason(targets[-1]))
                assert reason is None, reason
                assert not run_in_app(lambda: scene._edit_active)
                scene_js(screen, "S.setCameraPose(arguments[0])", view)
                wait_for_scene_idle(screen)
                choose(
                    screen,
                    *scene_object_pixel(screen, f"targetgroup:{targets[-1]}"),
                    "Edit Target...",
                )
                wait(screen).until(lambda _: gizmo(screen, "tcp:ball"))
                line = run_in_app(lambda: scene._edit_line)
                gizmo_choice(screen, "align-tool")
                gizmo_choice(screen, "vertical")
                updated = wait(screen).until(
                    lambda _: (
                        value := run_in_app(lambda: ui_state.active_textarea.value)
                    )
                    != source
                    and value
                )
                binding = target_binding(updated, line, 6)
                values = [ast.literal_eval(e) for e in binding.vector.elts]
                rotation = Rotation.from_euler(
                    "XYZ", values[3:], degrees=True
                ).as_matrix()
                assert np.degrees(np.arccos(np.clip(-rotation[2, 2], -1, 1))) < 0.5
                assert values[:3] == pytest.approx(literal[:3], abs=0.1)
                wait(screen).until(
                    lambda _: gizmo(screen, "tcp:ball")["rotationAxes"] == ["Z"]
                )
                assert gizmo(screen, "tcp:ball")["rotationDirections"][
                    "Z"
                ] == pytest.approx([0, 0, 1], abs=1e-6)
                assert probe(screen, "ringSpot", "X") is None
                assert probe(screen, "ringSpot", "Y") is None
                evidence.capture("01-vertical-target")
                gizmo_choice(screen, "vertical")
                wait(screen).until(
                    lambda _: gizmo(screen, "tcp:ball")["rotationAxes"]
                    == ["X", "Y", "Z"]
                )
                assert run_in_app(lambda: ui_state.active_textarea.value) == updated
                end_target_edit(screen)
                click_tab(screen, "program")
                focus_editor(screen).send_keys(Keys.CONTROL, "z")
                wait(screen).until(
                    lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                    == source
                )
                evidence.finish()
            finally:
                evidence.stop()
                reset_target_edit(screen)
                if screen.selenium.find_elements(By.CSS_SELECTOR, ".program-panel"):
                    close_panel(screen, "program-panel")

    def test_preview_cancel_commit_and_source_undo(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        scene = ui_state.urdf_scene
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(EDIT_PROGRAM) as targets,
        ):
            evidence = VisualEvidence(screen, "target-editing")
            try:
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    TCP_VIEW,
                )
                wait_for_scene_idle(screen)
                choose(
                    screen,
                    *scene_object_pixel(screen, f"targetgroup:{targets[-1]}"),
                    "Edit Target...",
                )
                wait(screen).until(lambda _: run_in_app(lambda: scene._edit_active))
                initial = run_in_app(lambda: scene._requested_edit_pose)
                live = waldoctl.commander.status.joints.angles.rad.copy()
                timeline = run_in_app(
                    lambda: waldoctl.commander.programs.active.dry_run.playback.playback_time
                )
                sequence = scene_js(
                    screen,
                    """
                    const i=getElement(S.root).core.inspection;
                    const block=i.data.records[0].blocks.findIndex(b=>!b.stationary);
                    i.current={record:0,block,row:i.data.records[0].blocks[block].start};
                    i.send(true);return i.seq;
                """,
                )
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene.inspection.sequence) >= sequence
                )
                wait(screen).until(
                    lambda _: not scene_js(screen, "return S.inspection().pinned")
                )
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                assert (
                    run_in_app(
                        lambda: waldoctl.commander.programs.active.dry_run.playback.playback_time
                    )
                    == timeline
                )
                assert run_in_app(lambda: scene._requested_edit_pose) == initial
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, spot, 20)
                # The drag moves the target and its marker; the arm stays put.
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose) != initial
                )
                wait(screen).until(
                    lambda _: np.linalg.norm(
                        ball_mm(screen)
                        - np.array(run_in_app(lambda: scene._requested_edit_pose[:3]))
                        * 1000
                    )
                    < 0.5
                )
                # The target's own marker rides the drag; no other ball is drawn.
                marker = f"targetgroup:{targets[-1]}"
                wait(screen).until(
                    lambda _: np.linalg.norm(
                        marker_mm(screen, marker) - ball_mm(screen)
                    )
                    < 0.5
                )
                assert not scene_js(screen, "return S.byName('tcp:ball').visible")
                wait_for_scene_idle(screen)
                evidence.capture("01-preview-drag-1920")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: run_in_app(lambda: scene._requested_edit_pose) == initial
                )
                assert run_in_app(lambda: scene._edit_active)
                wait(screen).until(
                    lambda _: np.linalg.norm(
                        marker_mm(screen, marker) - np.array(initial[:3]) * 1000
                    )
                    < 0.5
                )
                evidence.capture("02-cancelled-drag-1920")

                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, spot, 20)
                if evidence.enabled:
                    with viewport(screen, 1366, 768):
                        wait_for_scene_idle(screen)
                        evidence.capture("03-preview-drag-1366")
                wait_for_scene_idle(screen)
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('tcp:ticks')")
                )
                # The release is kept: written into the program at once.
                released = wait(screen).until(
                    lambda _: (
                        value := run_in_app(lambda: ui_state.active_textarea.value)
                    )
                    != EDIT_PROGRAM
                    and value
                )
                assert run_in_app(lambda: scene._edit_active)
                # The drag's value, retyped, replaces the drag: the target ends
                # exactly that far from where it started.
                dragged = wait(screen).until(lambda _: gizmo_field(screen))
                assert not dragged["typing"] and dragged["unit"] == "mm", dragged
                screen.selenium.find_element(
                    By.CSS_SELECTOR,
                    ".wc-gizmo-field:not([hidden]) .wc-gizmo-field-value",
                ).click()
                wait(screen).until(lambda _: (f := gizmo_field(screen)) and f["typing"])
                assert field_number(gizmo_field(screen)["input"]) == pytest.approx(
                    field_number(dragged["value"])
                )
                ActionChains(screen.selenium).key_down(Keys.CONTROL).send_keys(
                    "a"
                ).key_up(Keys.CONTROL).send_keys("25", Keys.ENTER).perform()
                wait(screen).until(
                    lambda _: np.linalg.norm(
                        np.subtract(
                            run_in_app(lambda: scene._requested_edit_pose[:3]),
                            initial[:3],
                        )
                    )
                    == pytest.approx(0.025, abs=0.0002)
                )
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                updated = wait(screen).until(
                    lambda _: (
                        value := run_in_app(lambda: ui_state.active_textarea.value)
                    )
                    != released
                    and value
                )
                end_target_edit(screen)
                assert updated != EDIT_PROGRAM
                assert (
                    "# TCP position · mm" in updated
                    and "# Tool orientation · °" in updated
                )
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                click_tab(screen, "program")
                wait_for_scene_idle(screen)
                expected_anchors = run_in_app(
                    lambda: dict(ui_state.active_textarea.line_anchors)
                )
                editor_id = run_in_app(lambda: ui_state.active_textarea.id)
                wait(screen).until(
                    lambda _: screen.selenium.execute_script(
                        "return getElement(arguments[0]).currentAnchorPositions()",
                        editor_id,
                    )
                    == expected_anchors
                )
                assert len(expected_anchors) == 2
                evidence.capture("04-committed-source-1920")
                # Each write is its own undo step.
                editor = focus_editor(screen)
                for expected in (released, EDIT_PROGRAM):
                    editor.send_keys(Keys.CONTROL, "z")
                    wait(screen).until(
                        lambda _, expected=expected: run_in_app(
                            lambda: ui_state.active_textarea.value
                        )
                        == expected
                    )
                evidence.capture("05-undo-1920")
                for expected in (released, updated):
                    editor.send_keys(Keys.CONTROL, Keys.SHIFT, "z")
                    wait(screen).until(
                        lambda _, expected=expected: run_in_app(
                            lambda: ui_state.active_textarea.value
                        )
                        == expected
                    )
                evidence.capture("06-redo-1920")
                evidence.finish()
            finally:
                evidence.stop()
                reset_target_edit(screen)
                # A failure before the panel opened must not hide behind this.
                if screen.selenium.find_elements(By.CSS_SELECTOR, ".program-panel"):
                    close_panel(screen, "program-panel")
