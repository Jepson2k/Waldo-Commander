"""Compact panels remain usable at laptop sizes and enlarged browser text."""

import asyncio
import json

import pytest
import waldoctl
from nicegui import Client, core
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl.setup import Frame, Pose, SetupSnapshot

from tests.helpers.browser_helpers import dismiss_dialogs, marked_element, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_par6_backend import par6_env, requires_par6  # noqa: F401
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


@pytest.fixture
def layout_screen(screen):
    try:
        yield screen
    finally:
        screen.selenium.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})


def review_layout(screen, tmp_path, monkeypatch, backend):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path / "setups"))
    SetupStore().save(
        "assembly",
        SetupSnapshot(
            frames={"fixture": Frame((25, 0, 0, 0, 0, 0))},
            poses={
                "pick": Pose((10, 200, 180, 90, 0, 90)),
                "place": Pose((40, 200, 180, 90, 0, 90)),
            },
        ),
    )
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=60)
    dismiss_dialogs(screen)
    assert run_in_app(lambda: ui_state.active_robot.name.lower()) == backend

    def marked(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e for e in client.elements.values() if marker in e._markers)

    def click(marker):
        target = WebDriverWait(screen.selenium, 10).until(
            lambda _: marked_element(screen, marker)
            if marked_element(screen, marker).is_displayed()
            else None
        )
        target.click()
        screen.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 600, "y": 4}
        )

    def settings():
        # The gear in the bottom-left rail opens the Settings dialog.
        click("tab-settings")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(lambda: ui_state.settings_content.dialog.value)
        )

    # Every category the dialog offers, by its tab marker.
    categories = run_in_app(
        lambda: [
            m[13:]
            for e in Client.instances[ui_state.active_client_id].elements.values()
            for m in e._markers
            if m.startswith("settings-cat-")
        ]
    )

    def select_tool(key):
        async def select():
            client = waldoctl.commander.client
            assert await client.wait_command(await client.select_tool(key), timeout=10)

        asyncio.run_coroutine_threadsafe(select(), core.loop).result(20)
        WebDriverWait(screen.selenium, 20).until(
            lambda _: run_in_app(lambda: marked("select-tool").value == key)
        )

    results = []
    # 941 is the viewport a maximised browser leaves on a 1080p screen.
    for width, height, zoom in [
        (1920, 941, 1),
        (1366, 900, 1),
        (1366, 768, 1),
        (1366, 768, 1.25),
    ]:
        screen.selenium.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": round(width / zoom),
                "height": round(height / zoom),
                "deviceScaleFactor": zoom,
                "mobile": False,
            },
        )
        # The footer is one row inside the viewport, and the control panel
        # sits clear of it.
        clearance = screen.selenium.execute_script("""
            const f = document.querySelector('.status-footer').getBoundingClientRect();
            const c = document.querySelector('.overlay-br').getBoundingClientRect();
            const rail = document.querySelector('.side-tab-bar.bottom-0').getBoundingClientRect();
            return {footerTop: f.top, footerBottom: f.bottom, footerHeight: f.height,
                    controlBottom: c.bottom, railBottom: rail.bottom, viewport: innerHeight};
        """)
        assert clearance["footerHeight"] <= 29, clearance
        assert clearance["footerBottom"] <= clearance["viewport"], clearance
        assert clearance["controlBottom"] <= clearance["footerTop"], clearance
        assert clearance["railBottom"] <= clearance["footerTop"], clearance

        settings()
        # Every category fits its panel: nothing runs off the right edge, the
        # dialog stays inside the viewport, and a row is its name and
        # description beside its control, not a card with a divider.
        measure_category = """
            const card = document.querySelector('.settings-dialog-card');
            const e = card.querySelector('.q-tab-panel:not(.q-tab-panel--inactive) .settings-content')
                || card.querySelector('.q-tab-panel .settings-content');
            const r = card.getBoundingClientRect();
            const tall = [...e.querySelectorAll('.settings-row')]
                .filter(row => row.offsetParent !== null && !row.querySelector('.settings-axis'))
                .map(row => row.getBoundingClientRect().height)
                .filter(h => h > 48);
            return {width:e.clientWidth, content:e.scrollWidth, bottom:r.bottom, right:r.right,
                    viewport:innerHeight, viewportWidth:innerWidth,
                    separators: card.querySelectorAll('.q-separator').length, tall,
                    rows: e.scrollHeight, shown: e.clientHeight};
        """
        for key in categories:
            click(f"settings-cat-{key}")
            WebDriverWait(screen.selenium, 10).until(
                lambda d: d.execute_script(
                    "return !!document.querySelector('.settings-dialog-card .q-tab-panel:not(.q-tab-panel--inactive) .settings-content')"
                )
            )
            dimensions = screen.selenium.execute_script(measure_category)
            if zoom > 1:
                # Only the tightest window is worth a picture of every category.
                screen.selenium.save_screenshot(
                    str(
                        tmp_path
                        / f"{backend}-settings-{key}-{width}-{height}-{zoom}.png"
                    )
                )
            assert dimensions["shown"] > 100, ("the category's rows are on screen", key)
            assert dimensions["content"] <= dimensions["width"] + 1, (key, dimensions)
            assert dimensions["bottom"] <= dimensions["viewport"] + 1, (key, dimensions)
            assert dimensions["right"] <= dimensions["viewportWidth"] + 1, (
                key,
                dimensions,
            )
            assert dimensions["separators"] == 0, (key, dimensions)
            assert not dimensions["tall"], (key, dimensions)
        click("settings-cat-tool")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: marked_element(screen, "select-tool").is_displayed()
        )
        if height >= 941:
            # A tool with variants adds its Variant row, and the form still fits.
            select_tool("SSG-48")
            WebDriverWait(screen.selenium, 20).until(
                lambda _: marked_element(screen, "select-tool-variant").is_displayed()
            )
            grown = screen.selenium.execute_script(measure_category)
            assert grown["rows"] <= grown["shown"] + 1, (
                "Settings → Tool scrolls on a 1080p screen with a gripper selected",
                grown,
            )
            select_tool("NONE")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(
                lambda: (
                    marked("select-tool").value == waldoctl.commander.status.tool.key
                )
            )
        )
        click("settings-cat-advanced")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: marked_element(screen, "settings-backend-select").is_displayed()
        )
        assert run_in_app(lambda: marked("settings-backend-select").value) == backend
        click("settings-close")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: not run_in_app(lambda: ui_state.settings_content.dialog.value)
        )

        # A skill's form opens from the editor's Insert menu, and Insert stays
        # on screen however small the window.
        click("tab-program")
        click("editor-commands-btn")
        click("editor-skills-menu")
        click("editor-skill-waldo.retract")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: marked_element(screen, "skill-insert").is_displayed()
        )
        WebDriverWait(screen.selenium, 10).until(
            lambda d: d.execute_script(
                """
                const e=document.getElementById(arguments[0]); const r=e.getBoundingClientRect();
                return e.contains(document.elementFromPoint(r.x+5,r.y+5));
                """,
                marked_element(screen, "skill-insert").get_attribute("id"),
            )
        )
        bounds = screen.selenium.execute_script(
            """
            const e=document.getElementById(arguments[0]); const r=e.getBoundingClientRect();
            return {bottom:r.bottom, height:innerHeight, visible:e.contains(document.elementFromPoint(r.x+5,r.y+5))};
        """,
            marked_element(screen, "skill-insert").get_attribute("id"),
        )
        assert bounds["bottom"] <= bounds["height"], bounds
        assert bounds["visible"], bounds
        screen.selenium.save_screenshot(
            str(tmp_path / f"{backend}-skills-{width}-{height}-{zoom}.png")
        )
        click("skill-close")

        WebDriverWait(screen.selenium, 10).until(
            lambda d: d.execute_script(
                "return (document.querySelector('.editor-tabs-scroll')?.clientWidth || 0) > 0"
            )
        )
        dimensions = screen.selenium.execute_script("""
            const e=document.querySelector('.editor-tabs-scroll');
            return {width:e.clientWidth, total:e.closest('.q-tab-panel').clientWidth};
        """)
        assert dimensions["width"] >= 140, dimensions
        screen.selenium.save_screenshot(
            str(tmp_path / f"{backend}-program-{width}-{height}-{zoom}.png")
        )
        results.append(
            {
                "viewport": [width, height],
                "zoom": zoom,
                "editor": dimensions,
                "skills": bounds,
            }
        )
    (tmp_path / f"{backend}-layout.json").write_text(json.dumps(results, indent=2))

    if backend == "par6":
        from nicegui import ui

        screen.selenium.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1366,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        click("footer-events")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: marked_element(screen, "diag-torque-chart").is_displayed()
        )
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(
                lambda: bool(marked("diag-torque-chart").options["series"][0]["data"])
            )
        )
        _ = marked_element(screen, "diag-expand-chart").location_once_scrolled_into_view
        screen.selenium.save_screenshot(str(tmp_path / "par6-torque.png"))
        click("diag-expand-chart")
        WebDriverWait(screen.selenium, 10).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, ".q-dialog .nicegui-echart")
        )
        screen.selenium.save_screenshot(str(tmp_path / "par6-torque-expanded.png"))
        (tmp_path / "expanded-browser.json").write_text(
            json.dumps(screen.selenium.get_log("browser"), indent=2)
        )
        click("expanded-chart-close")
        WebDriverWait(screen.selenium, 15).until(
            lambda d: d.execute_script(
                "return !Array.from(document.querySelectorAll('.q-dialog')).some(e => e.getClientRects().length)"
            )
        )
        click("bottom-panel-close")

        click("tab-par6-drives")

        def tune():
            client = Client.instances[ui_state.active_client_id]
            with client:
                expansion = next(
                    e
                    for e in client.elements.values()
                    if isinstance(e, ui.expansion) and e._props.get("label") == "Tuning"
                )
                expansion.set_value(True)

        run_in_app(tune)
        WebDriverWait(screen.selenium, 10).until(
            lambda _: marked_element(screen, "drives-save-config").is_displayed()
        )
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(lambda: bool(marked("drives-node-select").options))
        )
        _ = marked_element(
            screen, "drives-save-config"
        ).location_once_scrolled_into_view
        geometry = screen.selenium.execute_script(
            """
            const b=document.getElementById(arguments[0]); const e=b.closest('.plugin-panel-content');
            const r=b.getBoundingClientRect();
            return {scroll:e.scrollHeight, height:e.clientHeight, overflow:getComputedStyle(e).overflowY,
                    visible:b.contains(document.elementFromPoint(r.x+5,r.y+5))};
        """,
            marked_element(screen, "drives-save-config").get_attribute("id"),
        )
        assert geometry["scroll"] > geometry["height"], geometry
        assert geometry["overflow"] == "auto" and geometry["visible"], geometry
        screen.selenium.save_screenshot(str(tmp_path / "par6-drives-tuning.png"))


@pytest.mark.browser
@pytest.mark.timeout(120)
def test_compact_layout_parol6(layout_screen, tmp_path, monkeypatch):
    review_layout(layout_screen, tmp_path, monkeypatch, "parol6")


@requires_par6
@pytest.mark.browser
@pytest.mark.timeout(120)
@pytest.mark.usefixtures("par6_env")
def test_compact_layout_par6(layout_screen, tmp_path, monkeypatch):
    review_layout(layout_screen, tmp_path, monkeypatch, "par6")
