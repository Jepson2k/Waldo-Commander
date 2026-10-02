"""How far the 3D view's rings and gizmo step, from how far the camera is from its orbit target."""

from __future__ import annotations

import math
from collections.abc import Callable

#: (camera distance above which the band applies [m], joint step [°], cartesian step [mm]),
#: coarse to fine. The default view, 0.66 m from its target, lands in the second band.
SNAP_BANDS: tuple[tuple[float, float, float], ...] = (
    (1.2, 5.0, 10.0),
    (0.5, 1.0, 5.0),
    (0.3, 0.5, 1.0),
    (0.0, 0.1, 0.5),
)


def snap_for(distance_m: float | None) -> tuple[float, float]:
    """(joint step °, cartesian step mm) for a camera-to-target distance.

    A band's lower bound belongs to it, except the first band, which starts
    above 1.2 m. Before any report the second band applies.
    """
    if distance_m is None or not math.isfinite(distance_m) or distance_m < 0.0:
        return SNAP_BANDS[1][1], SNAP_BANDS[1][2]
    if distance_m > SNAP_BANDS[0][0]:
        return SNAP_BANDS[0][1], SNAP_BANDS[0][2]
    for lower, joint_deg, cart_mm in SNAP_BANDS[1:-1]:
        if distance_m >= lower:
            return joint_deg, cart_mm
    return SNAP_BANDS[-1][1], SNAP_BANDS[-1][2]


class SceneSnap:
    """The snap step the handles read; listeners hear about band changes only."""

    def __init__(self) -> None:
        self.joint_deg, self.cart_mm = snap_for(None)
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def set_distance(self, distance_m: float) -> None:
        """Take a camera distance report; a non-finite or negative one is ignored."""
        if not math.isfinite(distance_m) or distance_m < 0.0:
            return
        joint_deg, cart_mm = snap_for(distance_m)
        if joint_deg == self.joint_deg and cart_mm == self.cart_mm:
            return
        self.joint_deg, self.cart_mm = joint_deg, cart_mm
        for callback in list(self._listeners):
            callback()
