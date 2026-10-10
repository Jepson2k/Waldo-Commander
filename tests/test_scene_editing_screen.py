"""Target preview ownership and structural source editing in the real workspace."""

import numpy as np
import pytest
import waldoctl
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
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
    at_start,
    camera_kept,
    choose,
    field_number,
    ball_mm,
    gizmo_field,
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
                    [0.396, 0.516, 0.513, 0, 0.17, 0.16],
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
                # A failure before the panel opened must not hide behind this.
                if screen.selenium.find_elements(By.CSS_SELECTOR, ".program-panel"):
                    close_panel(screen, "program-panel")
