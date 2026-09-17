"""Long skill forms scroll while keeping their action buttons accessible."""

from nicegui import Client
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state
from waldoctl.setup import Pose, SetupSnapshot


@pytest.mark.browser
def test_skill_library_form_keeps_actions_visible(screen, tmp_path, monkeypatch):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    SetupStore(tmp_path).save(
        "bench", SetupSnapshot(poses={"pick": Pose((15, 222, 179, 85, 2, 87))})
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def choose():
        client = Client.instances[ui_state.active_client_id]
        with client:

            def marked(marker):
                return next(e for e in client.elements.values() if marker in e._markers)

            marked("skill-choice").set_value("waldo.approach")
            return marked("tab-skills").id, marked("skill-run").id

    tab_id, run_id = run_in_app(choose)
    screen.selenium.find_element(By.ID, f"c{tab_id}").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda driver: driver.find_element(By.ID, f"c{run_id}").is_displayed()
    )
    dimensions = screen.selenium.execute_script(
        "const e=document.getElementById(arguments[0]);"
        "const r=e.getBoundingClientRect();"
        "const form=document.querySelector('.skill-library-form-scroll');"
        "return {visible:r.bottom < innerHeight && e.contains(document.elementFromPoint(r.x+5,r.y+5)),"
        "width:form.clientWidth, content:form.scrollWidth};",
        f"c{run_id}",
    )
    assert dimensions["visible"], dimensions
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    screen.selenium.save_screenshot(str(tmp_path / "skill-library.png"))
