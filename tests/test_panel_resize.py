"""Selenium browser tests for the program column and the plugin-panel resize
system that survives around it.

The column tests share a single browser session and page load via the
class_screen fixture.
"""

import json
import time
from typing import TYPE_CHECKING, ClassVar

import pytest
from nicegui import ui
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from waldoctl import Commander, Panel, PanelSlot

from tests.helpers.browser_helpers import (
    click_tab,
    close_panel,
    dismiss_dialogs,
    js,
    marked_element,
)
from tests.helpers.plugin_panels import install_plugin_panels
from tests.helpers.wait import screen_wait_for_scene_ready

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
    return WebDriverWait(screen.selenium, 5).until(
        lambda _: (c := js(screen, COLUMN))
        and c["open"]
        and c["panelWidth"] > 0
        and c["columnRight"] > 0
        and c
    )


@pytest.mark.browser
class TestProgramColumn:
    """The program panel is a column: full height, width the only dimension."""

    def test_the_column_spans_the_viewport_above_the_footer(
        self, class_screen: "Screen"
    ) -> None:
        wait_ready(class_screen)
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
        wait_ready(class_screen)
        if js(
            class_screen,
            "return !!document.querySelector('.program-panel')?.offsetParent",
        ):
            close_panel(class_screen, "program-panel")
            WebDriverWait(class_screen.selenium, 5).until(
                lambda _: not js(class_screen, COLUMN)["open"]
            )
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
        wait_ready(class_screen)
        clear_storage(class_screen, STORAGE_KEY)
        before = open_program(class_screen)

        drag(class_screen, ".program-panel .resize-handle-right", dx=100)
        after = WebDriverWait(class_screen.selenium, 5).until(
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
        WebDriverWait(class_screen.selenium, 5).until(
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
        open_program(class_screen)
        drag(class_screen, ".program-panel .resize-handle-right", dx=-500)
        panel = class_screen.selenium.find_element(By.CSS_SELECTOR, ".program-panel")
        assert panel.rect["width"] >= 395

    def test_a_hidden_panels_preset_leaves_the_column_alone(
        self, class_screen: "Screen"
    ) -> None:
        """The gripper's camera preset is saved for when its tab opens; the
        container it shares is the program column's while that is open."""
        before = open_program(class_screen)
        js(class_screen, "PanelResize.resizePanel('gripper', 'camera')")
        saved = get_storage(class_screen, STORAGE_KEY)
        assert saved and saved["gripper"]["width"] == 660, saved
        assert saved["gripper"]["height"] == 675, saved

        after = js(class_screen, COLUMN)
        for key in ("top", "bottom", "right", "width", "columnRight"):
            assert abs(after[key] - before[key]) <= 1, (key, before, after)


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


@pytest.mark.browser
def test_a_bottom_plugin_panel_stops_the_column_above_it(
    screen: "Screen", monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugin_panels(monkeypatch, BenchNotesPanel)
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    wait_ready(screen, timeout=10)

    open_program(screen)
    marked_element(screen, "tab-bench-notes").click()
    stacked = """
        const r = s => document.querySelector(s).getBoundingClientRect();
        const column = r('.top-panels-container'), plugin = r('.bottom-panels-container');
        return {columnTop: column.top, columnBottom: column.bottom,
                pluginTop: plugin.top, pluginHeight: plugin.height};
    """
    shown = WebDriverWait(screen.selenium, 10).until(
        lambda _: (g := js(screen, stacked))["pluginHeight"] >= 200
        and g["columnBottom"] <= g["pluginTop"] - 11
        and g
    )
    assert shown["columnBottom"] > shown["columnTop"] + 100, shown


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


@pytest.mark.browser
def test_a_plugin_without_minima_gives_way_to_a_bottom_panel(
    screen: "Screen", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resizable plugin that leaves its minima unset is still resized: with
    a bottom panel open below it, the taller of the two gives way and neither
    is drawn over the other."""
    install_plugin_panels(monkeypatch, TallPanel, NotesPanel)
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)
    wait_ready(screen, timeout=10)
    js(screen, "PanelResize.clearAllSizes()")

    marked_element(screen, "tab-tall").click()
    marked_element(screen, "tab-notes").click()
    measure = """
        const top = document.querySelector('.top-panels-container').getBoundingClientRect();
        const bottom = document.querySelector('.bottom-panels-container').getBoundingClientRect();
        return {plugin: !!document.querySelector('.tall-panel')?.offsetParent,
                topBottom: top.bottom, bottomTop: bottom.top};
    """
    WebDriverWait(screen.selenium, 10).until(
        lambda _: (r := js(screen, measure))["plugin"]
        and r["topBottom"] <= r["bottomTop"] + 1
        and r
    )
