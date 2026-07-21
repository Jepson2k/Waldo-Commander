"""Shared helpers for browser/Selenium tests.

These helpers are used across multiple browser test files and provide
consistent patterns for interacting with the UI via Selenium.
"""

import concurrent.futures
from typing import TYPE_CHECKING, Any, Callable

from nicegui import core
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import time as _time

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


def js(screen: "Screen", script: str, *args) -> Any:
    """Execute JavaScript and return result."""
    return screen.selenium.execute_script(script, *args)


def run_in_app(func: Callable[[], Any], timeout: float = 5.0) -> Any:
    """Run ``func`` on the app's event loop and return its result.

    Screen tests execute in the pytest thread while the app (and NiceGUI's
    outbox serializer) runs in the server thread. Commander calls that build
    or mutate UI elements must run on the loop: constructing an element from
    the test thread races the outbox serializing it mid-``__init__``.
    """
    assert core.loop is not None, "app event loop not running"
    future: concurrent.futures.Future = concurrent.futures.Future()

    def _invoke() -> None:
        try:
            future.set_result(func())
        except BaseException as exc:  # noqa: BLE001 — re-raised in the test thread
            future.set_exception(exc)

    core.loop.call_soon_threadsafe(_invoke)
    return future.result(timeout)


def click_tab(screen: "Screen", tab_name: str, timeout: float = 10.0) -> None:
    """Click a tab by finding it via CSS selector, wait for it to become active.

    Args:
        screen: Selenium screen fixture
        tab_name: One of 'program', 'io', 'gripper', 'log', 'help'
        timeout: Max seconds to wait for tab to become active (default 10s for CI)
    """
    # Map tab names to their icon names
    tab_icons = {
        "program": "code",
        "io": "settings_input_component",
        "log": "article",
        "help": "help",
    }
    icon_name = tab_icons.get(tab_name)
    if not icon_name:
        raise ValueError(f"Unknown tab: {tab_name}. Valid: {list(tab_icons.keys())}")

    # Find tab by looking for the icon within a q-tab
    tabs = screen.selenium.find_elements(By.CSS_SELECTOR, ".q-tab")
    target_tab = None
    for tab in tabs:
        try:
            icon = tab.find_element(By.CSS_SELECTOR, ".q-icon")
            if icon.text == icon_name:
                target_tab = tab
                break
        except Exception:
            continue

    if not target_tab:
        raise AssertionError(f"Tab with icon '{icon_name}' not found")

    target_tab.click()

    # Wait for tab to become active (re-find element to avoid stale reference)
    def tab_is_active(driver):
        for tab in driver.find_elements(By.CSS_SELECTOR, ".q-tab"):
            try:
                icon = tab.find_element(By.CSS_SELECTOR, ".q-icon")
                if icon.text == icon_name and "q-tab--active" in (
                    tab.get_attribute("class") or ""
                ):
                    return True
            except Exception:
                continue
        return False

    WebDriverWait(screen.selenium, timeout).until(tab_is_active)


def find_button_by_icon(screen: "Screen", icon_name: str) -> WebElement | None:
    """Find a button containing a Material icon.

    Args:
        screen: Selenium screen fixture
        icon_name: Material icon name (e.g., 'camera_alt', 'code', 'close')

    Returns:
        The button WebElement if found, None otherwise
    """
    buttons = screen.selenium.find_elements(By.TAG_NAME, "button")
    for btn in buttons:
        try:
            icon = btn.find_element(By.TAG_NAME, "i")
            if icon.text == icon_name:
                return btn
        except Exception:
            continue
    return None


def click_button_by_icon(
    screen: "Screen", icon_name: str, timeout: float = 5.0
) -> None:
    """Click a button by its Material icon name.

    Args:
        screen: Selenium screen fixture
        icon_name: Material icon name (e.g., 'camera_alt', 'close')
        timeout: Max seconds to wait for button to be clickable

    Raises:
        AssertionError: If button with icon not found
    """
    btn = find_button_by_icon(screen, icon_name)
    if btn is None:
        raise AssertionError(f"Button with icon '{icon_name}' not found")

    WebDriverWait(screen.selenium, timeout).until(EC.element_to_be_clickable(btn))
    btn.click()


def close_panel(screen: "Screen", panel_class: str) -> None:
    """Close a panel by clicking its close button (the last one in the panel).

    Args:
        screen: Selenium screen fixture
        panel_class: CSS class of the panel (e.g., 'program-panel', 'response-panel')
    """
    panel = screen.selenium.find_element(By.CSS_SELECTOR, f".{panel_class}")
    # Find all close buttons and click the last one (panel close, not tab close)
    close_buttons = panel.find_elements(By.XPATH, ".//button[.//i[text()='close']]")
    if not close_buttons:
        raise AssertionError(f"No close button found in .{panel_class}")
    close_buttons[-1].click()


def dismiss_dialogs(screen: "Screen", timeout: float = 2.0) -> None:
    """Dismiss any open dialogs by clicking the backdrop or pressing Escape.

    Waits for the tutorial dialog (which appears ~1s after page load) to appear,
    dismisses it, then sets localStorage to prevent future dialogs.

    Args:
        screen: Selenium screen fixture
        timeout: Max seconds to wait for dialog operations
    """
    from selenium.common.exceptions import TimeoutException

    def has_visible_dialog() -> bool:
        """Check if any dialog backdrop is currently visible."""
        try:
            backdrops = screen.selenium.find_elements(
                By.CSS_SELECTOR, ".q-dialog__backdrop"
            )
            return any(b.is_displayed() for b in backdrops)
        except Exception:
            return False

    def close_dialogs() -> None:
        """Try to close any open dialogs."""
        js(
            screen,
            """
            const buttons = document.querySelectorAll('.q-dialog button');
            // Try skip/close buttons first
            for (const btn of buttons) {
                const text = btn.textContent.toLowerCase();
                if (text.includes('skip') || text.includes('close')) {
                    btn.click();
                    return;
                }
                const icon = btn.querySelector('i');
                if (icon && icon.textContent === 'close') {
                    btn.click();
                    return;
                }
            }
            // Accept any unchecked checkboxes (e.g. welcome/disclaimer dialogs)
            const cbs = document.querySelectorAll('.q-dialog .q-checkbox:not(.q-checkbox--truthy)');
            cbs.forEach(cb => cb.click());
            // Fallback: click the backdrop
            const backdrop = document.querySelector('.q-dialog__backdrop');
            if (backdrop) backdrop.click();
            """,
        )
        # After checking checkboxes, wait a tick for Vue reactivity, then click continue/ok
        _time.sleep(0.1)
        js(
            screen,
            """
            const buttons = document.querySelectorAll('.q-dialog button');
            for (const btn of buttons) {
                const text = btn.textContent.toLowerCase();
                if (text.includes('continue') || text.includes('ok')) {
                    btn.click();
                    return;
                }
            }
            """,
        )

    # Wait for tutorial dialog to appear (it has a 1s delay after page load)
    # Use short timeout since dialog may not appear if localStorage already set
    try:
        WebDriverWait(screen.selenium, timeout).until(lambda _: has_visible_dialog())
    except TimeoutException:
        pass  # No dialog appeared, that's fine

    # If a dialog is visible, close it and wait for it to be gone
    if has_visible_dialog():
        close_dialogs()
        WebDriverWait(screen.selenium, timeout).until(
            lambda _: not has_visible_dialog()
        )


def wait_for_codemirror_ready(screen: "Screen", timeout: float = 20.0) -> None:
    """Wait for CodeMirror editor to be fully interactive.

    Args:
        screen: Selenium screen fixture
        timeout: Max seconds to wait (default 20s for CI environments with SwiftShader)

    Raises:
        TimeoutError: If CodeMirror not ready in time
    """
    condition_js = """(() => {
        const cm = document.querySelector('.cm-editor');
        if (!cm) return false;
        const content = cm.querySelector('.cm-content');
        if (!content) return false;
        return content.isContentEditable;
    })()"""

    def check_ready(driver):
        return driver.execute_script(f"return {condition_js}")

    try:
        WebDriverWait(screen.selenium, timeout).until(check_ready)
    except Exception as e:
        raise TimeoutError(f"CodeMirror not ready after {timeout}s") from e


# ============================================================================
# Keyboard / focus helpers
# ============================================================================


def defocus_editor(screen: "Screen") -> None:
    """Blur the currently focused element so global keybindings can fire.

    The KeybindingsFocusDetector JS module gates `_editor_focused` on the
    document.activeElement being inside .cm-editor (or any contenteditable
    / input). After we blur, sleep ~0.2s for the focusout → JS poll →
    websocket → Python set_editor_focused(false) round-trip to settle.
    The keybindings.js focusout handler also has a 50ms internal delay.
    """
    screen.selenium.execute_script(
        "if (document.activeElement) document.activeElement.blur();"
    )
    _time.sleep(0.2)


def send_global_key(screen: "Screen", key: str) -> None:
    """Send a single keystroke to whatever currently has focus.

    Use after defocus_editor() to target the document body so NiceGUI's
    ui.keyboard listener (and the project's keybindings_manager) sees it.
    """
    ActionChains(screen.selenium).send_keys(key).perform()


def focus_editor(screen: "Screen") -> WebElement:
    """Click the CodeMirror content area to focus the editor.

    Returns the .cm-content WebElement so callers can chain send_keys on
    it (Selenium's send_keys on a contenteditable div needs the element
    reference; ActionChains alone won't reliably target the editor's
    keymap).
    """
    cm_content = screen.selenium.find_element(By.CSS_SELECTOR, ".cm-content")
    cm_content.click()
    return cm_content


def type_in_editor(screen: "Screen", text: str) -> None:
    """Send real keystrokes to the focused CodeMirror content area.

    Unlike append_to_editor (which uses view.dispatch and bypasses
    CodeMirror's keymap), this routes through the Mod-s save shortcut,
    autocomplete activation, and any other key handlers in the keymap.
    """
    cm_content = screen.selenium.find_element(By.CSS_SELECTOR, ".cm-content")
    cm_content.send_keys(text)


def wait_for_autocomplete(screen: "Screen", timeout: float = 3.0) -> WebElement:
    """Wait for the CodeMirror autocomplete popup to appear and return it."""
    return WebDriverWait(screen.selenium, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".cm-tooltip-autocomplete"))
    )


def get_autocomplete_labels(screen: "Screen") -> list[str]:
    """Read all completion entry text from the autocomplete popup."""
    return (
        screen.selenium.execute_script(
            """
            return Array.from(
                document.querySelectorAll('.cm-tooltip-autocomplete li')
            ).map(li => li.textContent);
            """
        )
        or []
    )


def wait_for_notification(screen: "Screen", text: str, timeout: float = 3.0) -> None:
    """Wait for a Quasar notification containing `text` to appear in the DOM.

    Quasar renders ui.notify() output as .q-notification elements. We poll
    rather than use a single presence check because the notification text
    may take a tick to populate after the element first appears.
    """

    def matches(driver):
        return any(
            text in n.text
            for n in driver.find_elements(By.CSS_SELECTOR, ".q-notification")
        )

    WebDriverWait(screen.selenium, timeout).until(matches)
