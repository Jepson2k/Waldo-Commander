"""Loop timing utilities for the web commander.

Provides PhaseTimer for instrumenting named phases in a loop,
LoopMetrics for tracking loop period statistics, and format_hz_summary
for human-readable rate display.

Copied from parol6.server.loop_timer with parol6 dependencies removed.
"""

import math
import time
from typing import TYPE_CHECKING

import numpy as np
from numba import njit

if TYPE_CHECKING:
    from typing import Self


# ~25 seconds of data at 20 Hz; power of 2 for fast modulo via bitmask.
BUFFER_SIZE = 512
BUFFER_MASK = 511


@njit(cache=True)
def _quickselect_partition(arr: np.ndarray, left: int, right: int) -> int:
    """Partition array around last element as pivot. Returns pivot index."""
    pivot = arr[right]
    i = left - 1
    for j in range(left, right):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]
    arr[i + 1], arr[right] = arr[right], arr[i + 1]
    return i + 1


@njit(cache=True)
def _quickselect(arr: np.ndarray, k: int) -> float:
    """Find k-th smallest element in-place. Modifies arr."""
    left = 0
    right = len(arr) - 1
    while left < right:
        pivot_idx = _quickselect_partition(arr, left, right)
        if pivot_idx == k:
            return arr[k]
        elif pivot_idx < k:
            left = pivot_idx + 1
        else:
            right = pivot_idx - 1
    return arr[k]


@njit(cache=True)
def _compute_phase_stats(
    samples: np.ndarray, scratch: np.ndarray, n: int
) -> tuple[float, float, float]:
    """Compute phase stats: mean, max, p99. Uses pre-allocated scratch buffer."""
    if n == 0:
        return 0.0, 0.0, 0.0

    total = 0.0
    max_val = samples[0]
    for i in range(n):
        total += samples[i]
        if samples[i] > max_val:
            max_val = samples[i]
    mean = total / n

    # Copy to scratch first so quickselect doesn't reorder the original.
    if n >= 20:
        for i in range(n):
            scratch[i] = samples[i]
        k = int(n * 0.99)
        p99 = _quickselect(scratch[:n], k)
    else:
        p99 = max_val

    return mean, max_val, p99


@njit(cache=True)
def _compute_loop_stats(
    samples: np.ndarray, scratch: np.ndarray, n: int
) -> tuple[float, float, float, float, float, float]:
    """Compute loop stats using single-pass Welford's algorithm for mean+std.

    Uses pre-allocated scratch buffer for percentile computation.
    Only one copy to scratch (p99 first, then p95 on same data).
    """
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    # Welford's online algorithm for mean and variance in one pass.
    mean = 0.0
    m2 = 0.0  # sum of squared differences
    min_val = samples[0]
    max_val = samples[0]

    for i in range(n):
        x = samples[i]
        delta = x - mean
        mean += delta / (i + 1)
        delta2 = x - mean
        m2 += delta * delta2
        if x < min_val:
            min_val = x
        if x > max_val:
            max_val = x

    std = math.sqrt(m2 / n) if n > 0 else 0.0

    if n >= 20:
        for i in range(n):
            scratch[i] = samples[i]

        k99 = int(n * 0.99)
        p99 = _quickselect(scratch[:n], k99)

        # Reuse the same partially-sorted scratch: valid because k95 < k99.
        k95 = int(n * 0.95)
        p95 = _quickselect(scratch[:n], k95)
    else:
        p95 = max_val
        p99 = max_val

    return mean, std, min_val, max_val, p95, p99


class PhaseMetrics:
    """Rolling statistics for a single phase.

    Uses pre-allocated numpy arrays and calls @njit helper functions
    for the heavy computation.
    """

    __slots__ = (
        "_buffer",
        "_scratch",
        "_buffer_idx",
        "_buffer_count",
        "last_s",
        "mean_s",
        "max_s",
        "p99_s",
    )

    def __init__(self) -> None:
        self._buffer = np.zeros(BUFFER_SIZE, dtype=np.float64)
        self._scratch = np.zeros(BUFFER_SIZE, dtype=np.float64)
        self._buffer_idx = 0
        self._buffer_count = 0
        self.last_s = 0.0
        self.mean_s = 0.0
        self.max_s = 0.0
        self.p99_s = 0.0

    def record(self, duration: float) -> None:
        """Record a duration sample."""
        self.last_s = duration
        self._buffer[self._buffer_idx] = duration
        self._buffer_idx = (self._buffer_idx + 1) & BUFFER_MASK
        if self._buffer_count < BUFFER_SIZE:
            self._buffer_count += 1

    def compute_stats(self) -> None:
        """Compute statistics from buffer."""
        if self._buffer_count == 0:
            return
        mean, max_val, p99 = _compute_phase_stats(
            self._buffer, self._scratch, self._buffer_count
        )
        self.mean_s = mean
        self.max_s = max_val
        self.p99_s = p99


class PhaseTimer:
    """Timer for measuring durations of multiple named phases in a loop.

    Usage:
        timer = PhaseTimer(["read", "execute", "write"])
        while True:
            with timer.phase("read"):
                do_read()
            with timer.phase("execute"):
                do_execute()
            timer.tick()  # Compute stats periodically
    """

    def __init__(self, phase_names: list[str], stats_interval: int = 50):
        self._phases: dict[str, PhaseMetrics] = {
            name: PhaseMetrics() for name in phase_names
        }
        self._stats_interval = stats_interval
        self._tick_count = 0
        self._current_phase: str | None = None
        self._phase_start: float = 0.0

    @property
    def phases(self) -> dict[str, PhaseMetrics]:
        """Access phase metrics by name."""
        return self._phases

    def start(self, phase: str) -> None:
        """Start timing a phase."""
        self._current_phase = phase
        self._phase_start = time.perf_counter()

    def stop(self) -> float:
        """Stop timing current phase and record duration. Returns duration."""
        if self._current_phase is None:
            return 0.0
        duration = time.perf_counter() - self._phase_start
        self._phases[self._current_phase].record(duration)
        self._current_phase = None
        return duration

    def phase(self, name: str) -> "PhaseContext":
        """Context manager for timing a phase."""
        return PhaseContext(self, name)

    def tick(self) -> None:
        """Call once per loop iteration to compute stats periodically."""
        self._tick_count += 1
        if self._tick_count % self._stats_interval == 0:
            for p in self._phases.values():
                p.compute_stats()

    def get_summary(self) -> dict[str, dict[str, float]]:
        """Get summary of all phases as dict."""
        return {
            name: {
                "mean_ms": p.mean_s * 1000,
                "max_ms": p.max_s * 1000,
                "p99_ms": p.p99_s * 1000,
            }
            for name, p in self._phases.items()
        }


class PhaseContext:
    """Context manager for timing a phase."""

    __slots__ = ("_timer", "_phase")

    def __init__(self, timer: PhaseTimer, phase: str):
        self._timer = timer
        self._phase = phase

    def __enter__(self) -> "Self":
        self._timer.start(self._phase)
        return self

    def __exit__(self, *args: object) -> None:
        self._timer.stop()


class LoopMetrics:
    """Metrics tracked by the loop timer with rolling statistics.

    Provides unified timing, logging, and degradation checking across subsystems.
    Use configure() to set target period, then call tick() each iteration.

    Uses pre-allocated numpy arrays and calls @njit helper functions
    for the heavy computation.
    """

    __slots__ = (
        "loop_count",
        "overrun_count",
        "mean_period_s",
        "std_period_s",
        "min_period_s",
        "max_period_s",
        "p95_period_s",
        "p99_period_s",
        # Overshoot tracking (how much we miss the deadline by)
        "mean_overshoot_s",
        "max_overshoot_s",
        "p99_overshoot_s",
        "_buffer",
        "_scratch",
        "_buffer_idx",
        "_buffer_count",
        "_overshoot_buffer",
        "_overshoot_idx",
        "_overshoot_count",
        "_target_period_s",
        "_prev_time",
        "_last_log_time",
        "_last_warn_time",
        "_stats_interval",
        "_start_time",
        "_grace_period_s",
    )

    def __init__(self) -> None:
        self.loop_count = 0
        self.overrun_count = 0
        self.mean_period_s = 0.0
        self.std_period_s = 0.0
        self.min_period_s = 0.0
        self.max_period_s = 0.0
        self.p95_period_s = 0.0
        self.p99_period_s = 0.0
        self.mean_overshoot_s = 0.0
        self.max_overshoot_s = 0.0
        self.p99_overshoot_s = 0.0
        self._buffer = np.zeros(BUFFER_SIZE, dtype=np.float64)
        self._scratch = np.zeros(BUFFER_SIZE, dtype=np.float64)
        self._buffer_idx = 0
        self._buffer_count = 0
        self._overshoot_buffer = np.zeros(BUFFER_SIZE, dtype=np.float64)
        self._overshoot_idx = 0
        self._overshoot_count = 0
        self._target_period_s = 0.0
        self._prev_time = 0.0
        self._last_log_time = 0.0
        self._last_warn_time = 0.0
        self._stats_interval = 50
        self._start_time = 0.0
        self._grace_period_s = 10.0

    def configure(
        self, target_period_s: float, stats_interval: int, grace_period_s: float = 15.0
    ) -> None:
        """Configure target period, stats interval, and startup grace period.

        Args:
            target_period_s: Target loop period in seconds
            stats_interval: How often to compute rolling statistics (in loop iterations)
            grace_period_s: Duration after mark_started() during which overbudget
                warnings are suppressed. Defaults to 15s to allow for JIT warmup
                and process pool initialization.
        """
        self._target_period_s = target_period_s
        self._stats_interval = stats_interval
        self._grace_period_s = grace_period_s

    def mark_started(self, now: float) -> None:
        """Mark the start time for grace period calculation."""
        self._start_time = now

    def tick(self, now: float) -> None:
        """Call once per iteration. Auto-records period and computes stats periodically."""
        if self._prev_time > 0:
            self.record_period(now - self._prev_time)
        self._prev_time = now
        self.loop_count += 1
        if self.loop_count % self._stats_interval == 0:
            self.compute_stats()

    def should_log(self, now: float, interval: float) -> bool:
        """Returns True and updates timestamp if interval has passed."""
        if now - self._last_log_time >= interval:
            self._last_log_time = now
            return True
        return False

    def check_degraded(
        self, now: float, threshold: float, rate_limit: float
    ) -> tuple[bool, float]:
        """Check if p99 exceeds target by threshold. Returns (should_warn, degradation_pct).

        Rate-limited to once per rate_limit seconds. Suppressed during startup grace period.
        """
        if self._target_period_s <= 0 or self.p99_period_s <= 0:
            return False, 0.0
        if self._start_time > 0 and (now - self._start_time) < self._grace_period_s:
            return False, 0.0
        if now - self._last_warn_time < rate_limit:
            return False, 0.0
        if self.p99_period_s > self._target_period_s * (1.0 + threshold):
            degradation = (self.p99_period_s / self._target_period_s - 1.0) * 100.0
            self._last_warn_time = now
            return True, degradation
        return False, 0.0

    def record_period(self, period: float) -> None:
        """Record a period sample into the circular buffer."""
        self._buffer[self._buffer_idx] = period
        self._buffer_idx = (self._buffer_idx + 1) & BUFFER_MASK
        if self._buffer_count < BUFFER_SIZE:
            self._buffer_count += 1

    def record_overshoot(self, overshoot: float) -> None:
        """Record busy-loop overshoot (time past deadline) into buffer."""
        self._overshoot_buffer[self._overshoot_idx] = overshoot
        self._overshoot_idx = (self._overshoot_idx + 1) & BUFFER_MASK
        if self._overshoot_count < BUFFER_SIZE:
            self._overshoot_count += 1

    def compute_stats(self) -> None:
        """Compute statistics from buffers."""
        if self._buffer_count > 0:
            mean, std, min_val, max_val, p95, p99 = _compute_loop_stats(
                self._buffer, self._scratch, self._buffer_count
            )
            self.mean_period_s = mean
            self.std_period_s = std
            self.min_period_s = min_val
            self.max_period_s = max_val
            self.p95_period_s = p95
            self.p99_period_s = p99

        if self._overshoot_count > 0:
            mean, max_val, p99 = _compute_phase_stats(
                self._overshoot_buffer, self._scratch, self._overshoot_count
            )
            self.mean_overshoot_s = mean
            self.max_overshoot_s = max_val
            self.p99_overshoot_s = p99

    def reset_stats(self, include_counters: bool = False) -> None:
        """Reset rolling statistics.

        Args:
            include_counters: If True, also reset overrun_count (loop_count is preserved).
        """
        self._buffer.fill(0.0)
        self._buffer_idx = 0
        self._buffer_count = 0
        self.mean_period_s = 0.0
        self.std_period_s = 0.0
        self.min_period_s = 0.0
        self.max_period_s = 0.0
        self.p95_period_s = 0.0
        self.p99_period_s = 0.0
        self._overshoot_buffer.fill(0.0)
        self._overshoot_idx = 0
        self._overshoot_count = 0
        self.mean_overshoot_s = 0.0
        self.max_overshoot_s = 0.0
        self.p99_overshoot_s = 0.0
        if include_counters:
            self.overrun_count = 0


def format_hz_summary(m: LoopMetrics) -> str:
    """Format metrics as 'XXX.XHz sigma=X.XXms p99=X.XXms'."""
    if m.mean_period_s <= 0:
        return "0.0Hz sigma=0.00ms p99=0.00ms"
    hz = 1.0 / m.mean_period_s
    return f"{hz:.1f}Hz \u03c3={m.std_period_s * 1000:.2f}ms p99={m.p99_period_s * 1000:.2f}ms"
