"""Token colours reach the screen as the design says they do."""

import asyncio
import re

import numpy as np
import pytest
from nicegui.elements.colors import Colors
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from waldo_commander.common.theme import hex_of


def _srgb(linear: float) -> float:
    """Three.js's output encoding: a linear channel as sRGB."""
    return (
        12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055
    )


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)
    ]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_following_error_gradient_renders_the_legend_colours():
    """three.js takes vertex colours as linear light and encodes them to sRGB
    on output, so the gradient's ends must come out as the legend's swatches."""
    from waldo_commander.services.urdf_scene.physics_overlay import (
        FULL_FOLLOWING_ERROR_RAD,
        following_error_colors,
    )

    on_track, diverged = following_error_colors(
        np.array([0.0, FULL_FOLLOWING_ERROR_RAD])
    )
    for vertex, token in (
        (on_track, "physics-on-track"),
        (diverged, "physics-diverged"),
    ):
        shown = "#" + "".join(f"{round(_srgb(c) * 255):02x}" for c in vertex)
        assert shown == hex_of(token), (token, shown, hex_of(token))


@pytest.mark.integration
async def test_notification_colours_keep_their_white_text_readable(user: User) -> None:
    """ui.notify writes Quasar's white text on the semantic colour it is
    given, so each one the app maps to a token must hold 4.5:1 against it."""
    await user.open("/")
    await wait_for_app_ready()
    colors = next(e for e in user.client.elements.values() if isinstance(e, Colors))
    for kind in ("positive", "negative", "warning", "info"):
        match = re.fullmatch(r"var\(--wc-([\w-]+)\)", colors.props[kind])
        assert match, (kind, colors.props[kind])
        fill = hex_of(match.group(1))
        assert _contrast(fill, "#ffffff") >= 4.5, (kind, match.group(1), fill)


@pytest.mark.integration
async def test_every_filled_button_names_a_readable_text_colour(user: User) -> None:
    """A filled button left on Quasar's default takes the bright action fill
    with white text at about 2:1; every filled button the panels build names
    its fill and text colour together, and the pair clears 3:1. Buttons
    styled by their own classes carry no fill."""
    from nicegui import ui

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    # A plugin panel builds when its tab first opens.
    for panel in ui_state.plugin_panels:
        user.find(marker=f"tab-{panel.id}").click()
        await asyncio.sleep(0)
    problems = []
    for element in list(user.client.elements.values()):
        if not isinstance(element, ui.button):
            continue
        props = element.props
        if "flat" in props or "outline" in props:
            continue
        label = props.get("label") or props.get("icon")
        fill, text = props.get("color"), props.get("text-color")
        if fill is None:
            continue  # coloured by its classes, not by a Quasar fill
        if text is None:
            problems.append(f"{label}: {fill} with no text colour")
            continue
        ratio = _contrast(
            hex_of(fill.removeprefix("wc-")), hex_of(text.removeprefix("wc-"))
        )
        if ratio < 3.0:
            problems.append(f"{label}: {fill} on {text} is {ratio:.2f}:1")
    assert not problems, "\n".join(problems)
