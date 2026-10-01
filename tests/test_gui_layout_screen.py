"""Compact panels remain usable at laptop sizes and enlarged browser text."""

import asyncio

import pytest
import waldoctl
from nicegui import Client, core
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl.setup import Frame, Pose, SetupSnapshot

from tests.helpers.browser_helpers import marked_element, run_in_app
from tests.helpers.browser_session import wait
from tests.helpers.wait import screen_wait_for_scene_ready
from tests.test_par6_backend import par6_env, requires_par6  # noqa: F401
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state
from waldo_commander.components.simulation_engine import SimulationEngine
from waldo_commander.components.skill_library import SkillStrip


@pytest.fixture
def layout_screen(screen):
    try:
        yield screen
    finally:
        screen.selenium.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})


def review_layout(screen, tmp_path, monkeypatch, backend):
    # This test measures controls, not the inserted sample's motion plan.
    # Keep the live daemon/status and scene, but avoid starting planners for
    # each edit with fixture-only poses and a temporary setup directory.
    # Native planning/execution is covered by test_par6_backend and scenarios.
    monkeypatch.setattr(
        SimulationEngine, "schedule_debounced_simulation", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(SkillStrip, "_schedule_preview", lambda self: None)
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
    assert run_in_app(lambda: ui_state.active_robot.name.lower()) == backend

    def marked(marker):
        client = Client.instances[ui_state.active_client_id]
        return next(e for e in client.elements.values() if marker in e._markers)

    def click(marker):
        def click_visible(_):
            target = marked_element(screen, marker)
            if not target.is_displayed():
                return False
            target.click()
            return True

        WebDriverWait(
            screen.selenium,
            10,
            poll_frequency=0.05,
            ignored_exceptions=(StaleElementReferenceException,),
        ).until(click_visible)
        screen.selenium.execute_cdp_cmd(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 600, "y": 4}
        )

    def settings():
        # The gear in the bottom-left rail opens the Settings dialog.
        click("tab-settings")
        wait(screen, 10).until(
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
        wait(screen, 20).until(
            lambda _: run_in_app(lambda: marked("select-tool").value == key)
        )

    # 941 is the viewport a maximised browser leaves on a 1080p screen. At
    # 125%, 960 physical pixels leave 1093x768 CSS pixels, tighter than either
    # 1366x768 or 1366x900 at 100%.
    for width, height, zoom in [(1920, 941, 1), (1366, 960, 1.25)]:
        screen.selenium.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": round(width / zoom),
                "height": round(height / zoom),
                "deviceScaleFactor": zoom,
                "mobile": False,
            },
        )
        wait(screen, 10).until(
            lambda d: d.execute_script("""
                return ['.status-footer', '.overlay-br', '.side-tab-bar.bottom-0']
                    .every(selector => document.querySelector(selector));
            """)
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
            const e = arguments[0].closest('.q-tab-panel').querySelector('.settings-content');
            const r = card.getBoundingClientRect();
            const content = e.getBoundingClientRect();
            const clipped = [...e.querySelectorAll('.settings-row > :not(.settings-text)')]
                .filter(control => control.offsetParent !== null)
                .map(control => control.getBoundingClientRect())
                .filter(control => control.left < content.left - 1 || control.right > content.right + 1)
                .map(control => ({left:control.left, right:control.right}));
            const tall = [...e.querySelectorAll('.settings-row')]
                .filter(row => row.offsetParent !== null && !row.querySelector('.settings-axis'))
                .map(row => row.getBoundingClientRect().height)
                .filter(h => h > 48);
            return {width:e.clientWidth, content:e.scrollWidth, bottom:r.bottom, right:r.right,
                    viewport:innerHeight, viewportWidth:innerWidth,
                    separators: card.querySelectorAll('.q-separator').length, tall, clipped,
                    contentRight:content.right,
                    rows: e.scrollHeight, shown: e.clientHeight};
        """
        for key in categories:
            click(f"settings-cat-{key}")
            wait(screen, 10).until(
                lambda d: d.execute_script(
                    """
                    const panel = arguments[0].closest('.q-tab-panel');
                    const bounds = panel.getBoundingClientRect();
                    const body = panel.closest('.settings-body').getBoundingClientRect();
                    return panel.offsetParent !== null
                        && Math.abs(bounds.left - body.left) < 1
                        && Math.abs(bounds.right - body.right) < 1
                        && !panel.getAnimations().some(a => a.playState === 'running')
                        && !panel.parentElement.querySelector('[class*="-leave-active"]');
                    """,
                    marked_element(screen, f"settings-group-{key}"),
                )
            )
            dimensions = screen.selenium.execute_script(
                measure_category, marked_element(screen, f"settings-group-{key}")
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
            assert dimensions["contentRight"] <= dimensions["right"] + 1, (
                key,
                dimensions,
            )
            assert not dimensions["clipped"], (key, dimensions)
        click("settings-cat-tool")
        wait(screen, 10).until(
            lambda _: marked_element(screen, "select-tool").is_displayed()
        )
        if height >= 941:
            # PAROL6's gripper adds a Variant row. PAR6 only permits the
            # gripper fitted in its daemon config, so review that tool.
            if backend == "parol6":
                select_tool("SSG-48")
                wait(screen, 20).until(
                    lambda _: marked_element(
                        screen, "select-tool-variant"
                    ).is_displayed()
                )
            grown = screen.selenium.execute_script(
                measure_category, marked_element(screen, "settings-group-tool")
            )
            assert grown["rows"] <= grown["shown"] + 1, (
                "Settings → Tool scrolls on a 1080p screen with a gripper selected",
                grown,
            )
            if backend == "parol6":
                select_tool("NONE")
        wait(screen, 10).until(
            lambda _: run_in_app(
                lambda: (
                    marked("select-tool").value == waldoctl.commander.status.tool.key
                )
            )
        )
        click("settings-cat-advanced")
        wait(screen, 10).until(
            lambda _: marked_element(screen, "settings-backend-select").is_displayed()
        )
        assert run_in_app(lambda: marked("settings-backend-select").value) == backend
        click("settings-close")
        wait(screen, 10).until(
            lambda _: not run_in_app(lambda: ui_state.settings_content.dialog.value)
        )

        # A skill goes in from the editor's Insert menu as its call, and the
        # strip over it, a pose field's teach controls and all, fits the
        # program column however small the window, leaving the code in view.
        click("tab-program")
        click("editor-commands-btn")
        click("editor-skills-menu")
        click("editor-skill-waldo.transfer")
        # The strip can rebuild while the preview and cursor settle. Query
        # the rendered controls together, rather than resolve a server-side
        # element id that can be replaced before it reaches the browser.
        wait(screen, 30).until(
            lambda d: d.execute_script("""
                return [...document.querySelectorAll('.skill-strip button')]
                    .some(e => e.offsetParent !== null && e.textContent.includes('Teach now'));
            """)
        )
        bounds = screen.selenium.execute_script(
            """
            const strip=[...document.querySelectorAll('.skill-strip')].find(e => e.offsetParent);
            const r=strip.getBoundingClientRect();
            const code=strip.parentElement.querySelector('.cm-editor').getBoundingClientRect();
            return {content:strip.scrollWidth, width:strip.clientWidth, right:r.right,
                    viewportWidth:innerWidth, bottom:r.bottom, codeTop:code.top, code:code.height};
        """
        )
        assert bounds["content"] <= bounds["width"] + 1, bounds
        assert bounds["right"] <= bounds["viewportWidth"] + 1, bounds
        assert bounds["codeTop"] >= bounds["bottom"] - 1, bounds
        assert bounds["code"] > 60, ("the code stays in view under the strip", bounds)

        wait(screen, 10).until(
            lambda d: d.execute_script(
                "return (document.querySelector('.editor-tabs-scroll')?.clientWidth || 0) > 0"
            )
        )
        dimensions = screen.selenium.execute_script("""
            const e=document.querySelector('.editor-tabs-scroll');
            return {width:e.clientWidth, total:e.closest('.q-tab-panel').clientWidth};
        """)
        assert dimensions["width"] >= 140, dimensions

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
        wait(screen, 10).until(
            lambda _: marked_element(screen, "diag-torque-chart").is_displayed()
        )
        wait(screen, 10).until(
            lambda _: run_in_app(
                lambda: bool(marked("diag-torque-chart").options["series"][0]["data"])
            )
        )
        _ = marked_element(screen, "diag-expand-chart").location_once_scrolled_into_view
        click("diag-expand-chart")
        wait(screen).until(
            lambda d: d.execute_script(
                "return !!document.querySelector('.q-dialog .nicegui-echart')"
            )
        )
        click("expanded-chart-close")
        wait(screen, 15).until(
            lambda d: d.execute_script(
                "return !Array.from(document.querySelectorAll('.q-dialog')).some(e => e.getClientRects().length)"
            )
        )

        # The remaining check concerns the drive panel's scrolling. Set up
        # its unobstructed layout directly; bottom-panel close interactions
        # have their own footer tests.
        def close_diagnostics():
            with Client.instances[ui_state.active_client_id]:
                ui_state.bottom_panel.close()

        run_in_app(close_diagnostics)
        # The form fits at 900px. Use the smallest desktop height so this
        # actually exercises scrolling instead of requiring needless overflow.
        screen.selenium.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False},
        )

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
        wait(screen, 10).until(
            lambda _: marked_element(screen, "drives-save-config").is_displayed()
        )
        wait(screen, 10).until(
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


@pytest.mark.browser
@pytest.mark.timeout(120)
def test_compact_layout_parol6(layout_screen, tmp_path, monkeypatch):
    review_layout(layout_screen, tmp_path, monkeypatch, "parol6")


@requires_par6
@pytest.mark.browser
@pytest.mark.timeout(240)
@pytest.mark.usefixtures("par6_env")
def test_compact_layout_par6(layout_screen, tmp_path, monkeypatch):
    review_layout(layout_screen, tmp_path, monkeypatch, "par6")
