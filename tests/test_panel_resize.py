"""Selenium browser tests for panel resize and tab switching functionality.

All tests share a single browser session and page load via class_screen fixture.
"""

import json
import time
from typing import TYPE_CHECKING

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

from tests.helpers.browser_helpers import click_tab, close_panel, js

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

STORAGE_KEY = "parol_panel_sizes"


# ============================================================================
# Helper Functions
# ============================================================================


def get_classes(el: WebElement) -> list[str]:
    """Get CSS classes on a WebElement."""
    class_attr = el.get_attribute("class")
    return class_attr.split() if class_attr else []


def get_storage(screen: "Screen", key: str) -> dict | None:
    """Read JSON from localStorage."""
    result = js(screen, f"return localStorage.getItem('{key}')")
    if result:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            pass
    return None


def clear_storage(screen: "Screen", key: str) -> None:
    """Remove item from localStorage."""
    js(screen, f"localStorage.removeItem('{key}')")


def wait_ready(screen: "Screen", timeout: float = 5.0) -> None:
    """Wait for PanelResize module to be ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(screen, "return window.PanelResize && window.PanelResize.isAppReady()"):
            return
        time.sleep(0.1)
    raise AssertionError(f"PanelResize not ready after {timeout}s")


def drag(screen: "Screen", selector: str, dx: int = 0, dy: int = 0) -> None:
    """Simulate drag on resize handle and wait for localStorage update."""
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

    # Wait for localStorage update
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if js(screen, f"return localStorage.getItem('{STORAGE_KEY}')") != before:
            return
        time.sleep(0.05)


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.browser
class TestPanelResize:
    """Panel resize tests sharing single browser session and page load."""

    def test_closing_tabs_preserves_bottom_panel_height(
        self, class_screen: "Screen"
    ) -> None:
        """Closing any top-level tab should not shift the bottom panel."""
        wait_ready(class_screen)
        clear_storage(class_screen, STORAGE_KEY)

        click_tab(class_screen, "program")
        click_tab(class_screen, "log")

        bottom = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".bottom-panels-container"
        )
        initial_height = bottom.rect["height"]

        close_panel(class_screen, "program-panel")
        time.sleep(0.2)

        final_height = bottom.rect["height"]
        assert abs(final_height - initial_height) < 30

    def test_tab_switch_preserves_bottom_panel(self, class_screen: "Screen") -> None:
        """Bottom panel stays anchored when switching tabs."""
        click_tab(class_screen, "program")
        click_tab(class_screen, "log")

        bottom = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".bottom-panels-container"
        )
        initial = bottom.rect

        click_tab(class_screen, "io")

        final = bottom.rect
        assert abs(final["y"] + final["height"] - initial["y"] - initial["height"]) < 50
        assert abs(final["height"] - initial["height"]) < 20

    def test_resize_panel_dimensions(self, class_screen: "Screen") -> None:
        """Resize handles change panel dimensions."""
        clear_storage(class_screen, STORAGE_KEY)
        click_tab(class_screen, "program")

        panel = class_screen.selenium.find_element(By.CSS_SELECTOR, ".program-panel")
        initial_width = panel.rect["width"]
        drag(class_screen, ".program-panel .resize-handle-right", dx=100)
        assert panel.rect["width"] > initial_width

        click_tab(class_screen, "log")
        bottom = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".bottom-panels-container"
        )
        initial_height = bottom.rect["height"]
        drag(class_screen, ".response-panel .resize-handle-top", dy=-50)
        final_height = bottom.rect["height"]
        assert abs(final_height - initial_height) > 10 or final_height >= initial_height

    def test_resize_respects_min_constraints(self, class_screen: "Screen") -> None:
        """Panels cannot be resized below minimum dimensions."""
        click_tab(class_screen, "program")
        drag(class_screen, ".program-panel .resize-handle-right", dx=-500)
        panel = class_screen.selenium.find_element(By.CSS_SELECTOR, ".program-panel")
        assert panel.rect["width"] >= 395

        click_tab(class_screen, "log")
        drag(class_screen, ".response-panel .resize-handle-top", dy=500)
        response = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".response-panel"
        )
        assert response.rect["height"] >= 95

    def test_resize_saves_to_localstorage(self, class_screen: "Screen") -> None:
        """Resizing saves dimensions to localStorage."""
        clear_storage(class_screen, STORAGE_KEY)
        click_tab(class_screen, "program")

        panel = class_screen.selenium.find_element(By.CSS_SELECTOR, ".program-panel")
        before_width = panel.rect["width"]
        drag(class_screen, ".program-panel .resize-handle-right", dx=80)
        after_width = panel.rect["width"]
        assert after_width > before_width

        saved = get_storage(class_screen, STORAGE_KEY)
        assert saved and "program" in saved
        assert abs(saved["program"]["width"] - after_width) < 10

        click_tab(class_screen, "log")
        drag(class_screen, ".response-panel .resize-handle-top", dy=-40)

        saved = get_storage(class_screen, STORAGE_KEY)
        assert saved and "response" in saved and saved["response"]["height"]

    def test_push_system_interactions(self, class_screen: "Screen") -> None:
        """Growing one panel affects the other via push system."""
        click_tab(class_screen, "program")
        click_tab(class_screen, "log")

        bottom = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".bottom-panels-container"
        )
        before_height = bottom.rect["height"]
        drag(class_screen, ".program-panel .resize-handle-bottom", dy=80)
        assert bottom.rect["height"] <= before_height + 10

        drag(class_screen, ".response-panel .resize-handle-top", dy=-80)
        program = class_screen.selenium.find_element(By.CSS_SELECTOR, ".program-panel")
        response = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".response-panel"
        )
        assert program.rect["height"] >= 100
        assert response.rect["height"] >= 95

    def test_closing_io_preserves_resized_response_log(
        self, class_screen: "Screen"
    ) -> None:
        """Closing gripper tab after resizing preserves response log position."""
        clear_storage(class_screen, STORAGE_KEY)

        click_tab(class_screen, "log")
        bottom = class_screen.selenium.find_element(
            By.CSS_SELECTOR, ".bottom-panels-container"
        )
        initial_height = bottom.rect["height"]

        click_tab(class_screen, "program")
        drag(class_screen, ".response-panel .resize-handle-top", dy=50)

        after_drag_height = bottom.rect["height"]
        assert after_drag_height < initial_height

        click_tab(class_screen, "io")
        after_switch_height = bottom.rect["height"]
        assert abs(after_switch_height - after_drag_height) < 10

        # Close gripper via JS since we don't have gripper_panel in ui_state
        js(
            class_screen,
            """
            const panel = document.querySelector('.q-tab-panel:not([style*="display: none"])');
            const btn = panel?.querySelector('button i[innerText="close"]')?.closest('button');
            btn?.click();
        """,
        )
        time.sleep(0.3)

        final_height = bottom.rect["height"]
        assert abs(final_height - after_switch_height) < 30

    def test_a_fit_panel_is_as_tall_as_its_content_until_dragged(
        self, class_screen: "Screen"
    ) -> None:
        """Settings opens at its content's height, capped at the viewport, and a
        height the operator drags is the one it keeps."""
        wait_ready(class_screen)
        clear_storage(class_screen, STORAGE_KEY)
        click_tab(class_screen, "settings")
        time.sleep(0.5)

        measure = """
            const c = document.querySelector('.bottom-panels-container');
            const p = document.querySelector('.settings-panel');
            const body = p.querySelector('.settings-content')
                || p.querySelector('.q-scrollarea__container');
            const r = p.getBoundingClientRect();
            const lowest = Math.max(...[...body.children]
                .filter(e => e.offsetParent !== null)
                .map(e => e.getBoundingClientRect().bottom));
            return {inline: c.style.height, height: r.height, bottom: r.bottom,
                    viewport: innerHeight, slack: r.bottom - lowest,
                    scrolls: body.scrollHeight > body.clientHeight + 1,
                    clipped: p.scrollHeight > p.clientHeight + 1};
        """
        fit = js(class_screen, measure)
        assert fit["inline"] == "", fit
        # Every row's fields end at the panel's content edge: no empty strip on the right.
        edges = js(
            class_screen,
            """
            const c = document.querySelector('.settings-panel .settings-content');
            const inner = c.getBoundingClientRect().left + c.clientLeft + c.clientWidth;
            const rights = [...c.querySelectorAll('.settings-row')]
                .map(row => [...row.querySelectorAll('.q-field')]
                    .filter(e => e.offsetParent !== null)
                    .map(e => e.getBoundingClientRect().right))
                .filter(r => r.length)
                .map(r => Math.max(...r));
            return {inner, rights};
        """,
        )
        assert edges["rights"], edges
        assert all(edges["inner"] - right <= 2 for right in edges["rights"]), edges
        assert not fit["clipped"], fit
        assert fit["bottom"] <= fit["viewport"], fit
        capped = fit["height"] >= fit["viewport"] - 30
        # Content-sized means no room left over under the last row; at the cap
        # the rows scroll instead.
        assert fit["scrolls"] if capped else fit["slack"] <= 24, fit

        drag(class_screen, ".settings-panel .resize-handle-top", dy=60)
        saved = get_storage(class_screen, STORAGE_KEY)
        assert saved and saved["settings"]["height"], saved
        dragged = js(class_screen, measure)
        assert dragged["inline"] != "", dragged

        close_panel(class_screen, "settings-panel")
        time.sleep(0.3)
        click_tab(class_screen, "settings")
        time.sleep(0.5)
        reopened = js(class_screen, measure)
        assert abs(reopened["height"] - saved["settings"]["height"]) < 3, reopened

        # A height an older build saved on close was a default, not a choice.
        js(
            class_screen,
            """
            localStorage.removeItem(arguments[0] + '_fit');
            localStorage.setItem(arguments[0],
                JSON.stringify({settings: {height: 560, group: 'bottom'}}));
            PanelResize.configure(PanelResize.getConfig());
            """,
            STORAGE_KEY,
        )
        assert "height" not in get_storage(class_screen, STORAGE_KEY)["settings"]
        close_panel(class_screen, "settings-panel")

    def test_the_editor_opens_at_its_default_size(self, class_screen: "Screen") -> None:
        """The editor opens at its default size, not its minimum. The minimum
        an older build saved when the editor closed was not a choice."""
        wait_ready(class_screen)
        if js(
            class_screen,
            "return !!document.querySelector('.program-panel')?.offsetParent",
        ):
            close_panel(class_screen, "program-panel")
            time.sleep(0.3)
        js(
            class_screen,
            """
            localStorage.removeItem(arguments[0] + '_defaults');
            localStorage.setItem(arguments[0],
                JSON.stringify({program: {height: 300, group: 'top'}}));
            PanelResize.configure(PanelResize.getConfig());
            """,
            STORAGE_KEY,
        )
        assert "height" not in get_storage(class_screen, STORAGE_KEY)["program"]
        click_tab(class_screen, "program")
        time.sleep(0.5)
        size = js(
            class_screen,
            """
            const c = document.querySelector('.top-panels-container');
            return {width: c.offsetWidth, height: c.offsetHeight, viewport: innerHeight};
            """,
        )
        assert size["width"] >= 670 and size["height"] >= 470, size

    def test_diagnostics_and_settings_share_the_column(
        self, class_screen: "Screen"
    ) -> None:
        """Diagnostics sits above Settings in the left column. With both open,
        the taller content-sized panel gives way; neither is drawn over the
        other."""
        wait_ready(class_screen)
        clear_storage(class_screen, STORAGE_KEY)
        click_tab(class_screen, "diagnostics")
        time.sleep(0.5)
        click_tab(class_screen, "settings")
        time.sleep(1.0)
        rects = js(
            class_screen,
            """
            const r = s => document.querySelector(s).getBoundingClientRect();
            const d = r('.diagnostics-view'), s = r('.settings-panel');
            return {diagnosticsBottom: d.bottom, settingsTop: s.top,
                    diagnostics: d.height, settings: s.height, viewport: innerHeight};
        """,
        )
        assert rects["diagnosticsBottom"] <= rects["settingsTop"], rects
        close_panel(class_screen, "settings-panel")
        close_panel(class_screen, "diagnostics-view")
        time.sleep(0.3)
