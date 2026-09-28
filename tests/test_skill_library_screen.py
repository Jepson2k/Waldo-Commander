"""A skill goes into the program as its call with the arguments as fields: in
the browser Tab moves between them, a write from the strip above the code
keeps them live, and the strip leaves the code and the scene usable."""

import pytest
import waldoctl
from nicegui import Client
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import dismiss_dialogs, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state
from waldoctl.setup import Pose, SetupSnapshot


@pytest.mark.browser
def test_skill_fields_tab_in_order_and_stay_live_through_a_strip_write(
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
        ),
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    driver = screen.selenium
    driver.set_window_size(1366, 1024)

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

    def selected() -> str:
        """The text of the editor's selection, where a snippet field is selected."""
        return driver.execute_script(
            "const s=getElement(arguments[0]).editor.state;"
            "return s.sliceDoc(s.selection.main.from, s.selection.main.to);",
            run_in_app(lambda: ui_state.active_textarea.id),
        )

    def strip():
        return ui_state.editor_panel.skill_strip(waldoctl.commander.programs.active_id)

    def strip_field() -> str | None:
        return run_in_app(lambda: strip()._field if strip() is not None else None)

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

    # The call goes in with its first field selected and the editor focused,
    # and the saved pose it names has a motion to draw.
    WebDriverWait(driver, 10).until(lambda _: selected() == 'setup.resolve("pick")')
    WebDriverWait(driver, 15).until(lambda _: run_in_app(preview_objects) > 0)
    layout = driver.execute_script(
        "const strip=[...document.querySelectorAll('.skill-strip')].find(e => e.offsetParent);"
        "const r=strip.getBoundingClientRect();"
        "const code=strip.parentElement.querySelector('.cm-editor').getBoundingClientRect();"
        "const canvas=document.querySelector('canvas').getBoundingClientRect();"
        "const hit=document.elementFromPoint(canvas.x+canvas.width*0.6, canvas.y+canvas.height/2);"
        "return {content:strip.scrollWidth, width:strip.clientWidth, bottom:r.bottom,"
        "codeTop:code.top, code:code.height, scene: hit && hit.tagName};"
    )
    assert layout["content"] <= layout["width"] + 1, layout
    assert layout["codeTop"] >= layout["bottom"] - 1, layout
    assert layout["code"] > 100, ("the code stays in view under the strip", layout)
    # Nothing covers the scene: where no panel is, it is still the scene.
    assert layout["scene"] == "CANVAS", layout
    driver.save_screenshot(str(tmp_path / "skill-fields.png"))

    # Tab and Shift+Tab move between the fields in the order of the signature.
    ActionChains(driver).send_keys(Keys.TAB).perform()
    WebDriverWait(driver, 5).until(lambda _: selected() == "30.0")
    ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.TAB).key_up(
        Keys.SHIFT
    ).perform()
    WebDriverWait(driver, 5).until(lambda _: selected() == 'setup.resolve("pick")')
    WebDriverWait(driver, 5).until(lambda _: strip_field() == "target")

    # Choosing a pose in the strip writes only that field from the server:
    # the snippet stays live, so Tab still moves on to the next field.
    def choose_place() -> None:
        with Client.instances[ui_state.active_client_id]:
            strip().write_field('setup.resolve("place")')

    run_in_app(choose_place)
    WebDriverWait(driver, 5).until(lambda _: selected() == 'setup.resolve("place")')
    ActionChains(driver).send_keys(Keys.TAB).perform()
    WebDriverWait(driver, 5).until(lambda _: selected() == "30.0")
    ActionChains(driver).send_keys(Keys.TAB).perform()
    WebDriverWait(driver, 5).until(lambda _: selected() == "0.2")
