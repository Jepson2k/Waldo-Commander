"""What the 3D view's browser half sends the app, for ``user``-fixture tests.

A gesture's events carry the interaction epoch the browser last heard, its
drag id and a sequence number, as ``scene3d/js/gestures.js`` numbers them. A
``Drag`` keeps the epoch it began under, the way events already in flight do
when the app starts a new one.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from typing import Any

from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

_drags = itertools.count(1)


class Drag:
    """One gesture of ``kind`` on ``scene``, numbered as the browser numbers it."""

    def __init__(
        self, user: User, scene: Any, kind: str, epoch: int | None = None
    ) -> None:
        self._target = UserInteraction(user, {scene}, None)
        self.kind = kind
        self.drag = next(_drags)
        self.seq = 0
        self.epoch = scene.gestures.epoch if epoch is None else epoch

    def send(self, t: str, **fields: Any) -> Drag:
        self.seq += 1
        self._target.trigger(
            "gesture",
            {
                "t": t,
                "kind": self.kind,
                "epoch": self.epoch,
                "drag": self.drag,
                "seq": self.seq,
                **fields,
            },
        )
        return self

    def begin(self, **fields: Any) -> Drag:
        return self.send("begin", **fields)

    def move(self, **fields: Any) -> Drag:
        return self.send("move", **fields)

    def keep(self) -> Drag:
        return self.send("keep")

    def release(self, **fields: Any) -> Drag:
        return self.send("end", aborted=False, **fields)

    def abort(self) -> Drag:
        return self.send("end", aborted=True)


async def held_until(
    drag: Drag, condition: Callable[[], bool], timeout_s: float
) -> bool:
    """Wait for *condition* with *drag* held still, sending the keep-alives
    the browser sends meanwhile; whether it came true in time."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            return False
        drag.keep()
        await asyncio.sleep(0.1)
    return True


def ring(user: User, scene: Any, joint: int) -> Drag:
    """Grab the ring of the panel's joint ``joint``; move it with ``delta=`` (°)."""
    return Drag(user, scene, "ring").begin(joint=joint)


def gizmo(user: User, scene: Any, mode: str = "translate", axis: str = "X") -> Drag:
    """Grab the gizmo where the browser last placed it."""
    return Drag(user, scene, "tcp").begin(mode=mode, axis=axis, rev=scene._tcp_rev)


def ball(
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    rx: float = 0.0,
    ry: float = 0.0,
    rz: float = 0.0,
) -> dict[str, float]:
    """The gizmo ball's pose in its frame: metres, and XYZ Euler radians."""
    return {"x": x, "y": y, "z": z, "rx": rx, "ry": ry, "rz": rz}


def right_click(
    user: User,
    scene: Any,
    hits: list[str],
    ground: list[float] | None = None,
    gen: int = 1,
) -> None:
    """A right-click that found ``hits`` (nearest first) and met the floor at ``ground``."""
    UserInteraction(user, {scene}, None).trigger(
        "context",
        {
            "epoch": scene.gestures.epoch,
            "gen": gen,
            "hits": hits,
            "ground": ground,
            "cx": 100.0,
            "cy": 100.0,
        },
    )
