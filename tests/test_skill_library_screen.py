"""A skill's form opens beside the 3D view from the editor's Insert menu: the
scene stays usable, the path is drawn, and Insert stays in reach."""

import pytest
from nicegui import Client
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_vision import localization_scene
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal


@pytest.mark.browser
def test_a_skill_form_opens_beside_the_scene_and_keeps_insert_in_reach(
    screen, tmp_path, monkeypatch
):
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
    driver = screen.selenium
    driver.set_window_size(1366, 768)

    def element_id(marker: str) -> int | None:
        def find():
            client = Client.instances[ui_state.active_client_id]
            return next(
                (e.id for e in client.elements.values() if marker in e._markers), None
            )

        return run_in_app(find)

    def click(marker: str) -> None:
        target = WebDriverWait(driver, 10).until(
            lambda d: (
                (found := element_id(marker)) is not None
                and (el := d.find_element(By.ID, f"c{found}")).is_displayed()
                and el
            )
        )
        target.click()

    def preview_objects() -> int:
        scene = ui_state.urdf_scene
        assert scene is not None
        return len(scene._skill_preview_objects)

    click("tab-program")
    click("editor-commands-btn")
    click("editor-skills-menu")
    # A skill's diagram is its label in the menu; one that failed to load is
    # an empty square beside a word.
    loaded = WebDriverWait(driver, 10).until(
        lambda d: d.execute_script(
            "const imgs=[...document.querySelectorAll('.q-menu .skill-menu-icon img')];"
            "return imgs.length && imgs.every(i => i.complete && i.naturalWidth > 0);"
        )
    )
    assert loaded
    click("editor-skill-waldo.approach")

    # The saved pose fills Approach's target, so the form has a motion to draw.
    WebDriverWait(driver, 15).until(lambda _: run_in_app(preview_objects) > 0)
    insert_id = element_id("skill-insert")
    layout = driver.execute_script(
        "const e=document.getElementById(arguments[0]);"
        "const r=e.getBoundingClientRect();"
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const canvas=document.querySelector('canvas').getBoundingClientRect();"
        "const hit=document.elementFromPoint(canvas.x+canvas.width*0.6, canvas.y+canvas.height/2);"
        "return {insert:r.bottom < innerHeight && e.contains(document.elementFromPoint(r.x+5,r.y+5)),"
        "width:form.clientWidth, content:form.scrollWidth, scene: hit && hit.tagName,"
        "hit: hit && hit.className};",
        f"c{insert_id}",
    )
    assert layout["insert"], layout
    assert layout["content"] <= layout["width"] + 1, layout
    # No backdrop: the scene is still the scene where no panel covers it (the
    # editor's default width reaches past the centre of a 1366-wide window).
    assert layout["scene"] == "CANVAS", layout
    driver.save_screenshot(str(tmp_path / "skill-form.png"))

    click("skill-close")
    WebDriverWait(driver, 10).until(lambda _: run_in_app(preview_objects) == 0)

    # A skill that takes a camera: its calibration picker and limits fit the
    # form too, with Insert still in reach.
    click("editor-commands-btn")
    click("editor-skills-menu")
    click("editor-skill-waldo.locate_board")
    WebDriverWait(driver, 10).until(
        lambda d: "Uses the active camera." in d.find_element(By.TAG_NAME, "body").text
    )
    dimensions = driver.execute_script(
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const r=document.getElementById(arguments[0]).getBoundingClientRect();"
        "return {width:form.clientWidth, content:form.scrollWidth, bottom:r.bottom, height:innerHeight};",
        f"c{element_id('skill-insert')}",
    )
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    assert dimensions["bottom"] < dimensions["height"], dimensions
    driver.save_screenshot(str(tmp_path / "vision-localization.png"))
    click("skill-close")

    # The longest form: two poses, a signal and its values.
    click("editor-commands-btn")
    click("editor-skills-menu")
    click("editor-skill-waldo.transfer_with_signal")

    def choose_place():
        client = Client.instances[ui_state.active_client_id]
        with client:
            next(
                e for e in client.elements.values() if "skill-place-pose" in e._markers
            ).set_value("place")

    WebDriverWait(driver, 10).until(
        lambda _: element_id("skill-place-pose") is not None
    )
    run_in_app(choose_place)
    WebDriverWait(driver, 10).until(
        lambda d: "Closed value" in d.find_element(By.TAG_NAME, "body").text
    )
    dimensions = driver.execute_script(
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const r=document.getElementById(arguments[0]).getBoundingClientRect();"
        "return {width:form.clientWidth, content:form.scrollWidth, bottom:r.bottom, height:innerHeight};",
        f"c{element_id('skill-insert')}",
    )
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    assert dimensions["bottom"] < dimensions["height"], dimensions
    driver.save_screenshot(str(tmp_path / "tray-transfer.png"))
    click("skill-close")
