"""The jog step: follows the camera distance in bands until the user pins a value."""

from __future__ import annotations

import math
from collections.abc import Callable

from nicegui.binding import BindableProperty


def band_for(camera_distance_m: float | None) -> tuple[float, float]:
    """(joint step °, cartesian step mm) for a camera-to-target distance.

    Coarse when zoomed out, fine when zoomed in; the middle band until the
    scene has reported a distance."""
    if camera_distance_m is None or not math.isfinite(camera_distance_m):
        return 1.0, 5.0
    if camera_distance_m > 1.6:
        return 5.0, 10.0
    if camera_distance_m >= 1.0:
        return 1.0, 5.0
    if camera_distance_m >= 0.6:
        return 0.5, 1.0
    return 0.1, 0.5


class JogStep:
    """Bindable jog step. ``override`` pins one value for both units; ``None`` is auto."""

    camera_distance_m = BindableProperty(on_change=lambda step, _: step._recompute())
    override = BindableProperty(on_change=lambda step, _: step._recompute())
    joint_deg = BindableProperty()
    cart_mm = BindableProperty()
    is_auto = BindableProperty()

    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []
        self.camera_distance_m: float | None = None
        self.override: float | None = None
        self.joint_deg, self.cart_mm = band_for(None)
        self.is_auto = True

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` whenever the effective step or its auto state changes."""
        self._listeners.append(callback)

    def set_camera_distance(self, distance_m: float) -> None:
        if math.isfinite(distance_m) and distance_m >= 0.0:
            self.camera_distance_m = float(distance_m)

    def set_override(self, value: float | None) -> None:
        if value is not None and not (math.isfinite(value) and value > 0.0):
            raise ValueError(
                f"jog step must be a positive finite number, got {value!r}"
            )
        self.override = None if value is None else float(value)

    def _recompute(self) -> None:
        joint_deg, cart_mm = band_for(self.camera_distance_m)
        if self.override is not None:
            joint_deg = cart_mm = self.override
        is_auto = self.override is None
        if (joint_deg, cart_mm, is_auto) == (
            self.joint_deg,
            self.cart_mm,
            self.is_auto,
        ):
            return
        self.joint_deg = joint_deg
        self.cart_mm = cart_mm
        self.is_auto = is_auto
        for callback in list(self._listeners):
            callback()
