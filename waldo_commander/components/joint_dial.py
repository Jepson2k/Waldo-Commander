"""A joint's travel drawn as a ring, with the angle on it."""

from __future__ import annotations

import dataclasses
import math

from nicegui import ui

#: Joint dial drawing box (viewBox side), ring radius and knob radius, in SVG units.
DIAL_SIZE = 64.0
DIAL_RADIUS = 26.0
DIAL_KNOB_RADIUS = 4.0
#: A joint has to move this far before its dial is redrawn.
DIAL_REDRAW_DEG = 0.5


@dataclasses.dataclass(frozen=True)
class DialGeometry:
    """SVG path data for one joint dial: ``track`` spans the limits, ``fill``
    runs from the joint's zero (clamped into the limits) to the angle, ``ticks``
    marks the limits and ``knob`` sits on the angle."""

    track: str
    fill: str
    ticks: str
    knob: tuple[float, float]


def _dial_point(deg: float, r: float) -> tuple[float, float]:
    """Point on the ring at ``deg``: zero at the top, clockwise positive."""
    rad = math.radians(deg)
    c = DIAL_SIZE / 2
    return c + r * math.sin(rad), c - r * math.cos(rad)


def _dial_arc(start_deg: float, end_deg: float, r: float) -> str:
    span = end_deg - start_deg
    if abs(span) >= 360.0:
        x0, y0 = _dial_point(start_deg, r)
        x1, y1 = _dial_point(start_deg + 180.0, r)
        return (
            f"M{x0:.2f} {y0:.2f} A{r:g} {r:g} 0 1 1 {x1:.2f} {y1:.2f}"
            f" A{r:g} {r:g} 0 1 1 {x0:.2f} {y0:.2f}"
        )
    if abs(span) < 1e-6:
        return ""
    x0, y0 = _dial_point(start_deg, r)
    x1, y1 = _dial_point(end_deg, r)
    large = 1 if abs(span) > 180.0 else 0
    sweep = 1 if span > 0 else 0
    return f"M{x0:.2f} {y0:.2f} A{r:g} {r:g} 0 {large} {sweep} {x1:.2f} {y1:.2f}"


def dial_geometry(lo: float, hi: float, angle: float, r: float) -> DialGeometry:
    """Geometry of a joint dial with limits ``lo..hi`` (degrees) at ``angle``.

    A span of a full turn or more draws the track as a whole circle and
    drops the limit ticks; a non-finite angle sits at the fill's origin."""
    origin = min(max(0.0, lo), hi)
    a = min(max(angle, lo), hi) if math.isfinite(angle) else origin
    if hi - lo >= 360.0:
        ticks = ""
    else:
        tick_parts = []
        for limit in (lo, hi):
            x0, y0 = _dial_point(limit, r - 5.0)
            x1, y1 = _dial_point(limit, r + 5.0)
            tick_parts.append(f"M{x0:.2f} {y0:.2f} L{x1:.2f} {y1:.2f}")
        ticks = " ".join(tick_parts)
    return DialGeometry(
        track=_dial_arc(lo, hi, r),
        fill=_dial_arc(origin, a, r),
        ticks=ticks,
        knob=_dial_point(a, r),
    )


class JointDial(ui.element, component="joint_dial.js"):
    """One joint's dial. A redraw is a method call on the dial rather than an
    element update: NiceGUI re-renders a changed element from the component
    that rendered it, which for a dial is the whole joint panel."""

    def __init__(self, lo: float, hi: float) -> None:
        super().__init__()
        self.lo = lo
        self.hi = hi
        self.last_deg = math.nan
        g = dial_geometry(lo, hi, math.nan, DIAL_RADIUS)
        self._props["size"] = DIAL_SIZE
        self._props["track"] = g.track
        self._props["ticks"] = g.ticks
        self._props["knob-radius"] = DIAL_KNOB_RADIUS
        self._set_angle_parts(g)

    def _set_angle_parts(self, g: DialGeometry) -> None:
        self._props["fill"] = g.fill
        self._props["knob"] = [round(g.knob[0], 2), round(g.knob[1], 2)]

    def show(self, angle: float) -> None:
        """Redraw for ``angle`` (degrees) once it has moved ``DIAL_REDRAW_DEG``."""
        if not math.isfinite(angle) or abs(angle - self.last_deg) < DIAL_REDRAW_DEG:
            return
        self.last_deg = angle
        # The props stay current for a remount without sending an update.
        with self._props.suspend_updates():
            self._set_angle_parts(dial_geometry(self.lo, self.hi, angle, DIAL_RADIUS))
        self.run_method("show", self._props["fill"], self._props["knob"])
