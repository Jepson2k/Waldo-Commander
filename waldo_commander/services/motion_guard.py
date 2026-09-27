"""One place that knows the robot was stopped, and who is driving it.

Every stop Commander sends goes through :func:`MotionGuard.stop_robot` or
:func:`MotionGuard.note_stop`, so a motion source that keeps its own state
between commands (automatic calibration, held jogs, drags) can tell that a
human stopped the robot while it was not looking. The controller does not
count stops, and a Stop between two moves leaves no trace in its status.

The reservation lets one autonomous owner (a program, automatic calibration)
drive at a time; everything else that moves the robot refuses while it is
held. Stopping never needs the reservation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

PROGRAM = "A program"
CALIBRATION = "Automatic calibration"

STOP_TIMEOUT_S = 3.0


class MotionBusy(RuntimeError):
    """Another owner holds the robot."""


class Reservation:
    """A held claim on the robot; releasing it twice is harmless."""

    def __init__(self, guard: MotionGuard, owner: str) -> None:
        self._guard = guard
        self.owner = owner

    def release(self) -> None:
        if self._guard._reservation is self:
            self._guard._reservation = None

    def __enter__(self) -> Reservation:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class MotionGuard:
    def __init__(self) -> None:
        self.stop_generation = 0
        self._reservation: Reservation | None = None
        self._listeners: list[Callable[[int, str], None]] = []

    @property
    def owner(self) -> str | None:
        return None if self._reservation is None else self._reservation.owner

    def note_stop(self, reason: str) -> int:
        """Record that the robot was stopped, and tell every listener."""
        self.stop_generation += 1
        logger.info("Stop (%s), generation %d", reason, self.stop_generation)
        for listener in list(self._listeners):
            try:
                listener(self.stop_generation, reason)
            except Exception:
                logger.exception("Stop listener failed")
        return self.stop_generation

    def add_stop_listener(
        self, listener: Callable[[int, str], None]
    ) -> Callable[[], None]:
        """Call ``listener(generation, reason)`` on every stop; returns its remover."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return remove

    async def stop_robot(
        self, client: Any, reason: str, *, estop: bool = False
    ) -> bool:
        """Stop (or protectively stop) the robot; True only when the controller acknowledged.

        The stop is noted before the request goes out, so a motion source
        that checks after its own await sees it even while the request is
        still queued behind that source's.
        """
        self.note_stop(reason)
        verb = "E-stop" if estop else "Stop"
        try:
            async with asyncio.timeout(STOP_TIMEOUT_S):
                result = await (client.estop() if estop else client.stop())
        except Exception as e:
            # A stop path must report failure, never raise past the button
            # or tool that asked for it.
            logger.warning("%s (%s) was not confirmed: %r", verb, reason, e)
            return False
        if result <= 0:
            logger.warning(
                "%s (%s) was not acknowledged by the controller", verb, reason
            )
            return False
        return True

    def reserve(self, owner: str) -> Reservation:
        """Claim the robot for ``owner``; raises :class:`MotionBusy` while someone else holds it."""
        if (busy := self.busy_reason()) is not None:
            raise MotionBusy(busy)
        self._reservation = Reservation(self, owner)
        return self._reservation

    def busy_reason(self) -> str | None:
        """Why the robot can't be driven by someone new, or None."""
        if self._reservation is None:
            return None
        return f"{self._reservation.owner} is moving the robot"

    def reset(self) -> None:
        self._reservation = None


motion_guard = MotionGuard()
