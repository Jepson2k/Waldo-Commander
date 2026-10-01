"""Observed motion survives export and capture ends on controller disable."""

import asyncio
import textwrap
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import cast
from types import SimpleNamespace
from uuid import uuid4

import pytest
import waldoctl
import numpy as np
from waldoctl.recordings import Demonstration, RecordedSample
from waldoctl.skills import MissingCapability, SkillError

from waldo_commander.demonstrations import (
    load_demonstration,
    record_demonstration,
    save_demonstration,
    span_to_lines,
    to_program,
)
from waldo_commander.skills import replay_demonstration
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.stepping_client import (
    AsyncSteppingClientWrapper,
    GUIStepController,
    StepIO,
)


@asynccontextmanager
async def _ready_client(robot):
    """A client of the session controller, which it leaves simulated, enabled
    and homed at full speed, as an app start would."""
    async with robot.create_async_client() as client:
        await client.simulator(True)
        await client.reset_state()
        await client.reset()
        assert await client.resume() == 1
        assert await client.set_execution_speed(1) == 1
        assert await client.home(wait=True, timeout=10.0) >= 0
        yield client


@pytest.mark.integration
async def test_observed_motion_records_cadence_gaps_and_controller_loss(
    session_controller, tmp_path, monkeypatch
):
    """A recording keeps the controller's cadence and its gaps, round-trips
    through a file, refuses replay across a gap, a start-pose mismatch or a
    new controller session, previews and replays; it ends as a disconnect
    when the wire goes quiet near its end, and on an estop."""
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    async with _ready_client(session_controller) as client:
        first = asyncio.Event()
        task = asyncio.create_task(
            record_demonstration(
                client, duration_s=2, on_sample=lambda sample: first.set()
            )
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
                recording.samples[-1].joints_deg[0] - recording.samples[0].joints_deg[0]
                > 2
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
            preview = _run_simulation_isolated(source, np.radians(first_joints))
            assert preview["error"] is None, preview["error"]
            assert any(
                b.move_type is not None and b.rows > 0
                for b in preview["commanded"].blocks
            )
            result = await replay_demonstration.async_call(client, recording)
            assert result.completed_samples == len(recording.samples)
            assert await client.angles() == pytest.approx(
                recording.samples[-1].joints_deg, abs=0.5
            )

            # A controller that stops publishing inside the last stale window ended
            # the capture as a clean `duration_limit`, so the file and the panel
            # claimed a complete recording whose final seconds were never
            # observed. Which limit was reached is the clock's answer: how long
            # the wire has been quiet.
            live = client.stream_status
            frames = 44  # ~2.2 s at the suite's 20 Hz status rate

            async def stalls_after_a_while():
                seen = 0
                async for status in live():
                    yield status
                    seen += 1
                    if seen >= frames:
                        await asyncio.sleep(60)  # the wire goes quiet, mid-capture

            with monkeypatch.context() as stalled:
                stalled.setattr(client, "stream_status", stalls_after_a_while)
                # The silence starts inside the final stale window, so the wait is
                # cut short by the duration rather than by the stale timeout.
                quiet = await record_demonstration(
                    client, duration_s=3.0, stale_timeout_s=1.0, gap_threshold_s=0.2
                )
            assert quiet.ended == "disconnected", (
                f"the capture lost the controller with {3.0 - quiet.duration_s:.1f} s "
                f"left and reported {quiet.ended}"
            )
            # Every frame the wire delivered was kept: the first is the baseline the
            # capture compares against rather than a sample of its own.
            assert len(quiet.samples) == frames - 1

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
async def test_gripper_recording_replays_with_its_tool_and_standalone(
    session_controller,
):
    """A gripper transition replays through the managed program client with
    its tool positions; a fresh client that never selected a tool replays the
    motion on the tool the controller carries, and refuses to replay the
    gripper without one."""
    async with _ready_client(session_controller) as client:
        index = await client.select_tool("PNEUMATIC")
        assert await client.wait_command(index, timeout=5)
        assert await client.wait_status(
            lambda s: s.tool_status.key == "PNEUMATIC", timeout=3
        )
        controller = GUIStepController(uuid4().hex)
        controller.initialize()
        controller.signal_play()
        try:
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
            assert recording is not None, (
                "No continuous gripper transition was observed"
            )
            recording.require_continuous()

            managed = cast(
                waldoctl.RobotClient,
                AsyncSteppingClientWrapper(client, StepIO(controller.session_id)),
            )
            result = await asyncio.wait_for(
                replay_demonstration.async_call(
                    managed, recording, replay_gripper=True, timeout=2
                ),
                10,
            )
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
                        cast(waldoctl.RobotClient, fresh),
                        recording,
                        replay_gripper=True,
                    )
        finally:
            controller.signal_play()
            controller.cleanup()
            await client.stop()
            index = await client.select_tool("NONE")
            assert await client.wait_command(index, timeout=5)


@pytest.mark.integration
async def test_a_recorded_sequence_converts_to_moves_and_replays_what_it_cannot(
    session_controller, tmp_path, monkeypatch
):
    """Observed motion becomes an ordinary program: moves where the planner
    reproduces the recorded path, delays where the arm waited, and a replay
    call over a span it cannot. A wrist that swings out and back while the
    tool point barely moves is not a short move to where it ended, and a
    recording from another backend is not converted for this one."""
    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    robot = session_controller
    async with _ready_client(session_controller) as client:
        first = asyncio.Event()
        stop = asyncio.Event()
        task = asyncio.create_task(
            record_demonstration(
                client, duration_s=20, stop=stop, on_sample=lambda sample: first.set()
            )
        )
        try:
            await asyncio.wait_for(first.wait(), 5)
            start = await client.angles()
            assert start is not None
            # Three legs with a hold between them: a shoulder swing, a lift, and a
            # return. The holds are what the conversion reads as waypoints.
            legs = []
            for axis, delta in ((0, 6.0), (2, -5.0), (0, -6.0)):
                target = list(start)
                target[axis] += delta
                start = target
                legs.append(target)
                index = await client.move_j(target, duration=1.0)
                assert await client.wait_command(index, timeout=10)
                await asyncio.sleep(0.6)
            # The capture outlasts the demonstration by a moment.
            await asyncio.sleep(0.4)
            stop.set()
            recording = await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    # Real status streams drop publications under load; convert a measured
    # continuous span rather than erasing the gaps.
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
    assert recording.duration_s > 1.0, "need a span with motion in it to convert"

    path = tmp_path / "demonstration.json"
    save_demonstration(path, recording)
    conversion = to_program(recording, robot, name="converted", source_path=path)
    assert not conversion.replayed, conversion.summary()
    assert conversion.position_error_mm <= 5.0
    assert conversion.orientation_error_deg <= 2.0

    # Each hold between moves became a delay of its observed length, and the
    # hold at the end of the capture did not.
    still = sum(
        (b.observed_ns - a.observed_ns) / 1e9
        for a, b in zip(recording.samples, recording.samples[1:])
        if max(abs(x - y) for x, y in zip(a.joints_deg, b.joints_deg)) <= 0.05
    )
    delays = [span for span in conversion.spans if span.kind == "delay"]
    delayed = sum(span.seconds for span in delays)
    trailing = max(delays, key=lambda span: span.stop)
    assert trailing.stop == len(recording.samples) - 1, (
        "the capture outlasted the demonstration; its hold is the last span"
    )
    assert trailing.seconds == 0.0, "the trailing hold is a comment, not a delay"
    assert delayed < still, "the trailing hold is a comment, not a delay"
    assert "rbt.delay(" in conversion.source
    assert "before or after the demonstration" in conversion.source
    moves = [s for s in conversion.spans if s.kind in ("move_j", "move_l")]
    assert moves, conversion.source

    # The generated program plans through the real preview, and its last
    # position is where the demonstration ended.
    preview = _run_simulation_isolated(
        conversion.source, np.radians(recording.samples[0].joints_deg)
    )
    assert preview["error"] is None, preview["error"]
    planned = [b for b in preview["commanded"].blocks if b.move_type is not None]
    assert len(planned) >= len(moves)
    final = preview["final_joints_rad"]
    assert np.degrees(final) == pytest.approx(recording.samples[-1].joints_deg, abs=0.5)

    # A tolerance the planner cannot meet keeps the observations instead.
    strict = to_program(
        recording, robot, name="converted", source_path=path, tolerance_mm=1e-6
    )
    assert (
        strict.replayed
        and "replay_demonstration(rbt, recording.select(" in strict.source
    )
    assert "load_demonstration" in strict.source
    with pytest.raises(ValueError, match="save the recording"):
        to_program(recording, robot, name="converted", tolerance_mm=1e-6)

    # The same span as lines for a program being recorded: no program around
    # them, and a piece the planner cannot follow replayed from a copy the
    # converter saves itself.
    captures = tmp_path / "captures"
    lines = span_to_lines(recording, robot, program="bench", directory=captures)
    assert not lines.replayed and lines.source.startswith("rbt.")
    assert all(
        line in lines.source
        for span in conversion.spans[1:]
        for line in span.lines
        if line.startswith("rbt.")
    ), "the lines differ from what the same span converts to as a program"
    assert not captures.exists(), "nothing needed the recording saved"

    # Publications missing mid-move: the lines take the arm across the stretch
    # nobody observed with a planned move that says so, rather than starting
    # the next piece from wherever the last one left it.
    moving = next(
        n
        for n, sample in enumerate(recording.samples)
        if abs(sample.joints_deg[0] - recording.samples[0].joints_deg[0]) > 2.0
    )
    gapped = replace(
        recording,
        samples=recording.samples[:moving] + recording.samples[moving + 4 :],
    )
    assert gapped.gaps
    bridged = span_to_lines(
        gapped, robot, program="bench", directory=tmp_path / "gapped"
    )
    assert "# not observed" in bridged.source, bridged.source
    preview = _run_simulation_isolated(
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n"
        + textwrap.indent(bridged.source, "    ")
        + "\n",
        np.radians(gapped.samples[0].joints_deg),
    )
    assert preview["error"] is None, preview["error"]
    final = preview["final_joints_rad"]
    assert np.degrees(final) == pytest.approx(gapped.samples[-1].joints_deg, abs=0.5)

    strict_lines = span_to_lines(
        recording, robot, program="bench", directory=captures, tolerance_mm=1e-6
    )
    assert strict_lines.replayed
    saved = list(captures.glob("bench-*.json"))
    assert len(saved) == 1 and load_demonstration(saved[0]) == recording
    assert (
        f"replay_demonstration(rbt, load_demonstration({str(saved[0])!r}).select("
        in strict_lines.source
    )

    # Kept as recorded, every motion replays from the copy already saved;
    # holds are still waits.
    raw = span_to_lines(
        recording,
        robot,
        program="bench",
        directory=captures,
        as_recorded=True,
        recording_path=strict_lines.recording_path,
    )
    kinds = {span.kind for span in raw.spans}
    assert "replay" in kinds and not kinds & {"move_l", "move_j"}, kinds
    assert raw.recording_path == saved[0]
    assert list(captures.glob("bench-*.json")) == saved, "no second copy is saved"

    # Two captures converted within the same second are two files: the
    # second must not take the first one's, which replay lines already name.
    import time

    captures2 = tmp_path / "captures2"
    with monkeypatch.context() as patched:
        patched.setattr(time, "strftime", lambda fmt, *args: "20260924-120000")
        first = span_to_lines(
            recording, robot, program="bench", directory=captures2, tolerance_mm=1e-6
        )
        second = span_to_lines(
            recording, robot, program="bench", directory=captures2, tolerance_mm=1e-6
        )
    assert first.recording_path != second.recording_path, (
        "a second capture in the same second took the first one's file"
    )
    assert load_demonstration(first.recording_path) == recording
    assert load_demonstration(second.recording_path) == recording

    # A wrist that swings out and back while the tool point barely moves:
    # J6 out 60° and back to 1°, with J5 nudging the tool point under a
    # millimetre so it is never quite still in space.
    swing = np.concatenate([np.linspace(0.0, 60.0, 31), np.linspace(60.0, 1.0, 31)[1:]])
    nudge = 0.6 * np.sin(np.linspace(0.0, np.pi, len(swing)))
    samples = tuple(
        RecordedSample(
            seq=n + 1,
            observed_ns=1_000_000_000 + n * 50_000_000,
            received_ns=1_000_000_000 + n * 50_000_000,
            joints_deg=(90.0, -90.0, 180.0, 0.0, 30.0 + float(j5), 180.0 + float(j6)),
        )
        for n, (j5, j6) in enumerate(zip(nudge, swing))
    )
    swung = Demonstration(
        backend=robot.backend_package,
        session_id=1,
        simulator=True,
        tcp_transform=(0.0,) * 6,
        requested_rate_hz=20.0,
        gap_threshold_s=0.2,
        ended="stopped",
        samples=samples,
    )
    lines = span_to_lines(swung, robot, program="swing", directory=tmp_path)
    preview = _run_simulation_isolated(
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n"
        + textwrap.indent(lines.source, "    ")
        + "\n",
        np.radians(samples[0].joints_deg),
    )
    assert preview["error"] is None, preview["error"]
    wrist = float(np.degrees(preview["commanded"].joints_rad[:, 5]).max())
    assert wrist >= 235.0, (
        f"the converted lines never swing the wrist out ({wrist:.1f}°):\n{lines.source}"
    )

    elsewhere = replace(swung, backend="par6")
    with pytest.raises(ValueError, match="belongs to par6"):
        span_to_lines(elsewhere, robot, program="swing", directory=tmp_path)
    with pytest.raises(ValueError, match="belongs to par6"):
        to_program(elsewhere, robot, name="swing")
