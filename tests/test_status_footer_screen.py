"""The status footer stays one row however many I/O lines the backend reports."""

import pytest
import waldoctl
from nicegui import Client
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import click_tab, dismiss_dialogs, js, run_in_app
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


@pytest.mark.browser
def test_bottom_panel_stays_clear_of_the_control_panel(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    screen.selenium.set_window_size(1366, 768)
    # The program column narrows the panel from the left as the control panel does from the right.
    click_tab(screen, "program")
    click_tab(screen, "diagnostics")
    rects = WebDriverWait(screen.selenium, 10).until(
        lambda _: (
            r := js(
                screen,
                """
                const p = document.querySelector('.bottom-panel').getBoundingClientRect();
                const c = document.querySelector('.overlay-br').getBoundingClientRect();
                return {panelRight: p.right, panelLeft: p.left, controlLeft: c.left,
                        panelTop: p.top, controlBottom: c.bottom};
                """,
            )
        )
        and r["panelRight"] <= r["controlLeft"] - 11
        and r
    )
    assert rects["panelLeft"] < rects["panelRight"], rects
    grid = js(
        screen,
        """
        const g = document.querySelector('.bottom-panel .diag-grid');
        return {width: g.clientWidth,
                columns: getComputedStyle(g).gridTemplateColumns.split(' ').length};
        """,
    )
    # Two 300 px columns and their gap do not fit, so the sections stack.
    assert grid["width"] < 616 and grid["columns"] == 1, grid
