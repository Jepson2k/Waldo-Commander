"""Save/reload a populated workcell beside a scene that stays editable, and
insert it as one editor transaction."""

import pytest
import waldoctl
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
from tests.helpers.browser_session import wait
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    at_start,
    choose,
    click_object,
    confirmed,
    create_shape,
    object_mode,
    fill,
    probe,
    program_previewed,
)
from tests.helpers.scene_surface import wait_for_scene_idle
from tests.helpers.visual_evidence import VisualEvidence, preserve_capture_buffers
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_object_editing_screen import in_scene_panel
from waldo_commander.services import world_files
from waldo_commander.state import ui_state


def _library(screen):
    point = wait(screen).until(lambda _: probe(screen, "floorSpot"))
    choose(screen, *point[:2], "Workcell library…")
    wait(screen).until(lambda _: in_scene_panel(screen, "workcell-library"))


@pytest.mark.browser
class TestWorldLibrary:
    def test_workcell_save_reload_and_source_undo(
        self, class_screen, tmp_path, monkeypatch
    ):
        screen = class_screen
        monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
        with preserve_capture_buffers(screen), viewport(screen, 1920, 1080):
            screen_wait_for_scene_ready(screen, timeout_s=40)
            at_start(screen)
            run_in_app(lambda: setattr(waldoctl.commander.scene, "shapes", []))
            confirmed(screen, lambda: not waldoctl.commander.scene.shapes)
            handle = waldoctl.commander.scene
            with program_previewed(EDIT_PROGRAM):
                create_shape(screen, "box", "Fixture")
                fill(screen, "object-dim-x", 180)
                confirmed(screen, lambda: handle.shapes[-1].x == 0.18)
                create_shape(screen, "cylinder", "Stock")
                object_mode(screen, "Move")
                fill(screen, "object-x", 400)
                confirmed(screen, lambda: handle.shapes[-1].pose[0] == 0.4)
                fill(screen, "object-y", -0.18 * 1000)
                confirmed(screen, lambda: handle.shapes[-1].pose[1] == -0.18)
                original = list(handle.shapes)
                evidence = VisualEvidence(
                    screen, "workcell-library", synchronized_frames=True
                )
                try:
                    _library(screen)
                    # Enter saves, as the Save button beside the name does.
                    fill(screen, "workcell-name", "bench-cell")
                    wait(screen).until(
                        lambda _: "Saved bench-cell"
                        in marked_element(screen, "workcell-feedback").text
                    )
                    wait(screen).until(
                        lambda _: "Fixture, Stock"
                        in marked_element(screen, "workcell-preview:bench-cell").text
                    )
                    evidence.capture("01-saved-workcell-1920")
                    # The library is no dialog: the scene beside it stays live.
                    click_object(screen, "shape:Fixture")
                    fill(screen, "object-x", 550)
                    confirmed(screen, lambda: handle.shapes[0].pose[0] == 0.55)
                    evidence.capture("02-edited-beside-library-1920")
                    click_marked(screen, "workcell-load:bench-cell")
                    confirmed(screen, lambda: handle.shapes == original)
                    wait(screen).until(
                        lambda _: "Loaded"
                        in marked_element(screen, "workcell-note:bench-cell").text
                    )
                    evidence.capture("03-reloaded-world-1920")
                    click_tab(screen, "program")
                    editor = focus_editor(screen)
                    editor.send_keys(
                        Keys.CONTROL, Keys.HOME, Keys.NULL, Keys.ARROW_DOWN
                    )
                    wait(screen).until(
                        lambda _: run_in_app(
                            lambda: waldoctl.commander.programs.active.dry_run.playback.active_cursor_line
                        )
                        == 2
                    )
                    click_marked(screen, "workcell-insert")
                    wait(screen).until(
                        lambda _: "set_shapes("
                        in run_in_app(lambda: ui_state.active_textarea.value)
                    )
                    updated = run_in_app(lambda: ui_state.active_textarea.value)
                    assert updated.index("set_shapes(") < updated.index("rbt.move_j(")
                    assert (
                        updated.splitlines()[1] == "from waldoctl import Box, Cylinder"
                    )
                    wait_for_scene_idle(screen)
                    evidence.capture("04-inserted-source-1920")
                    editor = focus_editor(screen)
                    editor.send_keys(Keys.CONTROL, "z")
                    wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        == EDIT_PROGRAM
                    )
                    editor.send_keys(Keys.CONTROL, Keys.SHIFT, "z", Keys.NULL)
                    wait(screen).until(
                        lambda _: run_in_app(lambda: ui_state.active_textarea.value)
                        == updated
                    )
                    evidence.stop()
                    close_panel(screen, "program-panel")
                    if evidence.enabled:
                        with viewport(screen, 1366, 768):
                            evidence.capture("05-library-1366")
                            click_marked(screen, "workcell-close")
                            click_object(screen, "shape:Fixture")
                            evidence.capture("06-reloaded-inspector-1366")
                    else:
                        click_marked(screen, "workcell-close")
                    evidence.finish()
                finally:
                    evidence.stop()
