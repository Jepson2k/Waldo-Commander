"""The existing speed menu switches to controller state during a program."""

import asyncio

import pytest
import waldoctl
from nicegui import Client, core
from selenium.webdriver.common.action_chains import ActionChains
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
def test_speed_menu_shows_a_paused_live_program(screen, tmp_path):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def element_id(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e.id for e in client.elements.values() if marker in e._markers)

    def element(marker):
        identifier = run_in_app(lambda: element_id(marker))
        return screen.selenium.find_element(By.ID, f"c{identifier}")

    def drive(coro):
        assert core.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, core.loop).result(30)

    async def launch():
        client = Client.instances[ui_state.active_client_id]
        with client:
            rbt = waldoctl.commander.client
            await rbt.simulator(True)
            await ensure_robot_ready_for_motion()
            await rbt.resume()
            await rbt.set_execution_speed(1)
            target = await rbt.angles()
            assert target is not None
            target[0] += 8
            assert ui_state.active_textarea is not None
            ui_state.active_textarea.value = (
                "from parol6 import RobotClient\n\n"
                "with RobotClient() as rbt:\n"
                f"    rbt.move_j({target!r}, duration=30)\n"
                "    print('Motion completed')\n"
            )
            await script_exec.start()
            assert await rbt.wait_status(lambda s: bool(s.action_current), timeout=10)

    async def selected_and_paused():
        state = await waldoctl.commander.client.execution_speed()
        return state.paused and state.resume_scale == 0.5

    async def paused():
        return (await waldoctl.commander.client.execution_speed()).paused

    async def cleanup():
        with Client.instances[ui_state.active_client_id]:
            if is_any_program_running():
                await script_exec.stop()
            await waldoctl.commander.client.resume()
            await waldoctl.commander.client.set_execution_speed(1)

    element("tab-program").click()
    try:
        drive(launch())
        element("editor-play-btn").click()
        WebDriverWait(screen.selenium, 5).until(lambda _: drive(paused()))
        assert run_in_app(is_any_program_running)
        element("editor-speed").click()
        WebDriverWait(screen.selenium, 5).until(
            lambda _: element("editor-speed-half").is_displayed()
        )
        WebDriverWait(screen.selenium, 5).until(
            lambda _: not element("editor-speed-double").is_displayed()
        )
        element("editor-speed-half").click()
        WebDriverWait(screen.selenium, 5).until(lambda _: drive(selected_and_paused()))
        ActionChains(screen.selenium).move_to_element(
            screen.selenium.find_element(By.TAG_NAME, "body")
        ).perform()
        ActionChains(screen.selenium).move_to_element(element("editor-speed")).perform()
        WebDriverWait(screen.selenium, 5).until(
            lambda driver: driver.execute_script(
                "return [...document.querySelectorAll('.q-tooltip')].some(e => "
                "e.checkVisibility() && e.textContent.includes('50% selected') "
                "&& e.textContent.includes('Paused'));"
            )
        )
        screen.selenium.save_screenshot(str(tmp_path / "execution-paused.png"))
    finally:
        drive(cleanup())
