"""Long skill forms scroll while keeping their action buttons accessible."""

import pytest
from nicegui import Client
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_vision import localization_scene
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


@pytest.mark.browser
def test_skill_library_form_keeps_actions_visible(screen, tmp_path, monkeypatch):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    SetupStore(tmp_path).save(
        "bench",
        SetupSnapshot(
            poses={
                "pick": Pose((15, 222, 179, 85, 2, 87)),
                "place": Pose((45, 222, 179, 85, 2, 87)),
            },
            signals={"grip": DigitalSignal("parol6", "output", 0, 2, 2)},
            cameras=localization_scene()[1].cameras,
        ),
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def choose(identity="waldo.approach"):
        client = Client.instances[ui_state.active_client_id]
        with client:

            def marked(marker):
                return next(e for e in client.elements.values() if marker in e._markers)

            marked("skill-choice").set_value(identity)
            if identity == "waldo.transfer_with_signal":
                marked("skill-place-pose").set_value("place")
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
    run_in_app(lambda: choose("waldo.locate_board"))
    WebDriverWait(screen.selenium, 10).until(
        lambda driver: (
            "Uses the active camera." in driver.find_element(By.TAG_NAME, "body").text
        )
    )
    dimensions = screen.selenium.execute_script(
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const r=document.getElementById(arguments[0]).getBoundingClientRect();"
        "return {width:form.clientWidth, content:form.scrollWidth, bottom:r.bottom, height:innerHeight};",
        f"c{run_id}",
    )
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    assert dimensions["bottom"] < dimensions["height"], dimensions
    ActionChains(screen.selenium).move_to_element(
        screen.selenium.find_element(By.TAG_NAME, "body")
    ).perform()
    WebDriverWait(
        screen.selenium, 10, ignored_exceptions=(StaleElementReferenceException,)
    ).until(
        lambda driver: (
            not any(
                e.is_displayed()
                for e in driver.find_elements(By.CSS_SELECTOR, ".q-tooltip")
            )
        )
    )
    screen.selenium.save_screenshot(str(tmp_path / "vision-localization.png"))
    run_in_app(lambda: choose("waldo.transfer_with_signal"))
    WebDriverWait(screen.selenium, 10).until(
        lambda driver: "Closed value" in driver.find_element(By.TAG_NAME, "body").text
    )
    dimensions = screen.selenium.execute_script(
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const r=document.getElementById(arguments[0]).getBoundingClientRect();"
        "return {width:form.clientWidth, content:form.scrollWidth, bottom:r.bottom, height:innerHeight};",
        f"c{run_id}",
    )
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    assert dimensions["bottom"] < dimensions["height"], dimensions
    screen.selenium.save_screenshot(str(tmp_path / "tray-transfer.png"))
