"""Inspect actual recorded values from the editor's browser dialog."""

import asyncio

import pytest
import waldoctl
from nicegui import Client, core
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import (
    ensure_robot_ready_for_motion,
    screen_wait_for_scene_ready,
)
from waldo_commander.components.script_execution import script_exec
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.state import ui_state


@pytest.mark.browser
def test_run_record_browser_shows_events_and_local_values(
    screen, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_RUN_RECORD_DIR", str(tmp_path / "runs"))
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def element(marker):
        def identifier():
            client = Client.instances[ui_state.active_client_id]
            return next(e.id for e in client.elements.values() if marker in e._markers)

        return screen.selenium.find_element(By.ID, f"c{run_in_app(identifier)}")

    async def launch():
        with Client.instances[ui_state.active_client_id]:
            await waldoctl.commander.client.simulator(True)
            await ensure_robot_ready_for_motion()
            assert ui_state.active_textarea is not None
            ui_state.active_textarea.value = (
                "from parol6 import RobotClient\n"
                "from waldoctl.skills import skill\n\n"
                "@skill(id='personal.dwell', version='1.0.0')\n"
                "async def dwell(rbt, seconds=0.2):\n"
                "    await rbt.delay(seconds)\n"
                "    return {'duration_s': seconds}\n\n"
                "with RobotClient() as rbt:\n"
                "    dwell(rbt)\n"
            )
            await script_exec.start()

    def drive(coro):
        assert core.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, core.loop).result(30)

    element("tab-program").click()
    element("editor-more-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("editor-records-btn").is_displayed()
    )
    element("editor-records-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("record-runs-enabled").is_displayed()
    )
    element("record-runs-enabled").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: run_in_app(lambda: script_exec.record_runs)
    )
    screen.click("Close")
    drive(launch())
    WebDriverWait(screen.selenium, 20).until(
        lambda _: not run_in_app(is_any_program_running)
    )
    assert run_in_app(lambda: script_exec.last_exit_code) == 0
    element("editor-more-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("editor-records-btn").is_displayed()
    )
    element("editor-records-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: "completed" in element("run-record-summary").text
    )
    table = element("run-record-events")
    WebDriverWait(screen.selenium, 5).until(lambda _: "run_started" in table.text)
    element("run-record-filter").send_keys("skill")
    WebDriverWait(screen.selenium, 5).until(
        lambda _: (
            "skill_started" in table.text and "controller_context" not in table.text
        )
    )
    screen.selenium.save_screenshot(str(tmp_path / "run-records.png"))
    table.find_element(By.CSS_SELECTOR, "tbody tr").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: (
            "Local event values"
            in screen.selenium.find_element(By.TAG_NAME, "body").text
        )
    )
    assert '"seconds": 0.2' in screen.selenium.find_element(By.TAG_NAME, "body").text
    screen.selenium.save_screenshot(str(tmp_path / "run-record-values.png"))
