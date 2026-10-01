"""The workspace envelope shows in the scene when its mode is on."""

import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import click_tab, marked_element
from tests.helpers.wait import screen_wait_for_scene_ready


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

    # Settings is a dialog from the gear; the envelope row is under View.
    click_tab(screen, "settings")
    marked_element(screen, "settings-cat-view").click()
    WebDriverWait(screen.selenium, 5).until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//*[contains(@class, 'settings-row')][.//*[text()='Workspace envelope']]//*[contains(@class, 'q-select')]",
            )
        )
    ).click()

    on_option = WebDriverWait(screen.selenium, 5).until(
        EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(@class, 'q-item')]//*[text()='On']")
        )
    )
    on_option.click()

    find_envelope = """
        const sceneDiv = document.querySelector('.nicegui-scene');
        if (!sceneDiv) return {found: false, objects: []};
        const scene = window['scene_' + sceneDiv.id];
        if (!scene) return {found: false, objects: []};
        let found = false;
        let objects = [];
        scene.traverse(obj => {
            if (obj.name) objects.push(obj.name);
            if (obj.name && obj.name.toLowerCase().includes('envelope')) found = true;
        });
        return {found: found, objects: objects};
    """
    try:
        result = WebDriverWait(screen.selenium, 10).until(
            lambda d: (r := d.execute_script(find_envelope)) and r["found"] and r
        )
    except TimeoutException:
        result = screen.selenium.execute_script(find_envelope)

    assert result and result.get("found") is True, (
        f"Envelope sphere should be visible in scene when mode='on'. "
        f"Found objects: {result.get('objects', []) if result else []}"
    )
