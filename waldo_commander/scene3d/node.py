"""The scene's objects as the app keeps them.

Each ``Node`` mirrors one three.js object: what it is, where it sits and how
it looks. A change marks the node dirty and the scene sends what changed once
per event-loop pass; setting a value the node already has sends nothing.
``with node:`` makes the node the parent of objects created inside the block.
"""

from __future__ import annotations

import logging
import math
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Self

from .protocol import euler_matrix, fixed, fixed_all, quaternion, significant

if TYPE_CHECKING:
    from .element import WcScene

logger = logging.getLogger(__name__)

current_parent: ContextVar[Node | None] = ContextVar("wc_scene_parent", default=None)

POSITION = 1
ROTATION = 2
SCALE = 4
MATERIAL = 8
VISIBLE = 16
NAME = 32
GLOW = 64
CLIPPING = 128

_IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class ClipPlane(NamedTuple):
    """A clipping plane ``nx*x + ny*y + nz*z + d = 0`` in world coordinates.

    Geometry on the negative side is hidden.
    """

    nx: float
    ny: float
    nz: float
    d: float = 0.0


def _finite(*values: float) -> None:
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"non-finite scene value in {values}")


class Node:
    """One object in the scene."""

    __slots__ = (
        "scene",
        "id",
        "kind",
        "args",
        "wireframe",
        "name",
        "parent",
        "children",
        "x",
        "y",
        "z",
        "R",
        "sx",
        "sy",
        "sz",
        "color",
        "opacity",
        "side_",
        "material_is_set",
        "visible_",
        "glow",
        "clipping_planes",
        "q",
        "deleted",
        "_token",
    )

    def __init__(
        self,
        scene: WcScene,
        kind: str,
        args: list[Any],
        parent: Node | None,
        *,
        wireframe: bool = False,
    ) -> None:
        self.scene = scene
        self.id = scene._new_id()
        self.kind = kind
        self.args = args
        self.wireframe = wireframe
        self.name: str | None = None
        self.parent = parent
        self.children: list[Node] = []
        self.x = self.y = self.z = 0.0
        self.R: list[list[float]] = [list(row) for row in _IDENTITY]
        self.sx = self.sy = self.sz = 1.0
        self.color: str | None = None
        self.opacity = 1.0
        self.side_ = "front"
        self.material_is_set = False
        self.visible_ = True
        self.glow = False
        self.clipping_planes: list[ClipPlane] = []
        self.q = 0.0
        self.deleted = False
        self._token: list[Any] = []
        if parent is not None:
            scene._created_node(self)
            parent.children.append(self)

    def __repr__(self) -> str:
        return f"<Node {self.kind} {self.id} {self.name or ''}>".replace(" >", ">")

    def __enter__(self) -> Self:
        self._token.append(current_parent.set(self))
        return self

    def __exit__(self, *_: object) -> None:
        current_parent.reset(self._token.pop())

    def _changed(self, bits: int) -> None:
        if self.deleted:
            logger.debug("%r changed after it was deleted", self)
            return
        self.scene._changed(self, bits)

    def with_name(self, name: str) -> Self:
        if name != self.name:
            self._changed(NAME)
            self.name = name
        return self

    def move(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Self:
        _finite(x, y, z)
        if (x, y, z) != (self.x, self.y, self.z):
            self._changed(POSITION)
            self.x, self.y, self.z = float(x), float(y), float(z)
        return self

    def rotate(self, r_x: float, r_y: float, r_z: float) -> Self:
        """Turn about the parent's fixed x, then y, then z axis (radians)."""
        return self.rotate_R(euler_matrix(r_x, r_y, r_z))

    def rotate_R(self, R: list[list[float]]) -> Self:
        rows = [[float(v) for v in row] for row in R]
        _finite(*(v for row in rows for v in row))
        if rows != self.R:
            self._changed(ROTATION)
            self.R = rows
        return self

    def scale(
        self, sx: float = 1.0, sy: float | None = None, sz: float | None = None
    ) -> Self:
        sy = sx if sy is None else sy
        sz = sx if sz is None else sz
        _finite(sx, sy, sz)
        if (sx, sy, sz) != (self.sx, self.sy, self.sz):
            self._changed(SCALE)
            self.sx, self.sy, self.sz = float(sx), float(sy), float(sz)
        return self

    def material(
        self,
        color: str | None,
        opacity: float = 1.0,
        side: Literal["front", "back", "both"] = "front",
    ) -> Self:
        """Colour, opacity and drawn side; a ``None`` colour shows the vertex colours."""
        _finite(opacity)
        if (
            not self.material_is_set
            or color != self.color
            or opacity != self.opacity
            or side != self.side_
        ):
            self._changed(MATERIAL)
            self.color, self.opacity, self.side_ = color, float(opacity), side
            self.material_is_set = True
        return self

    def visible(self, value: bool = True) -> Self:
        if value != self.visible_:
            self._changed(VISIBLE)
            self.visible_ = bool(value)
        return self

    def hover_effect(self, effect: Literal["glow"]) -> Self:
        """Glow while the pointer is over this object."""
        if effect != "glow":
            raise ValueError(f"unknown hover effect {effect!r}")
        if not self.glow:
            self._changed(GLOW)
            self.glow = True
        return self

    def set_clipping_planes(self, planes: list[ClipPlane]) -> Self:
        """Hide this object's geometry on the negative side of any of *planes*."""
        rounded = [ClipPlane(*(round(float(v), 4) for v in p)) for p in planes]
        _finite(*(v for p in rounded for v in p))
        if rounded != self.clipping_planes:
            self._changed(CLIPPING)
            self.clipping_planes = rounded
        return self

    def clear_clipping_planes(self) -> Self:
        return self.set_clipping_planes([])

    def delete(self) -> None:
        """Remove this object and everything under it."""
        if self.deleted:
            return
        self.scene._deleted_node(self)

    def _forget(self) -> None:
        """Drop this subtree from the scene's index, deepest first."""
        for child in self.children:
            child._forget()
        self.deleted = True
        self.scene.objects.pop(self.id, None)

    # -- wire ---------------------------------------------------------------

    def _state(self, bits: int) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if bits & NAME and self.name is not None:
            state["n"] = self.name
        if bits & POSITION:
            state["p"] = fixed_all((self.x, self.y, self.z))
        if bits & ROTATION:
            state["r"] = quaternion(self.R)
        if bits & SCALE:
            state["s"] = [
                significant(self.sx),
                significant(self.sy),
                significant(self.sz),
            ]
        if bits & MATERIAL and self.material_is_set:
            state["m"] = [self.color, round(self.opacity, 3), self.side_]
        if bits & VISIBLE:
            state["v"] = int(self.visible_)
        if bits & GLOW:
            state["g"] = int(self.glow)
        if bits & CLIPPING:
            state["l"] = [list(p) for p in self.clipping_planes]
        return state

    def _create_op(self) -> list[Any]:
        bits = NAME | MATERIAL | CLIPPING
        if (self.x, self.y, self.z) != (0.0, 0.0, 0.0):
            bits |= POSITION
        if self.R != [list(row) for row in _IDENTITY]:
            bits |= ROTATION
        if (self.sx, self.sy, self.sz) != (1.0, 1.0, 1.0):
            bits |= SCALE
        if not self.visible_:
            bits |= VISIBLE
        if self.glow:
            bits |= GLOW
        state = self._state(bits)
        if not self.clipping_planes:
            state.pop("l", None)
        if self.wireframe:
            state["w"] = 1
        if self.kind == "joint":
            state["q"] = fixed(self.q)
        parent = self.parent.id if self.parent is not None else 0
        return ["c", self.id, parent, self.kind, self.args, state]

    def _update_op(self, bits: int) -> list[Any]:
        return ["u", self.id, self._state(bits)]
