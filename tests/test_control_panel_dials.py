"""Control panel: joint dials and level chips."""

from __future__ import annotations

import asyncio
import math
import re
from typing import TYPE_CHECKING

import pytest
import waldoctl
from nicegui import core
from selenium.webdriver.support.ui import WebDriverWait

from tests.helpers.browser_helpers import js, marked_element, run_in_app
from tests.helpers.wait import (
    JOG_SAFE_POSE_DEG,
    screen_wait_for_scene_ready,
    wait_until,
)
from waldo_commander.components.joint_dial import (
    DIAL_RADIUS,
    DIAL_SIZE,
    dial_angle,
    dial_static,
)
from waldo_commander.state import ui_state

if TYPE_CHECKING:
    from nicegui.testing.screen import Screen

R = DIAL_RADIUS


def _point(deg: float, r: float = R) -> tuple[float, float]:
    c = DIAL_SIZE / 2
    return c + r * math.sin(math.radians(deg)), c - r * math.cos(math.radians(deg))


def _numbers(path: str) -> list[float]:
    return [float(t) for t in re.findall(r"-?\d+(?:\.\d+)?", path)]


def _arc(path: str) -> dict:
    """The one arc of ``M x0 y0 A r r 0 large sweep x1 y1``."""
    n = _numbers(path)
    assert len(n) == 9, f"not a single arc: {path!r}"
    return {"start": (n[0], n[1]), "large": n[5], "sweep": n[6], "end": (n[7], n[8])}


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 0.02) -> bool:
    return math.dist(a, b) <= tol


@pytest.mark.unit
def test_dial_geometry_draws_travel_from_zero_clamped_into_the_limits() -> None:
    # J3's range lies entirely above zero: the fill starts at the low limit and
    # runs clockwise to the angle, where the knob sits.
    j3_track, j3_ticks = dial_static(107.866, 287.8675, R)
    j3_fill, j3_knob = dial_angle(107.866, 287.8675, 180.0, R)
    fill = _arc(j3_fill)
    assert _close(fill["start"], _point(107.866)) and _close(fill["end"], _point(180.0))
    assert fill["sweep"] == 1 and fill["large"] == 0
    assert _close(j3_knob, _point(180.0))
    track = _arc(j3_track)
    assert _close(track["start"], _point(107.866))
    assert _close(track["end"], _point(287.8675))
    assert track["large"] == 1, "180.0015° of travel is just over half a turn"
    assert len(_numbers(j3_ticks)) == 8, "a tick segment at each limit"

    # J2's range lies below zero: the fill starts at the high limit and runs
    # counter-clockwise to the angle.
    j2_fill, j2_knob = dial_angle(-145.0088, -3.375, -90.0, R)
    fill = _arc(j2_fill)
    assert _close(fill["start"], _point(-3.375)) and _close(fill["end"], _point(-90.0))
    assert fill["sweep"] == 0
    assert _close(j2_knob, _point(-90.0))

    # J6 covers a whole turn: the track is a full circle drawn as two arcs,
    # without limit ticks, and a full-turn fill closes on itself.
    j6_track, j6_ticks = dial_static(0.0, 360.0, R)
    assert j6_track.count("A") == 2 and _numbers(j6_track)[:2] == list(_point(0.0))
    assert j6_ticks == ""
    j6_fill, j6_knob = dial_angle(0.0, 360.0, 90.0, R)
    quarter = _arc(j6_fill)
    assert _close(quarter["end"], _point(90.0)) and _close(j6_knob, _point(90.0))
    assert dial_angle(0.0, 360.0, 360.0, R)[0] == j6_track

    # J1 straddles zero: at zero there is nothing to fill and the knob is at the
    # top; an angle past a limit is drawn at the limit; an unknown angle sits at
    # the fill's origin.
    j1_fill, j1_knob = dial_angle(-123.05, 123.05, 0.0, R)
    assert j1_fill == "" and _close(j1_knob, _point(0.0))
    assert _close(dial_angle(-123.05, 123.05, 200.0, R)[1], _point(123.05))
    unknown_fill, unknown_knob = dial_angle(-123.05, 123.05, math.nan, R)
    assert unknown_fill == "" and _close(unknown_knob, _point(0.0))


@pytest.mark.browser
def test_idle_dials_hide_their_caps_even_at_a_limit(screen: Screen) -> None:
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40.0)
    lo, _hi = run_in_app(lambda: ui_state.control_panel._get_joint_limits(2))
    pose = list(JOG_SAFE_POSE_DEG)
    pose[2] = lo

    async def park_elbow_at_its_lower_limit() -> None:
        await waldoctl.commander.client.teleport(pose)

    asyncio.run_coroutine_threadsafe(park_elbow_at_its_lower_limit(), core.loop).result(
        15
    )
    screen.selenium.execute_cdp_cmd(
        "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5}
    )
    caps = WebDriverWait(screen.selenium, 10).until(
        lambda _: (
            r := js(
                screen,
                """
                const caps = [...document.querySelectorAll('.joint-cap')];
                const shown = c => getComputedStyle(c).visibility !== 'hidden'
                    && getComputedStyle(c).opacity !== '0';
                return {disabled: caps.filter(c => c.classList.contains('disabled')).length,
                        shown: caps.filter(shown).length};
                """,
            )
        )
        and r["disabled"] > 0
        and r
    )
    assert caps["shown"] == 0, f"an idle dial shows its caps: {caps}"


@pytest.mark.browser
def test_a_dial_shows_a_move_made_while_the_cartesian_tab_was_open(
    screen: Screen,
) -> None:
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40.0)
    lo, hi = run_in_app(lambda: ui_state.control_panel._get_joint_limits(0))
    pose = list(JOG_SAFE_POSE_DEG)

    def teleport_j1(deg: float) -> None:
        pose[0] = deg

        async def teleport() -> None:
            await waldoctl.commander.client.teleport(pose)
            assert await wait_until(
                lambda: abs(waldoctl.commander.status.joints.angles.deg[0] - deg) < 0.5,
                timeout_s=10.0,
            ), f"J1 never reached {deg}°"

        asyncio.run_coroutine_threadsafe(teleport(), core.loop).result(15)

    def j1_knob_at(deg: float) -> bool:
        knob = js(
            screen,
            """
            const k = document.querySelector('.joint-dial-cell .dial-knob');
            return k && [+k.getAttribute('cx'), +k.getAttribute('cy')];
            """,
        )
        return bool(knob) and math.dist(knob, dial_angle(lo, hi, deg, R)[1]) < 0.5

    teleport_j1(85.0)
    WebDriverWait(screen.selenium, 10).until(lambda _: j1_knob_at(85.0))

    marked_element(screen, "tab-cartesian").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda _: not js(screen, "return !!document.querySelector('.joint-dial-cell')")
    )
    teleport_j1(40.0)
    marked_element(screen, "tab-joint").click()
    WebDriverWait(screen.selenium, 10).until(
        lambda _: j1_knob_at(40.0),
        message="J1 moved to 40° while the Cartesian tab was open, "
        "but its dial does not show it on the Joint tab",
    )


@pytest.mark.browser
def test_the_joint_tab_is_as_tall_as_its_dials(screen: Screen) -> None:
    screen.open("/")
    screen_wait_for_scene_ready(screen, timeout_s=40.0)
    sizes = WebDriverWait(screen.selenium, 10).until(
        lambda _: js(
            screen,
            """
            const panels = document.querySelector('.cp-jog-panels').getBoundingClientRect();
            const cells = [...document.querySelectorAll('.joint-dial-cell')].map(c => c.getBoundingClientRect());
            if (!cells.length) return null;
            return {panelsHeight: panels.height,
                    dialsHeight: Math.max(...cells.map(c => c.bottom)) - panels.top};
            """,
        )
    )
    assert sizes["panelsHeight"] <= sizes["dialsHeight"] + 8, sizes
