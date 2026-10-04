"""Shared helpers for browser/Selenium tests.

These helpers are used across multiple browser test files and provide
consistent patterns for interacting with the UI via Selenium.
"""

import concurrent.futures
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable

from nicegui import core
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
)
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


def ensure_robot_homed(timeout: float = 15.0) -> None:
    """Home the simulated robot and wait for the homed status flag.

    Planned-motion previews mirror the controller's unhomed gate, so a test
    that expects path segments must start from a homed robot instead of
    relying on an earlier test having homed it (the mock homes in ~0.2s).
    """
    import asyncio

    from waldo_commander.state import robot_state, ui_state

    if robot_state.homed:
        return
    assert core.loop is not None, "app event loop not running"
    client = ui_state.control_panel.client
    asyncio.run_coroutine_threadsafe(client.home(), core.loop).result(timeout)
    deadline = _time.monotonic() + timeout
    while not robot_state.homed:
        if _time.monotonic() > deadline:
            raise TimeoutError("robot never reported homed after home()")
        _time.sleep(0.1)


def marked_element(screen: "Screen", marker: str) -> WebElement:
    """The DOM element of the active page's element carrying ``marker``.

    Markers live server-side only, so the id is looked up on the app loop.
    Raises ``NoSuchElementException`` when no element on the page carries it,
    so it can sit inside a ``WebDriverWait``.
    """
    from nicegui import Client

    from waldo_commander.state import ui_state

    def lookup() -> int | None:
        client = Client.instances.get(ui_state.active_client_id)
        if client is None:
            return None
        return next(
            (e.id for e in client.elements.values() if marker in e._markers), None
        )

    identifier = run_in_app(lookup)
    if identifier is None:
        raise NoSuchElementException(marker)
    return screen.selenium.find_element(By.ID, f"c{identifier}")


def click_marked(screen: "Screen", marker: str, timeout: float = 10.0) -> None:
    """Click the marked element once it takes the click: a control still
    sliding or fading in can be covered, or not yet interactable."""

    def clicked(_) -> bool:
        try:
            marked_element(screen, marker).click()
        except (
            ElementClickInterceptedException,
            ElementNotInteractableException,
            NoSuchElementException,
            StaleElementReferenceException,
        ):
            return False
        return True

    WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(
        clicked, message=f"{marker} never took a click"
    )


@contextmanager
def viewport(
    screen: "Screen", width: int, height: int, *, mobile: bool = False
) -> Iterator[None]:
    """Emulate a viewport of exactly ``width`` × ``height`` CSS pixels.

    The driver is shared across tests, so the override is always cleared.
    """
    screen.selenium.execute_cdp_cmd(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile},
    )
    try:
        yield
    finally:
        screen.selenium.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})


# Footer buttons and the gear that replaced the rail tabs: marker, and the
# selector that is on screen once the click has landed.
_SHELL_BUTTONS = {
    "log": ("footer-log", ".bottom-panel"),
    "diagnostics": ("footer-events", ".bottom-panel"),
    "settings": ("tab-settings", ".settings-dialog-card"),
}


def click_tab(screen: "Screen", tab_name: str, timeout: float = 10.0) -> None:
    """Open a panel by name and wait for it to be on screen.

    Args:
        screen: Selenium screen fixture
        tab_name: 'program', 'io' or 'gripper' (rail tabs), 'log' or
            'diagnostics' (footer buttons, open the bottom panel), or
            'settings' (the gear, opens the Settings dialog)
        timeout: Max seconds to wait (default 10s for CI)
    """
    if tab_name in _SHELL_BUTTONS:
        marker, shown = _SHELL_BUTTONS[tab_name]
        marked_element(screen, marker).click()
        WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(
            lambda d: any(
                e.is_displayed() for e in d.find_elements(By.CSS_SELECTOR, shown)
            )
        )
        return

    tab_icons = {
        "program": "code",
        "io": "settings_input_component",
    }
    icon_name = tab_icons.get(tab_name)
    if not icon_name:
        raise ValueError(
            f"Unknown tab: {tab_name}. Valid: {list(tab_icons) + list(_SHELL_BUTTONS)}"
        )

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

    WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(tab_is_active)


_FIND_HOVER_PIXEL = """
const name = arguments[0];
const c = getElement(document.querySelector('.nicegui-scene'));
if (!c || !c.renderer || !c._raycaster) return null;
let root = null;
for (const o of c.objects.values()) if (o.mesh && o.mesh.name === name) root = o.mesh;
if (!root) return null;
root.updateWorldMatrix(true, true);
const canvas = c.renderer.domElement;
const rect = canvas.getBoundingClientRect();
const rc = c._raycaster;
const v = root.position.clone();
const center = root.position.clone();
let best = null;
root.traverse((m) => {
  if (best || !m.isMesh || !m.geometry || !m.geometry.attributes.position) return;
  m.geometry.computeBoundingSphere();
  center.copy(m.geometry.boundingSphere.center).applyMatrix4(m.matrixWorld);
  const pos = m.geometry.attributes.position;
  const stride = Math.max(1, Math.floor(pos.count / 400));
  for (let i = 0; i < pos.count && !best; i += stride) {
    v.fromBufferAttribute(pos, i).applyMatrix4(m.matrixWorld).lerp(center, 0.25);
    v.project(c.camera);
    if (Math.abs(v.x) > 0.95 || Math.abs(v.y) > 0.95) continue;
    const px = rect.left + (v.x + 1) / 2 * rect.width;
    const py = rect.top + (1 - v.y) / 2 * rect.height;
    if (document.elementFromPoint(px, py) !== canvas) continue;
    rc.setFromCamera({ x: v.x, y: v.y }, c.camera);
    const hits = rc.intersectObjects(c.interactiveObjects, true);
    if (!hits.length) continue;
    let o = hits[0].object;
    while (o && o !== root) o = o.parent;
    if (o === root) best = [px, py];
  }
});
return best;
"""

_PROJECT_LOCAL = """
const [name, points] = arguments;
const c = getElement(document.querySelector('.nicegui-scene'));
let root = null;
for (const o of c.objects.values()) if (o.mesh && o.mesh.name === name) root = o.mesh;
if (!root) return null;
root.updateWorldMatrix(true, false);
const rect = c.renderer.domElement.getBoundingClientRect();
const v = root.position.clone();
return points.map(([x, y, z]) => {
  v.set(x, y, z).applyMatrix4(root.matrixWorld).project(c.camera);
  return [rect.left + (v.x + 1) / 2 * rect.width, rect.top + (1 - v.y) / 2 * rect.height];
});
"""


def scene_canvas(screen: "Screen") -> WebElement:
    return screen.selenium.find_element(By.CSS_SELECTOR, ".nicegui-scene canvas")


def pointer_to(screen: "Screen", x: float, y: float, actions: ActionChains) -> None:
    """Queue a real pointer move to viewport point (x, y) on ``actions``."""
    canvas = scene_canvas(screen)
    rect = canvas.rect
    actions.move_to_element_with_offset(
        canvas,
        round(x - (rect["x"] + rect["width"] / 2)),
        round(y - (rect["y"] + rect["height"] / 2)),
    )


def project_local(
    screen: "Screen", name: str, points: list[list[float]]
) -> list[list[float]] | None:
    """Viewport pixels of ``points`` given in the frame of the scene object ``name``."""
    return js(screen, _PROJECT_LOCAL, name, points)


def hover_scene_object(screen: "Screen", name: str, timeout: float = 20.0) -> None:
    """Rest the real mouse on a pixel where ``name`` is the first thing the scene's
    pointer ray hits, so the scene reports it as hovered."""
    deadline = _time.monotonic() + timeout
    while True:
        pixel = js(screen, _FIND_HOVER_PIXEL, name)
        if pixel is not None:
            break
        if _time.monotonic() > deadline:
            raise AssertionError(f"no pixel of {name!r} is hit first on the canvas")
        _time.sleep(0.2)
    actions = ActionChains(screen.selenium, duration=0)
    pointer_to(screen, pixel[0], pixel[1], actions)
    actions.perform()


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

    WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(
        EC.element_to_be_clickable(btn)
    )
    btn.click()


def close_panel(screen: "Screen", panel_class: str) -> None:
    """Close a panel by clicking its close button (the last one in the panel).

    Args:
        screen: Selenium screen fixture
        panel_class: CSS class of the panel (e.g., 'program-panel', 'bottom-panel')
    """
    panel = screen.selenium.find_element(By.CSS_SELECTOR, f".{panel_class}")
    # Find all close buttons and click the last one (panel close, not tab close)
    close_buttons = panel.find_elements(By.XPATH, ".//button[.//i[text()='close']]")
    if not close_buttons:
        raise AssertionError(f"No close button found in .{panel_class}")
    close_buttons[-1].click()


def dismiss_dialogs(screen: "Screen", timeout: float = 2.0) -> None:
    """Close any dialog that is open right now.

    The screen fixtures acknowledge the first-visit and safety dialogs before
    the page loads, so this normally finds nothing and returns at once. The
    check runs in JS: an empty Selenium lookup would block on the driver's
    implicit wait.

    Args:
        screen: Selenium screen fixture
        timeout: Max seconds to wait for an open dialog to close
    """

    def has_visible_dialog() -> bool:
        return bool(
            js(
                screen,
                "return [...document.querySelectorAll('.q-dialog__backdrop')]"
                ".some(b => b.getClientRects().length > 0);",
            )
        )

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

    if has_visible_dialog():
        close_dialogs()
        WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(
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
        WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(check_ready)
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
    return WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(
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

    WebDriverWait(screen.selenium, timeout, poll_frequency=0.05).until(matches)
