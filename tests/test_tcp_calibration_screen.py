"""Saved TCP measurements remain readable in the running calibration panel."""

from nicegui import Client
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl.setup import Frame, SetupSnapshot, TcpCalibration

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


@pytest.mark.browser
def test_saved_tcp_calibration_is_readable_with_hardware_scene(
    screen, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    SetupStore(tmp_path).save(
        "bench",
        SetupSnapshot(
            frames={"axes": Frame((0, 0, 0, 84.6, 2.07, 86.63))},
            tcp_calibrations={
                "tip": TcpCalibration(
                    (0.008, 0.001, 25.0, -8.04, -5.94, -0.83),
                    "NONE",
                    position_rms_mm=0.007,
                    position_samples=4,
                    orientation_reference="axes",
                )
            },
        ),
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def marked(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e for e in client.elements.values() if marker in e._markers)

    tab_id = run_in_app(lambda: marked("tab-setup").id)
    screen.selenium.find_element(By.ID, f"c{tab_id}").click()
    load_id = run_in_app(lambda: marked("setup-load").id)
    screen.selenium.find_element(By.ID, f"c{load_id}").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda _: run_in_app(
            lambda: "tip" in marked("tcp-calibration-existing").options
        )
    )
    next(
        tab
        for tab in screen.selenium.find_elements(By.CSS_SELECTOR, ".q-tab")
        if tab.text.strip() == "TCP"
    ).click()

    def select():
        client = Client.instances[ui_state.active_client_id]
        with client:
            marked("tcp-calibration-existing").set_value("tip")
            return marked("tcp-calibration-z").id, marked("tcp-calibration-apply").id

    z_id, apply_id = run_in_app(select)
    WebDriverWait(screen.selenium, 10).until(
        lambda driver: driver.find_element(By.ID, f"c{z_id}").is_displayed()
    )
    z_input = screen.selenium.find_element(By.ID, f"c{z_id}")
    WebDriverWait(screen.selenium, 10).until(
        lambda _: float(z_input.get_attribute("value")) == 25.0
    )
    screen.selenium.execute_script(
        "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest'});",
        f"c{apply_id}",
    )
    screen.selenium.save_screenshot(str(tmp_path / "tcp-calibration.png"))
    assert "RMS 0.007 mm" in screen.selenium.find_element(By.TAG_NAME, "body").text
    geometry = screen.selenium.execute_script(
        """
const e = document.getElementById(arguments[0]);
const panel = e.closest('.q-tab-panel');
return {width: panel.clientWidth, content: panel.scrollWidth};
""",
        f"c{apply_id}",
    )
    assert geometry["content"] <= geometry["width"] + 1, geometry
    screen.selenium.execute_script(
        "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest'});",
        f"c{apply_id}",
    )
    screen.selenium.save_screenshot(str(tmp_path / "tcp-calibration.png"))
