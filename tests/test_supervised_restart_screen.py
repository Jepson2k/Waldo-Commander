"""Select a restart entry in the running editor after stopping a real script."""

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
def test_browser_reviews_stopped_run_and_selects_an_entry(
    screen, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_RUN_RECORD_DIR", str(tmp_path / "runs"))
    marker = tmp_path / "selected.txt"
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def element(marker):
        def identifier():
            client = Client.instances[ui_state.active_client_id]
            return next(e.id for e in client.elements.values() if marker in e._markers)

        return screen.selenium.find_element(By.ID, f"c{run_in_app(identifier)}")

    def drive(coro):
        assert core.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, core.loop).result(30)

    async def interrupted():
        with Client.instances[ui_state.active_client_id]:
            await waldoctl.commander.client.simulator(True)
            await ensure_robot_ready_for_motion()
            assert ui_state.active_textarea is not None
            ui_state.active_textarea.value = f'''from parol6 import RobotClient
from waldoctl.restart import restart_entry
@restart_entry
def after_pick():
    """Held part checked."""
    raise RuntimeError('Wrong entry selected')
@restart_entry
def after_place():
    """Part placed and tool clear."""
    with open({str(marker)!r}, 'w') as file:
        file.write('after_place')
if __name__ == '__main__':
    with RobotClient() as rbt:
        rbt.delay(30)
'''
            script_exec.record_runs = True
            assert await script_exec.start()
            assert await waldoctl.commander.client.wait_status(
                lambda s: bool(s.action_current), timeout=15
            )
            await script_exec.stop()

    element("tab-program").click()
    drive(interrupted())
    assert run_in_app(lambda: script_exec.last_outcome) == "stopped"
    element("editor-more-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("editor-restart-btn").is_displayed()
    )
    element("editor-restart-btn").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda _: "Controller ready" in element("restart-controller-state").text
    )
    assert "stopped · same source" in element("restart-previous-run").text
    assert not element("restart-start").is_enabled()
    element("restart-physical-confirmation").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("restart-start").is_enabled()
    )
    element("restart-entry-choice").click()
    screen.click("after_place — Part placed and tool clear.")
    WebDriverWait(screen.selenium, 5).until(
        lambda _: not element("restart-start").is_enabled()
    )
    assert not marker.exists()
    element("restart-physical-confirmation").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("restart-start").is_enabled()
    )
    screen.selenium.save_screenshot(str(tmp_path / "supervised-restart.png"))
    element("restart-start").click()
    WebDriverWait(screen.selenium, 20).until(
        lambda _: marker.exists() and not run_in_app(is_any_program_running)
    )
    assert marker.read_text() == "after_place"
    assert run_in_app(lambda: script_exec.last_exit_code) == 0
