"""Browser regression tests for editor fixes that need real keystrokes.

These cover three regressions that cannot be exercised with the user
fixture because the bugs live behind real key event delivery through
CodeMirror's keymap and NiceGUI's slot/event dispatch:

1. Ctrl+S → on_save → _save_tab — happy path writes to disk
2. Ctrl+S → on_save → _save_tab → ui.notify — failure path shows toast
   (regression for the asyncio.create_task slot-context bug)
3. Typing rbt.move outside parens — completion popup appears
   (regression for the CM.lintGutter() suppressing autocomplete)

The Ctrl+S failure-path test sets a tab filename containing a null byte;
if cleanup ever fails partway it would corrupt downstream tests, so this
class lives in its own file with its own class_screen session rather than
extending TestEditorInteractivity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import (
    click_tab,
    focus_editor,
    get_autocomplete_labels,
    js,
    wait_for_autocomplete,
    wait_for_codemirror_ready,
    wait_for_notification,
)
from tests.helpers.programs import set_active_recording

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


@pytest.fixture(autouse=True, scope="module")
def _clean_stale_state():
    """Reset module-level globals that persist across nicegui_reset_globals().

    Mirrors the pattern in tests/test_editor_visualization.py:25-44 — earlier
    test classes (TestEditorInteractivity, TestEditorVisualization) may leave
    recording enabled or tabs with modified content, and these singletons
    are NOT reset by NiceGUI's test infrastructure between classes.

    Symmetric: cleans on both setup AND teardown so subsequent modules
    can't be polluted by anything our tests left behind in the editor
    tab state.
    """
    import waldoctl

    from waldo_commander.state import (
        simulation_state,
    )

    def _reset() -> None:
        set_active_recording(False)
        waldoctl.commander.programs.items.clear()
        waldoctl.commander.programs.active_id = None
        simulation_state.path_segments.clear()
        simulation_state.targets.clear()

    _reset()
    yield
    _reset()


def _set_editor_content(screen: "Screen", text: str) -> None:
    """Replace the active CodeMirror doc via dispatch — used to clear the
    editor before tests that need a known starting state. The autocomplete
    test deliberately does NOT use this for its actual input; that path
    needs real keystrokes via send_keys."""
    js(
        screen,
        """
        const text = arguments[0];
        const cm = document.querySelector('.cm-content');
        if (!cm || !cm.cmView || !cm.cmView.view) return;
        const view = cm.cmView.view;
        view.dispatch({
            changes: {from: 0, to: view.state.doc.length, insert: text}
        });
        """,
        text,
    )


@pytest.mark.browser
class TestEditorRegressions:
    """Editor regression tests sharing a single browser session."""

    def test_ctrl_s_saves_active_tab_to_disk(self, class_screen: "Screen") -> None:
        """Pressing Ctrl+S inside the editor should write the tab to disk.

        Confirms the on_save lambda → _save_tab coroutine chain completes
        when triggered via real Ctrl+S keystrokes — guards against any
        rewiring of on_save that would break the coroutine handoff.
        """
        from waldo_commander.state import ui_state
        import waldoctl

        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        active_tab = waldoctl.commander.programs.active
        assert active_tab is not None, "expected an active tab after opening program"

        original_filename = active_tab.filename
        original_content = active_tab.source
        target_name = "regression_ctrl_s_test.py"
        target_content = "# ctrl-s regression test\n"
        target_path = ui_state.editor_panel.PROGRAM_DIR / target_name
        active_tab.filename = target_name
        # Push the content through CodeMirror so the editor's doc and
        # tab.source stay in sync. Setting tab.source from Python alone
        # races with the editor's on_change sync — on a slow CI runner the
        # editor's empty initial value can clobber our update before
        # _save_tab reads it, and the file ends up empty.
        _set_editor_content(class_screen, target_content)

        try:
            cm_content = focus_editor(class_screen)
            cm_content.send_keys(Keys.CONTROL + "s")

            # Wait for the file to *contain* the expected content, not just
            # to exist. The save creates the file before writing content, so
            # a bare exists() check races against the open-then-write window
            # and observed-empty reads on slow CI runners (the failing
            # ubuntu/3.13 + 3.14 jobs that prompted this fix).
            def _has_target_content(_d: object) -> bool:
                try:
                    return (
                        target_path.exists()
                        and target_path.read_text() == target_content
                    )
                except OSError:
                    return False

            WebDriverWait(class_screen.selenium, 10).until(_has_target_content)
            assert target_path.read_text() == target_content
        finally:
            target_path.unlink(missing_ok=True)
            active_tab.filename = original_filename
            # Restore content via the same dispatch path so the editor doc
            # matches the restored tab.source (otherwise the next test
            # would see a stale editor doc).
            _set_editor_content(class_screen, original_content)

    def test_ctrl_s_failure_shows_error_notification(
        self, class_screen: "Screen"
    ) -> None:
        """Pressing Ctrl+S when the save will fail should pop a toast.

        Load-bearing regression for the asyncio.create_task slot-context
        bug: if _save_tab is wrapped in create_task again, the failure
        path's ui.notify raises RuntimeError from context.client (because
        the spawned task has an empty slot stack), and the notification
        is silently swallowed by Python's "Task exception was never
        retrieved" warning.

        We force the failure by setting the active tab's filename to a
        path containing a null byte, which makes Path.write_text raise
        ValueError("embedded null byte").
        """
        import waldoctl

        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        active_tab = waldoctl.commander.programs.active
        assert active_tab is not None

        original_filename = active_tab.filename
        active_tab.filename = "regression\x00invalid.py"
        try:
            cm_content = focus_editor(class_screen)
            cm_content.send_keys(Keys.CONTROL + "s")

            wait_for_notification(class_screen, "Save failed:", timeout=5.0)
        finally:
            active_tab.filename = original_filename

    def test_autocomplete_popup_appears_outside_parens(
        self, class_screen: "Screen"
    ) -> None:
        """Typing `rbt.move` at the top level should show the completion popup
        with `rbt.move_j` and `rbt.move_l` entries.

        Regression for the CM.lintGutter() bug — when lintGutter is in the
        extensions array, lintGutterTooltip's null showTooltip provider
        suppresses the popup outside paren contexts. The fix replaces it
        with CM.linter(() => []) which keeps lintState working without the
        suppressing tooltip provider.
        """
        import waldoctl

        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        active_tab = waldoctl.commander.programs.active
        assert active_tab is not None
        original_content = active_tab.source

        # Clear via dispatch so we start from a known empty state. The
        # actual test typing must use real send_keys (below) so it goes
        # through CodeMirror's keymap and triggers autocomplete activation.
        _set_editor_content(class_screen, "")

        cm_content = focus_editor(class_screen)
        try:
            cm_content.send_keys("rbt.move")

            wait_for_autocomplete(class_screen, timeout=5.0)
            labels = get_autocomplete_labels(class_screen)

            assert any("rbt.move_j" in label for label in labels), (
                f"expected rbt.move_j in completion labels, got: {labels}"
            )
            assert any("rbt.move_l" in label for label in labels), (
                f"expected rbt.move_l in completion labels, got: {labels}"
            )
        finally:
            # Restore original content so a subsequent test class that
            # opens its own browser sees the same starter program.
            _set_editor_content(class_screen, original_content)
