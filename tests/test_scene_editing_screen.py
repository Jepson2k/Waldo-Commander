"""Target preview ownership and structural source editing in the real workspace."""

import ast
import math

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from tests.helpers.browser_helpers import (
    click_marked,
    click_tab,
    close_panel,
    focus_editor,
    run_in_app,
    viewport,
)
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    assert_dial_readable,
    at_start,
    camera_kept,
    choose,
    drag_dial,
    grab_ring,
    hold,
    probe,
    program_previewed,
)
from tests.helpers.scene_surface import (
    scene_js,
    scene_object_pixel,
    wait_for_scene_idle,
)
from tests.helpers.visual_evidence import VisualEvidence
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state


@pytest.mark.browser
class TestSceneEditing:
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
                    [0.55, 0.65, 0.65, 0, 0.17, 0.16],
                )
                wait_for_scene_idle(screen)
                choose(
                    screen,
                    *scene_object_pixel(screen, f"targetgroup:{targets[-1]}"),
                    "Edit Target...",
                )
                wait(screen).until(lambda _: run_in_app(lambda: scene._edit_active))
                initial = run_in_app(scene.get_editing_angles)
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
                assert run_in_app(scene.get_editing_angles) == pytest.approx(initial)
                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, spot, 20)
                wait(screen).until(
                    lambda _: not np.allclose(
                        run_in_app(scene.get_editing_angles), initial
                    )
                )
                wait_for_scene_idle(screen)
                evidence.capture("01-preview-drag-1920")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: np.allclose(run_in_app(scene.get_editing_angles), initial)
                )
                assert run_in_app(lambda: scene._edit_active)
                evidence.capture("02-cancelled-drag-1920")

                spot = wait(screen).until(
                    lambda _: probe(screen, "arrowTip", "tcp:ball")
                )
                hold(screen, spot, 20)
                with viewport(screen, 1366, 768):
                    wait_for_scene_idle(screen)
                    evidence.capture("03-preview-drag-1366")
                wait_for_scene_idle(screen)
                ActionChains(screen.selenium).release().perform()
                wait(screen).until(
                    lambda _: not scene_js(screen, "return !!S.byName('tcp:ticks')")
                )
                click_marked(screen, "edit-bar-confirm")
                wait(screen).until(lambda _: not run_in_app(lambda: scene._edit_active))
                updated = run_in_app(lambda: ui_state.active_textarea.value)
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
                editor = focus_editor(screen)
                editor.send_keys(Keys.CONTROL, "z")
                wait(screen).until(
                    lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                    == EDIT_PROGRAM
                )
                evidence.capture("05-undo-1920")
                editor.send_keys(Keys.CONTROL, Keys.SHIFT, "z")
                wait(screen).until(
                    lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                    == updated
                )
                evidence.capture("06-redo-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()
                if run_in_app(lambda: scene._edit_active):
                    click_marked(screen, "edit-bar-cancel")
                close_panel(screen, "program-panel")

    def test_joint_preview_dial_cancel_and_commit(self, class_screen):
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        at_start(screen)
        scene = ui_state.urdf_scene
        with (
            viewport(screen, 1920, 1080),
            camera_kept(screen),
            program_previewed(EDIT_PROGRAM) as targets,
        ):
            evidence = VisualEvidence(
                screen, "target-joint-hover", synchronized_frames=True
            )
            try:
                scene_js(
                    screen,
                    "S.setCameraPose(arguments[0])",
                    [0.36, -0.42, 0.5, 0, 0.06, 0.13],
                )
                wait_for_scene_idle(screen)
                choose(
                    screen,
                    *scene_object_pixel(screen, f"targetgroup:{targets[-1]}"),
                    "Edit Target...",
                )
                wait(screen).until(lambda _: run_in_app(lambda: scene._edit_active))
                initial = run_in_app(scene.get_editing_angles)
                live = waldoctl.commander.status.joints.angles.rad.copy()
                grab_ring(screen, "link:L1", 0)
                wait(screen).until(
                    lambda _: scene_js(screen, "return S.glowing('link:L1')")
                )
                assert_dial_readable(screen)
                evidence.capture("01-preview-dial-1920")
                drag_dial(screen, 15)
                wait(screen).until(
                    lambda _: abs(run_in_app(scene.get_editing_angles)[0] - initial[0])
                    > 0.1
                )
                assert_dial_readable(screen)
                evidence.capture("02-preview-drag-1920")
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                ActionChains(screen.selenium).send_keys(Keys.ESCAPE).release().perform()
                wait(screen).until(
                    lambda _: np.allclose(run_in_app(scene.get_editing_angles), initial)
                )
                evidence.capture("03-cancelled-drag-1920")
                with viewport(screen, 1366, 768):
                    grab_ring(screen, "link:L1", 0)
                    wait_for_scene_idle(screen)
                    drag_dial(screen, 15)
                    assert_dial_readable(screen)
                    evidence.capture("04-preview-drag-1366")
                    ActionChains(screen.selenium).release().perform()
                    accepted_delta = (
                        run_in_app(scene.get_editing_angles)[0] - initial[0]
                    )
                    click_marked(screen, "edit-bar-confirm")
                    wait(screen).until(
                        lambda _: not run_in_app(lambda: scene._edit_active)
                    )
                source = run_in_app(lambda: ui_state.active_textarea.value)
                assert "rbt.move_l(" in source and "# TCP position · mm" in source
                call = ast.parse(source).body[-1].value
                pose = ast.literal_eval(
                    next(k.value for k in call.keywords if k.arg == "pose")
                )
                assert pose[:2] == pytest.approx(
                    [-280 * math.sin(accepted_delta), 280 * math.cos(accepted_delta)],
                    abs=0.1,
                )
                assert abs(pose[0]) > 10
                assert waldoctl.commander.status.joints.angles.rad == pytest.approx(
                    live
                )
                wait_for_scene_idle(screen)
                evidence.capture("05-committed-1920")
                evidence.finish()
            finally:
                evidence.stop()
                ActionChains(screen.selenium).release().perform()
                if run_in_app(lambda: scene._edit_active):
                    click_marked(screen, "edit-bar-cancel")
