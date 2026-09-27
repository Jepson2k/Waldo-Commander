"""Selenium browser tests for the program column and the plugin-panel resize
system that survives around it.

All tests share a single browser session and page load via class_screen fixture.
"""

import json
import time
from typing import TYPE_CHECKING

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import click_tab, close_panel, js

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
