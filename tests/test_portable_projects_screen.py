"""Download selected contents and upload the resulting archive in Chromium."""

import asyncio
import json

import pytest
import waldoctl
from nicegui import Client, core
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl.setup import SetupSnapshot, TcpCalibration
from waldoctl.shapes import ShapeWorld, Sphere
from waldoctl.world import world_to_dict

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import (
    ensure_robot_ready_for_motion,
    screen_wait_for_scene_ready,
)
from waldo_commander.components.script_execution import script_exec
from waldo_commander.demonstrations import record_demonstration, save_demonstration
from waldo_commander.services.portable_projects import inspect_project
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


@pytest.mark.browser
def test_browser_exports_selected_data_and_imports_without_starting_a_program(
    screen, tmp_path, monkeypatch
):
    from waldo_commander.components import portable_projects as component

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path / "setups"))
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("WALDO_RUN_RECORD_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(component, "library_dir", lambda: tmp_path / "worlds")
    for name in ("programs", "recordings", "worlds"):
        (tmp_path / name).mkdir()
    SetupStore().save(
        "bench",
        SetupSnapshot(
            tcp_calibrations={"tip": TcpCalibration((1, 2, 3, 4, 5, 6), "NONE")}
        ),
    )
    (tmp_path / "worlds/fixture.json").write_text(
        json.dumps(
            world_to_dict(
                ShapeWorld(
                    program=(
                        Sphere(name="fixture", radius=0.01, pose=(1, 1, 1, 0, 0, 0)),
                    )
                )
            )
        )
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    def marked(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e for e in client.elements.values() if marker in e._markers)

    def element(marker):
        return screen.selenium.find_element(
            By.ID, f"c{run_in_app(lambda: marked(marker).id)}"
        )

    def drive(coro):
        assert core.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, core.loop).result(30)

    async def prepare():
        with Client.instances[ui_state.active_client_id]:
            client = waldoctl.commander.client
            await client.simulator(True)
            await ensure_robot_ready_for_motion()
            recording = await record_demonstration(client, duration_s=0.2)
            save_demonstration(tmp_path / "recordings/capture.json", recording)
            ui_state.editor_panel.PROGRAM_DIR = tmp_path / "programs"
            script_exec.set_program_dir(tmp_path / "programs")
            program = waldoctl.commander.programs.active
            program.filename = "demo.py"
            ui_state.active_filename_input.value = "demo.py"
            ui_state.active_textarea.value = "# Portable demo\nprint('portable-demo')\n"
            script_exec.record_runs = True
            assert await script_exec.start()
            async with asyncio.timeout(15):
                while is_any_program_running():
                    await asyncio.sleep(0.05)
            assert script_exec.last_exit_code == 0
            return (
                await client.tcp_transform(),
                await client.shapes(),
                script_exec.last_record,
            )

    element("tab-program").click()
    tcp, world, record = drive(prepare())
    element("editor-more-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("editor-projects-btn").is_displayed()
    )
    element("editor-projects-btn").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: element("project-export").is_displayed()
    )

    def select():
        marked("project-setups").value = ["bench"]
        marked("project-recordings").value = ["capture"]
        marked("project-worlds").value = ["fixture"]
        marked("project-debug-records").value = [record.stem]
        marked(
            "project-requirements"
        ).value += "\nmissing-waldo-project-test-dependency==1"

    run_in_app(select)
    screen.selenium.execute_cdp_cmd(
        "Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(tmp_path)}
    )
    screen.selenium.save_screenshot(str(tmp_path / "project-export.png"))
    element("project-export").click()
    archive = tmp_path / "waldo-project.zip"
    WebDriverWait(screen.selenium, 10).until(lambda _: archive.is_file())
    manifest, files = inspect_project(archive.read_bytes())
    assert len(files) == 5
    assert files["programs/demo.py"] == b"# Portable demo\nprint('portable-demo')\n"
    assert (
        "setups/bench.json" in files
        and "recordings/capture.json" in files
        and "worlds/fixture.json" in files
    )
    assert (
        json.loads(next(v for k, v in files.items() if k.startswith("debug/")))[
            "export"
        ]
        == "numeric-debug"
    )
    screen.click("Import")
    element("project-upload").find_element(
        By.CSS_SELECTOR, 'input[type="file"]'
    ).send_keys(str(archive))
    WebDriverWait(screen.selenium, 10).until(
        lambda _: "Validated 5 files" in element("project-import-summary").text
    )
    assert (
        "Missing: missing-waldo-project-test-dependency==1"
        in element("project-dependencies").text
    )
    assert not list((tmp_path / "programs").glob("projects/*"))
    element("project-import").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda _: "Imported to" in element("project-import-summary").text
    )
    imported = next((tmp_path / "programs/projects").iterdir())
    assert (imported / "programs/demo.py").read_bytes() == files["programs/demo.py"]
    assert not run_in_app(is_any_program_running)
    assert run_in_app(lambda: script_exec.last_record) == record
    assert drive(waldoctl.commander.client.tcp_transform()) == tcp
    assert drive(waldoctl.commander.client.shapes()) == world
    screen.selenium.save_screenshot(str(tmp_path / "project-import.png"))
    element("project-open").click()
    WebDriverWait(screen.selenium, 5).until(
        lambda _: (
            run_in_app(lambda: waldoctl.commander.programs.active.file_path)
            == str(imported / "programs/demo.py")
        )
    )
