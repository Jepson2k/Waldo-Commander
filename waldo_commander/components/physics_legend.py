"""The key to the predicted overlays.

A picture whose magnitudes are unstated lies. The predicted path is
coloured by following error and contact arrows are scaled by force, and
neither number is guessable from the scene — a millimetre of sag drawn
at its true size is invisible, and an arrow whose length means nothing
in particular reads as though it did. So the scale is written down.

Shown only while a predicted record that differs from the commanded one
is on screen, row by row for what that record carries.
"""

from __future__ import annotations

import math

import waldoctl
from nicegui import ui

from waldo_commander.components.playback import layers_available
from waldo_commander.services.urdf_scene.physics_overlay import (
    FORCE_SCALE_M_PER_N,
    FULL_FOLLOWING_ERROR_RAD,
)
from waldo_commander.state import simulation_state

_TRACKING_DEG = math.degrees(FULL_FOLLOWING_ERROR_RAD)
_ARROW_CM_PER_N = FORCE_SCALE_M_PER_N * 100.0


class PhysicsLegend:
    """A small key in the corner of the scene, hidden until it applies."""

    def __init__(self) -> None:
        self._root: ui.element | None = None
        self._rows: list[tuple[ui.element, str]] = []

    def build(self) -> None:
        """Draw the legend into the current container."""
        with (
            ui.column()
            # Bottom-left, clear of the icon rail: the right half of the
            # scene belongs to the control panel, and a key painted
            # underneath it is worse than no key at all.
            .classes(
                "absolute bottom-24 left-24 z-30 rounded-lg px-3 py-2 gap-2 bg-neutral-800 text-neutral-100 border border-neutral-600"
            )
            .style("pointer-events: none;") as root
        ):
            self._root = root
            self._rows = [
                (
                    self._swatch_row(
                        "Predicted path",
                        f"green on its command, red at {_TRACKING_DEG:.1f}° of error",
                        ("#59d973", "#f25940"),
                    ),
                    "predicted_visible",
                ),
                (
                    self._swatch_row(
                        "Contact force",
                        f"arrow length {_ARROW_CM_PER_N:.1f} cm per newton",
                        ("#ff5d5d", "#ff5d5d"),
                    ),
                    "contacts_visible",
                ),
                (
                    self._swatch_row(
                        "Centre of mass",
                        "of the whole simulated scene",
                        ("#ffd166", "#ffd166"),
                    ),
                    "com_visible",
                ),
            ]
        root.mark("physics-legend")
        simulation_state.add_change_listener(self.refresh)
        self.refresh()

    @staticmethod
    def _swatch_row(title: str, detail: str, colors: tuple[str, str]) -> ui.element:
        with ui.row().classes("items-center gap-2 no-wrap") as row:
            ui.element("div").style(
                f"width: 18px; height: 8px; border-radius: 2px;"
                f" background: linear-gradient(90deg, {colors[0]}, {colors[1]});"
            )
            with ui.column().classes("gap-0"):
                ui.label(title).classes("text-xs font-medium leading-tight")
                ui.label(detail).classes("text-xs text-neutral-300 leading-tight")
        return row

    def refresh(self) -> None:
        """Show the rows whose overlay is both enabled and has data."""
        if self._root is None:
            return
        active = waldoctl.commander.programs.active
        available = layers_available(active.dry_run if active is not None else None)
        view = waldoctl.commander.settings.view
        shown = 0
        for row, flag in self._rows:
            on = available.get(flag, False) and getattr(view, flag)
            row.set_visibility(on)
            shown += int(on)
        self._root.set_visibility(shown > 0)


physics_legend = PhysicsLegend()
