"""Completion deadlines shared by managed dispatch and motion waits."""

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


#: Patience for a managed wait with no explicit deadline, derived from the
#: controller's own plan: what it still has to play, with slack for
#: acceleration limits and settling, plus a grace period for a command that
#: has not been planned yet (or never completes).
PLAN_SLACK = 1.5
PLAN_GRACE_S = 5.0


class PlanWatchdog:
    """Deadline sized from the controller's queued motion, re-armed whenever
    the plan progresses. A move only times out when the controller reports
    nothing left to play and still does not complete it."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._progress: tuple | None = None
        self._deadline = math.inf

    def check(self, status: object | None) -> bool:
        """True while the wait may go on."""
        if status is None:
            return True
        queued = getattr(status, "queued_duration", None)
        if queued is None:
            return True  # a backend without a plan estimate is not policed
        progress = (
            getattr(status, "completed_index", None),
            getattr(status, "executing_index", None),
            round(float(queued), 3),
        )
        now = self._clock()
        if progress != self._progress:
            self._progress = progress
            self._deadline = now + max(float(queued), 0.0) * PLAN_SLACK + PLAN_GRACE_S
        return now < self._deadline


class CompletionBudget:
    def __init__(self, timeout: float | None) -> None:
        if timeout is not None and (
            isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("Completion timeout must be positive and finite")
        self.timeout = timeout
        self._clock: Callable[[], float] = time.monotonic
        self._start = self._clock()
        self._owner: object | None = None
        self.confirmed_index: int | None = None

    def bind(self, owner: object, clock: Callable[[], float]) -> None:
        if self._owner is not None:
            if self._owner != owner:
                raise RuntimeError(
                    "A completion budget cannot span controller sessions"
                )
            return
        elapsed = self._clock() - self._start
        self._owner = owner
        self._clock = clock
        self._start = clock() - elapsed

    @property
    def remaining(self) -> float:
        if self.timeout is None:
            return math.inf
        return max(0.0, self.timeout - (self._clock() - self._start))


current_budget: ContextVar[CompletionBudget | None] = ContextVar(
    "waldo_completion_budget", default=None
)


@contextmanager
def completion_scope(timeout: float) -> Iterator[CompletionBudget]:
    budget = CompletionBudget(timeout)
    token = current_budget.set(budget)
    try:
        yield budget
    finally:
        current_budget.reset(token)
