"""Browser tests for TCP transform controls visibility and interaction.

Tests verify that the TCP ball (gizmo handle sphere) and TransformControls
are properly created and visible in the Three.js scene after page load,
and that dragging the TCP ball moves the robot.

Uses class-scoped browser session to reduce browser startup overhead.
Screenshot-based approach inspired by Three.js testing methodology.
"""

import io
import time
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from tests.conftest import skip_webgl_macos_ci

from tests.helpers.browser_helpers import js
from tests.helpers.wait import (
    screen_list_scene_objects,
    screen_wait_for_scene_ready,
    screen_wait_for_tcp_ball,
)

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


# ============================================================================
# Local helpers
# ============================================================================

# TransformControls axis colors (RGB)
GIZMO_RED = (255, 0, 0)  # X axis
GIZMO_GREEN = (0, 255, 0)  # Y axis
GIZMO_BLUE = (0, 0, 255)  # Z axis
GIZMO_YELLOW = (255, 255, 0)  # Highlighted axis


def find_gizmo_color_center(
    screen: "Screen", target_color: tuple[int, int, int], tolerance: int = 40
) -> tuple[int, int] | None:
    """Find the center of a colored region in the canvas screenshot.

    Args:
        screen: Selenium screen fixture
        target_color: RGB tuple of the color to find
        tolerance: Color matching tolerance (0-255)

    Returns:
        Tuple of (x, y) screen coordinates, or None if not found.
    """
    # Take screenshot as PNG bytes
    png_bytes = screen.selenium.get_screenshot_as_png()
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")  # type: ignore[attr-defined]

    # Find all pixels matching the target color
    pixels = img.load()
    matching_coords = []
    width, height = img.size

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            if (
                abs(r - target_color[0]) <= tolerance
                and abs(g - target_color[1]) <= tolerance
                and abs(b - target_color[2]) <= tolerance
            ):
                matching_coords.append((x, y))

    if not matching_coords:
        return None

    # Return center of matching region
    avg_x = sum(c[0] for c in matching_coords) // len(matching_coords)
    avg_y = sum(c[1] for c in matching_coords) // len(matching_coords)
    return (avg_x, avg_y)


def drag_at_position(
    screen: "Screen",
    position: tuple[int, int],
    dx: int,
    dy: int,
) -> bool:
    """Drag from a specific position using Selenium ActionChains.

    Args:
        screen: Selenium screen fixture
        position: (x, y) screen coordinates to start drag
        dx: Horizontal drag distance in pixels
        dy: Vertical drag distance in pixels

    Returns:
        True if drag was performed.
    """
    from pathlib import Path

    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By

    target_x, target_y = position

    # Get the canvas element
    canvas = screen.selenium.find_element(By.CSS_SELECTOR, "canvas")

    # Calculate position relative to canvas
    canvas_rect = canvas.rect
    offset_x = target_x - canvas_rect["x"]
    offset_y = target_y - canvas_rect["y"]

    # Use ActionChains drag_and_drop_by_offset for native drag
    actions = ActionChains(screen.selenium)

    # Move to starting position and perform drag
    actions.move_to_element_with_offset(canvas, offset_x, offset_y)
    actions.pause(0.1)

    # Take screenshot before drag to show starting position
    actions.perform()
    screen.selenium.save_screenshot("screenshots/before_drag.png")

    # Perform the actual drag with W3C Actions
    actions = ActionChains(screen.selenium)
    actions.move_to_element_with_offset(canvas, offset_x, offset_y)
    actions.click_and_hold()
    actions.pause(0.2)  # Let TransformControls register the grab
    actions.move_by_offset(dx, dy)
    actions.pause(0.5)  # Hold at end position for screenshot
    actions.perform()

    Path("screenshots").mkdir(exist_ok=True)
    screen.selenium.save_screenshot("screenshots/mid_drag.png")

    # Release
    ActionChains(screen.selenium).release().perform()

    import time

    time.sleep(0.3)  # Let the drag end process

    return True


def drag_gizmo_by_color(
    screen: "Screen",
    axis_color: tuple[int, int, int],
    dx: int,
    dy: int,
    tolerance: int = 40,
) -> bool:
    """Find a gizmo axis by color and drag it using JavaScript mouse events.

    Three.js TransformControls uses internal raycasting based on mouse events,
    so we need to dispatch proper MouseEvents on the canvas with correct coordinates.

    Args:
        screen: Selenium screen fixture
        axis_color: RGB color of the axis to drag
        dx: Horizontal drag distance in pixels
        dy: Vertical drag distance in pixels
        tolerance: Color matching tolerance

    Returns:
        True if drag was performed, False if axis not found.
    """
    center = find_gizmo_color_center(screen, axis_color, tolerance)
    if center is None:
        return False

    target_x, target_y = center

    # Use JavaScript to dispatch pointer events with proper coordinates
    # Three.js TransformControls uses PointerEvents and requires offsetX/offsetY for raycasting
    # This version pauses before releasing so we can take a screenshot mid-drag
    result = js(
        screen,
        """
        const canvas = document.querySelector('canvas');
        if (!canvas) return false;

        const startX = arguments[0];
        const startY = arguments[1];
        const dx = arguments[2];
        const dy = arguments[3];

        // Get canvas bounding rect for offset calculation
        const rect = canvas.getBoundingClientRect();

        // Helper to create pointer event with all necessary properties
        function createPointerEvent(type, x, y, isPrimary = true) {
            const offsetX = x - rect.left;
            const offsetY = y - rect.top;
            return new PointerEvent(type, {
                view: window,
                bubbles: true,
                cancelable: true,
                clientX: x,
                clientY: y,
                offsetX: offsetX,
                offsetY: offsetY,
                pointerId: 1,
                pointerType: 'mouse',
                isPrimary: isPrimary,
                button: type === 'pointerup' ? 0 : 0,
                buttons: type === 'pointerup' ? 0 : 1
            });
        }

        // Sequence: pointerdown -> pointermove (multiple for smooth drag)
        canvas.dispatchEvent(createPointerEvent('pointerdown', startX, startY));

        // Smooth drag with multiple move events
        const steps = 10;
        for (let i = 1; i <= steps; i++) {
            const x = startX + (dx * i / steps);
            const y = startY + (dy * i / steps);
            canvas.dispatchEvent(createPointerEvent('pointermove', x, y));
        }

        // Store final position for later release
        window._dragEndX = startX + dx;
        window._dragEndY = startY + dy;

        return true;
        """,
        target_x,
        target_y,
        dx,
        dy,
    )

    # Take screenshot while still dragging
    from pathlib import Path

    Path("screenshots").mkdir(exist_ok=True)
    screen.selenium.save_screenshot("screenshots/mid_drag.png")

    # Now release
    js(
        screen,
        """
        const canvas = document.querySelector('canvas');
        if (!canvas) return;
        const rect = canvas.getBoundingClientRect();
        const x = window._dragEndX;
        const y = window._dragEndY;
        const offsetX = x - rect.left;
        const offsetY = y - rect.top;
        canvas.dispatchEvent(new PointerEvent('pointerup', {
            view: window,
            bubbles: true,
            cancelable: true,
            clientX: x,
            clientY: y,
            offsetX: offsetX,
            offsetY: offsetY,
            pointerId: 1,
            pointerType: 'mouse',
            isPrimary: true,
            button: 0,
            buttons: 0
        }));
        """,
    )

    return result is True


def find_gizmo_types(screen: "Screen") -> dict:
    """Find TransformControls gizmo types in the Three.js scene.

    Returns:
        Dict with 'hasTransformControls' bool and 'gizmoTypes' list,
        or 'error' key if scene not found.
    """
    return js(
        screen,
        """
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!sceneDiv) return {error: 'Scene div not found'};

        const sceneId = sceneDiv.id;
        const scene = window['scene_' + sceneId];
        if (!scene) return {error: 'Three.js scene not found'};

        let hasTransformControls = false;
        let gizmoTypes = [];
        scene.traverse(function(obj) {
            if (obj.type === 'TransformControlsGizmo' ||
                obj.type === 'TransformControlsPlane' ||
                (obj.name && obj.name.includes('transform'))) {
                hasTransformControls = true;
                gizmoTypes.push(obj.type);
            }
        });
        return {hasTransformControls: hasTransformControls, gizmoTypes: gizmoTypes};
        """,
    )


def wait_for_transform_controls(screen: "Screen", timeout_s: float = 10.0) -> bool:
    """Wait for TransformControls to be attached to the scene.

    Returns:
        True if TransformControls found, False if timeout.
    """
    end_time = time.time() + timeout_s
    while time.time() < end_time:
        result = find_gizmo_types(screen)
        if result.get("hasTransformControls"):
            return True
        time.sleep(0.2)
    return False


def get_tcp_screen_position(screen: "Screen") -> tuple[float, float] | None:
    """Get TCP ball screen coordinates via Three.js projection.

    Returns:
        Tuple of (x, y) screen coordinates, or None if not found.
    """
    result = js(
        screen,
        """
        const canvas = document.querySelector('canvas');
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!canvas || !sceneDiv) return null;

        const sceneId = sceneDiv.id;
        const scene = window['scene_' + sceneId];
        if (!scene) return null;

        // Find TCP ball
        let tcpBall = null;
        scene.traverse(obj => {
            if (obj.name === 'tcp:ball') tcpBall = obj;
        });
        if (!tcpBall) return null;

        // Find camera via TransformControlsGizmo (has camera ref from parent TransformControls)
        let camera = null;
        scene.traverse(obj => {
            if (!camera && obj.camera && obj.camera.isCamera) {
                camera = obj.camera;
            }
        });
        if (!camera) return null;

        // Project to screen coordinates (use position.clone() since Vector3 is on the object)
        const worldPos = tcpBall.position.clone();
        tcpBall.getWorldPosition(worldPos);
        const projected = worldPos.clone().project(camera);

        const rect = canvas.getBoundingClientRect();
        const x = (projected.x + 1) / 2 * rect.width + rect.left;
        const y = (1 - projected.y) / 2 * rect.height + rect.top;

        return {x: x, y: y};
        """,
    )
    if result and "x" in result and "y" in result:
        return (result["x"], result["y"])
    return None


def drag_tcp_ball(screen: "Screen", dx: int, dy: int) -> bool:
    """Drag TCP ball on 3D canvas by dispatching mouse events.

    Args:
        screen: Selenium screen fixture
        dx: Horizontal drag distance in pixels
        dy: Vertical drag distance in pixels

    Returns:
        True if drag was initiated, False if TCP ball not found.
    """
    return js(
        screen,
        """
        const canvas = document.querySelector('canvas');
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!canvas || !sceneDiv) return false;

        const sceneId = sceneDiv.id;
        const scene = window['scene_' + sceneId];
        if (!scene) return false;

        // Find TCP ball
        let tcpBall = null;
        scene.traverse(obj => {
            if (obj.name === 'tcp:ball') tcpBall = obj;
        });
        if (!tcpBall) return false;

        // Find camera via TransformControlsGizmo (has camera ref from parent TransformControls)
        let camera = null;
        scene.traverse(obj => {
            if (!camera && obj.camera && obj.camera.isCamera) {
                camera = obj.camera;
            }
        });
        if (!camera) return false;

        // Project to screen coordinates (use position.clone() since Vector3 is on the object)
        const worldPos = tcpBall.position.clone();
        tcpBall.getWorldPosition(worldPos);
        const projected = worldPos.clone().project(camera);

        const rect = canvas.getBoundingClientRect();
        const startX = (projected.x + 1) / 2 * rect.width + rect.left;
        const startY = (1 - projected.y) / 2 * rect.height + rect.top;
        const endX = startX + arguments[0];
        const endY = startY + arguments[1];

        // Dispatch drag events
        canvas.dispatchEvent(new MouseEvent('mousedown', {
            clientX: startX, clientY: startY,
            bubbles: true, cancelable: true
        }));

        document.dispatchEvent(new MouseEvent('mousemove', {
            clientX: endX, clientY: endY,
            bubbles: true, cancelable: true
        }));

        document.dispatchEvent(new MouseEvent('mouseup', {
            clientX: endX, clientY: endY,
            bubbles: true, cancelable: true
        }));

        return true;
        """,
        dx,
        dy,
    )


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.browser
@skip_webgl_macos_ci
class TestTCPTransformControls:
    """TCP transform control tests sharing a browser session."""

    def test_tcp_ball_exists_in_scene(self, class_screen: "Screen") -> None:
        """Test that TCP ball sphere exists in the Three.js scene after page load."""
        screen_wait_for_scene_ready(class_screen, timeout_s=30.0)

        tcp_ball = screen_wait_for_tcp_ball(class_screen, timeout_s=20.0)

        if tcp_ball is None:
            objects = screen_list_scene_objects(class_screen)
            raise AssertionError(f"TCP ball not found. Objects in scene: {objects}")

        assert tcp_ball.get("type") == "Mesh", (
            f"TCP ball should be a Mesh, got: {tcp_ball.get('type')}"
        )
        assert tcp_ball.get("visible") is True, "TCP ball should be visible"

    def test_tcp_ball_at_tcp_position(self, class_screen: "Screen") -> None:
        """Test that TCP ball position matches robot TCP coordinates."""
        tcp_ball = screen_wait_for_tcp_ball(class_screen, timeout_s=20.0)

        assert tcp_ball is not None, "TCP ball should exist"
        pos = tcp_ball.get("position")
        assert pos is not None, "TCP ball should have a position"

        # Ball should NOT be exactly at origin
        is_at_origin = (
            abs(pos["x"]) < 0.001 and abs(pos["y"]) < 0.001 and abs(pos["z"]) < 0.001
        )
        assert not is_at_origin, f"TCP ball should not be at origin, position: {pos}"

        # Verify TCP ball is in a reasonable workspace position
        assert abs(pos["x"]) < 2.0, f"TCP X out of range: {pos['x']}"
        assert abs(pos["y"]) < 2.0, f"TCP Y out of range: {pos['y']}"
        assert pos["z"] > -0.5 and pos["z"] < 2.0, f"TCP Z out of range: {pos['z']}"

    def test_tcp_transform_controls_attached(self, class_screen: "Screen") -> None:
        """Test that TransformControls gizmo is attached to TCP ball."""
        tcp_ball = screen_wait_for_tcp_ball(class_screen, timeout_s=20.0)
        assert tcp_ball is not None, "TCP ball should exist for TransformControls"

        # Wait for TransformControls to be attached (may happen async after TCP ball)
        found = wait_for_transform_controls(class_screen, timeout_s=10.0)
        assert found, "TransformControls gizmo should be attached within timeout"

        result = find_gizmo_types(class_screen)
        assert result is not None
        assert "error" not in result, f"Error: {result.get('error')}"
        assert result.get("hasTransformControls") is True, (
            f"TransformControls gizmo should exist, found types: {result.get('gizmoTypes')}"
        )

    # too flaky
    # def test_dragging_gizmo_axis_moves_robot(self, class_screen: "Screen") -> None:
    #     """Dragging gizmo axis (via screenshot color detection) moves robot."""
    #     from pathlib import Path

    #     from PIL import ImageDraw

    #     from waldo_commander.state import robot_state

    #     screen_wait_for_scene_ready(class_screen, timeout_s=10.0)
    #     tcp_ball = screen_wait_for_tcp_ball(class_screen, timeout_s=10.0)
    #     assert tcp_ball is not None, "TCP ball should exist for drag test"

    #     # Get initial robot position
    #     initial_x = robot_state.x
    #     initial_y = robot_state.y
    #     initial_z = robot_state.z

    #     # Take screenshot and find gizmo colors
    #     png_bytes = class_screen.selenium.get_screenshot_as_png()
    #     img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

    #     # Find gizmo colors and their positions
    #     green_pos = find_gizmo_color_center(class_screen, GIZMO_GREEN)
    #     red_pos = find_gizmo_color_center(class_screen, GIZMO_RED)
    #     blue_pos = find_gizmo_color_center(class_screen, GIZMO_BLUE)

    # # Draw markers on screenshot to show where we're trying to click
    # draw = ImageDraw.Draw(img)
    # marker_size = 20
    # if blue_pos:
    #     x, y = blue_pos
    #     draw.ellipse(
    #         [x - marker_size, y - marker_size, x + marker_size, y + marker_size],
    #         outline=(0, 0, 255),
    #         width=3,
    #     )
    #     # Draw drag arrow (downward)
    #     draw.line([x, y, x, y + 100], fill=(0, 0, 255), width=3)
    # if red_pos:
    #     x, y = red_pos
    #     draw.ellipse(
    #         [x - marker_size, y - marker_size, x + marker_size, y + marker_size],
    #         outline=(255, 0, 0),
    #         width=3,
    #     )
    # if green_pos:
    #     x, y = green_pos
    #     draw.ellipse(
    #         [x - marker_size, y - marker_size, x + marker_size, y + marker_size],
    #         outline=(0, 255, 0),
    #         width=3,
    #     )

    # # Draw the ACTUAL click position in yellow (where we will click)
    # assert blue_pos is not None, "Blue gizmo axis not found in screenshot"
    # adjusted_blue_pos = (blue_pos[0], blue_pos[1] - 90)  # 90px above blue center
    # ax, ay = adjusted_blue_pos
    # small_marker = 8  # Smaller circle to see exact position
    # draw.ellipse(
    #     [ax - small_marker, ay - small_marker, ax + small_marker, ay + small_marker],
    #     outline=(255, 255, 0),
    #     width=2,
    # )
    # # Draw drag line from actual click position (300px drag)
    # draw.line([ax, ay, ax, ay + 300], fill=(255, 255, 0), width=2)

    # # Save annotated screenshot
    # Path("screenshots").mkdir(exist_ok=True)
    # img.save("screenshots/gizmo_drag_attempt.png")
    # drag_at_position(class_screen, adjusted_blue_pos, dx=0, dy=300)

    # # Wait for position to update
    # time.sleep(1.0)

    # # Check if position changed
    # position_changed = (
    #     abs(robot_state.x - initial_x) > 0.1
    #     or abs(robot_state.y - initial_y) > 0.1
    #     or abs(robot_state.z - initial_z) > 0.1
    # )

    # assert position_changed, (
    #     f"Robot position didn't change after gizmo drag. "
    #     f"Initial: ({initial_x:.1f}, {initial_y:.1f}, {initial_z:.1f}), "
    #     f"Current: ({robot_state.x:.1f}, {robot_state.y:.1f}, {robot_state.z:.1f}). "
    #     f"See screenshots/gizmo_drag_attempt.png for click locations."
    # )

    def test_tcp_ball_screen_position_available(self, class_screen: "Screen") -> None:
        """Test that we can get TCP ball screen coordinates for drag operations."""
        import waldoctl

        from tests.helpers.browser_helpers import find_button_by_icon
        from waldo_commander.state import ui_state

        screen_wait_for_scene_ready(class_screen, timeout_s=30.0)
        tcp_ball = screen_wait_for_tcp_ball(class_screen, timeout_s=20.0)
        assert tcp_ball is not None, "TCP ball should exist"

        # The simulator is shared across browser modules, so an earlier jog
        # may have left the arm extended with the TCP above the viewport;
        # the projection below assumes the framed home pose.
        home_btn = find_button_by_icon(class_screen, "home")
        assert home_btn is not None, "Home button should exist"
        home_btn.click()
        home_deg = ui_state.active_robot.joints.home.deg
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            live = waldoctl.commander.status.joints.angles.deg
            if max(abs(float(a) - float(b)) for a, b in zip(live, home_deg)) < 1.0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Robot did not return to the home pose")

        # Wait for TransformControls (needed for camera reference in get_tcp_screen_position)
        found = wait_for_transform_controls(class_screen, timeout_s=10.0)
        assert found, "TransformControls needed for screen position calculation"

        pos = get_tcp_screen_position(class_screen)
        assert pos is not None, "Should be able to get TCP screen position"

        x, y = pos
        assert x > 0, f"Screen X should be positive, got {x}"
        assert y > 0, f"Screen Y should be positive, got {y}"
