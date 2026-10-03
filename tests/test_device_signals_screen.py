"""Named I/O controls fit the setup panel with saved data populated."""

from nicegui import Client
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import ElementClickInterceptedException
from waldoctl.setup import SetupSnapshot
from waldoctl.signals import DigitalSignal

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


@pytest.mark.browser
def test_named_signal_controls_remain_readable(screen, tmp_path, monkeypatch):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    SetupStore(tmp_path).save(
        "bench",
        SetupSnapshot(
            signals={
                "part_ready": DigitalSignal("parol6", "input", 1, 2, 2),
                "valve": DigitalSignal("parol6", "output", 0, 2, 2, active_high=False),
            }
        ),
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def marked(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e for e in client.elements.values() if marker in e._markers)

    def click(marker):
        id = run_in_app(lambda: marked(marker).id)

        def try_click(driver):
            try:
                driver.find_element(By.ID, f"c{id}").click()
                return True
            except ElementClickInterceptedException:
                return False

        WebDriverWait(screen.selenium, 10).until(try_click)

    click("tab-setup")
    click("setup-load")
    WebDriverWait(screen.selenium, 10).until(
        lambda _: run_in_app(lambda: "valve" in marked("signal-existing").options)
    )
    next(
        tab
        for tab in screen.selenium.find_elements(By.CSS_SELECTOR, ".q-tab")
        if tab.text.strip().casefold() == "signals"
    ).click()

    def select():
        client = Client.instances[ui_state.active_client_id]
        with client:
            marked("signal-existing").set_value("valve")
            return marked("signal-name").id, marked("signal-message").id

    name_id, message_id = run_in_app(select)
    WebDriverWait(screen.selenium, 10).until(
        lambda driver: driver.find_element(By.ID, f"c{name_id}").get_attribute("value")
        == "valve"
    )
    click("signal-read")
    WebDriverWait(screen.selenium, 10).until(
        lambda driver: "Observed logical value:"
        in driver.find_element(By.TAG_NAME, "body").text
    )
    screen.selenium.execute_script(
        "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest'});",
        f"c{message_id}",
    )
    geometry = screen.selenium.execute_script(
        """
const e = document.getElementById(arguments[0]);
const panel = e.closest('.q-tab-panel');
return {width: panel.clientWidth, content: panel.scrollWidth};
""",
        f"c{message_id}",
    )
    assert geometry["content"] <= geometry["width"] + 1, geometry
    screen.selenium.save_screenshot(str(tmp_path / "named-device-signals.png"))
