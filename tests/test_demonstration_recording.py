"""Observed motion survives export and capture ends on controller disable."""

import asyncio
from dataclasses import replace
from typing import cast
from types import SimpleNamespace
from uuid import uuid4

import pytest
import waldoctl
import numpy as np
from nicegui.testing import User
from nicegui import run
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl.skills import MissingCapability, SkillError

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
)
from waldo_commander.demonstrations import (
    load_demonstration,
    record_demonstration,
    save_demonstration,
)
from waldo_commander.skills import replay_demonstration
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.stepping_client import (
    AsyncSteppingClientWrapper,
    GUIStepController,
    StepIO,
)


@pytest.mark.integration
async def test_observed_motion_records_cadence_gaps_and_controller_loss(
    user: User, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    first = asyncio.Event()
    task = asyncio.create_task(
        record_demonstration(client, duration_s=2, on_sample=lambda sample: first.set())
    )
    try:
        from waldo_commander import demonstrations

        with monkeypatch.context() as timer:
            # Model an early timer wakeup while retaining the real status stream.
            timer.setattr(
                demonstrations,
                "asyncio",
                SimpleNamespace(
                    timeout=lambda seconds: asyncio.timeout(
                        max(0.00001, seconds - 0.005)
                    )
                ),
            )
            await asyncio.wait_for(first.wait(), 5)
            start = await client.angles()
            assert start is not None
            target = list(start)
            target[0] += 3
            index = await client.move_j(target, duration=0.8)
            assert await client.wait_command(index, timeout=10)
            recording = await task
        assert recording.ended == "duration_limit"
        assert len(recording.samples) >= 5
        assert recording.duration_s > 0
        assert recording.observed_rate_hz > 0
        assert (
            recording.samples[-1].joints_deg[0] - recording.samples[0].joints_deg[0] > 2
        )
        path = tmp_path / "demonstration.json"
        save_demonstration(path, recording)
        assert load_demonstration(path) == recording

        missing = replace(
            recording, samples=(recording.samples[0], *recording.samples[3:])
        )
        assert missing.gaps[0].missing_publications >= 2
        with pytest.raises(ValueError, match="uninterrupted"):
            await replay_demonstration.async_call(client, missing)
        selected = recording.select(1, 3)
        assert selected.samples[0].observed_ns == recording.samples[1].observed_ns

        # Real status streams can drop publications under CI load. Select a
        # measured continuous span; never erase gaps or synthesize timestamps.
        boundaries = [
            0,
            *(gap.sample_index for gap in recording.gaps),
            len(recording.samples),
        ]
        begin, end = max(
            zip(boundaries, boundaries[1:]), key=lambda span: span[1] - span[0]
        )
        recording = recording.select(begin, end)
        recording.require_continuous()
        save_demonstration(path, recording)
        first_joints = list(recording.samples[0].joints_deg)
        mismatched = first_joints.copy()
        mismatched[0] += 3
        index = await client.move_j(mismatched, duration=0.8)
        assert await client.wait_command(index, timeout=10)
        with pytest.raises(SkillError, match="Start joint"):
            await replay_demonstration.async_call(client, recording)
        assert await client.angles() == pytest.approx(mismatched, abs=0.5)
        index = await client.move_j(first_joints, speed=0.5)
        assert await client.wait_command(index, timeout=10)
        with pytest.raises(SkillError, match="new controller session"):
            await replay_demonstration.async_call(
                client, replace(recording, session_id=recording.session_id ^ 1)
            )

        source = (
            "from parol6 import RobotClient\n"
            "from waldo_commander.demonstrations import load_demonstration\n"
            "from waldo_commander.skills import replay_demonstration\n"
            "with RobotClient() as rbt:\n"
            f"    replay_demonstration(rbt, load_demonstration({str(path)!r}))\n"
        )
        preview = await run.cpu_bound(
            _run_simulation_isolated,
            source,
            np.radians(first_joints),
            dry_run_client_cls=DryRunRobotClient,
        )
        assert preview["error"] is None, preview["error"]
        assert preview["segments"]
        result = await replay_demonstration.async_call(client, recording)
        assert result.completed_samples == len(recording.samples)
        assert await client.angles() == pytest.approx(
            recording.samples[-1].joints_deg, abs=0.5
        )

        def element(marker):
            return next(iter(user.find(marker=marker).elements))

        user.find(marker="tab-demonstrations").click()
        element("demo-name").set_value("demonstration")
        user.find(marker="demo-load").click()
        await user.should_see("Loaded observations")
        assert element("demo-chart").options["series"][0]["data"]
        user.find(marker="demo-insert").click()
        await user.should_see("Inserted replay")
        assert (
            "replay_demonstration(rbt, recording"
            in waldoctl.commander.programs.active.source
        )
        element("demo-duration").set_value(30)
        user.find(marker="demo-capture").click()
        await user.should_see("Capturing controller observations")
        await user.should_see("observations", retries=30)
        user.find(marker="demo-stop").click()
        await user.should_see("Capture ended", retries=50)

        first.clear()
        task = asyncio.create_task(
            record_demonstration(
                client, duration_s=30, on_sample=lambda sample: first.set()
            )
        )
        await asyncio.wait_for(first.wait(), 5)
        await client.estop()
        ended = await asyncio.wait_for(task, 3)
        assert ended.ended in {"disabled", "reference_lost"}
        assert ended.samples and ended.session_id == recording.session_id
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.reset()
        await client.resume()


@pytest.mark.integration
async def test_gripper_recording_replays_through_managed_pause_and_fault(user: User):
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    index = await client.select_tool("PNEUMATIC")
    assert await client.wait_command(index, timeout=5)
    assert await client.wait_status(
        lambda s: s.tool_status.key == "PNEUMATIC", timeout=3
    )
    # Choose an observed, uninterrupted open-to-close span. A dropped
    # publication remains a gap; acquire another transition if needed.
    recording = None
    for _ in range(3):
        index = await client.tool.open()
        assert await client.wait_command(index, timeout=5)
        assert await client.wait_status(
            lambda s: s.tool_status.positions[0] == 0, timeout=3
        )
        started = asyncio.Event()
        capture = asyncio.create_task(
            record_demonstration(
                client, duration_s=1, on_sample=lambda sample: started.set()
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        index = await client.tool.close()
        assert await client.wait_command(index, timeout=5)
        observed = await capture
        boundaries = [
            0,
            *(gap.sample_index for gap in observed.gaps),
            len(observed.samples),
        ]
        for begin, end in zip(boundaries, boundaries[1:]):
            span = observed.select(begin, end)
            if (
                len(span.samples) >= 2
                and span.samples[0].tool.positions[0] == 0
                and span.samples[-1].tool.positions[0] == 1
            ):
                recording = span
                break
        if recording is not None:
            break
    assert recording is not None, "No continuous gripper transition was observed"
    recording.require_continuous()

    controller = GUIStepController(uuid4().hex)
    controller.initialize()
    controller.signal_play()
    managed = cast(
        waldoctl.RobotClient,
        AsyncSteppingClientWrapper(client, StepIO(controller.session_id)),
    )
    task = None
    try:
        task = asyncio.create_task(
            replay_demonstration.async_call(
                managed,
                recording,
                replay_gripper=True,
                timeout=2,
            )
        )
        if not await client.wait_status(lambda s: bool(s.action_current), timeout=5):
            if task.done():
                await (
                    task
                )  # Surface a replay refusal instead of hiding it as missing status.
            pytest.fail("Replay did not publish an active command before the pause")
        controller.signal_pause()
        assert await client.pause() == 1
        await asyncio.sleep(2.5)
        assert not task.done(), "Replay completion expired during managed pause"
        assert await client.resume() == 1
        controller.signal_play()
        result = await asyncio.wait_for(task, 10)
        assert result.completed_samples == len(recording.samples)
        assert result.completed_tool_positions >= 2
        assert await client.wait_status(
            lambda s: s.tool_status.positions[0] == 1, timeout=3
        )

        # Standalone replay, as documented: a fresh client that never called
        # select_tool() replays on the tool the controller already carries.
        async with type(client)(host=client.host, port=client.port) as fresh:
            standalone = await replay_demonstration.async_call(
                cast(waldoctl.RobotClient, fresh), recording, timeout=2
            )
            assert standalone.completed_samples == len(recording.samples)
            with pytest.raises(MissingCapability, match="select_tool"):
                await replay_demonstration.async_call(
                    cast(waldoctl.RobotClient, fresh), recording, replay_gripper=True
                )

        task = asyncio.create_task(replay_demonstration.async_call(managed, recording))
        if not await client.wait_status(lambda s: bool(s.action_current), timeout=5):
            if task.done():
                await (
                    task
                )  # Surface a replay refusal instead of hiding it as missing status.
            pytest.fail("Replay did not publish an active command before the pause")
        controller.signal_pause()
        assert await client.pause() == 1
        await client.estop()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, 3)
        assert await client.queue() == []
    finally:
        controller.signal_play()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.stop()
        await client.reset()
        await client.resume()
        index = await client.select_tool("NONE")
        assert await client.wait_command(index, timeout=5)
        controller.cleanup()
