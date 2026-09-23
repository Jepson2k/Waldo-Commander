"""A skill's form opens beside the 3D view from the editor's Insert menu: the
scene stays usable, the path is drawn, and Insert stays in reach."""

import pytest
from nicegui import Client
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state
from waldoctl.setup import Pose, SetupSnapshot


@pytest.mark.browser
def test_a_skill_form_opens_beside_the_scene_and_keeps_insert_in_reach(
    screen, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    SetupStore(tmp_path).save(
        "bench", SetupSnapshot(poses={"pick": Pose((15, 222, 179, 85, 2, 87))})
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    driver = screen.selenium
    driver.set_window_size(1366, 768)

    def element_id(marker: str) -> int | None:
        def find():
            client = Client.instances[ui_state.active_client_id]
            return next(
                (e.id for e in client.elements.values() if marker in e._markers), None
            )

        return run_in_app(find)

    def click(marker: str) -> None:
        target = WebDriverWait(driver, 10).until(
            lambda d: (
                (found := element_id(marker)) is not None
                and (el := d.find_element(By.ID, f"c{found}")).is_displayed()
                and el
            )
        )
        target.click()

    def preview_objects() -> int:
        scene = ui_state.urdf_scene
        assert scene is not None
        return len(scene._skill_preview_objects)

    click("tab-program")
    click("editor-commands-btn")
    click("editor-skills-menu")
    # A skill's diagram is its label in the menu; one that failed to load is
    # an empty square beside a word.
    loaded = WebDriverWait(driver, 10).until(
        lambda d: d.execute_script(
            "const imgs=[...document.querySelectorAll('.q-menu .skill-menu-icon img')];"
            "return imgs.length && imgs.every(i => i.complete && i.naturalWidth > 0);"
        )
    )
    assert loaded
    click("editor-skill-waldo.approach")

    # The saved pose fills Approach's target, so the form has a motion to draw.
    WebDriverWait(driver, 15).until(lambda _: run_in_app(preview_objects) > 0)
    insert_id = element_id("skill-insert")
    layout = driver.execute_script(
        "const e=document.getElementById(arguments[0]);"
        "const r=e.getBoundingClientRect();"
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const canvas=document.querySelector('canvas').getBoundingClientRect();"
        "const hit=document.elementFromPoint(canvas.x+canvas.width/2, canvas.y+canvas.height/2);"
        "return {insert:r.bottom < innerHeight && e.contains(document.elementFromPoint(r.x+5,r.y+5)),"
        "width:form.clientWidth, content:form.scrollWidth, scene: hit && hit.tagName};",
        f"c{insert_id}",
    )
    assert layout["insert"], layout
    assert layout["content"] <= layout["width"] + 1, layout
    # No backdrop: the middle of the scene is still the scene.
    assert layout["scene"] == "CANVAS", layout
    driver.save_screenshot(str(tmp_path / "skill-form.png"))

    click("skill-close")
    WebDriverWait(driver, 10).until(lambda _: run_in_app(preview_objects) == 0)
