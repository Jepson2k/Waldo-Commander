"""The observed-data chart renders in the installed recording panel."""

import asyncio

import pytest
import waldoctl
from nicegui import Client, core
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import (
    dismiss_dialogs,
    ensure_robot_homed,
    run_in_app,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.demonstrations import record_demonstration, save_demonstration
from waldo_commander.state import ui_state


@pytest.mark.browser
def test_recording_chart_displays_actual_motion(screen, tmp_path, monkeypatch):
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    ensure_robot_homed()

    async def capture():
        client = waldoctl.commander.client
        started = asyncio.Event()
        task = asyncio.create_task(
            record_demonstration(
                client, duration_s=2, on_sample=lambda sample: started.set()
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        joints = await client.angles()
        joints[0] += 4
        index = await client.move_j(joints, duration=1)
        assert await client.wait_command(index, timeout=10)
        recording = await task
        save_demonstration(tmp_path / "bench-motion.json", recording)
        return recording

    assert core.loop is not None
    recording = asyncio.run_coroutine_threadsafe(capture(), core.loop).result(20)

    def load():
        client = Client.instances[ui_state.active_client_id]
        with client:

            def marked(marker):
                return next(e for e in client.elements.values() if marker in e._markers)

            marked("demo-name").set_value("bench-motion")
            return (
                marked("tab-demonstrations").id,
                marked("demo-load").id,
                marked("demo-chart").id,
                marked("demo-insert").id,
            )

    tab, load_button, chart, insert = run_in_app(load)
    screen.selenium.find_element(By.ID, f"c{tab}").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda d: d.find_element(By.ID, f"c{load_button}").is_displayed()
    )
    screen.selenium.find_element(By.ID, f"c{load_button}").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda d: "Loaded observations" in d.find_element(By.TAG_NAME, "body").text
    )
    WebDriverWait(screen.selenium, 10).until(
        lambda d: d.execute_script(
            "const e=document.getElementById(arguments[0]); return e && e.querySelector('canvas,svg') !== null;",
            f"c{chart}",
        )
    )
    assert len(recording.samples) > 10
    assert recording.samples[-1].joints_deg[0] - recording.samples[0].joints_deg[0] > 3
    assert screen.selenium.execute_script(
        "const r=document.getElementById(arguments[0]).getBoundingClientRect(); return r.height > 0 && r.bottom <= innerHeight;",
        f"c{insert}",
    )
    screen.selenium.save_screenshot(str(tmp_path / "demonstration-recording.png"))

    def trim():
        client = Client.instances[ui_state.active_client_id]
        with client:

            def marked(marker):
                return next(e for e in client.elements.values() if marker in e._markers)

            origin = recording.samples[0].observed_ns
            marked("demo-name").set_value("trimmed-motion")
            marked("demo-from-seconds").set_value(
                (recording.samples[1].observed_ns - origin) / 1e9
            )
            marked("demo-to-seconds").set_value(
                (recording.samples[3].observed_ns - origin) / 1e9
            )
            return marked("demo-save").id

    save = run_in_app(trim)
    screen.selenium.find_element(By.ID, f"c{save}").click()
    trimmed_path = tmp_path / "trimmed-motion.json"
    WebDriverWait(screen.selenium, 10).until(lambda _: trimmed_path.exists())
    from waldo_commander.demonstrations import load_demonstration

    trimmed = load_demonstration(trimmed_path)
    assert [s.observed_ns for s in trimmed.samples] == [
        s.observed_ns for s in recording.samples[1:4]
    ]
    assert [s.joints_deg for s in trimmed.samples] == [
        s.joints_deg for s in recording.samples[1:4]
    ]
