"""The skill grid shows its diagrams, previews on hover, and long forms keep
their action buttons reachable."""

from nicegui import Client
import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_vision import localization_scene
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state
from waldoctl.setup import Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal


@pytest.mark.browser
def test_skill_grid_previews_on_hover_and_forms_keep_actions_visible(
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

    def element_ids():
        client = Client.instances[ui_state.active_client_id]
        with client:

            def marked(marker):
                return next(e for e in client.elements.values() if marker in e._markers)

            return {
                marker: marked(marker).id
                for marker in (
                    "tab-skills",
                    "skill-tile-waldo.retract",
                    "skill-tile-waldo.approach",
                    "skill-tile-waldo.locate_board",
                    "skill-tile-waldo.transfer_with_signal",
                    "skill-back",
                    "skill-run",
                )
            }

    def preview_objects() -> int:
        scene = ui_state.urdf_scene
        assert scene is not None
        return len(scene._skill_preview_objects)

    ids = run_in_app(element_ids)
    driver = screen.selenium
    driver.find_element(By.ID, f"c{ids['tab-skills']}").click()
    retract = driver.find_element(By.ID, f"c{ids['skill-tile-waldo.retract']}")
    WebDriverWait(driver, 10).until(lambda _: retract.is_displayed())

    # A tile whose diagram failed to load is an empty box with a word under it,
    # which is the dropdown again with more padding.
    loaded = driver.execute_script(
        "return [...document.querySelectorAll('.skill-tile img')]"
        ".map(i => i.complete && i.naturalWidth > 0);"
    )
    assert loaded and all(loaded), loaded

    # Retract needs no arguments, so hovering it has a motion to show; leaving
    # the tile must take it away again rather than leave a stray path behind.
    ActionChains(driver).move_to_element(retract).perform()
    WebDriverWait(driver, 15).until(lambda _: run_in_app(preview_objects) > 0)
    ActionChains(driver).move_to_element(
        driver.find_element(By.CSS_SELECTOR, "canvas")
    ).perform()
    WebDriverWait(driver, 10).until(lambda _: run_in_app(preview_objects) == 0)

    driver.find_element(By.ID, f"c{ids['skill-tile-waldo.approach']}").click()
    WebDriverWait(driver, 10).until(
        lambda _: driver.find_element(By.ID, f"c{ids['skill-run']}").is_displayed()
    )
    # Clicking hides the grid under the pointer; the leave that follows is not
    # the operator leaving the skill they just opened.
    WebDriverWait(driver, 15).until(lambda _: run_in_app(preview_objects) > 0)
    dimensions = driver.execute_script(
        "const e=document.getElementById(arguments[0]);"
        "const r=e.getBoundingClientRect();"
        "const form=document.querySelector('.skill-library-form-scroll');"
        "return {visible:r.bottom < innerHeight && e.contains(document.elementFromPoint(r.x+5,r.y+5)),"
        "width:form.clientWidth, content:form.scrollWidth};",
        f"c{ids['skill-run']}",
    )
    assert dimensions["visible"], dimensions
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    driver.save_screenshot(str(tmp_path / "skill-library.png"))

    # A skill that takes a camera: its calibration picker and limits form fit
    # the panel too, with Run once still in reach.
    driver.find_element(By.ID, f"c{ids['skill-back']}").click()
    locate = driver.find_element(By.ID, f"c{ids['skill-tile-waldo.locate_board']}")
    WebDriverWait(driver, 10).until(lambda _: locate.is_displayed())
    locate.click()
    WebDriverWait(driver, 10).until(
        lambda d: "Uses the active camera." in d.find_element(By.TAG_NAME, "body").text
    )
    dimensions = driver.execute_script(
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const r=document.getElementById(arguments[0]).getBoundingClientRect();"
        "return {width:form.clientWidth, content:form.scrollWidth, bottom:r.bottom, height:innerHeight};",
        f"c{ids['skill-run']}",
    )
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    assert dimensions["bottom"] < dimensions["height"], dimensions
    ActionChains(driver).move_to_element(
        driver.find_element(By.CSS_SELECTOR, "canvas")
    ).perform()
    WebDriverWait(
        driver, 10, ignored_exceptions=(StaleElementReferenceException,)
    ).until(
        lambda d: not any(
            e.is_displayed() for e in d.find_elements(By.CSS_SELECTOR, ".q-tooltip")
        )
    )
    driver.save_screenshot(str(tmp_path / "vision-localization.png"))

    # The longest form: two poses, a signal and its values.
    driver.find_element(By.ID, f"c{ids['skill-back']}").click()
    transfer = driver.find_element(
        By.ID, f"c{ids['skill-tile-waldo.transfer_with_signal']}"
    )
    WebDriverWait(driver, 10).until(lambda _: transfer.is_displayed())
    transfer.click()

    def choose_place():
        client = Client.instances[ui_state.active_client_id]
        with client:
            next(
                e for e in client.elements.values() if "skill-place-pose" in e._markers
            ).set_value("place")

    run_in_app(choose_place)
    WebDriverWait(driver, 10).until(
        lambda d: "Closed value" in d.find_element(By.TAG_NAME, "body").text
    )
    dimensions = driver.execute_script(
        "const form=document.querySelector('.skill-library-form-scroll');"
        "const r=document.getElementById(arguments[0]).getBoundingClientRect();"
        "return {width:form.clientWidth, content:form.scrollWidth, bottom:r.bottom, height:innerHeight};",
        f"c{ids['skill-run']}",
    )
    assert dimensions["content"] <= dimensions["width"] + 1, dimensions
    assert dimensions["bottom"] < dimensions["height"], dimensions
    driver.save_screenshot(str(tmp_path / "tray-transfer.png"))
