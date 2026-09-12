"""Named digital I/O with bounded observation waits and explicit preview data."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from waldoctl.client import RobotClient
from waldoctl.signals import DigitalSignal, SignalObservation, SignalWaitResult
from waldoctl.skills import MissingCapability, UnresolvedPreview, skill


@dataclass(frozen=True)
class SignalFixture:
    """An explicit constant logical level for a sensor-dependent preview."""

    value: bool

    def __post_init__(self) -> None:
        if type(self.value) is not bool:
            raise ValueError("A signal fixture requires a boolean logical level")


def _seconds(value: float, label: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive finite number of seconds")


def _binding(rbt: RobotClient, signal: DigitalSignal) -> bool:
    if f"backend.{signal.backend}" not in rbt.skill_capabilities:
        raise MissingCapability(f"This signal mapping belongs to {signal.backend}")
    return "execution.preview" in rbt.skill_capabilities


async def _observe(
    rbt: RobotClient,
    signal: DigitalSignal,
    timeout: float,
    fixture: SignalFixture | None,
) -> SignalObservation:
    preview = _binding(rbt, signal)
    if fixture is not None:
        if not preview:
            raise ValueError("Signal fixtures require a preview client")
        return SignalObservation(fixture.value, time.time(), "fixture")
    if preview:
        raise UnresolvedPreview(
            "Named signals need an explicit SignalFixture in preview"
        )
    levels = await rbt.io(timeout=timeout)
    if levels is None:
        raise ConnectionError("The controller did not return fresh digital I/O")
    return SignalObservation(signal.decode(levels), time.time())


@skill(id="waldo.read_signal", version="1.0.0", requires=frozenset({"io.digital"}))
async def read_signal(
    rbt: RobotClient,
    signal: DigitalSignal,
    *,
    timeout: float = 1.0,
    fixture: SignalFixture | None = None,
) -> SignalObservation:
    """Read a mapped logical level with a host receipt timestamp."""
    _seconds(timeout, "Observation timeout")
    return await _observe(rbt, signal, timeout, fixture)


@skill(id="waldo.wait_signal", version="1.0.0", requires=frozenset({"io.digital"}))
async def wait_signal(
    rbt: RobotClient,
    signal: DigitalSignal,
    value: bool = True,
    *,
    timeout: float = 5.0,
    fixture: SignalFixture | None = None,
) -> SignalWaitResult:
    """Wait for a logical level; timeout is distinct from lost communication.

    The wait reads the status broadcast the controller already sends, so it
    sees a level the tick it is published and never asks for I/O the stream
    carries anyway. Silence is lost communication: a controller that
    broadcasts nothing raises rather than reporting a timeout it cannot
    distinguish from a level that never arrived.
    """
    if type(value) is not bool:
        raise ValueError("The expected logical level must be a boolean")
    _seconds(timeout, "Wait timeout")
    if _binding(rbt, signal):
        observation = await _observe(rbt, signal, timeout, fixture)
        if observation.value == value:
            return SignalWaitResult("matched", observation, 0.0)
        # A constant fixture cannot change. Account for the wait on the
        # preview's program clock without polling the wall clock.
        await rbt.delay(timeout)
        return SignalWaitResult("timeout", observation, timeout)

    start = time.monotonic()
    latest: SignalObservation | None = None
    refused: ValueError | None = None

    def reached(status) -> bool:
        nonlocal latest, refused
        try:
            levels = [int(level) for level in status.io]
            latest = SignalObservation(signal.decode(levels), time.time())
        except ValueError as error:
            # A mapping the controller's I/O layout no longer fits. Stop the
            # wait and report it: `wait_status` logs a raising predicate and
            # carries on, which would show up as a timeout.
            refused = error
            return True
        return latest.value == value

    matched = await rbt.wait_status(reached, timeout=timeout)
    if refused is not None:
        raise refused
    if latest is None:
        raise ConnectionError("The controller broadcast no status to observe I/O in")
    return SignalWaitResult(
        "matched" if matched else "timeout", latest, time.monotonic() - start
    )


@skill(id="waldo.write_signal", version="1.0.0", requires=frozenset({"io.digital"}))
async def write_signal(
    rbt: RobotClient,
    signal: DigitalSignal,
    value: bool,
    *,
    timeout: float = 2.0,
    fixture: SignalFixture | None = None,
) -> SignalObservation:
    """Write one mapped output and confirm its reported electrical level."""
    _seconds(timeout, "Write timeout")
    raw = signal.encode(value)
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Digital output application was not confirmed")
        return seconds

    # Refuse a mismatched controller layout before sending a write.
    await _observe(rbt, signal, min(1.0, remaining()), fixture)
    index = await rbt.write_io(signal.index, raw, timeout=remaining())
    if index < 0:
        raise TimeoutError("Digital output acceptance was not confirmed")
    result = await wait_signal.async_call(
        rbt, signal, value, timeout=remaining(), fixture=fixture
    )
    if result.outcome != "matched" or result.observation is None:
        raise TimeoutError("Digital output application was not confirmed")
    return result.observation
