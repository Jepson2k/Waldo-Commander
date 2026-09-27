"""The program panel paints over what shares its space, and its last line shows."""

import pytest
from nicegui import Client
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import (
    click_tab,
    dismiss_dialogs,
    js,
    run_in_app,
    wait_for_codemirror_ready,
)
from tests.helpers.wait import screen_wait_for_scene_ready
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state

# Widen the program panel until the given x sits under the close button, then
# report the button's box and what the page draws at its centre.
_CLOSE_AT = """
const [x, marker] = arguments;
const panel = document.querySelector('.program-panel').getBoundingClientRect();
const close = document.getElementById(marker).getBoundingClientRect();
const width = panel.width + x - (close.left + close.width / 2);
PanelResize.resizePanel('program', {width: width, height: panel.height});
const now = document.getElementById(marker).getBoundingClientRect();
const cx = now.left + now.width / 2;
const cy = now.top + now.height / 2;
const hit = document.elementFromPoint(cx, cy);
return {cx: cx, cy: cy, width: width, onClose: !!hit && !!hit.closest('#' + marker),
        inPanel: !!hit && !!hit.closest('.program-panel')};
"""


def _open_program(screen) -> str:
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    WebDriverWait(screen.selenium, 10).until(
        lambda _: js(screen, "return !!window.PanelResize && PanelResize.isAppReady()")
    )
    click_tab(screen, "program")
    wait_for_codemirror_ready(screen)

    def close_id() -> int:
        client = Client.instances[ui_state.active_client_id]
        return next(
            e.id
            for e in client.elements.values()
            if "program-panel-close" in e._markers
        )

    return f"c{run_in_app(close_id)}"


def _restore_sizes(screen) -> None:
    js(
        screen,
        "PanelResize.resizePanel('program', 'default'); PanelResize.clearAllSizes();",
    )


@pytest.mark.browser
def test_a_widened_program_panel_stays_on_top_and_closes_while_recording(screen):
    close = _open_program(screen)
    try:
        # Widened under the readout card, the panel is drawn over the card and
        # its close button, now inside the card's box, still takes the click.
        readout = js(
            screen,
            "const r = document.querySelector('.readout-panel').getBoundingClientRect();"
            "return {left: r.left, right: r.right};",
        )
        x = (readout["left"] + readout["right"]) / 2
        under_card = WebDriverWait(screen.selenium, 10).until(
            lambda _: (m := js(screen, _CLOSE_AT, x, close))["cx"] > readout["left"]
            and m
        )
        assert under_card["inPanel"] and under_card["onClose"], under_card
        screen.selenium.find_element(By.ID, close).click()
        WebDriverWait(screen.selenium, 5).until(
            lambda _: not run_in_app(lambda: ui_state.program_panel_visible)
        )

        # The standing Recording notice sits over the middle of the header;
        # it must not take the close button's click either.
        click_tab(screen, "program")
        screen.selenium.find_element(
            By.ID,
            f"c{run_in_app(lambda: ui_state.editor_panel.playback.record_btn.id)}",
        ).click()
        notice = WebDriverWait(screen.selenium, 10).until(
            lambda _: js(
                screen,
                "const n = document.querySelector('.recording-notification');"
                "if (!n) return null; const r = n.getBoundingClientRect();"
                # It slides in from above the viewport.
                "if (r.top < 0) return null;"
                "return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};",
            )
        )
        x = (notice["left"] + notice["right"]) / 2
        under_notice = WebDriverWait(screen.selenium, 10).until(
            lambda _: (m := js(screen, _CLOSE_AT, x, close))
            and abs(m["cx"] - x) < 2
            and m
        )
        assert notice["top"] <= under_notice["cy"] <= notice["bottom"], (
            "the notice no longer overlaps the header",
            notice,
            under_notice,
        )
        assert under_notice["onClose"], under_notice
        screen.selenium.find_element(By.ID, close).click()
        WebDriverWait(screen.selenium, 5).until(
            lambda _: not run_in_app(lambda: ui_state.program_panel_visible)
        )
    finally:
        if run_in_app(is_any_program_recording):
            run_in_app(motion_recorder.toggle_recording)
        _restore_sizes(screen)


@pytest.mark.browser
def test_the_last_line_scrolls_clear_of_the_fade_and_the_playback_bar(screen):
    _open_program(screen)
    lines = [f"step_{i} = {i}" for i in range(80)] + ["last_line = True"]

    def fill() -> None:
        textarea = ui_state.active_textarea
        assert textarea is not None
        textarea.value = "\n".join(lines) + "\n"

    run_in_app(fill)
    measured = WebDriverWait(screen.selenium, 10).until(
        lambda _: js(
            screen,
            """
            const scroller = document.querySelector('.program-panel .cm-scroller');
            scroller.scrollTop = scroller.scrollHeight;
            const last = [...scroller.querySelectorAll('.cm-line')]
                .find(l => l.textContent === 'last_line = True');
            if (!last || scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 1)
                return null;
            const bar = document.querySelector('.program-panel .bottom-playback-bar');
            return {line: last.getBoundingClientRect().bottom,
                    fade: scroller.getBoundingClientRect().bottom - 16,
                    bar: bar.getBoundingClientRect().top};
            """,
        )
    )
    assert measured["line"] <= measured["fade"] + 1, measured
    assert measured["line"] <= measured["bar"] + 1, measured
