"""The desktop footer stays one row; narrow screens keep every readout below
the existing controls. Panels stay clear of both layouts."""

import asyncio

import pytest
import waldoctl
from nicegui import Client, core
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import (
    click_tab,
    close_panel,
    dismiss_dialogs,
    js,
    marked_element,
    run_in_app,
    viewport,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state

MEASURE = """
    const footer = document.querySelector('.status-footer');
    const f = footer.getBoundingClientRect();
    const well = footer.querySelector('.pose-well').getBoundingClientRect();
    const children = [...footer.querySelectorAll(':scope > *')]
        .filter(e => e.offsetParent !== null)
        .map(e => e.getBoundingClientRect());
    return {height: f.height, top: f.top, bottom: f.bottom, left: f.left, right: f.right,
            viewport: innerHeight, viewportWidth: innerWidth,
            wellRight: well.right, wellLeft: well.left, wellTop: well.top, wellBottom: well.bottom,
            lowest: Math.max(...children.map(r => r.bottom)),
            highest: Math.min(...children.map(r => r.top)),
            dots: footer.querySelectorAll('.io-dot').length};
"""


def _show_lines(count: int | None) -> None:
    """Rebuild the dots for *count* lines each way and hold them there: the
    status loop re-reads the robot's real line count every tick. None hands
    the footer back to the status loop."""

    def apply():
        footer = ui_state.readout_panel
        if count is None:
            footer.__dict__.pop("update_conn_io", None)
        else:
            io = waldoctl.commander.status.io
            io.inputs = [0] * count
            io.outputs = [0] * count
        with Client.instances[ui_state.active_client_id]:
            type(footer).update_conn_io(footer)
        if count is not None:
            footer.update_conn_io = lambda: None

    run_in_app(apply)


def _measure_with(screen, dots: int) -> dict:
    return WebDriverWait(screen.selenium, 10).until(
        lambda _: (m := js(screen, MEASURE))["dots"] == dots and m
    )


def _select_tool(key: str) -> None:
    async def select() -> None:
        client = waldoctl.commander.client
        assert await client.wait_command(await client.select_tool(key), timeout=10)

    asyncio.run_coroutine_threadsafe(select(), core.loop).result(20)


LAUNCHERS = """
    const f = document.querySelector('.status-footer').getBoundingClientRect();
    const tool = document.querySelector('.footer-tool');
    return {footer: {left: f.left, right: f.right, top: f.top, bottom: f.bottom},
            tool: !!tool && tool.offsetParent !== null,
            launchers: arguments[0].map(id => {
                const r = document.getElementById(id).getBoundingClientRect();
                return {left: r.left, right: r.right, width: r.width};
            })};
"""


def _launchers(screen, markers: list[str]) -> dict:
    ids = [marked_element(screen, m).get_attribute("id") for m in markers]
    return js(screen, LAUNCHERS, ids)


@pytest.mark.browser
def test_many_io_lines_keep_the_footer_one_row(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    screen.selenium.set_window_size(1366, 768)
    before = _measure_with(screen, 4)
    assert before["height"] <= 29, before

    try:
        _show_lines(12)
        many = _measure_with(screen, 24)
    finally:
        _show_lines(None)
    assert many["height"] <= 29, many
    assert (
        many["highest"] >= many["top"] - 1 and many["lowest"] <= many["bottom"] + 1
    ), many
    assert many["bottom"] <= many["viewport"], many
    # The pose well is whole and inside the viewport, dots or no dots.
    assert many["wellLeft"] >= many["left"] and many["wellRight"] <= many["right"], many
    assert many["wellRight"] <= many["viewportWidth"], many
    assert many["wellTop"] >= many["top"] and many["wellBottom"] <= many["bottom"], many

    restored = _measure_with(screen, 4)
    assert abs(restored["height"] - before["height"]) < 1, (restored, before)

    # Narrower windows give up the pose before the Diagnostics and Log
    # launchers, even with a tool named.
    try:
        _select_tool("SSG-48")
        for width in (1100, 1000):
            with viewport(screen, width, 768):
                shown = WebDriverWait(screen.selenium, 10).until(
                    lambda _: (
                        m := _launchers(screen, ["footer-events", "footer-log"])
                    )["tool"]
                    and m["footer"]["right"] <= width
                    and m
                )
                for launcher in shown["launchers"]:
                    assert launcher["width"] > 0, (width, shown)
                    assert launcher["left"] >= shown["footer"]["left"], (width, shown)
                    assert launcher["right"] <= shown["footer"]["right"], (width, shown)
                assert js(screen, MEASURE)["height"] <= 29, width
    finally:
        _select_tool("NONE")


@pytest.mark.browser
def test_bottom_panel_pushes_the_column_up_and_stays_clear_of_the_control_panel(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    screen.selenium.set_window_size(1366, 768)
    click_tab(screen, "program")
    click_tab(screen, "diagnostics")
    geometry = """
        const box = s => { const e = document.querySelector(s); if (!e || e.offsetParent === null) return null;
                           const b = e.getBoundingClientRect(); return {left: b.left, right: b.right, top: b.top, bottom: b.bottom}; };
        return {panel: box('.bottom-panel'), column: box('.program-panel'), control: box('.overlay-br'),
                viewport: innerHeight, framedBottom: SceneFraming.getInset().bottom};
    """
    opened = WebDriverWait(screen.selenium, 10).until(
        lambda _: (g := js(screen, geometry))["panel"]
        and g["column"]["bottom"] <= g["panel"]["top"] - 11
        and g
    )
    panel, column, control = opened["panel"], opened["column"], opened["control"]
    assert abs(panel["left"] - column["left"]) <= 1, opened
    assert panel["right"] <= control["left"] - 11, opened
    assert opened["framedBottom"] == round(opened["viewport"] - panel["top"]), opened
    wrapped = js(
        screen,
        """
        return [...document.querySelectorAll('.bottom-panel .font-mono')]
            .filter(e => e.offsetParent !== null && e.textContent.trim())
            .filter(e => e.getClientRects().length > 1
                || e.offsetHeight > 1.5 * parseFloat(getComputedStyle(e).lineHeight))
            .map(e => e.textContent);
        """,
    )
    assert wrapped == [], f"diagnostics values wrap: {wrapped}"

    close_panel(screen, "bottom-panel")
    closed = WebDriverWait(screen.selenium, 10).until(
        lambda _: (g := js(screen, geometry))["panel"] is None
        and g["column"]["bottom"] > opened["column"]["bottom"] + 300
        and g
    )
    assert closed["framedBottom"] < opened["framedBottom"], closed

    # On a short desktop the bottom panel gives way, so the column keeps
    # room for its playbar and the Stop on it.
    playbar = """
        const box = s => { const e = document.querySelector(s); if (!e || e.offsetParent === null) return null;
                           const b = e.getBoundingClientRect(); return {top: b.top, bottom: b.bottom}; };
        return {panel: box('.bottom-panel'), column: box('.top-panels-container'),
                playbar: box('.program-panel .bottom-playback-bar'), viewport: innerHeight};
    """
    with viewport(screen, 1000, 480):
        click_tab(screen, "diagnostics")
        kiosk = WebDriverWait(screen.selenium, 10).until(
            lambda _: (g := js(screen, playbar))["panel"]
            and g["column"]["bottom"] <= g["panel"]["top"] - 11
            and g
        )
        column, bar = kiosk["column"], kiosk["playbar"]
        assert column["bottom"] - column["top"] >= 199, kiosk
        assert bar is not None, kiosk
        assert column["top"] <= bar["top"] and bar["bottom"] <= column["bottom"], kiosk
        assert bar["bottom"] <= kiosk["viewport"], kiosk
        close_panel(screen, "bottom-panel")


@pytest.mark.browser
def test_the_bottom_panel_reopens_on_its_tab_after_a_reload(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    WebDriverWait(screen.selenium, 10).until(
        lambda _: js(screen, "return !!window.PanelResize?.isAppReady()")
    )
    saved = "return JSON.parse(localStorage.getItem('parol_active_tabs') || '{}').panel"

    click_tab(screen, "log")
    WebDriverWait(screen.selenium, 5).until(lambda _: js(screen, saved) == "log")

    screen.selenium.refresh()
    screen_wait_for_scene_ready(screen, timeout_s=40)
    WebDriverWait(screen.selenium, 10).until(
        lambda _: run_in_app(
            lambda: ui_state.bottom_panel.visible
            and ui_state.bottom_panel.tabs.value == "log"
        )
    )
    WebDriverWait(screen.selenium, 10).until(
        lambda _: marked_element(screen, "response-log").is_displayed()
    )

    close_panel(screen, "bottom-panel")
    WebDriverWait(screen.selenium, 5).until(lambda _: js(screen, saved) is None)


@pytest.mark.browser
def test_on_a_phone_the_controls_clear_the_footer_and_settings_opens(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    with viewport(screen, 390, 844, mobile=True):
        clearance = """
            const f = document.querySelector('.status-footer').getBoundingClientRect();
            const c = document.querySelector('.overlay-br').getBoundingClientRect();
            return {footerTop: f.top, footerBottom: f.bottom, controlBottom: c.bottom,
                    viewport: innerHeight, width: innerWidth};
        """
        phone = WebDriverWait(screen.selenium, 10).until(
            lambda _: (g := js(screen, clearance))["width"] == 390 and g
        )
        assert phone["controlBottom"] <= phone["footerTop"], phone
        assert phone["footerBottom"] <= phone["viewport"], phone

        # The rail and its gear are hidden on a phone; the footer has one.
        gear = marked_element(screen, "footer-settings")
        assert gear.is_displayed()
        placed = _launchers(screen, ["footer-settings"])
        assert placed["launchers"][0]["right"] <= placed["footer"]["right"], placed
        gear.click()
        WebDriverWait(screen.selenium, 10).until(
            lambda _: run_in_app(lambda: ui_state.settings_content.dialog.value)
        )
        marked_element(screen, "settings-close").click()
        WebDriverWait(screen.selenium, 10).until(
            lambda _: not run_in_app(lambda: ui_state.settings_content.dialog.value)
        )


@pytest.mark.browser
def test_controls_only_layout_keeps_all_footer_readings_and_restores_the_editor(
    screen, tmp_path
):
    screen.selenium.set_window_size(1366, 900)
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=60)
    dismiss_dialogs(screen)
    click_tab(screen, "program")
    source = "# Keep this unsaved program across phone orientation changes.\n"
    run_in_app(lambda: setattr(ui_state.active_textarea, "value", source))
    panel_id = js(screen, "return document.querySelector('.overlay-br').id")
    measure = """
        const visible = e => !!e && !!e.getClientRects().length && e.offsetParent !== null;
        const box = e => { const r=e.getBoundingClientRect();
            return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height}; };
        const footer=document.querySelector('.status-footer');
        const control=document.querySelector('.overlay-br');
        const readings=[...footer.querySelectorAll('.pose-cell')];
        const robotName=footer.querySelector('.readout-robot-name');
        return {footer:box(footer), control:box(control), id:control.id,
            robotClipped:robotName.scrollWidth > robotName.clientWidth,
            editor:visible(document.querySelector('.program-panel')),
            scene:visible(document.querySelector('.nicegui-scene')),
            readings:readings.map(e=>({visible:visible(e), ...box(e)})),
            dials:[...control.querySelectorAll('.joint-dial')].map(box),
            width:innerWidth, height:innerHeight};
    """
    for width, height in ((390, 844), (667, 375), (844, 390), (568, 320)):
        with viewport(screen, width, height, mobile=True):
            layout = WebDriverWait(screen.selenium, 10).until(
                lambda _: (g := js(screen, measure))["width"] == width and g
            )
            assert not layout["editor"] and not layout["scene"], layout
            assert len(layout["readings"]) == 7, layout
            assert not layout["robotClipped"], layout
            f, c = layout["footer"], layout["control"]
            assert c["top"] >= 0 and c["bottom"] <= f["top"] + 1, layout
            assert f["bottom"] <= height and f["left"] >= 0 and f["right"] <= width, (
                layout
            )
            for reading in layout["readings"]:
                assert reading["visible"], layout
                assert f["left"] <= reading["left"] < reading["right"] <= f["right"], (
                    layout
                )
                assert f["top"] <= reading["top"] < reading["bottom"] <= f["bottom"], (
                    layout
                )
            assert layout["id"] == panel_id, "resizing rebuilt the controls"
            assert len(layout["dials"]) == 6, layout
            assert (
                max(d["top"] for d in layout["dials"])
                - min(d["top"] for d in layout["dials"])
                < 1
            ), "joint controls must remain in their existing single row"
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            WebDriverWait(screen.selenium, 5).until(
                lambda _: not js(
                    screen, "return !!document.querySelector('.q-tooltip')"
                )
            )
            screen.selenium.save_screenshot(
                str(tmp_path / f"footer-{width}x{height}.png")
            )
            for name in ("diagnostics", "log"):
                click_tab(screen, name)
                panel = js(
                    screen,
                    "return document.querySelector('.bottom-panel').getBoundingClientRect().toJSON()",
                )
                assert 0 <= panel["left"] < panel["right"] <= width, panel
                assert 0 <= panel["top"] < panel["bottom"] <= f["top"], panel
                close_panel(screen, "bottom-panel")
    with viewport(screen, 1366, 480):
        restored = WebDriverWait(screen.selenium, 10).until(
            lambda _: (g := js(screen, measure))["editor"] and g["scene"] and g
        )
        assert restored["footer"]["height"] <= 29, restored
        assert restored["id"] == panel_id
        assert run_in_app(lambda: ui_state.active_textarea.value) == source
        screen.selenium.save_screenshot(str(tmp_path / "restored-desktop.png"))
    with viewport(screen, 390, 844, mobile=True):
        screen.selenium.refresh()
        screen_wait_for_scene_ready(screen, timeout_s=60)
        fresh = WebDriverWait(screen.selenium, 10).until(
            lambda _: (g := js(screen, measure))["control"]["height"] > 0
            and all(r["visible"] for r in g["readings"])
            and g
        )
        assert not fresh["scene"] and not fresh["editor"], fresh
        assert fresh["control"]["bottom"] <= fresh["footer"]["top"] + 1, fresh
        _select_tool("SSG-48")
        WebDriverWait(screen.selenium, 10).until(
            lambda _: js(
                screen,
                "return document.querySelector('.footer-tool').textContent.includes('SSG')",
            )
        )
        screen.selenium.save_screenshot(str(tmp_path / "phone-tool-selected.png"))
        with viewport(screen, 568, 320, mobile=True):
            small = WebDriverWait(screen.selenium, 10).until(
                lambda _: (g := js(screen, measure))["width"] == 568
                and g["control"]["top"] >= 0
                and g["control"]["bottom"] <= g["footer"]["top"] + 1
                and g
            )
            for reading in small["readings"]:
                assert reading["visible"], small
            log = marked_element(screen, "footer-log")
            screen.selenium.execute_script(
                "arguments[0].scrollIntoView({block:'nearest'})", log
            )
            log.click()
            WebDriverWait(screen.selenium, 10).until(
                lambda _: marked_element(screen, "response-log").is_displayed()
            )
            close_panel(screen, "bottom-panel")
            screen.selenium.execute_cdp_cmd(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}
            )
            WebDriverWait(screen.selenium, 5).until(
                lambda _: not js(
                    screen, "return !!document.querySelector('.q-tooltip')"
                )
            )
            screen.selenium.save_screenshot(str(tmp_path / "small-landscape-tool.png"))
        _select_tool("NONE")
