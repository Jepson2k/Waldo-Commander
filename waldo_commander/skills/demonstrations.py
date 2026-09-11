"""Conservative waypoint replay through ordinary native planned commands."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from waldoctl.client import RobotClient
from waldoctl.recordings import Demonstration, RecordedTool
from waldoctl.skills import MissingCapability, SkillError, report_progress, skill
from waldoctl.status import ActionState, StatusBuffer
from waldoctl.tools import GripperTool, ToolType

from waldo_commander.skills._motion import completed, validate_motion


@dataclass(frozen=True)
class ReplayResult:
    completed_samples: int
    completed_tool_positions: int
    last_command_index: int


def _identity(tool: RecordedTool | None) -> tuple[str, str]:
    return (tool.key, tool.variant_key) if tool is not None else ("NONE", "")


def _near(actual, expected, tolerance: float, label: str) -> None:
    if (
        actual is None
        or len(actual) != len(expected)
        or any(
            not math.isfinite(a) or abs(a - b) > tolerance
            for a, b in zip(actual, expected)
        )
    ):
        raise SkillError(
            f"{label} differs from the recording; reconcile it before replay"
        )


async def _fresh(stream: AsyncIterator[StatusBuffer], timeout: float) -> StatusBuffer:
    async with asyncio.timeout(timeout):
        baseline = await anext(stream)
        while True:
            status = await anext(stream)
            if status.session_id != baseline.session_id or status.seq > baseline.seq:
                return status


@skill(
    id="waldo.replay_demonstration",
    version="1.0.0",
    requires=frozenset({"motion.joint"}),
)
async def replay_demonstration(
    rbt: RobotClient,
    recording: Demonstration,
    *,
    timeout: float = 30.0,
    observation_timeout: float = 2.0,
    replay_gripper: bool = False,
    reconciled_session: bool = False,
) -> ReplayResult:
    """Replay an uninterrupted span, stopping at every observed waypoint.

    Each original interval is a minimum duration for its native joint move;
    the active motion profile may lengthen it. Completion settling and command
    overhead also lengthen playback. Original observations stay unchanged.
    Identical joint observations become native delays. This does not reconstruct
    the demonstrator's continuous path or concurrently replay tool motion.

    Start joints must match within 0.5 degrees, with the same backend, source
    mode, selected tool and TCP. A new controller session requires explicit
    ``reconciled_session=True`` after referencing and checking the physical scene.
    No approach, reset, referencing, or automatic recovery is performed.

    Gripper replay is opt-in, uses normalized observed positions sequentially
    at waypoint boundaries, and never treats recorded grasp signals as results.
    ``timeout`` is the per-command completion budget; managed WC pauses exclude
    intentional debug holds. Observation freshness always uses wall-clock time.
    """
    recording.require_continuous()
    validate_motion(1.0, timeout)
    validate_motion(1.0, observation_timeout)
    if type(replay_gripper) is not bool or type(reconciled_session) is not bool:
        raise ValueError("Replay options must be booleans")
    caps = rbt.skill_capabilities
    if f"backend.{recording.backend}" not in caps:
        raise MissingCapability(f"This recording belongs to {recording.backend}")
    preview = "execution.preview" in caps
    if not preview and "observation.timed" not in caps:
        raise MissingCapability("Replay requires identified, timed status observations")
    first = recording.samples[0]
    identity = _identity(first.tool)
    if any(_identity(s.tool) != identity for s in recording.samples):
        raise ValueError("Select a recording span with one tool configuration")
    gripper: GripperTool | None = None
    if replay_gripper:
        try:
            tool = rbt.tool
        except RuntimeError as error:
            # The client only learns its tool from its own select_tool().
            raise MissingCapability(
                "Call select_tool() for the recorded gripper before replaying its actions"
            ) from error
        if tool.tool_type != ToolType.GRIPPER or tool.key != identity[0]:
            raise MissingCapability("Select the recorded gripper before replay")
        if any(
            s.tool is None
            or s.tool.fault_code
            or not s.tool.positions
            or any(abs(p - s.tool.positions[0]) > 1e-6 for p in s.tool.positions)
            for s in recording.samples
        ):
            raise ValueError(
                "Gripper replay requires fault-free, coupled position observations"
            )
        gripper = cast(GripperTool, tool)

    stream = None if preview else rbt.stream_status()
    session_id = 0

    def healthy(status: StatusBuffer) -> None:
        if status.session_id != session_id or not status.enabled or not status.homed:
            raise SkillError("Controller session or reference was lost during replay")
        if status.simulator_active != recording.simulator:
            raise SkillError("Controller mode changed during replay")
        if (status.tool_status.key, status.tool_status.variant_key) != identity:
            raise SkillError("Selected tool changed during replay")
        if status.tool_status.fault_code:
            raise SkillError("Tool fault during replay")

    async def execute() -> ReplayResult:
        index = -1
        tool_count = 0
        previous_position: float | None = None
        for number, sample in enumerate(recording.samples):
            if number:
                previous = recording.samples[number - 1]
                interval = (sample.observed_ns - previous.observed_ns) / 1e9
                dispatch = (
                    rbt.delay(interval)
                    if sample.joints_deg == previous.joints_deg
                    else rbt.move_j(
                        list(sample.joints_deg), duration=interval, wait=False
                    )
                )
                index = await completed(rbt, dispatch, timeout, "Recorded waypoint")
            if gripper is not None:
                assert sample.tool is not None
                position = sample.tool.positions[0]
                if position != previous_position:
                    index = await completed(
                        rbt,
                        gripper.set_position(position),
                        timeout,
                        "Recorded gripper position",
                    )
                    previous_position = position
                    tool_count += 1
            report_progress(
                f"Replayed observation {number + 1}/{len(recording.samples)}",
                fraction=(number + 1) / len(recording.samples),
            )
        return ReplayResult(len(recording.samples), tool_count, index)

    async def watch() -> None:
        assert stream is not None
        previous_seq = -1
        while True:
            async with asyncio.timeout(observation_timeout):
                status = await anext(stream)
            healthy(status)
            if status.seq <= previous_seq:
                raise SkillError("Replay observations stopped advancing")
            previous_seq = status.seq

    tasks: list[asyncio.Task] = []
    try:
        async with asyncio.timeout(observation_timeout):
            _near(
                await rbt.tcp_transform(), recording.tcp_transform, 1e-5, "Applied TCP"
            )
            _near(await rbt.angles(), first.joints_deg, 0.5, "Start joint position")
        if stream is not None:
            async with asyncio.timeout(observation_timeout):
                queued = await rbt.queue()
            if queued is None:
                raise ConnectionError("The controller queue could not be read")
            if queued:
                raise SkillError("Clear queued commands before replay")
            status = await _fresh(stream, observation_timeout)
            session_id = status.session_id
            if not session_id or (
                session_id != recording.session_id and not reconciled_session
            ):
                raise SkillError("Reconcile the new controller session before replay")
            # The controller's word, not the client's: a client that never
            # called select_tool() in-process still replays on the right tool.
            if (status.tool_status.key, status.tool_status.variant_key) != identity:
                raise SkillError("Select the recorded tool before replay")
            healthy(status)
            if status.action_state != ActionState.IDLE or any(
                not math.isfinite(v) or abs(v) > 0.02 for v in status.speeds
            ):
                raise SkillError(
                    f"Stop existing motion before replay (command={status.executing_index}, "
                    f"state={status.action_state}, speeds={status.speeds.tolist()})"
                )
            _near(status.angles, first.joints_deg, 0.5, "Start joint position")
        if preview:
            return await execute()
        execution = asyncio.create_task(execute())
        watcher = asyncio.create_task(watch())
        tasks.extend((execution, watcher))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await watcher
            raise SkillError("Replay observation stream ended")
        return await execution
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if stream is not None:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
