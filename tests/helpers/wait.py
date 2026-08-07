"""Shared async wait helpers for integration tests.

These helpers provide condition-based waiting instead of blind asyncio.sleep(),
making tests more reliable and faster.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Callable

import waldoctl
from nicegui.testing import User
from waldoctl import ActionState

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


async def simulate_click(user: User, marker: str, hold_ms: float = 50) -> None:
    """Simulate a mouse click with proper mousedown/mouseup events.

    The jog buttons use mousedown/mouseup events to detect clicks vs holds.
    NiceGUI's .click() method doesn't trigger these events correctly,
    so we need to manually trigger them.

    Args:
        user: NiceGUI User test fixture
        marker: The marker attribute to find the element
        hold_ms: Time in milliseconds to hold between mousedown and mouseup
    """
    element = user.find(marker=marker)
    element.trigger("mousedown")
    await asyncio.sleep(hold_ms / 1000.0)
    element.trigger("mouseup")


async def wait_for_motion_stable(
    get_value_fn: Callable[[], float],
    timeout_s: float = 3.0,
    tolerance: float = 0.1,
    stable_ticks: int = 10,
) -> float:
    """Wait for a value to stabilize (stop changing).

    Polls a value and waits until it stops changing for several consecutive
    readings, indicating motion has completed.

    Args:
        get_value_fn: Callable returning current value to monitor
        timeout_s: Maximum time to wait
        tolerance: Maximum change between ticks to consider "stable"
        stable_ticks: Number of consecutive stable ticks required

    Returns:
        The final stable value

    Raises:
        ValueError: If value accessor fails (e.g., empty angles list)
    """
    interval = 0.1
    last_value = None
    stable_count = 0

    # Get initial value with error handling
    try:
        last_value = get_value_fn()
    except (IndexError, KeyError, TypeError) as e:
        raise ValueError(
            f"wait_for_motion_stable: Cannot get initial value. "
            f"Ensure waldoctl.commander.status.joints.angles is populated. Error: {e}"
        ) from e

    for _ in range(int(timeout_s / interval)):
        await asyncio.sleep(interval)
        try:
            current = get_value_fn()
        except (IndexError, KeyError, TypeError) as e:
            raise ValueError(
                f"wait_for_motion_stable: Value accessor failed mid-wait. Error: {e}"
            ) from e

        if last_value is not None and abs(current - last_value) < tolerance:
            stable_count += 1
            if stable_count >= stable_ticks:
                return current
        else:
            stable_count = 0
        last_value = current

    # Return last value even if didn't fully stabilize
    return get_value_fn()


async def enable_sim(user: User, timeout_s: float = 5.0) -> None:
    """Enable simulator mode and wait for it to be ready.

    This helper handles race conditions between test fixtures and app startup:
    1. Waits for app to be ready (startup + backend + page)
    2. Validates we have valid angles (6 values)
    3. Toggles simulator if needed

    Args:
        user: NiceGUI User test fixture
        timeout_s: Maximum time to wait for simulator to activate

    Raises:
        TimeoutError: If simulator doesn't become ready within timeout
    """
    from waldo_commander.state import readiness_state

    def _has_valid_angles() -> bool:
        angles = waldoctl.commander.status.joints.angles
        return len(angles) >= 6

    # Wait for app to be ready first
    try:
        await asyncio.wait_for(readiness_state.app_ready.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"enable_sim: App not ready after {timeout_s}s. "
            f"startup={readiness_state._startup_done}, "
            f"backend={readiness_state._backend_done}, "
            f"page={readiness_state._page_done}"
        ) from None

    # If we have valid angles and simulator is active, we're good
    if _has_valid_angles() and waldoctl.commander.status.simulator_active:
        return

    # Toggle simulator if needed
    if not waldoctl.commander.status.simulator_active:
        user.find(marker="btn-robot-toggle").click()
        await asyncio.sleep(0.1)

    # Wait for simulator_active flag to be set by the toggle handler
    for _ in range(int(timeout_s / 0.1)):
        if waldoctl.commander.status.simulator_active and _has_valid_angles():
            return
        await asyncio.sleep(0.1)

    if not _has_valid_angles():
        raise TimeoutError(
            f"enable_sim: No valid angles after waiting. "
            f"simulator_active={waldoctl.commander.status.simulator_active}, "
            f"angles={waldoctl.commander.status.joints.angles}"
        )


async def wait_for_tool_key(
    tool_key: str,
    timeout_s: float = 2.0,
) -> None:
    """Wait for waldoctl.commander.status.tool.key to match expected value.

    After calling client.select_tool(), the backend must broadcast a status
    update before waldoctl.commander.status.tool.key reflects the change.

    Raises:
        TimeoutError: If tool_key doesn't match within timeout
    """
    interval = 0.05
    for _ in range(int(timeout_s / interval)):
        if waldoctl.commander.status.tool.key == tool_key:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(
        f"tool_key did not become '{tool_key}' within {timeout_s}s, "
        f"still '{waldoctl.commander.status.tool.key}'"
    )


async def wait_for_app_ready(timeout_s: float = 40.0) -> None:
    """Wait for app to be fully ready (startup + backend + page).

    This is the primary wait function for tests. It ensures all components
    are initialized and the app is in a stable state for testing.

    Args:
        timeout_s: Maximum time to wait (default sized for loaded CI runners,
            where startup has been observed to exceed 20s)

    Raises:
        TimeoutError: If app doesn't become ready within timeout
    """
    from waldo_commander.state import readiness_state

    try:
        await asyncio.wait_for(readiness_state.app_ready.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"App not ready after {timeout_s}s. "
            f"startup={readiness_state._startup_done}, "
            f"backend={readiness_state._backend_done}, "
            f"page={readiness_state._page_done}"
        ) from None


async def wait_for_urdf_ready(timeout_s: float = 5.0) -> None:
    """Wait for URDF scene initialization to complete.

    This waits for the URDF 3D scene to be fully loaded and configured,
    including joint mappings and camera setup.

    Args:
        timeout_s: Maximum time to wait

    Raises:
        TimeoutError: If URDF scene doesn't initialize within timeout
    """
    from waldo_commander.state import readiness_state

    try:
        await asyncio.wait_for(
            readiness_state.urdf_scene_ready.wait(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"URDF scene not ready after {timeout_s}s. "
            f"urdf_scene_ready_ts={readiness_state.urdf_scene_ready_ts}"
        ) from None


async def wait_for_motion_start(
    timeout_s: float = 5.0,
    require_detection: bool = True,
) -> bool:
    """Wait for motion to begin after a command is issued.

    In the NiceGUI test environment, commander.status may not be updated from
    STATUS messages immediately because _status_consumer runs as a separate
    async task.

    This function:
    1. Records initial angles for comparison
    2. Yields control repeatedly to allow background tasks to run
    3. Checks if action_state becomes EXECUTING
    4. Checks if any joint angle has changed from baseline

    Args:
        timeout_s: Maximum time to wait. Default 5.0s, generous enough for
            slow CI runners (Windows in particular). Override for unit tests
            that intentionally check no-motion scenarios.
        require_detection: If True (default), raises TimeoutError if motion
            isn't detected — surfaces "no motion" as a clear failure rather
            than letting the subsequent assertion fail with a confusing
            "moved 0.00mm" message. Set False for tests that expect no motion.

    Returns:
        True if motion was detected.

    Raises:
        TimeoutError: If require_detection=True and no motion detected within
            timeout_s.
    """
    import time

    # Record initial state for comparison
    initial_angles = (
        list(waldoctl.commander.status.joints.angles.deg)
        if len(waldoctl.commander.status.joints.angles) > 0
        else []
    )
    start_time = time.time()

    # Brief delay to allow command to be sent
    await asyncio.sleep(0.1)

    # Yield control multiple times to give _status_consumer a chance to run
    interval = 0.05
    for _ in range(int(timeout_s / interval)):
        await asyncio.sleep(interval)

        # Check if action_state indicates motion
        if waldoctl.commander.status.action.state == ActionState.EXECUTING:
            return True

        # Check if timestamp updated since we started
        if waldoctl.commander.status.last_update > start_time:
            # Check if any joint angle changed
            current_angles = waldoctl.commander.status.joints.angles.deg
            if len(current_angles) >= 6 and initial_angles:
                for i in range(min(6, len(current_angles), len(initial_angles))):
                    if abs(current_angles[i] - initial_angles[i]) > 0.01:
                        return True

    # Motion not detected within timeout
    if require_detection:
        raise TimeoutError(
            f"wait_for_motion_start: No motion detected after {timeout_s}s. "
            f"action_state={waldoctl.commander.status.action.state}, "
            f"initial_angles={initial_angles[:3] if initial_angles else []}, "
            f"current_angles={list(waldoctl.commander.status.joints.angles.deg[:3]) if len(waldoctl.commander.status.joints.angles) > 0 else []}"
        )

    # Continue anyway and let wait_for_motion_stable handle detection
    return False


async def wait_for_value_change(
    get_value_fn: Callable[[], float],
    timeout_s: float = 1.0,
    min_delta: float = 0.05,
) -> float:
    """Wait for a value to change from its initial reading.

    Alternative to wait_for_motion_start when action_state isn't reliable
    (e.g., quick incremental moves that complete before polling catches them).

    Args:
        get_value_fn: Callable returning current value to monitor
        timeout_s: Maximum time to wait
        min_delta: Minimum change required to consider "started"

    Returns:
        The new value after change detected

    Raises:
        TimeoutError: If no change detected within timeout
    """
    baseline = get_value_fn()
    interval = 0.05
    for _ in range(int(timeout_s / interval)):
        await asyncio.sleep(interval)
        current = get_value_fn()
        if abs(current - baseline) >= min_delta:
            return current
    raise TimeoutError(
        f"Value did not change by {min_delta} within {timeout_s}s. "
        f"baseline={baseline}, current={get_value_fn()}"
    )


async def ensure_robot_ready_for_motion(timeout_s: float = 5.0) -> None:
    """Validate robot state is ready for motion testing.

    This is a comprehensive check that should be called after enable_sim()
    to ensure all prerequisites for motion commands are met.

    Args:
        timeout_s: Maximum time to wait for app_ready

    Raises:
        TimeoutError: If app_ready not signaled within timeout
        AssertionError: If robot state is invalid for motion
    """
    from waldo_commander.state import readiness_state

    # Wait for app to be ready
    try:
        await asyncio.wait_for(readiness_state.app_ready.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"ensure_robot_ready_for_motion: app_ready not signaled after {timeout_s}s. "
            f"startup={readiness_state._startup_done}, "
            f"backend={readiness_state._backend_done}, "
            f"page={readiness_state._page_done}"
        ) from None

    # Validate angles
    angles = waldoctl.commander.status.joints.angles
    assert len(angles) >= 6, (
        f"ensure_robot_ready_for_motion: Invalid angles. "
        f"Expected >=6 elements, got: {len(angles)}"
    )

    # Validate motion mode is active
    assert (
        waldoctl.commander.status.simulator_active
        or waldoctl.commander.status.connected
    ), (
        "ensure_robot_ready_for_motion: No motion mode active. "
        f"simulator_active={waldoctl.commander.status.simulator_active}, "
        f"connected={waldoctl.commander.status.connected}"
    )


# ============================================================================
# Panel Resize Test Helpers
# ============================================================================


async def get_element_rect(user: User, selector: str) -> dict | None:
    """Get bounding rect of element via JavaScript.

    Args:
        user: NiceGUI User test fixture
        selector: CSS selector for the element

    Returns:
        Dict with top, right, bottom, left, width, height or None if not found
    """
    result = await user.page.evaluate(
        f"""(() => {{
            const el = document.querySelector('{selector}');
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            return {{
                top: rect.top,
                right: rect.right,
                bottom: rect.bottom,
                left: rect.left,
                width: rect.width,
                height: rect.height
            }};
        }})()"""
    )
    return result


async def get_element_classes(user: User, selector: str) -> list[str]:
    """Get list of CSS classes on an element.

    Args:
        user: NiceGUI User test fixture
        selector: CSS selector for the element

    Returns:
        List of class names
    """
    result = await user.page.evaluate(
        f"""(() => {{
            const el = document.querySelector('{selector}');
            if (!el) return [];
            return Array.from(el.classList);
        }})()"""
    )
    return result or []


async def get_localstorage_item(user: User, key: str) -> dict | None:
    """Read and parse JSON from localStorage.

    Args:
        user: NiceGUI User test fixture
        key: localStorage key

    Returns:
        Parsed JSON object or None if not found
    """
    import json

    result = await user.page.evaluate(f"localStorage.getItem('{key}')")
    if result:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    return None


async def set_localstorage_item(user: User, key: str, value: dict) -> None:
    """Set a JSON value in localStorage.

    Args:
        user: NiceGUI User test fixture
        key: localStorage key
        value: Dict to store as JSON
    """
    import json

    json_str = json.dumps(value).replace("'", "\\'")
    await user.page.evaluate(f"localStorage.setItem('{key}', '{json_str}')")


async def clear_localstorage_item(user: User, key: str) -> None:
    """Remove an item from localStorage.

    Args:
        user: NiceGUI User test fixture
        key: localStorage key to remove
    """
    await user.page.evaluate(f"localStorage.removeItem('{key}')")


async def simulate_drag(user: User, selector: str, dx: int = 0, dy: int = 0) -> None:
    """Simulate a mouse drag on an element.

    Triggers mousedown on the element, then mousemove and mouseup on document.

    Args:
        user: NiceGUI User test fixture
        selector: CSS selector for the element to drag
        dx: Horizontal pixels to drag (positive = right)
        dy: Vertical pixels to drag (positive = down)
    """
    await user.page.evaluate(
        f"""(() => {{
            const el = document.querySelector('{selector}');
            if (!el) return;
            const rect = el.getBoundingClientRect();
            const startX = rect.left + rect.width / 2;
            const startY = rect.top + rect.height / 2;

            el.dispatchEvent(new MouseEvent('mousedown', {{
                clientX: startX,
                clientY: startY,
                bubbles: true
            }}));

            document.dispatchEvent(new MouseEvent('mousemove', {{
                clientX: startX + {dx},
                clientY: startY + {dy},
                bubbles: true
            }}));

            document.dispatchEvent(new MouseEvent('mouseup', {{
                clientX: startX + {dx},
                clientY: startY + {dy},
                bubbles: true
            }}));
        }})()"""
    )


async def wait_for_panel_resize_ready(user: User, timeout_s: float = 3.0) -> None:
    """Wait for PanelResize module to be configured and app-ready.

    Args:
        user: NiceGUI User test fixture
        timeout_s: Maximum time to wait

    Raises:
        TimeoutError: If PanelResize doesn't become ready
    """
    interval = 0.1
    for _ in range(int(timeout_s / interval)):
        is_ready = await user.page.evaluate(
            "window.PanelResize && window.PanelResize.isAppReady()"
        )
        if is_ready:
            return
        await asyncio.sleep(interval)

    raise TimeoutError(f"PanelResize not ready after {timeout_s}s")


# ============================================================================
# Synchronous Screen (Selenium) Wait Helpers
# ============================================================================


def screen_wait_for_condition(
    screen: "Screen",
    condition_js: str,
    timeout_s: float = 5.0,
    poll_interval: float = 0.1,
    label: str = "condition",
) -> bool:
    """Wait for a JavaScript condition to become true.

    Args:
        screen: Selenium screen fixture
        condition_js: JavaScript expression that returns a boolean
        timeout_s: Maximum time to wait
        poll_interval: Time between polls
        label: Description for logging

    Returns:
        True if condition became true, False if timeout
    """
    import logging

    start = time.time()
    deadline = start + timeout_s
    while time.time() < deadline:
        result = screen.selenium.execute_script(f"return {condition_js}")
        if result:
            elapsed = time.time() - start
            logging.debug(f"screen_wait: {label} ready after {elapsed:.3f}s")
            return True
        time.sleep(poll_interval)
    elapsed = time.time() - start
    logging.debug(f"screen_wait: {label} TIMEOUT after {elapsed:.3f}s")
    return False


def screen_wait_for_element(
    screen: "Screen",
    selector: str,
    timeout_s: float = 5.0,
) -> bool:
    """Wait for an element to exist in the DOM."""
    return screen_wait_for_condition(
        screen,
        f"document.querySelector('{selector}') !== null",
        timeout_s,
        label=f"element '{selector}'",
    )


def screen_wait_for_element_visible(
    screen: "Screen",
    selector: str,
    timeout_s: float = 5.0,
) -> bool:
    """Wait for an element to be visible (has dimensions)."""
    js = f"""(() => {{
        const el = document.querySelector('{selector}');
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }})()"""
    return screen_wait_for_condition(
        screen, js, timeout_s, label=f"visible '{selector}'"
    )


def screen_wait_for_element_hidden(
    screen: "Screen",
    selector: str,
    timeout_s: float = 5.0,
) -> bool:
    """Wait for an element to be hidden or removed."""
    js = f"""(() => {{
        const el = document.querySelector('{selector}');
        if (!el) return true;
        const rect = el.getBoundingClientRect();
        return rect.width === 0 || rect.height === 0;
    }})()"""
    return screen_wait_for_condition(
        screen, js, timeout_s, label=f"hidden '{selector}'"
    )


def screen_wait_for_class(
    screen: "Screen",
    selector: str,
    class_name: str,
    timeout_s: float = 5.0,
) -> bool:
    """Wait for an element to have a specific class."""
    js = f"""(() => {{
        const el = document.querySelector('{selector}');
        return el && el.classList.contains('{class_name}');
    }})()"""
    return screen_wait_for_condition(
        screen, js, timeout_s, label=f"class '{class_name}' on '{selector}'"
    )


def screen_wait_for_no_class(
    screen: "Screen",
    selector: str,
    class_name: str,
    timeout_s: float = 5.0,
) -> bool:
    """Wait for an element to NOT have a specific class."""
    js = f"""(() => {{
        const el = document.querySelector('{selector}');
        return el && !el.classList.contains('{class_name}');
    }})()"""
    return screen_wait_for_condition(
        screen, js, timeout_s, label=f"no class '{class_name}' on '{selector}'"
    )


def screen_wait_for_codemirror_ready(screen: "Screen", timeout_s: float = 10.0) -> None:
    """Wait for CodeMirror editor to be interactive."""
    js = """(() => {
        const cm = document.querySelector('.cm-editor');
        if (!cm) return false;
        const content = cm.querySelector('.cm-content');
        if (!content) return false;
        return content.getAttribute('contenteditable') === 'true';
    })()"""
    if not screen_wait_for_condition(screen, js, timeout_s, label="CodeMirror ready"):
        raise AssertionError(f"CodeMirror not ready after {timeout_s}s")


def screen_wait_for_scene_ready(screen: "Screen", timeout_s: float = 30.0) -> None:
    """Wait for Three.js 3D scene to be fully initialized.

    Dismisses any startup dialogs (tutorial/safety), then checks that canvas exists
    and data-initializing attribute is removed, indicating scene has finished loading.

    Args:
        timeout_s: Maximum time to wait (default 30s for CI environments with SwiftShader)
    """
    from tests.helpers.browser_helpers import dismiss_dialogs

    # Dismiss any startup dialogs first (may appear with screen fixture)
    dismiss_dialogs(screen)

    js = """(() => {
        const canvas = document.querySelector('canvas');
        if (!canvas) return false;
        const sceneEl = canvas.closest('[data-initializing]');
        // Once initialized, data-initializing is removed
        return sceneEl === null && canvas.parentElement;
    })()"""
    if not screen_wait_for_condition(screen, js, timeout_s, label="3D scene ready"):
        raise AssertionError(f"3D scene not ready after {timeout_s}s")


def screen_get_scene_object(screen: "Screen", name: str) -> dict | None:
    """Find a Three.js object by name in the scene.

    Args:
        screen: Selenium screen fixture
        name: Name of the object to find (e.g., "tcp:ball")

    Returns:
        Dict with object info (name, type, visible, position), or None if not found
    """
    result = screen.selenium.execute_script(
        """
        const name = arguments[0];
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!sceneDiv) return null;

        const sceneId = sceneDiv.id;
        const scene = window['scene_' + sceneId];
        if (!scene) return null;

        let found = null;
        scene.traverse(function(obj) {
            if (obj.name === name) {
                found = {
                    name: obj.name,
                    type: obj.type,
                    visible: obj.visible,
                    position: obj.position ? {
                        x: obj.position.x,
                        y: obj.position.y,
                        z: obj.position.z
                    } : null
                };
            }
        });
        return found;
    """,
        name,
    )
    return result


def screen_list_scene_objects(screen: "Screen") -> list[dict]:
    """List all named objects in the Three.js scene for debugging.

    Args:
        screen: Selenium screen fixture

    Returns:
        List of dicts with object name, type, and visibility
    """
    result = screen.selenium.execute_script(
        """
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!sceneDiv) return [];

        const sceneId = sceneDiv.id;
        const scene = window['scene_' + sceneId];
        if (!scene) return [];

        const objects = [];
        scene.traverse(function(obj) {
            if (obj.name) {
                objects.push({
                    name: obj.name,
                    type: obj.type,
                    visible: obj.visible
                });
            }
        });
        return objects;
    """
    )
    return result or []


def screen_wait_for_tcp_ball(screen: "Screen", timeout_s: float = 20.0) -> dict | None:
    """Wait for TCP ball to exist in the Three.js scene.

    Args:
        screen: Selenium screen fixture
        timeout_s: Maximum time to wait (default 20s for CI environments)

    Returns:
        TCP ball object info, or None if timeout
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        tcp_ball = screen_get_scene_object(screen, "tcp:ball")
        if tcp_ball is not None:
            return tcp_ball
        time.sleep(0.1)
    return None


def screen_wait_for_button_icon(
    screen: "Screen",
    icon_name: str,
    timeout_s: float = 5.0,
) -> bool:
    """Wait for a button with a specific Material icon to be visible."""
    js = f"""(() => {{
        const buttons = document.querySelectorAll('button');
        for (const btn of buttons) {{
            const icon = btn.querySelector('i');
            if (icon && icon.innerText === '{icon_name}') {{
                const rect = btn.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }}
        }}
        return false;
    }})()"""
    return screen_wait_for_condition(
        screen, js, timeout_s, label=f"button icon '{icon_name}'"
    )


def screen_wait_for_tab_inactive(
    screen: "Screen",
    icon_name: str,
    timeout_s: float = 3.0,
) -> bool:
    """Wait for a tab (identified by icon) to become inactive."""
    js = f"""(() => {{
        const tabs = document.querySelectorAll('.q-tab');
        for (const tab of tabs) {{
            const icon = tab.querySelector('i');
            if (icon && icon.innerText === '{icon_name}') {{
                return !tab.classList.contains('q-tab--active');
            }}
        }}
        return false;
    }})()"""
    return screen_wait_for_condition(
        screen, js, timeout_s, label=f"tab '{icon_name}' inactive"
    )
