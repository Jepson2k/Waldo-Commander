"""The camera frames the part of the scene the column and footer leave clear."""

import pytest
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import click_tab, close_panel, dismiss_dialogs, js
from tests.helpers.wait import screen_wait_for_scene_ready

VIEW = """
    const cam = window.SceneFraming && SceneFraming.camera();
    if (!cam) return null;
    const el = document.querySelector('.nicegui-scene');
    const wrap = document.querySelector('.panels-wrap');
    const footer = document.querySelector('.status-footer').getBoundingClientRect();
    return {enabled: !!(cam.view && cam.view.enabled),
            fullWidth: cam.view ? cam.view.fullWidth : null,
            fullHeight: cam.view ? cam.view.fullHeight : null,
            offsetY: cam.view ? cam.view.offsetY : null,
            aspect: cam.aspect,
            width: el.clientWidth, height: el.clientHeight,
            columnRight: parseFloat(wrap.style.getPropertyValue('--wc-column-right')) || 0,
            footerCover: Math.round(innerHeight - footer.top)};
"""


def _view(screen) -> dict:
    view = js(screen, VIEW)
    assert view is not None, "scene-framing.js is not attached to the scene"
    return view


def _wait_view(screen, predicate, timeout: float = 10.0) -> dict:
    return WebDriverWait(screen.selenium, timeout).until(
        lambda _: (v := _view(screen)) and predicate(v) and v
    )


@pytest.mark.browser
def test_view_offset_follows_the_column_and_footer(screen):
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40)
    dismiss_dialogs(screen)

    # Nothing but the footer covers the view: offset up by its height, no shift sideways.
    closed = _wait_view(screen, lambda v: v["enabled"])
    assert closed["fullWidth"] == closed["width"], closed
    assert closed["offsetY"] == closed["footerCover"] > 0, closed
    assert closed["fullHeight"] == closed["height"] + closed["footerCover"], closed

    click_tab(screen, "program")
    opened = _wait_view(
        screen, lambda v: v["columnRight"] > 0 and v["fullWidth"] > v["width"]
    )
    assert opened["fullWidth"] == opened["width"] + opened["columnRight"], opened
    assert opened["offsetY"] == opened["footerCover"], opened
    assert abs(opened["aspect"] - opened["fullWidth"] / opened["fullHeight"]) < 1e-6, (
        opened
    )

    # A window resize lets the scene reset its aspect; the offset comes back.
    screen.selenium.set_window_size(1200, 700)
    resized = _wait_view(
        screen,
        lambda v: v["width"] != opened["width"]
        and v["enabled"]
        and v["fullWidth"] == v["width"] + v["columnRight"],
    )
    assert (
        abs(resized["aspect"] - resized["fullWidth"] / resized["fullHeight"]) < 1e-6
    ), resized

    close_panel(screen, "program-panel")
    reclosed = _wait_view(
        screen, lambda v: v["columnRight"] == 0 and v["fullWidth"] == v["width"]
    )
    assert reclosed["offsetY"] == reclosed["footerCover"], reclosed
