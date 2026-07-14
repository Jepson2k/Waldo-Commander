"""Browser (screen-fixture) test for the control-lease indicator UI.

Covers the real-DOM behavior the ``user``-fixture tests can't: when an MCP
session holds control the active browser shows the amber AI-driving glow and an
edge Take-control button, and the human's "Take control" click reclaims control
and hides them again. The lease singleton is shared between the in-process app
and the test, so the test plays the MCP side by seizing the lease directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from selenium.webdriver.common.by import By

from tests.conftest import skip_webgl_macos_ci
from tests.helpers.browser_helpers import dismiss_dialogs
from tests.helpers.wait import (
    screen_wait_for_element,
    screen_wait_for_element_hidden,
    screen_wait_for_element_visible,
    screen_wait_for_scene_ready,
)
from waldo_commander.services.control_lease import BROWSER, MCP, control_lease
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen


@pytest.mark.browser
@skip_webgl_macos_ci
def test_control_lease_indicator_and_take_control(screen: "Screen") -> None:
    control_lease.reset()
    try:
        screen.open("/")
        # Deterministic readiness gate: wait for the page to finish building
        # before touching the DOM (racing the control-panel build is what flaked
        # this on a slow runner). Mirrors the other screen tests.
        screen_wait_for_scene_ready(screen, timeout_s=40.0)
        # Control panel built (the AI-driving glow element exists in the DOM).
        assert screen_wait_for_element(screen, ".control-lease-glow", 10.0)
        # Clear the startup tutorial dialog so its backdrop doesn't swallow clicks.
        dismiss_dialogs(screen)

        # Default holder: the active browser tab is in control → glow hidden.
        assert screen_wait_for_element_hidden(screen, ".control-lease-glow", 5.0)
        assert control_lease.held_by(BROWSER, ui_state.active_client_id)

        # An MCP session seizes control. The 1 Hz active-tab loop (check_ping)
        # refreshes the indicator, so the glow + Take-control button appear ~1s.
        control_lease.seize(MCP, "screen-mcp", "MCP session screen-m")
        assert screen_wait_for_element_visible(screen, ".control-lease-glow", 5.0)
        assert screen_wait_for_element_visible(screen, ".btn-take-control", 5.0)

        # The glow must wrap the whole viewport, not the control panel: an
        # ancestor with backdrop-filter (the overlay-card) turns position:fixed
        # into panel-relative and shrinks the "screen edge" glow to the card.
        rect = screen.selenium.execute_script(
            "const r = document.querySelector('.control-lease-glow')"
            ".getBoundingClientRect();"
            "return [r.left, r.top, r.width, r.height,"
            " window.innerWidth, window.innerHeight];"
        )
        left, top, width, height, vw, vh = rect
        assert (left, top) == (0, 0) and width >= vw - 1 and height >= vh - 1, (
            f"glow does not cover the viewport: {rect}"
        )
        # While an AI session holds the lease the glow breathes.
        assert screen.selenium.execute_script(
            "return document.querySelector('.control-lease-glow')"
            ".classList.contains('control-glow-breathe');"
        ), "glow must carry the breathing animation while the AI drives"

        # Human presses Take control → browser reclaims, glow hides again.
        screen.selenium.find_element(By.CSS_SELECTOR, ".btn-take-control").click()
        assert screen_wait_for_element_hidden(screen, ".control-lease-glow", 5.0)
        assert control_lease.held_by(BROWSER, ui_state.active_client_id)
    finally:
        control_lease.reset()
