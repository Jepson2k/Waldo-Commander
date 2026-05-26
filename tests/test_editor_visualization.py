"""Browser tests for editor ↔ 3D visualization features.

Tests verify that moving the cursor in the editor highlights the
corresponding path segment in the 3D scene.

(Infeasible-timing diagnostics are covered as a non-browser unit test in
``tests/test_simulation_services.py`` against the new lint diagnostic API.)

All tests share a single browser session via class_screen fixture.
"""

from typing import TYPE_CHECKING

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import click_tab, wait_for_codemirror_ready
from tests.helpers.programs import set_active_recording
from tests.helpers.wait import screen_wait_for_scene_ready

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


@pytest.fixture(autouse=True, scope="module")
def _clean_stale_state():
    """Reset module-level globals that persist across nicegui_reset_globals().

    Previous test classes (e.g. TestEditorInteractivity) may leave recording
    enabled or tabs with modified content. These module-level singletons are
    NOT reset by NiceGUI's test infrastructure, so we clear them here.
    """
    import waldoctl

    from waldo_commander.state import (
        simulation_state,
    )

    set_active_recording(False)
    waldoctl.commander.programs.items.clear()
    waldoctl.commander.programs.active_id = None
    simulation_state.path_segments.clear()
    simulation_state.targets.clear()
    yield


# ============================================================================
# Local helpers
# ============================================================================


def set_editor_content(screen: "Screen", content: str) -> None:
    """Replace all CodeMirror editor content and verify the change took effect."""
    screen.selenium.execute_script(
        """
        const cm = document.querySelector('.cm-content');
        if (!cm || !cm.cmView || !cm.cmView.view) return;
        const view = cm.cmView.view;
        const len = view.state.doc.length;
        view.dispatch({
            changes: {from: 0, to: len, insert: arguments[0]}
        });
        """,
        content,
    )
    # Verify content actually changed in the browser
    expected_snippet = content.strip()[:40]
    WebDriverWait(screen.selenium, 5).until(
        lambda d: expected_snippet
        in (
            d.execute_script(
                "const c = document.querySelector('.cm-content');"
                "return c && c.cmView ? c.cmView.view.state.doc.toString() : '';"
            )
            or ""
        ),
        message=f"Editor content didn't update to contain '{expected_snippet}'",
    )


def move_cursor_to_line(screen: "Screen", line_number: int) -> None:
    """Move CodeMirror cursor to a specific 1-indexed line."""
    screen.selenium.execute_script(
        """
        const cm = document.querySelector('.cm-content');
        if (!cm || !cm.cmView || !cm.cmView.view) return;
        const view = cm.cmView.view;
        const line = view.state.doc.line(arguments[0]);
        view.dispatch({
            selection: {anchor: line.from},
            scrollIntoView: true
        });
        """,
        line_number,
    )


_GET_PATH_COLORS_JS = """(() => {
    const sceneDiv = document.querySelector('.nicegui-scene');
    if (!sceneDiv) return [];
    const sceneId = sceneDiv.id;
    const scene = window['scene_' + sceneId];
    if (!scene) return [];

    let pathGroup = null;
    scene.traverse(obj => {
        if (obj.name === 'simulation:paths') pathGroup = obj;
    });
    if (!pathGroup) return [];

    const colors = [];
    pathGroup.traverse(obj => {
        if (obj !== pathGroup && obj.material && obj.material.color) {
            colors.push(obj.material.color.getHexString());
        }
    });
    return colors;
})()"""


def _get_path_colors(driver) -> list[str]:
    """Get hex color strings of all path objects in the 3D scene."""
    return driver.execute_script(f"return {_GET_PATH_COLORS_JS}") or []


class HasGlowPathObjects:
    """WebDriverWait condition: some path objects changed color (glow highlight).

    Compares current colors against a baseline snapshot. Returns the number
    of changed objects if any differ, False otherwise.
    """

    def __init__(self, baseline: list[str]):
        self._baseline = baseline

    def __call__(self, driver):
        colors = _get_path_colors(driver)
        if not colors or len(colors) != len(self._baseline):
            return False
        changed = sum(1 for a, b in zip(colors, self._baseline) if a != b)
        return changed if changed > 0 else False


class NoGlowPathObjects:
    """WebDriverWait condition: path colors returned to their baseline."""

    def __init__(self, baseline: list[str]):
        self._baseline = baseline

    def __call__(self, driver):
        colors = _get_path_colors(driver)
        if not colors:
            return False  # no path objects yet — keep waiting
        return colors == self._baseline


class PathColorsStableAfterChange:
    """WebDriverWait condition: path colors changed from baseline, then stabilized.

    Prevents false-positive "stability" from stale path objects left by a
    previous test.  Requires colors to differ from the snapshot taken at
    construction time before checking consecutive-poll stability.
    """

    def __init__(self, baseline: list[str]):
        self._baseline = baseline
        self._prev: list[str] | None = None

    def __call__(self, driver):
        colors = _get_path_colors(driver)
        if not colors:
            self._prev = None
            return False
        # Still seeing the old baseline — not ready yet
        if colors == self._baseline:
            self._prev = None
            return False
        # Changed from baseline; now wait for two consecutive identical polls
        if colors == self._prev:
            return colors
        self._prev = colors
        return False


# ============================================================================
# Tests
# ============================================================================

# Program with two moves for cursor-line highlighting (each on a distinct line)
_PROGRAM_TWO_MOVES = """\
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], duration=2.0)
        await rbt.move_j([95, -95, 185, -5, -5, 185], duration=2.0)
"""


@pytest.mark.browser
class TestEditorVisualization:
    """Browser tests for editor ↔ 3D scene visualization."""

    def test_cursor_line_highlights_path_in_scene(self, class_screen: "Screen") -> None:
        """Moving cursor to a move-command line applies a glow highlight to its path segment."""
        # Ensure scene and editor are ready (idempotent if already done by prev test)
        screen_wait_for_scene_ready(class_screen)
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        # Snapshot current path colors before changing content — the new
        # simulation must produce a DIFFERENT set before we consider it stable.
        baseline_colors = _get_path_colors(class_screen.selenium)

        # Set a 2-move program for distinct line-based segments
        set_editor_content(class_screen, _PROGRAM_TWO_MOVES)

        # Wait for path colors to change from baseline and then stabilize
        # (debounce 1s + simulation + TARGET annotation cycle + second simulation)
        try:
            stable_colors = WebDriverWait(
                class_screen.selenium, 30, poll_frequency=0.5
            ).until(PathColorsStableAfterChange(baseline_colors))
        except TimeoutException:
            current_colors = _get_path_colors(class_screen.selenium)
            cm_content = class_screen.selenium.execute_script(
                "const c = document.querySelector('.cm-content');"
                "return c && c.cmView ? c.cmView.view.state.doc.toString() : '<no cm>';"
            )
            raise AssertionError(
                f"Path colors never changed from baseline. "
                f"baseline={len(baseline_colors)} colors, "
                f"current={len(current_colors)} colors, "
                f"CM content starts with: {(cm_content or '')[:120]!r}"
            )

        # Snapshot the stable (unhighlighted) colors for comparison
        pre_highlight_colors = list(stable_colors)

        # Move cursor to line 5 (first move_j) — should glow-highlight that segment
        move_cursor_to_line(class_screen, 5)

        # Wait for some path objects to change color (glow highlight applied via
        # JS → websocket → Python → websocket → JS round-trip)
        glow_count = WebDriverWait(class_screen.selenium, 10).until(
            HasGlowPathObjects(pre_highlight_colors)
        )
        assert glow_count > 0, "Expected glow-highlighted path objects"

        # Move cursor to line 1 (import — no segment) — should revert highlight
        move_cursor_to_line(class_screen, 1)

        WebDriverWait(class_screen.selenium, 10).until(
            NoGlowPathObjects(pre_highlight_colors)
        )
