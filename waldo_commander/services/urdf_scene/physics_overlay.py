"""Pooled contact-force and centre-of-mass annotations from prediction records."""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation as ScipyRotation
from waldoctl import TickIndex

from waldo_commander import scene3d as s3d
from waldo_commander.common.theme import hex_of

logger = logging.getLogger(__name__)

#: Contact arrows are drawn at this many metres per newton.
FORCE_SCALE_M_PER_N = 0.004

#: The most contact arrows drawn at once. A grasp resolves a handful; a
#: pathological scene must not create hundreds of scene objects.
MAX_CONTACT_ARROWS = 12

#: Base size of a contact arrow \[m\] before the force scales it.
_ARROW_BASE_M = 0.01

#: Radius of the centre-of-mass drop line \[m\].
_DROP_RADIUS_M = 0.001

_CONE_AXIS = np.array([0.0, 1.0, 0.0])


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
    """The predicted record's overlays for one scene.

    Owned by ``UrdfScene``, in a group of its own so a rebuild never
    disturbs the path diff, and keyed on both records' digests so an
    identical pair is not redrawn.
    """

    def __init__(self, scene_owner: Any) -> None:
        self._owner = scene_owner
        self._group: Any = None
        self._com: Any = None
        self._com_drop: Any = None
        self._contacts: list[Any] = []
        self._digest: tuple[bytes, bytes] | None = None

    @property
    def is_built(self) -> bool:
        return self._group is not None

    def render(self, commanded: TickIndex | None, predicted: TickIndex | None) -> None:
        if (
            commanded is None
            or predicted is None
            or not predicted.rows
            or predicted.digest == commanded.digest
            or not any(name in predicted.channels for name in ("com", "contact_starts"))
        ):
            self.clear()
            return
        digest = (commanded.digest, predicted.digest)
        if predicted.digest and self._digest == digest:
            return
        self._build(commanded, predicted)

    def _build(self, commanded: TickIndex, predicted: TickIndex) -> None:
        scene = self._live_scene()
        if scene is None:
            return
        self.clear()
        com_hex = hex_of("physics-com")
        contact_hex = hex_of("physics-contact")
        try:
            with scene:
                with s3d.group().with_name("simulation:physics") as grp:
                    self._group = grp
                    self._com = s3d.sphere(0.012).material(com_hex, 0.9)
                    self._com.visible(False)
                    # A drop line to the ground: a lone sphere in a
                    # perspective view gives no depth to read its height
                    # against. A thin cylinder rather than a line because
                    # a line's endpoints are fixed at creation, and this
                    # has to follow the marker every frame.
                    self._com_drop = s3d.cylinder(
                        top_radius=_DROP_RADIUS_M,
                        bottom_radius=_DROP_RADIUS_M,
                        height=1.0,
                        radial_segments=6,
                    ).material(com_hex, 0.35)
                    self._com_drop.visible(False)
                    self._contacts = [
                        s3d.cylinder(
                            top_radius=0.0,
                            bottom_radius=_ARROW_BASE_M * 0.35,
                            height=_ARROW_BASE_M,
                            radial_segments=12,
                        ).material(contact_hex, 0.9)
                        for _ in range(MAX_CONTACT_ARROWS)
                    ]
                    for arrow in self._contacts:
                        arrow.visible(False)
        except Exception:
            logger.exception("Physics overlay build failed")
            self.clear()
            return
        self._digest = (commanded.digest, predicted.digest)

    def clear(self) -> None:
        group, self._group = self._group, None
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

        Called once per playback frame, and only ever moves, rotates or
        hides drawables it already made.
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
        # A non-finite row (a plant that diverged) is shown as nothing.
        have = com is not None and len(com) > row and bool(np.isfinite(com[row]).all())
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
        if not np.isfinite(starts[row : row + 2]).all():
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
        if not magnitude >= 1e-3 or not np.isfinite(magnitude):
            return False
        if not np.isfinite(at).all():
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
