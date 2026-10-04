"""Saved setup data stays readable in its panels: the named-signal and TCP
calibration tabs of the setup panel, and the camera panel in its widest
state, a solved fixed-mount calibration with its saved-data row, all fit
without horizontal clipping.

The tests share one page through ``class_screen``, built over a setup
directory holding the saved setup and with the camera backends serving a
rendered board. The camera test saves into that setup, so it runs last.
"""

import numpy as np
import pytest
from nicegui import Client
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
)
from selenium.webdriver.common.by import By
from waldoctl.setup import Frame, SetupSnapshot, TcpCalibration
from waldoctl.signals import DigitalSignal

from tests.helpers.browser_helpers import js, run_in_app
from tests.helpers.browser_session import wait
from tests.helpers.charuco_render import render_board_view
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_handeye_panel_integration import _FrameBackend, _jpeg
from tests.test_handeye_service import IMAGE_SIZE, K_TRUE, SPEC, _samples
from waldo_commander.services import handeye
from waldo_commander.services.camera_calibration import CaptureBinding
from waldo_commander.services.camera_service import camera_service
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state

_FITS = """
const panel = document.getElementById(arguments[0]).closest(arguments[1]);
return {width: panel.clientWidth, content: panel.scrollWidth};
"""


@pytest.fixture(scope="class")
def saved_setup(tmp_path_factory: pytest.TempPathFactory):
    from waldo_commander.services import camera_service as module

    directory = tmp_path_factory.mktemp("setups")
    SetupStore(directory).save(
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
            signals={
                "part_ready": DigitalSignal("parol6", "input", 1, 2, 2),
                "valve": DigitalSignal("parol6", "output", 0, 2, 2, active_high=False),
            },
        ),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WALDO_SETUP_DIR", str(directory))
        mp.setattr(module, "LinuxpyBackend", _FrameBackend)
        mp.setattr(module, "OpenCVBackend", _FrameBackend)
        yield


def _marked(marker: str):
    client = Client.instances[ui_state.active_client_id]
    return next(e for e in client.elements.values() if marker in e._markers)


def _click(screen, marker: str) -> None:
    id = run_in_app(lambda: _marked(marker).id)

    def try_click(driver) -> bool:
        try:
            driver.find_element(By.ID, f"c{id}").click()
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException):
            # The tab's panel is still opening.
            return False

    wait(screen).until(try_click)


def _open_tab(screen, label: str) -> None:
    next(
        tab
        for tab in screen.selenium.find_elements(By.CSS_SELECTOR, ".q-tab")
        if tab.text.strip().casefold() == label.casefold()
    ).click()


def _page_text(screen) -> str:
    return js(screen, "return document.body.innerText")


@pytest.mark.browser
@pytest.mark.usefixtures("saved_setup")
class TestSetupPanels:
    def test_saved_signals_and_tcp_calibration_fit_the_setup_panel(
        self, class_screen
    ) -> None:
        screen = class_screen
        screen_wait_for_scene_ready(screen, timeout_s=40)
        _click(screen, "tab-setup")
        _click(screen, "setup-load")
        wait(screen).until(
            lambda _: run_in_app(
                lambda: "valve" in _marked("signal-existing").options
                and "tip" in _marked("tcp-calibration-existing").options
            )
        )

        _open_tab(screen, "Signals")

        def select_valve():
            with Client.instances[ui_state.active_client_id]:
                _marked("signal-existing").set_value("valve")
                return _marked("signal-name").id, _marked("signal-message").id

        name_id, message_id = run_in_app(select_valve)
        wait(screen).until(
            lambda driver: driver.find_element(By.ID, f"c{name_id}").get_attribute(
                "value"
            )
            == "valve"
        )
        _click(screen, "signal-read")
        wait(screen).until(lambda _: "Observed logical value:" in _page_text(screen))
        js(
            screen,
            "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest'});",
            f"c{message_id}",
        )
        signals = js(screen, _FITS, f"c{message_id}", ".q-tab-panel")
        assert signals["content"] <= signals["width"] + 1, signals

        _open_tab(screen, "TCP")

        def select_tip():
            with Client.instances[ui_state.active_client_id]:
                _marked("tcp-calibration-existing").set_value("tip")
                return _marked("tcp-calibration-z").id, _marked(
                    "tcp-calibration-apply"
                ).id

        z_id, apply_id = run_in_app(select_tip)
        z_input = wait(screen).until(
            lambda driver: (e := driver.find_element(By.ID, f"c{z_id}")).is_displayed()
            and e
        )
        wait(screen).until(lambda _: float(z_input.get_attribute("value")) == 25.0)
        js(
            screen,
            "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest'});",
            f"c{apply_id}",
        )
        assert "RMS 0.007 mm" in _page_text(screen)
        tcp = js(screen, _FITS, f"c{apply_id}", ".q-tab-panel")
        assert tcp["content"] <= tcp["width"] + 1, tcp

    def test_fixed_camera_calibration_layout(self, class_screen) -> None:
        """The solve is the real one (board views rendered through the real
        detector and `solve_hand_eye`), and the save is driven by the panel's
        own button through the real measurement path, so the text measured is
        text the panel produces. The samples are handed to the panel rather
        than captured a pose at a time, which a browser cannot afford; the
        capture/solve/save workflow is covered end to end by
        `test_handeye_panel_workflow`."""
        screen = class_screen
        samples = _samples()
        result = handeye.solve_hand_eye(
            [
                handeye.HandEyeSample(
                    np.linalg.inv(s.T_base_gripper), s.detection, s.timestamp
                )
                for s in samples
            ],
            SPEC,
            mount="fixed",
        )
        board_pose = np.eye(4)
        board_pose[:3, 3] = (-75, -100, 500)
        _FrameBackend.holder["jpeg"] = _jpeg(
            render_board_view(SPEC, K_TRUE, board_pose, IMAGE_SIZE)
        )
        screen_wait_for_scene_ready(screen, timeout_s=40)
        _click(screen, "tab-handeye")

        def populate():
            client = Client.instances[ui_state.active_client_id]
            with client:
                camera_service.start(0)
                panel = next(p for p in ui_state.plugin_panels if p.id == "handeye")
                panel._mount_select.set_value("fixed")
                panel._samples = [
                    handeye.HandEyeSample(
                        np.linalg.inv(s.T_base_gripper), s.detection, s.timestamp
                    )
                    for s in samples
                ]
                panel._refresh_samples()
                panel._result = result
                panel._sample_binding = CaptureBinding(
                    camera_service.camera_id,
                    camera_service._session_id,
                    "parol6",
                    TcpCalibration((0, 0, 0, 0, 0, 0), "NONE"),
                )
                panel._show_result(result)
                panel._save_btn.set_enabled(True)
                panel._data_editor.name.set_value("overhead")
                panel._refresh_stage()
                panel._open_step("save")
                return panel._save_btn.id, panel._data_editor.message.id

        try:
            save_id, message_id = run_in_app(populate)
            wait(screen).until(
                lambda _: run_in_app(lambda: camera_service._snapshot is not None)
            )
            js(
                screen,
                "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest', behavior: 'instant'});",
                f"c{save_id}",
            )
            screen.selenium.find_element(By.ID, f"c{save_id}").click()
            wait(screen).until(
                lambda _: run_in_app(
                    lambda: "Saved bench/overhead"
                    in _marked("camera-data-message").text
                )
            )
            js(
                screen,
                "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest', behavior: 'instant'});",
                f"c{message_id}",
            )
            wait(screen).until(
                lambda driver: "Saved bench/overhead"
                in driver.find_element(By.ID, f"c{message_id}").text
            )

            def result_visible(_driver) -> bool:
                return js(
                    screen,
                    """
const e = document.getElementById(arguments[0]);
e.scrollIntoView({block: 'nearest', behavior: 'instant'});
const a = e.getBoundingClientRect(), b = e.closest('.camera-panel-scroll').getBoundingClientRect();
return a.top >= b.top - 1 && a.bottom <= b.bottom + 1;
""",
                    f"c{message_id}",
                )

            wait(screen).until(result_visible)
            geometry = js(screen, _FITS, f"c{message_id}", ".camera-panel-scroll")
            assert geometry["content"] <= geometry["width"] + 1, geometry
        finally:
            run_in_app(camera_service.stop)
