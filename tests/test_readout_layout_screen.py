"""The readout's I/O strip stays in its header row however many lines there are."""

import pytest
import waldoctl
from nicegui import Client
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import dismiss_dialogs, js, run_in_app
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.state import ui_state

MEASURE = """
    const header = document.querySelector('.readout-header').getBoundingClientRect();
    const robot = document.querySelector('.readout-header > .q-chip').getBoundingClientRect();
    const io = document.querySelector('.io-chips').getBoundingClientRect();
    return {header: header.height, robot: robot.height, ioTop: io.top, ioBottom: io.bottom,
            headerTop: header.top, headerBottom: header.bottom, ioLeft: io.left,
            robotRight: robot.right, ioWidth: io.width,
            chips: document.querySelectorAll('.io-chips .q-chip').length};
"""


def _show_lines(count: int | None) -> None:
    """Rebuild the strip for *count* lines each way, and hold it there: the
    status loop re-reads the robot's real line count every tick. None hands
    the strip back to the status loop."""

    def apply():
        readout = ui_state.readout_panel
        if count is None:
            readout.__dict__.pop("update_conn_io", None)
        else:
            io = waldoctl.commander.status.io
            io.inputs = [0] * count
            io.outputs = [0] * count
        with Client.instances[ui_state.active_client_id]:
            type(readout).update_conn_io(readout)
        if count is not None:
            readout.update_conn_io = lambda: None

    run_in_app(apply)


def _measure_with(screen, chips: int) -> dict:
    return WebDriverWait(screen.selenium, 10).until(
        lambda _: (m := js(screen, MEASURE))["chips"] == chips and m
    )


@pytest.mark.browser
def test_many_io_lines_pack_into_the_header_row(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    screen.selenium.set_window_size(1366, 768)
    before = _measure_with(screen, 4)

    try:
        _show_lines(12)
        many = _measure_with(screen, 24)
    finally:
        _show_lines(None)
    assert many["header"] <= many["robot"] + 2, many
    assert many["ioTop"] >= many["headerTop"] - 1, many
    assert many["ioBottom"] <= many["headerBottom"] + 1, many
    assert many["ioLeft"] >= many["robotRight"], many
    assert many["ioWidth"] <= 200, many

    restored = _measure_with(screen, 4)
    assert abs(restored["header"] - before["header"]) < 1, (restored, before)
