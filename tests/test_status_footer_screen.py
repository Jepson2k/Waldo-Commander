"""The status footer stays one row however many I/O lines the backend reports
or however narrow the window, and the panels above it keep clear of it."""

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
        for width in (1100, 900):
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

    # On an 800x480 kiosk the bottom panel gives way, so the column keeps
    # room for its playbar and the Stop on it.
    playbar = """
        const box = s => { const e = document.querySelector(s); if (!e || e.offsetParent === null) return null;
                           const b = e.getBoundingClientRect(); return {top: b.top, bottom: b.bottom}; };
        return {panel: box('.bottom-panel'), column: box('.top-panels-container'),
                playbar: box('.program-panel .bottom-playback-bar'), viewport: innerHeight};
    """
    with viewport(screen, 800, 480):
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
