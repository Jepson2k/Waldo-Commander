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

#: The most vertices the achieved path is drawn with. A ten-minute run
#: records 30,000 rows, and every one would cross as a point triple AND a
#: colour triple in a single scene command built on the event loop. The
#: line is a few hundred pixels long; more vertices than this buy nothing.
MAX_ACHIEVED_POINTS = 2000

#: Base size of a contact arrow \[m\] before the force scales it.
_ARROW_BASE_M = 0.01

#: Radius of the centre-of-mass drop line \[m\].
_DROP_RADIUS_M = 0.001

_ON_TRACK = np.array([0.35, 0.85, 0.45])
_DIVERGED = np.array([0.95, 0.35, 0.25])
_CONE_AXIS = np.array([0.0, 1.0, 0.0])


def divergence_colors(error_rad: np.ndarray) -> list[list[float]]:
    """One RGB triple per row: green where the arm is on its command,
    red where it is not."""
    t = np.clip(error_rad / FULL_DIVERGENCE_RAD, 0.0, 1.0)[:, None]
    return (_ON_TRACK * (1 - t) + _DIVERGED * t).tolist()


def decimate(
    tcp: np.ndarray, error_rad: np.ndarray, budget: int = MAX_ACHIEVED_POINTS
) -> tuple[np.ndarray, np.ndarray]:
    """Thin a run to `budget` vertices, keeping both endpoints.

    Positions are sampled but the error is taken as the **maximum** over
    each collapsed span: a divergence spike lasting three rows is exactly
    what the overlay exists to show, and sampling the error would drop it.
    Both columns must be thinned together — the scene requires one colour
    per point.
    """
    rows = len(tcp)
    if rows <= budget:
        return tcp, error_rad
    edges = np.linspace(0, rows, budget, dtype=int)
    edges[-1] = rows
    starts = edges[:-1]
    worst = np.array([error_rad[a:b].max() for a, b in zip(starts, edges[1:]) if b > a])
    return tcp[starts[: len(worst)]], worst


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
        self._achieved: Any = None
        self._com: Any = None
        self._com_drop: Any = None
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
            # Same record, so the geometry stands; only the toggle can
            # have moved, and it is a visibility flip rather than a
            # rebuild. Reading it here is what makes the setting work at
            # all — it used to be consulted only when a record changed.
            if self._achieved is not None:
                self._achieved.visible(show_divergence)
            return
        self._build(ticks, show_divergence)

    def _build(self, ticks: TickIndex, show_divergence: bool) -> None:
        """Build the group, the path, and the per-frame pool.

        The pool is made here rather than on first use because a scene
        object can only be constructed inside its scene's context, and
        per-frame code runs from the playback batch, which is not one.
        Making them once and only ever moving them afterwards is also
        what keeps a frame cheap.
        """
        scene = self._live_scene()
        if scene is None:
            return
        self.clear()
        try:
            with scene:
                with ui.scene.group().with_name("simulation:physics") as grp:
                    self._group = grp
                    # One polyline over the whole run: where the TCP
                    # actually went, coloured by how far that is from the
                    # command that produced it. Built once and shown or
                    # hidden — a toggle must not need a rebuild.
                    points, error = decimate(ticks.tcp, ticks.tracking_error_rad())
                    self._achieved = ui.scene.polyline(
                        [[float(v) for v in row[:3]] for row in points],
                        colors=divergence_colors(error),
                    )
                    # color=None tells three.js to use the per-vertex colours.
                    self._achieved.material(None, 0.95)
                    self._achieved.visible(show_divergence)
                    self._com = ui.scene.sphere(0.012).material("#ffd166", 0.9)
                    self._com.visible(False)
                    # A drop line to the ground: a lone sphere in a
                    # perspective view gives no depth to read its height
                    # against. A thin cylinder rather than a line because
                    # a line's endpoints are fixed at creation, and this
                    # has to follow the marker every frame.
                    self._com_drop = ui.scene.cylinder(
                        top_radius=_DROP_RADIUS_M,
                        bottom_radius=_DROP_RADIUS_M,
                        height=1.0,
                        radial_segments=6,
                    ).material("#ffd166", 0.35)
                    self._com_drop.visible(False)
                    self._contacts = [
                        ui.scene.cylinder(
                            top_radius=0.0,
                            bottom_radius=_ARROW_BASE_M * 0.35,
                            height=_ARROW_BASE_M,
                            radial_segments=12,
                        ).material("#ff5d5d", 0.9)
                        for _ in range(MAX_CONTACT_ARROWS)
                    ]
                    for arrow in self._contacts:
                        arrow.visible(False)
        except Exception:
            logger.exception("Physics overlay build failed")
            self.clear()
            return
        self._digest = ticks.digest

    def clear(self) -> None:
        group, self._group = self._group, None
        self._achieved = None
        self._com = None
        self._com_drop = None
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
        if not (show_contacts or show_com):
            # Nothing asked for: hide the pool rather than leave the last
            # frame's arrows floating in the scene.
            for obj in (self._com, self._com_drop):
                if obj is not None:
                    obj.visible(False)
            for arrow in self._contacts:
                arrow.visible(False)
            return
        row = max(0, min(row, ticks.rows - 1))
        self._update_com(ticks, row, show_com)
        self._update_contacts(ticks, row, show_contacts)

    def _update_com(self, ticks: TickIndex, row: int, show: bool) -> None:
        com = ticks.channels.get("com")
        if self._com is None:
            return
        have = com is not None and len(com) > row
        self._com.visible(show and have)
        if self._com_drop is not None:
            self._com_drop.visible(show and have)
        if show and have:
            x, y, z = (float(v) for v in com[row])
            self._com.move(x, y, z)
            if self._com_drop is not None:
                # A unit cylinder stands along +Y; a quarter turn about X
                # stands it up along world +Z, then it is stretched to
                # reach the ground and centred on the half-way point.
                self._com_drop.scale(1.0, max(abs(z), 1e-4), 1.0)
                self._com_drop.rotate(math.pi / 2, 0.0, 0.0)
                self._com_drop.move(x, y, z / 2)

    def _update_contacts(self, ticks: TickIndex, row: int, show: bool) -> None:
        starts = ticks.channels.get("contact_starts")
        pos = ticks.channels.get("contact_pos")
        force = ticks.channels.get("contact_force")
        if starts is None or pos is None or force is None or len(starts) <= row + 1:
            return
        lo, hi = int(starts[row]), int(starts[row + 1])
        count = min(hi - lo, len(self._contacts)) if show else 0
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
