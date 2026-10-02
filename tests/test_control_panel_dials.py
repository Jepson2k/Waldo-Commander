"""Control panel: joint dial geometry."""

from __future__ import annotations

import math
import re

import pytest

from waldo_commander.components.joint_dial import (
    DIAL_RADIUS,
    DIAL_SIZE,
    dial_angle,
    dial_static,
)

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
