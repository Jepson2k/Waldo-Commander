"""What the simulated run measured, drawn over the scene.

MuJoCo computes; we render. Nothing here re-derives physics — every
number is a column the backend already produced, mapped onto drawables
the scene already knows how to make, so the arm, the paths and the
editable points keep being drawn the way they always were.

Two kinds of overlay, and picking the right one decides whether this is
fast or unusable:

- **Whole-run** geometry is built once per record and then left alone.
  The achieved path is one polyline over every row; rebuilding it per
  frame would be absurd.
- **Per-frame** annotations come from a small pool of drawables created
  once and thereafter only moved, rotated and hidden. They must never
  drop and recreate their group, which is what makes an overlay stutter.

The colour scale and the force scale are constants here rather than
guesses at the call site, because a picture whose magnitudes are unstated
lies: the legend beside the scene quotes both.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
from nicegui import ui
from scipy.spatial.transform import Rotation as ScipyRotation
from waldoctl import TickIndex

logger = logging.getLogger(__name__)

#: Tracking error at which the achieved path is drawn fully "diverged"
#: \[rad\]. Half a degree: below that the arm is doing what it was told,
#: above it something is worth looking at.
FULL_DIVERGENCE_RAD = 0.0087

#: Contact arrows are drawn at this many metres per newton.
FORCE_SCALE_M_PER_N = 0.004

#: The most contact arrows drawn at once. A grasp resolves a handful; a
#: pathological scene must not create hundreds of scene objects.
MAX_CONTACT_ARROWS = 12

#: Base size of a contact arrow \[m\] before the force scales it.
_ARROW_BASE_M = 0.01

_ON_TRACK = np.array([0.35, 0.85, 0.45])
_DIVERGED = np.array([0.95, 0.35, 0.25])
_CONE_AXIS = np.array([0.0, 1.0, 0.0])


def divergence_colors(error_rad: np.ndarray) -> list[list[float]]:
    """One RGB triple per row: green where the arm is on its command,
    red where it is not."""
    t = np.clip(error_rad / FULL_DIVERGENCE_RAD, 0.0, 1.0)[:, None]
    return (_ON_TRACK * (1 - t) + _DIVERGED * t).tolist()


def _cone_rpy(direction: np.ndarray) -> tuple[float, float, float]:
    """Euler angles taking a cone's own +Y axis onto *direction*."""
    cross = np.cross(_CONE_AXIS, direction)
    norm = float(np.linalg.norm(cross))
    if norm < 1e-9:
        return (0.0, 0.0, 0.0) if direction[1] > 0 else (math.pi, 0.0, 0.0)
    angle = math.acos(max(-1.0, min(1.0, float(np.dot(_CONE_AXIS, direction)))))
    rot = ScipyRotation.from_rotvec(angle * (cross / norm))
    rx, ry, rz = rot.as_euler("xyz")
    return float(rx), float(ry), float(rz)


class PhysicsOverlay:
    """The simulated run's overlays for one scene.

    Owned by ``UrdfScene``, in a group of its own so a rebuild never
    disturbs the path diff, and keyed on the record's digest so an
    identical run is not redrawn.
    """

    def __init__(self, scene_owner: Any) -> None:
        self._owner = scene_owner
        self._group: Any = None
        self._com: Any = None
        self._contacts: list[Any] = []
        self._digest: bytes | None = None

    @property
    def is_built(self) -> bool:
        return self._group is not None

    # ---- whole-run geometry -------------------------------------------------

    def render(self, ticks: TickIndex | None, *, show_divergence: bool) -> None:
        """(Re)build the whole-run geometry for *ticks*.

        A record with an unchanged digest paints an identical picture and
        is left alone; the backend's determinism contract is what makes
        that sound, and it is the flash guard.
        """
        if ticks is None or ticks.rows < 2:
            self.clear()
            return
        if self._digest is not None and ticks.digest and self._digest == ticks.digest:
            return
        self._build(ticks, show_divergence)

    def _build(self, ticks: TickIndex, show_divergence: bool) -> None:
        scene = self._live_scene()
        if scene is None:
            return
        self.clear()
        try:
            with scene:
                with ui.scene.group().with_name("simulation:physics") as grp:
                    self._group = grp
                    if show_divergence:
                        with grp:
                            # One polyline over the whole run: where the
                            # TCP actually went, coloured by how far that
                            # is from the command that produced it.
                            line = ui.scene.polyline(
                                [[float(v) for v in row[:3]] for row in ticks.tcp],
                                colors=divergence_colors(ticks.tracking_error_rad()),
                            )
                            # color=None tells three.js to use the
                            # per-vertex colours.
                            line.material(None, 0.95)
        except Exception:
            logger.exception("Physics overlay build failed")
            self.clear()
            return
        self._digest = ticks.digest

    def clear(self) -> None:
        group, self._group = self._group, None
        self._com = None
        self._contacts = []
        self._digest = None
        if group is not None:
            try:
                group.delete()
            except Exception as e:
                logger.debug("physics overlay group delete: %s", e)

    # ---- per-frame annotations ---------------------------------------------

    def update_frame(
        self,
        ticks: TickIndex | None,
        row: int,
        *,
        show_contacts: bool,
        show_com: bool,
    ) -> None:
        """Move this frame's annotations to *row*.

        Called from inside the scene's existing per-frame batch, and only
        ever moves, rotates or hides drawables it already made.
        """
        if ticks is None or self._group is None or ticks.rows == 0:
            return
        row = max(0, min(row, ticks.rows - 1))
        self._update_com(ticks, row, show_com)
        self._update_contacts(ticks, row, show_contacts)

    def _update_com(self, ticks: TickIndex, row: int, show: bool) -> None:
        com = ticks.channels.get("com")
        if com is None or len(com) <= row:
            return
        if self._com is None:
            if not show:
                return
            with self._group:
                self._com = ui.scene.sphere(0.012).material("#ffd166", 0.9)
        self._com.visible(show)
        if show:
            x, y, z = (float(v) for v in com[row])
            self._com.move(x, y, z)

    def _update_contacts(self, ticks: TickIndex, row: int, show: bool) -> None:
        starts = ticks.channels.get("contact_starts")
        pos = ticks.channels.get("contact_pos")
        force = ticks.channels.get("contact_force")
        if starts is None or pos is None or force is None or len(starts) <= row + 1:
            return
        lo, hi = int(starts[row]), int(starts[row + 1])
        count = min(hi - lo, MAX_CONTACT_ARROWS) if show else 0
        while len(self._contacts) < count:
            with self._group:
                self._contacts.append(
                    ui.scene.cylinder(
                        top_radius=0.0,
                        bottom_radius=_ARROW_BASE_M * 0.35,
                        height=_ARROW_BASE_M,
                        radial_segments=12,
                    ).material("#ff5d5d", 0.9)
                )
        for i, arrow in enumerate(self._contacts):
            if i >= count:
                arrow.visible(False)
                continue
            arrow.visible(self._place_arrow(arrow, pos[lo + i], force[lo + i]))

    @staticmethod
    def _place_arrow(arrow: Any, at: np.ndarray, force: np.ndarray) -> bool:
        """Point one cone along *force* at *at*, scaled by its magnitude.

        Returns whether it is worth showing: a force below the noise
        floor would draw as a degenerate stub that reads as a real
        contact.
        """
        magnitude = float(np.linalg.norm(force))
        if magnitude < 1e-3:
            return False
        direction = np.asarray(force, dtype=np.float64) / magnitude
        stretch = max(0.4, magnitude * FORCE_SCALE_M_PER_N / _ARROW_BASE_M)
        arrow.scale(1.0, stretch, 1.0)
        arrow.move(*(float(v) for v in at))
        arrow.rotate(*_cone_rpy(direction))
        return True

    def _live_scene(self) -> Any | None:
        scene = getattr(self._owner, "scene", None)
        if scene is None or getattr(scene, "is_deleted", False):
            return None
        return scene
