"""Named digital I/O with bounded observation waits and explicit preview data."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from waldoctl.client import RobotClient
from waldoctl.dry_run import is_dry_run
from waldoctl.signals import DigitalSignal, SignalObservation, SignalWaitResult
from waldoctl.skills import MissingCapability, UnresolvedPreview, skill

from waldo_commander.skills._motion import completed


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
    """Whether *rbt* previews rather than drives, once the mapping is
    known to belong to the backend and bank layout it drives."""
    robot = rbt.robot
    if robot is None or robot.backend_package != signal.backend:
        raise MissingCapability(f"This signal mapping belongs to {signal.backend}")
    # The level vector only checks the total, so banks that moved their
    # boundary would silently read an input as an output.
    if (signal.input_count, signal.output_count) != (
        robot.digital_inputs,
        robot.digital_outputs,
    ):
        raise ValueError(
            f"Controller I/O layout ({robot.digital_inputs} inputs, "
            f"{robot.digital_outputs} outputs) differs from the saved mapping "
            f"({signal.input_count}, {signal.output_count})"
        )
    return is_dry_run(rbt)


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


@skill(id="waldo.read_signal", version="1.0.0")
async def read_signal(
    rbt: RobotClient,
    signal: DigitalSignal,
    *,
    timeout: float = 1.0,
    fixture: SignalFixture | None = None,
) -> SignalObservation:
    """Read a named input or output once."""
    _seconds(timeout, "Observation timeout")
    return await _observe(rbt, signal, timeout, fixture)


@skill(id="waldo.wait_signal", version="1.0.0")
async def wait_signal(
    rbt: RobotClient,
    signal: DigitalSignal,
    value: bool = True,
    *,
    timeout: float = 5.0,
    fixture: SignalFixture | None = None,
) -> SignalWaitResult:
    """Wait until a named signal reaches a value.

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
    cached = True

    def reached(status) -> bool:
        nonlocal latest, refused, cached
        if cached:
            # The first evaluation is the frame the client already holds,
            # which is stale by however long the stream has been silent.
            cached = False
            return False
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
        raise ConnectionError(
            "The controller broadcast no fresh status to observe I/O in"
        )
    return SignalWaitResult(
        "matched" if matched else "timeout", latest, time.monotonic() - start
    )


@skill(id="waldo.write_signal", version="1.0.0")
async def write_signal(
    rbt: RobotClient,
    signal: DigitalSignal,
    value: bool,
    *,
    timeout: float = 2.0,
    fixture: SignalFixture | None = None,
) -> SignalObservation:
    """Set a named output and confirm the controller reports it.

    Each phase -- acceptance, the queued write, the level -- gets the whole
    ``timeout``: a managed program holds the write at a step boundary for as
    long as the operator takes, and that time is not the controller's.
    """
    _seconds(timeout, "Write timeout")
    raw = signal.encode(value)
    # Refuse a mismatched controller layout before sending a write.
    await _observe(rbt, signal, min(1.0, timeout), fixture)
    # Queued behind other work, an unconfirmed write would still land later.
    await completed(
        rbt, rbt.write_io(signal.index, raw, timeout=timeout), timeout, "Digital output"
    )
    result = await wait_signal.async_call(
        rbt, signal, value, timeout=timeout, fixture=fixture
    )
    if result.outcome != "matched" or result.observation is None:
        raise TimeoutError("Digital output application was not confirmed")
    return result.observation
