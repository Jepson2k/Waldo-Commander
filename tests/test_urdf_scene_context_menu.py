"""Test URDF scene context menu and envelope visibility.

Context-menu tests share a browser session (class_screen) so the 30-second
WebGL initialisation only happens once instead of per-test.
"""

import time
from typing import TYPE_CHECKING

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.wait import screen_wait_for_scene_ready

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


def _retry_find_elements(finder_func, retries: int = 3, delay: float = 0.2):
    """Retry finding elements to handle StaleElementReferenceException."""
    for i in range(retries):
        try:
            return finder_func()
        except StaleElementReferenceException:
            if i == retries - 1:
                raise
            time.sleep(delay)


def _open_context_menu(screen: "Screen"):
    """Right-click the 3D canvas and return the visible context menu element."""
    canvas = WebDriverWait(screen.selenium, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "canvas"))
    )

    for _attempt in range(5):
        ActionChains(screen.selenium).context_click(canvas).perform()
        try:
            return WebDriverWait(screen.selenium, 2).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, ".q-menu"))
            )
        except Exception:
            time.sleep(0.5)

    raise AssertionError("Context menu not visible after 5 right-click attempts")


# ---------------------------------------------------------------------------
# Context-menu tests — share one browser session via class_screen
# ---------------------------------------------------------------------------


@pytest.mark.browser
@skip_webgl_macos_ci
class TestContextMenu:
    """Context menu behaviour on the 3D scene canvas."""

    def test_right_click_shows_context_menu(self, class_screen: "Screen") -> None:
        """Right-clicking on the 3D scene shows the context menu."""
        screen_wait_for_scene_ready(class_screen)
        menu = _open_context_menu(class_screen)
        assert menu.is_displayed()

    def test_context_menu_has_options(self, class_screen: "Screen") -> None:
        """Context menu contains at least one item."""
        menu = _open_context_menu(class_screen)
        items = _retry_find_elements(
            lambda: menu.find_elements(By.CSS_SELECTOR, ".q-item")
        )
        assert len(items) > 0, "Context menu should have options"

    def test_context_menu_closes_on_click_outside(self, class_screen: "Screen") -> None:
        """Context menu closes when clicking outside."""
        menu = _open_context_menu(class_screen)
        assert menu.is_displayed()

        canvas = class_screen.selenium.find_element(By.CSS_SELECTOR, "canvas")
        ActionChains(class_screen.selenium).move_to_element(canvas).click().perform()
        time.sleep(0.3)

        menus = class_screen.selenium.find_elements(By.CSS_SELECTOR, ".q-menu")
        visible = [m for m in menus if m.is_displayed()]
        assert len(visible) == 0, "Context menu should close when clicking outside"


# ---------------------------------------------------------------------------
# Envelope test — needs enable_envelope before app startup, own session
# ---------------------------------------------------------------------------


@pytest.mark.browser
@skip_webgl_macos_ci
def test_envelope_visible_when_mode_on(screen, enable_envelope) -> None:
    """Envelope sphere is visible in scene when mode is 'on'."""
    screen.open("/")
    screen_wait_for_scene_ready(screen)

    from waldo_commander.services.urdf_scene.envelope_renderer import workspace_envelope

    for _ in range(150):  # Up to 15 seconds
        if workspace_envelope._generated and workspace_envelope.stl_url:
            break
        time.sleep(0.1)

    assert workspace_envelope._generated, (
        "Envelope should be generated before testing visibility"
    )

    settings_tab = WebDriverWait(screen.selenium, 5).until(
        EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(@class, 'q-tab')]//*[text()='Settings']")
        )
    )
    settings_tab.click()
    time.sleep(0.3)

    screen.selenium.execute_script(
        """
        const labels = document.querySelectorAll('*');
        for (const label of labels) {
            if (label.textContent.includes('Workspace Envelope') &&
                !label.textContent.includes('Show reachable')) {
                const row = label.closest('.row');
                if (row) {
                    const select = row.querySelector('.q-select');
                    if (select) select.click();
                    return;
                }
            }
        }
    """
    )
    time.sleep(0.3)

    on_option = WebDriverWait(screen.selenium, 5).until(
        EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(@class, 'q-item')]//*[text()='On']")
        )
    )
    on_option.click()
    time.sleep(1.0)

    result = screen.selenium.execute_script(
        """
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!sceneDiv) return {found: false, objects: []};
        const sceneId = sceneDiv.id;
        const scene = window['scene_' + sceneId];
        if (!scene) return {found: false, objects: []};
        let found = false;
        let objects = [];
        scene.traverse(obj => {
            if (obj.name) objects.push(obj.name);
            if (obj.name && obj.name.toLowerCase().includes('envelope')) found = true;
        });
        return {found: found, objects: objects};
    """
    )

    assert result and result.get("found") is True, (
        f"Envelope sphere should be visible in scene when mode='on'. "
        f"Found objects: {result.get('objects', []) if result else []}"
    )
