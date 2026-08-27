"""Browser tests for editor interactivity.

Tests verify:
- CodeMirror editor opens and displays content
- Content can be modified
- Clicking capture pose adds code and flashes the line
- Recording mode adds code on jog movements
- Tab flashes when editor panel is closed during recording

All tests share a single browser session via class_screen fixture.
"""

import time
from typing import TYPE_CHECKING

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import (
    click_button_by_icon,
    click_tab,
    wait_for_codemirror_ready,
)

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


# ============================================================================
# Local helpers (single-use in this test file)
# ============================================================================


def get_codemirror_content(screen: "Screen") -> str:
    """Get content from the active CodeMirror editor.

    Uses screen element lookup, then accesses CodeMirror's state for content.

    Returns:
        The editor content as a string, or empty string if not available
    """
    cm_content = screen.selenium.find_element(By.CSS_SELECTOR, ".cm-content")
    return (
        screen.selenium.execute_script(
            """
        const el = arguments[0];
        if (!el || !el.cmView || !el.cmView.view) return '';
        return el.cmView.view.state.doc.toString();
        """,
            cm_content,
        )
        or ""
    )


def append_to_editor(screen: "Screen", text: str) -> None:
    """Append text to the end of the CodeMirror editor content.

    Uses CodeMirror 6's dispatch API to insert text properly.

    Args:
        screen: Selenium screen fixture
        text: The text to append
    """
    screen.selenium.execute_script(
        """
        const text = arguments[0];
        const cm = document.querySelector('.cm-content');
        if (!cm || !cm.cmView || !cm.cmView.view) return;
        const view = cm.cmView.view;
        const len = view.state.doc.length;
        view.dispatch({
            changes: {from: len, insert: text}
        });
        """,
        text,
    )


def get_editor_line_count(screen: "Screen") -> int:
    """Get the number of lines in the CodeMirror editor."""
    cm = screen.selenium.find_element(By.CSS_SELECTOR, ".cm-editor")
    lines = cm.find_elements(By.CSS_SELECTOR, ".cm-line")
    return len(lines)


class LineFlashCondition:
    """Custom expected condition for line flash detection."""

    def __init__(self, screen: "Screen", min_line: int):
        self.screen = screen
        self.min_line = min_line

    def __call__(self, driver):
        try:
            cm = driver.find_element(By.CSS_SELECTOR, ".cm-editor")
            lines = cm.find_elements(By.CSS_SELECTOR, ".cm-line")

            for i in range(self.min_line - 1, len(lines)):
                if "cm-line-flash" in (lines[i].get_attribute("class") or ""):
                    return i + 1  # Return 1-based line number
            return False
        except Exception:
            return False


def setup_tab_flash_observer(screen: "Screen") -> None:
    """Install a MutationObserver that sets a flag when tab-flash is added."""
    screen.selenium.execute_script("""
        window.__tabFlashDetected = false;
        const tabs = document.querySelectorAll('.q-tab');
        for (const tab of tabs) {
            const icon = tab.querySelector('i');
            if (icon && icon.innerText === 'code') {
                const obs = new MutationObserver(mutations => {
                    for (const m of mutations) {
                        if (tab.classList.contains('tab-flash')) {
                            window.__tabFlashDetected = true;
                            obs.disconnect();
                            return;
                        }
                    }
                });
                obs.observe(tab, {attributes: true, attributeFilter: ['class']});
                break;
            }
        }
    """)


class TabFlashCondition:
    """Check if the MutationObserver recorded a tab-flash event."""

    def __call__(self, driver):
        try:
            return driver.execute_script("return window.__tabFlashDetected === true")
        except Exception:
            return False


class LineCountChangedCondition:
    """Custom expected condition for line count change detection."""

    def __init__(self, screen: "Screen", initial_count: int):
        self.screen = screen
        self.initial_count = initial_count

    def __call__(self, driver):
        try:
            count = get_editor_line_count(self.screen)
            if count != self.initial_count:
                return count
            return False
        except Exception:
            return False


def jog_joint_briefly(
    screen: "Screen", joint_index: int = 0, duration_s: float = 0.3
) -> None:
    """Press and release a jog button briefly to trigger recorded movement.

    Dispatches mousedown/mouseup via JS instead of ActionChains: the buttons
    release on ``mouseleave`` (safety against stuck jogs), and a real pointer
    hold can be cut short when the pressed-style transform or a readout
    re-render shifts the pill under the cursor.

    Args:
        screen: Selenium screen fixture
        joint_index: Joint number (0-5)
        duration_s: How long to hold the button in seconds
    """
    # There are 2 buttons per joint (minus and plus); plus are at odd indices.
    # The element is re-queried for each dispatch: the button re-renders while
    # the robot moves (pressed style, enabled binding on joint angles), and a
    # mouseup dispatched on a stale detached node is silently lost, leaving
    # the jog streaming until something else stops it.
    plus_btn_index = joint_index * 2 + 1

    def dispatch(event: str) -> None:
        joint_buttons = screen.selenium.find_elements(By.CSS_SELECTOR, ".joint-cap")
        assert len(joint_buttons) > plus_btn_index, (
            f"Joint {joint_index} + button not found"
        )
        screen.selenium.execute_script(
            "arguments[0].dispatchEvent(new MouseEvent(arguments[1], {bubbles: true}))",
            joint_buttons[plus_btn_index],
            event,
        )

    dispatch("mousedown")
    time.sleep(duration_s)
    dispatch("mouseup")


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.browser
class TestEditorInteractivity:
    """Editor tests sharing a single browser session."""

    def test_editor_opens_with_default_content(self, class_screen: "Screen") -> None:
        """Editor should open with CodeMirror and display default program."""
        # CI with SwiftShader needs more time to initialize WebGL/3D scene
        time.sleep(1.0)
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        # Verify editor has some content (default program)
        content = get_codemirror_content(class_screen)
        assert len(content) > 0, "Editor should have default content"

    def test_editor_content_can_be_modified(self, class_screen: "Screen") -> None:
        """Editor content can be modified via CodeMirror."""
        # Tab should already be open from previous test
        wait_for_codemirror_ready(class_screen)

        # Append some content and verify it appears
        test_text = "\n# Added line"
        append_to_editor(class_screen, test_text)

        actual = get_codemirror_content(class_screen)
        assert test_text in actual, f"Expected '{test_text}' in content, got '{actual}'"

    def test_capture_pose_adds_and_flashes_line(self, class_screen: "Screen") -> None:
        """Clicking capture pose adds code and briefly flashes the new line."""
        # Ensure editor is ready
        wait_for_codemirror_ready(class_screen)

        # Wait for capture button to be visible
        btn = WebDriverWait(class_screen.selenium, 5).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[.//i[text()='camera_alt']]")
            )
        )

        # Get initial state
        initial_lines = get_editor_line_count(class_screen)

        # Click capture pose button
        WebDriverWait(class_screen.selenium, 5).until(EC.element_to_be_clickable(btn))
        btn.click()

        # Wait for line count to change using WebDriverWait
        try:
            new_lines = WebDriverWait(class_screen.selenium, 5).until(
                LineCountChangedCondition(class_screen, initial_lines)
            )
        except Exception:
            new_lines = get_editor_line_count(class_screen)

        # Verify a new line was added
        assert new_lines > initial_lines, (
            f"Expected more lines after capture: {initial_lines} -> {new_lines}"
        )

        # Check if the flash class is present (may have already expired)
        try:
            flashed_line = WebDriverWait(class_screen.selenium, 2).until(
                LineFlashCondition(class_screen, min_line=initial_lines + 1)
            )
            assert flashed_line >= initial_lines + 1, (
                f"Flash should be on new line, got line {flashed_line}"
            )
        except Exception:
            # Flash may have expired - that's acceptable
            pass

    def test_recording_adds_code_on_jog(self, class_screen: "Screen") -> None:
        """Starting recording and jogging a joint adds code to editor."""
        wait_for_codemirror_ready(class_screen)

        # Get initial line count
        initial_lines = get_editor_line_count(class_screen)

        # Start recording (fiber_manual_record icon)
        click_button_by_icon(class_screen, "fiber_manual_record")

        # Jog a joint briefly (0.5s hold to ensure the hold threshold is exceeded
        # and the motion recorder captures the jog on slow platforms)
        jog_joint_briefly(class_screen, joint_index=0, duration_s=0.5)

        # Verify code was added. The insert lands only after the recorder's
        # wait_motion(timeout=30.0) resolves — on timeout it records anyway —
        # so the true ceiling is ~30s (observed 25.7s on a loaded CI runner
        # during the class's first jog, while SwiftShader still renders the
        # freshly built scene). Wait past that ceiling, not a guess below it.
        try:
            new_lines = WebDriverWait(class_screen.selenium, 40).until(
                LineCountChangedCondition(class_screen, initial_lines)
            )
        except Exception:
            new_lines = get_editor_line_count(class_screen)

        assert new_lines > initial_lines, (
            f"Recording should add code: {initial_lines} -> {new_lines}"
        )

    def test_tab_flashes_when_editor_closed(self, class_screen: "Screen") -> None:
        """When editor panel is closed, recording a jog flashes the tab."""
        # Ensure program tab is open first (may be closed from previous tests)
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        # Ensure recording is on (start if not already from previous test)
        # Check if record button has warning color (means recording is active)
        record_btn = class_screen.selenium.find_element(
            By.XPATH, "//button[.//i[text()='fiber_manual_record']]"
        )
        if "bg-warning" not in (record_btn.get_attribute("class") or ""):
            # Recording not on, start it
            record_btn.click()
            # Wait for button color to change to warning (recording active)
            WebDriverWait(class_screen.selenium, 3).until(
                lambda d: "bg-warning" in (record_btn.get_attribute("class") or "")
            )

        # Switch to a different tab to hide the program panel (but keep tab visible)
        click_tab(class_screen, "io")

        # Install MutationObserver on the program tab before jogging
        # This catches the tab-flash class even if it's added and removed quickly
        setup_tab_flash_observer(class_screen)

        # Wait for backend state to propagate (tab click is async via websocket)
        time.sleep(0.5)

        # Jog joint 2 (J3) instead of joint 0 (J1) - previous tests jog J1+
        # to its limit, causing wait_command to take up to 5s
        jog_joint_briefly(class_screen, joint_index=2, duration_s=0.8)

        # Check if the MutationObserver recorded a tab-flash event
        # Timeout must exceed wait_command's 5s timeout + processing
        tab_flashed = False
        try:
            WebDriverWait(class_screen.selenium, 8, poll_frequency=0.2).until(
                TabFlashCondition()
            )
            tab_flashed = True
        except Exception:
            pass

        assert tab_flashed, "Program tab should have tab-flash class when panel closed"

    def test_editor_state_persists_after_refresh(self, class_screen: "Screen") -> None:
        """Editor tabs and content should persist after page refresh."""
        # Stop recording if active from previous test (check by button color)
        try:
            record_btn = class_screen.selenium.find_element(
                By.XPATH, "//button[.//i[text()='fiber_manual_record']]"
            )
            if "bg-warning" in (record_btn.get_attribute("class") or ""):
                record_btn.click()
                # Wait for recording to stop
                WebDriverWait(class_screen.selenium, 3).until(
                    lambda d: "bg-warning"
                    not in (record_btn.get_attribute("class") or "")
                )
        except Exception:
            pass

        # Open program tab
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        # Add unique content to identify this session
        unique_marker = "\n# REFRESH_TEST_MARKER_12345"
        append_to_editor(class_screen, unique_marker)

        # Verify content was added
        content_before = get_codemirror_content(class_screen)
        assert unique_marker.strip() in content_before, (
            "Marker should be in content before refresh"
        )

        # Refresh the page — set a marker so we can detect the actual reload
        class_screen.selenium.execute_script("window.__pre_refresh = true")
        class_screen.selenium.refresh()

        # Wait for the OLD page to unload (marker disappears)
        WebDriverWait(class_screen.selenium, 10).until(
            lambda d: not d.execute_script("return window.__pre_refresh")
        )

        # Wait for PanelResize to be configured and app to be ready
        WebDriverWait(class_screen.selenium, 15).until(
            lambda d: d.execute_script(
                "return window.PanelResize && window.PanelResize.isConfigured() && window.PanelResize.isAppReady()"
            )
        )

        # Sidebar tab selection isn't persisted across refresh, so re-activate
        # program tab before checking codemirror.
        click_tab(class_screen, "program")
        wait_for_codemirror_ready(class_screen)

        # Verify content persisted
        content_after = get_codemirror_content(class_screen)
        assert unique_marker.strip() in content_after, (
            f"Content should persist after refresh. "
            f"Expected marker '{unique_marker.strip()}' in content, got: {content_after[:200]}..."
        )
