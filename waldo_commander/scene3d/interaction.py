"""Gestures from the browser, under rules the app enforces.

A drag in the 3D view can move the robot, so the browser only proposes it.
Every gesture event carries the interaction epoch the browser last heard and
the gesture's drag id and sequence number:

- An epoch the app has moved past (a Stop, a mode change, a reconnect) makes
  the event stale: a begin is refused, anything else is dropped.
- A begin starts nothing until the app's ``admit`` says it may.
- Samples arrive in order; a repeat or an older one is dropped.
- A gesture ends once, released or aborted; what follows its end is dropped.
- A gesture the browser stops reporting on for ``LIVENESS_S`` is aborted, so a
  lost release cannot leave the robot chasing its last target.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .element import WcScene

logger = logging.getLogger(__name__)

LIVENESS_S = 0.5
_WATCH_S = 0.1


@dataclass
class Gesture:
    kind: str
    drag: int
    seq: int
    seen: float
    data: dict[str, Any] = field(default_factory=dict)


class GestureHandler(Protocol):
    def admit(self, gesture: Gesture, args: dict[str, Any]) -> bool:
        """Whether *gesture* may start; it may keep what it needs in ``gesture.data``."""

    def move(self, gesture: Gesture, args: dict[str, Any]) -> None: ...

    def end(self, gesture: Gesture, args: dict[str, Any] | None, aborted: bool) -> None:
        """The gesture is over; *args* is None when the app ended it."""


def _number(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class Gestures:
    """The scene's open gestures and the rules every gesture event passes."""

    def __init__(self, scene: WcScene) -> None:
        self._scene = scene
        self.epoch = 0
        self.handlers: dict[str, GestureHandler] = {}
        self.authorized: Callable[[], bool] = lambda: True
        self._open: dict[int, Gesture] = {}
        self._ended: set[int] = set()
        self._watch: Any = None

    @property
    def open(self) -> list[Gesture]:
        return list(self._open.values())

    def bump(self) -> None:
        """Start a new epoch: every open gesture ends aborted, and events the
        browser sent before hearing of it are stale."""
        self.epoch += 1
        self.abort_all()
        self._scene._command(lambda epoch=self.epoch: ["epoch", epoch])

    def abort_all(self) -> None:
        for gesture in list(self._open.values()):
            self._finish(gesture, None, aborted=True)

    def abort(self, kind: str) -> None:
        for gesture in list(self._open.values()):
            if gesture.kind == kind:
                self._finish(gesture, None, aborted=True)

    def handle(self, args: Any) -> None:
        if not isinstance(args, dict):
            return
        phase, kind, drag, seq = (
            args.get("t"),
            args.get("kind"),
            args.get("drag"),
            args.get("seq"),
        )
        if not (
            isinstance(drag, int) and isinstance(seq, int) and kind in self.handlers
        ):
            logger.debug("malformed gesture event %r", args)
            return
        if args.get("epoch") != self.epoch or not self.authorized():
            if phase == "begin":
                self._reject(drag)
            else:
                gesture = self._open.get(drag)
                if gesture is not None:
                    self._finish(gesture, None, aborted=True)
            return
        gesture = self._open.get(drag)
        if phase == "begin":
            if gesture is not None or drag in self._ended:
                return
            gesture = Gesture(kind, drag, seq, time.monotonic())
            if not self.handlers[kind].admit(gesture, args):
                self._ended.add(drag)
                self._reject(drag)
                return
            self._open[drag] = gesture
            self._arm_watch()
            return
        if gesture is None or gesture.kind != kind or seq <= gesture.seq:
            return
        gesture.seq = seq
        gesture.seen = time.monotonic()
        if phase == "move":
            self.handlers[kind].move(gesture, args)
        elif phase == "end":
            self._finish(gesture, args, aborted=bool(args.get("aborted")))

    def _finish(
        self, gesture: Gesture, args: dict[str, Any] | None, *, aborted: bool
    ) -> None:
        if self._open.pop(gesture.drag, None) is None:
            return
        self._ended.add(gesture.drag)
        try:
            self.handlers[gesture.kind].end(gesture, args, aborted)
        except Exception:
            logger.exception("ending a %s gesture failed", gesture.kind)

    def _reject(self, drag: int) -> None:
        self._scene._command(lambda: ["reject", drag])

    def _arm_watch(self) -> None:
        if self._watch is None and self._open:
            self._watch = self._scene._loop.call_later(_WATCH_S, self._check)

    def _check(self) -> None:
        self._watch = None
        now = time.monotonic()
        for gesture in list(self._open.values()):
            if now - gesture.seen > LIVENESS_S:
                logger.info("a %s drag went quiet; ending it", gesture.kind)
                self._finish(gesture, None, aborted=True)
        self._arm_watch()

    def cancel_watch(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None


def finite(args: dict[str, Any], *keys: str) -> list[float] | None:
    """The numbers at *keys*, or None if any is missing or not finite."""
    values = [args.get(k) for k in keys]
    if not all(_number(v) for v in values):
        return None
    return [float(v) for v in values]  # type: ignore[arg-type]
