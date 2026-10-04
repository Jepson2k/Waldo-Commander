"""Selenium browser tests for the program column and the plugin panels that
share the left side with it.

The tests share one page through ``class_screen``. Three plugin panels are
installed for the whole class, as a plugin package would be: they sit
unopened until the last test, which opens them.
"""

import json
import time
from typing import TYPE_CHECKING, ClassVar

import pytest
from nicegui import ui
from waldoctl import Commander, Panel, PanelSlot

from tests.helpers.browser_helpers import (
    click_tab,
    close_panel,
    js,
    marked_element,
    run_in_app,
    wait_for_codemirror_ready,
)
from tests.helpers.browser_session import wait
from tests.helpers.plugin_panels import install_plugin_panels
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

STORAGE_KEY = "parol_panel_sizes"

COLUMN = """
    const wrap = document.querySelector('.panels-wrap');
    const c = document.querySelector('.top-panels-container');
    const p = document.querySelector('.program-panel');
    const r = c.getBoundingClientRect();
    const footer = document.querySelector('.status-footer').getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, right: r.right, width: c.offsetWidth,
            panelWidth: p ? p.offsetWidth : 0, viewport: innerHeight, footerTop: footer.top,
            open: wrap.classList.contains('column-open'),
            columnRight: PanelResize.layout().columnRight,
            handles: [...(p ? p.querySelectorAll('[class*="resize-handle-"]') : [])]
                .map(h => [...h.classList].find(c => c.startsWith('resize-handle-')))};
"""


def get_storage(screen: "Screen", key: str) -> dict | None:
    result = js(screen, f"return localStorage.getItem('{key}')")
    if result:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            pass
    return None


def clear_storage(screen: "Screen", key: str) -> None:
    js(screen, f"localStorage.removeItem('{key}')")


def wait_ready(screen: "Screen", timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(screen, "return window.PanelResize && window.PanelResize.isAppReady()"):
            return
        time.sleep(0.1)
    raise AssertionError(f"PanelResize not ready after {timeout}s")


def drag(screen: "Screen", selector: str, dx: int = 0, dy: int = 0) -> None:
    """Simulate drag on a resize handle and wait for localStorage to change."""
    before = js(screen, f"return localStorage.getItem('{STORAGE_KEY}')")

    js(
        screen,
        """
        const el = document.querySelector(arguments[0]);
        if (!el) return;
        const r = el.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        el.dispatchEvent(new MouseEvent('mousedown', {clientX: x, clientY: y, bubbles: true}));
        document.dispatchEvent(new MouseEvent('mousemove', {clientX: x + arguments[1], clientY: y + arguments[2], bubbles: true}));
        document.dispatchEvent(new MouseEvent('mouseup', {clientX: x + arguments[1], clientY: y + arguments[2], bubbles: true}));
    """,
        selector,
        dx,
        dy,
    )

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if js(screen, f"return localStorage.getItem('{STORAGE_KEY}')") != before:
            return
        time.sleep(0.05)


def open_program(screen: "Screen") -> dict:
    click_tab(screen, "program")
    return wait(screen, 5).until(
        lambda _: (c := js(screen, COLUMN))
        and c["open"]
        and c["panelWidth"] > 0
        and c["columnRight"] > 0
        and c
    )


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


class BenchNotesPanel(Panel):
    """A bottom-rail plugin of fixed height, as a third-party package ships one."""

    id: ClassVar[str] = "bench-notes"
    display_name: ClassVar[str] = "Bench notes"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_BOTTOM_TAB
    tab_icon: ClassVar[str] = "edit_note"
    default_width: ClassVar[int] = 320
    default_height: ClassVar[int] = 240

    def build(self, commander: Commander) -> None:
        ui.label("bench notes").mark("bench-notes")


class TallPanel(Panel):
    """A drag-resizable plugin that declares no minima, as waldoctl allows,
    with content taller than any viewport."""

    id: ClassVar[str] = "tall"
    display_name: ClassVar[str] = "Tall"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_TOP_TAB
    tab_icon: ClassVar[str] = "view_day"
    resizable: ClassVar[bool] = True

    def build(self, commander: Commander) -> None:
        ui.element("div").style("height: 3000px")


class NotesPanel(Panel):
    """A drag-resizable bottom plugin, so the two left panels share the height."""

    id: ClassVar[str] = "notes"
    display_name: ClassVar[str] = "Notes"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_BOTTOM_TAB
    tab_icon: ClassVar[str] = "sticky_note_2"
    resizable: ClassVar[bool] = True

    def build(self, commander: Commander) -> None:
        ui.label("notes").mark("notes")


@pytest.fixture(scope="class")
def plugin_panels():
    """The test plugins, discovered when the class's page is built."""
    with pytest.MonkeyPatch.context() as mp:
        install_plugin_panels(mp, BenchNotesPanel, TallPanel, NotesPanel)
        yield
    # Discovery is cached for the process: the next page build must scan again.
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()


@pytest.mark.browser
@pytest.mark.usefixtures("plugin_panels")
class TestProgramColumn:
    """The program panel is a column: full height, width the only dimension."""

    def test_the_column_spans_the_viewport_above_the_footer(
        self, class_screen: "Screen"
    ) -> None:
        wait_ready(class_screen, timeout=10)
        clear_storage(class_screen, STORAGE_KEY)
        col = open_program(class_screen)
        assert col["open"], col
        assert abs(col["top"] - 12) <= 1, col
        # Pinned to the footer clearance: 12 px above the 28 px footer, which
        # itself sits 12 px off the bottom.
        assert abs(col["bottom"] - (col["viewport"] - 52)) <= 1, col
        assert col["bottom"] <= col["footerTop"], col
        assert col["handles"] == ["resize-handle-right"], col
        assert abs(col["columnRight"] - col["right"]) <= 1, col

    def test_the_editor_opens_at_its_default_width(
        self, class_screen: "Screen"
    ) -> None:
        """A width nobody dragged is the default, not the minimum an older
        build saved when the editor closed."""
        wait_ready(class_screen, timeout=10)
        if js(
            class_screen,
            "return !!document.querySelector('.program-panel')?.offsetParent",
        ):
            close_panel(class_screen, "program-panel")
            wait(class_screen, 5).until(lambda _: not js(class_screen, COLUMN)["open"])
        js(
            class_screen,
            """
            localStorage.removeItem(arguments[0] + '_defaults');
            localStorage.setItem(arguments[0],
                JSON.stringify({program: {width: 450, group: 'top'}}));
            PanelResize.configure(PanelResize.getConfig());
            """,
            STORAGE_KEY,
        )
        assert "width" not in get_storage(class_screen, STORAGE_KEY)["program"]
        col = open_program(class_screen)
        assert col["width"] >= 670, col

    def test_width_persists_and_height_is_never_saved(
        self, class_screen: "Screen"
    ) -> None:
        wait_ready(class_screen, timeout=10)
        clear_storage(class_screen, STORAGE_KEY)
        before = open_program(class_screen)

        drag(class_screen, ".program-panel .resize-handle-right", dx=100)
        after = wait(class_screen, 5).until(
            lambda _: (c := js(class_screen, COLUMN))
            and abs(c["columnRight"] - c["right"]) <= 1
            and c
        )
        assert after["width"] > before["width"], (before, after)
        assert abs(after["bottom"] - before["bottom"]) <= 1, (before, after)

        saved = get_storage(class_screen, STORAGE_KEY)
        assert saved and abs(saved["program"]["width"] - after["panelWidth"]) < 10, (
            saved
        )
        assert "height" not in saved["program"], saved

        close_panel(class_screen, "program-panel")
        wait(class_screen, 5).until(
            lambda _: (c := js(class_screen, COLUMN))
            and not c["open"]
            and c["columnRight"] == 0
            and c
        )
        assert "height" not in get_storage(class_screen, STORAGE_KEY)["program"]

        reopened = open_program(class_screen)
        assert abs(reopened["width"] - after["width"]) < 3, (after, reopened)
        assert abs(reopened["bottom"] - (reopened["viewport"] - 52)) <= 1, reopened

    def test_the_column_cannot_shrink_below_its_minimum(
        self, class_screen: "Screen"
    ) -> None:
        wait_ready(class_screen, timeout=10)
        before = open_program(class_screen)
        minimum = js(
            class_screen, "return PanelResize.getConfig().panels.program.minWidth"
        )
        assert before["width"] > minimum + 100, (minimum, before)
        drag(class_screen, ".program-panel .resize-handle-right", dx=-500)
        after = wait(class_screen, 5).until(
            lambda _: (c := js(class_screen, COLUMN))
            and c["width"] < before["width"]
            and c
        )
        assert abs(after["width"] - minimum) <= 1, (minimum, after)

    def test_a_hidden_panels_preset_leaves_the_column_alone(
        self, class_screen: "Screen"
    ) -> None:
        """The gripper's camera preset is saved for when its tab opens; the
        container it shares is the program column's while that is open."""
        wait_ready(class_screen, timeout=10)
        before = open_program(class_screen)
        js(class_screen, "PanelResize.resizePanel('gripper', 'camera')")
        saved = get_storage(class_screen, STORAGE_KEY)
        assert saved and saved["gripper"]["width"] == 660, saved
        assert saved["gripper"]["height"] == 675, saved

        after = js(class_screen, COLUMN)
        for key in ("top", "bottom", "right", "width", "columnRight"):
            assert abs(after[key] - before[key]) <= 1, (key, before, after)

    def test_a_widened_program_panel_stays_on_top_and_closes_while_recording(
        self, class_screen: "Screen"
    ) -> None:
        screen = class_screen
        wait_ready(screen, timeout=10)
        open_program(screen)
        wait_for_codemirror_ready(screen)
        close = marked_element(screen, "program-panel-close").get_attribute("id")
        try:
            # Widened over the control card, the panel is drawn over the card and
            # its close button, now above the card's column, still takes the click.
            card = js(
                screen,
                "const r = document.querySelector('.overlay-br').getBoundingClientRect();"
                "return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};",
            )
            x = (card["left"] + card["right"]) / 2
            under_card = wait(screen).until(
                lambda _: (m := js(screen, _CLOSE_AT, x, close))["cx"] > card["left"]
                and m
            )
            assert under_card["inPanel"] and under_card["onClose"], under_card
            assert js(
                screen,
                "const [x, y] = arguments; const hit = document.elementFromPoint(x, y);"
                "return !!hit && !!hit.closest('.program-panel');",
                x - 30,
                (card["top"] + card["bottom"]) / 2,
            ), "the control card is drawn over the widened program panel"
            marked_element(screen, "program-panel-close").click()
            # Closed once the tab itself shows it: a click that lands before the
            # deselection reaches the page is undone by it.
            wait(screen, 5).until(
                lambda _: not js(
                    screen,
                    "return [...document.querySelectorAll('.q-tab--active .q-icon')]"
                    ".some(i => i.textContent === 'code');",
                )
            )

            # The standing Recording notice sits over the middle of the header;
            # it must not take the close button's click either.
            click_tab(screen, "program")
            marked_element(screen, "editor-record-btn").click()
            notice = wait(screen).until(
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
            under_notice = wait(screen).until(
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
            marked_element(screen, "program-panel-close").click()
            wait(screen, 5).until(
                lambda _: not run_in_app(lambda: ui_state.program_panel_visible)
            )
        finally:
            if run_in_app(is_any_program_recording):
                run_in_app(motion_recorder.toggle_recording)
            js(
                screen,
                "PanelResize.resizePanel('program', 'default'); PanelResize.clearAllSizes();",
            )

    def test_the_last_line_scrolls_clear_of_the_fade_and_the_playback_bar(
        self, class_screen: "Screen"
    ) -> None:
        screen = class_screen
        wait_ready(screen, timeout=10)
        open_program(screen)
        wait_for_codemirror_ready(screen)
        lines = [f"step_{i} = {i}" for i in range(80)] + ["last_line = True"]
        original = run_in_app(lambda: ui_state.active_textarea.value)

        def fill(text: str) -> None:
            ui_state.active_textarea.value = text

        run_in_app(lambda: fill("\n".join(lines) + "\n"))
        try:
            measured = wait(screen).until(
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
        finally:
            run_in_app(lambda: fill(original))

    def test_plugin_panels_share_the_left_side_without_overlapping(
        self, class_screen: "Screen"
    ) -> None:
        """A bottom plugin stops the program column above it, and a resizable
        plugin that leaves its minima unset still gives way to a bottom panel
        below it, so neither is drawn over the other. Last in the class: it
        leaves plugin panels open."""
        screen = class_screen
        wait_ready(screen, timeout=10)
        open_program(screen)
        marked_element(screen, "tab-bench-notes").click()
        stacked = """
            const r = s => document.querySelector(s).getBoundingClientRect();
            const column = r('.top-panels-container'), plugin = r('.bottom-panels-container');
            return {columnTop: column.top, columnBottom: column.bottom,
                    pluginTop: plugin.top, pluginHeight: plugin.height};
        """
        shown = wait(screen).until(
            lambda _: (g := js(screen, stacked))["pluginHeight"] >= 200
            and g["columnBottom"] <= g["pluginTop"] - 11
            and g
        )
        assert shown["columnBottom"] > shown["columnTop"] + 100, shown

        js(screen, "PanelResize.clearAllSizes()")
        measure = """
            const top = document.querySelector('.top-panels-container').getBoundingClientRect();
            const bottom = document.querySelector('.bottom-panels-container').getBoundingClientRect();
            return {plugin: !!document.querySelector('.tall-panel')?.offsetParent,
                    notes: !!document.querySelector('.notes-panel')?.offsetParent,
                    topBottom: top.bottom, bottomTop: bottom.top};
        """

        # A tab click while the other panel is still opening can miss; a tab
        # never closes its own panel, so click whichever is not up yet.
        def both_open(_):
            r = js(screen, measure)
            for shown, tab in ((r["plugin"], "tab-tall"), (r["notes"], "tab-notes")):
                if not shown:
                    marked_element(screen, tab).click()
            return r["plugin"] and r["notes"] and r

        wait(screen).until(both_open)
        wait(screen).until(
            lambda _: (r := js(screen, measure))["topBottom"] <= r["bottomTop"] + 1
            and r
        )
