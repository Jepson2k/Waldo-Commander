"""The camera panel displays a populated calibration without horizontal clipping.

A layout check, in a real browser, of the one state the panel is widest in: a
solved fixed-mount calibration with its residuals and its saved-data row. The
solve is the real one (board views rendered through the real detector and
`solve_hand_eye`) and the save is driven by the panel's own button through the
real measurement path, so the text being measured is text the panel produces.
The samples are handed to the panel rather than captured a pose at a time,
which is the part a browser cannot afford; the capture/solve/save workflow
itself is covered end to end by `test_handeye_panel_workflow`.
"""

import json

from nicegui import Client
import numpy as np
import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_handeye_panel_integration import _FrameBackend, _jpeg
from tests.test_handeye_service import SPEC, _samples
from tests.helpers.charuco_render import render_board_view
from tests.test_handeye_service import IMAGE_SIZE, K_TRUE
from waldo_commander.services import handeye
from waldo_commander.services.camera_calibration import CaptureBinding
from waldo_commander.services.camera_service import camera_service
from waldo_commander.state import ui_state
from waldoctl.setup import TcpCalibration


@pytest.mark.browser
def test_fixed_camera_calibration_layout(screen, tmp_path, monkeypatch):
    from waldo_commander.services import camera_service as module

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    monkeypatch.setattr(module, "LinuxpyBackend", _FrameBackend)
    monkeypatch.setattr(module, "OpenCVBackend", _FrameBackend)
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
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def marked(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e for e in client.elements.values() if marker in e._markers)

    tab_id = run_in_app(lambda: marked("tab-handeye").id)
    screen.selenium.find_element(By.ID, f"c{tab_id}").click()

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
            return panel._save_btn.id, panel._data_editor.message.id

    try:
        save_id, message_id = run_in_app(populate)
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(lambda: camera_service._snapshot is not None)
        )
        screen.selenium.execute_script(
            "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest', behavior: 'instant'});",
            f"c{save_id}",
        )
        screen.selenium.find_element(By.ID, f"c{save_id}").click()
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(
                lambda: "Saved bench/overhead" in marked("camera-data-message").text
            )
        )
        screen.selenium.execute_script(
            "document.getElementById(arguments[0]).scrollIntoView({block: 'nearest', behavior: 'instant'});",
            f"c{message_id}",
        )
        WebDriverWait(screen.selenium, 10).until(
            lambda driver: "Saved bench/overhead"
            in driver.find_element(By.ID, f"c{message_id}").text
        )

        def result_visible(driver):
            return driver.execute_script(
                """
const e = document.getElementById(arguments[0]);
e.scrollIntoView({block: 'nearest', behavior: 'instant'});
const a = e.getBoundingClientRect(), b = e.closest('.camera-panel-scroll').getBoundingClientRect();
return a.top >= b.top && a.bottom <= b.bottom;
""",
                f"c{message_id}",
            )

        WebDriverWait(screen.selenium, 10).until(result_visible)
        geometry = screen.selenium.execute_script(
            """
const panel = document.getElementById(arguments[0]).closest('.camera-panel-scroll');
return {width: panel.clientWidth, content: panel.scrollWidth};
""",
            f"c{message_id}",
        )
        assert geometry["content"] <= geometry["width"] + 1, geometry
        screen.selenium.save_screenshot(str(tmp_path / "camera-calibration.png"))
    except BaseException:
        screen.selenium.save_screenshot(
            str(tmp_path / "camera-calibration-failure.png")
        )
        detail = screen.selenium.execute_script(
            """
let e = document.getElementById(arguments[0]);
const chain = [];
while (e) {
 const r = e.getBoundingClientRect(), s = getComputedStyle(e);
 chain.push({id:e.id, cls:e.className, text:e.textContent.slice(0,250), box:[r.x,r.y,r.width,r.height], display:s.display, visibility:s.visibility, overflow:s.overflow});
 e = e.parentElement;
}
return chain;
""",
            f"c{message_id}",
        )
        (tmp_path / "camera-layout.json").write_text(json.dumps(detail, indent=2))
        raise
    finally:
        run_in_app(camera_service.stop)
