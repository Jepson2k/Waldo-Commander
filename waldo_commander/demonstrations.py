"""Capture and store controller observations without starting robot motion."""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from waldoctl.client import RobotClient
from waldoctl.robot import Robot
from waldoctl.setup import validate_name
from waldoctl.recordings import (
    MAX_RECORDING_SAMPLES,
    Demonstration,
    RecordedSample,
    RecordedTool,
    RecordingEnd,
)
from waldoctl.status import StatusBuffer

MAX_RECORDING_BYTES = 64 * 1024 * 1024


def _sample(status: StatusBuffer) -> RecordedSample:
    tool = status.tool_status
    observation = None
    if tool is not None and getattr(status, "tool_status_present", True):
        observation = RecordedTool(
            key=tool.key,
            variant_key=tool.variant_key,
            positions=tuple(float(v) for v in tool.positions),
            engaged=bool(tool.engaged),
            part_detected=bool(tool.part_detected),
            fault_code=int(tool.fault_code),
            state=int(tool.state),
            channels=tuple(float(v) for v in tool.channels),
        )
    return RecordedSample(
        seq=status.seq,
        observed_ns=status.mono_time_ns,
        received_ns=time.monotonic_ns(),
        joints_deg=tuple(float(v) for v in status.angles),
        tool=observation,
    )


def _tool_identity(sample: RecordedSample) -> tuple[str, str] | None:
    return (sample.tool.key, sample.tool.variant_key) if sample.tool else None


async def record_demonstration(
    client: RobotClient,
    *,
    duration_s: float = 30.0,
    stop: asyncio.Event | None = None,
    gap_threshold_s: float = 0.2,
    stale_timeout_s: float = 2.0,
    max_samples: int = MAX_RECORDING_SAMPLES,
    on_sample: Callable[[RecordedSample], None] | None = None,
) -> Demonstration:
    """Record fresh controller snapshots; disconnect/reference loss ends capture.

    Receipt times mark delivery to this recorder, including scheduling delay.
    The controller snapshot timestamps and publication sequence preserve the
    source cadence. ``stop`` ends acquisition only; it sends no robot command.
    """
    for value in (duration_s, gap_threshold_s, stale_timeout_s):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("Recording durations must be positive and finite")
    if type(max_samples) is not int or not 1 <= max_samples <= MAX_RECORDING_SAMPLES:
        raise ValueError("Invalid recording sample limit")
    caps = client.skill_capabilities
    backends = [c.removeprefix("backend.") for c in caps if c.startswith("backend.")]
    if "observation.timed" not in caps or len(backends) != 1:
        raise ValueError("This client does not provide identified, timed observations")

    stream = client.stream_status()
    samples: list[RecordedSample] = []
    ended: RecordingEnd = "duration_limit"
    try:
        async with asyncio.timeout(stale_timeout_s):
            rate = await client.status_rate()
            tcp = await client.tcp_transform()
            if rate is None or tcp is None:
                raise ConnectionError("Recording setup readback is unavailable")
            baseline = await anext(stream)
            while True:
                status = await anext(stream)
                if (
                    status.session_id != baseline.session_id
                    or status.seq > baseline.seq
                ):
                    break
        if not status.session_id or not status.mono_time_ns:
            raise ConnectionError("The controller did not supply recording timestamps")
        if not status.enabled or not status.homed:
            raise RuntimeError("Recording requires an enabled, referenced controller")
        session_id = status.session_id
        simulator = status.simulator_active
        first = _sample(status)
        samples.append(first)
        started = time.monotonic()
        if on_sample:
            on_sample(first)

        while len(samples) < max_samples:
            if stop is not None and stop.is_set():
                ended = "stopped"
                break
            remaining = duration_s - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(min(stale_timeout_s, remaining)):
                    status = await anext(stream)
            except TimeoutError:
                # How long the wire has been quiet decides this, not which of
                # the two limits the wait happened to be cut short by. A
                # controller that stops publishing inside the final stale window
                # used to be reported as a clean end, so the file and the panel
                # claimed a complete recording whose last seconds -- tens of
                # missed publications -- were never observed. The threshold is
                # the recording's own notion of a gap needing reconciliation.
                silent_for = time.monotonic() - samples[-1].received_ns / 1e9
                ended = (
                    "stopped"
                    if stop is not None and stop.is_set()
                    else "disconnected"
                    if silent_for >= min(stale_timeout_s, gap_threshold_s)
                    else "duration_limit"
                )
                break
            except (OSError, RuntimeError, StopAsyncIteration):
                ended = "disconnected"
                break
            if status.session_id != session_id:
                ended = "session_changed"
                break
            if not status.homed:
                ended = "reference_lost"
                break
            if not status.enabled:
                ended = "disabled"
                break
            if status.simulator_active != simulator:
                ended = "source_changed"
                break
            try:
                sample = _sample(status)
            except ValueError:
                ended = "invalid_observation"
                break
            previous = samples[-1]
            if (
                sample.seq <= previous.seq
                or sample.observed_ns <= previous.observed_ns
                or len(sample.joints_deg) != len(previous.joints_deg)
            ):
                ended = "invalid_observation"
                break
            if _tool_identity(sample) != _tool_identity(first):
                ended = "tool_changed"
                break
            samples.append(sample)
            if on_sample:
                on_sample(sample)
        else:
            ended = "sample_limit"
        return Demonstration(
            backend=backends[0],
            session_id=session_id,
            simulator=simulator,
            tcp_transform=tuple(tcp),
            requested_rate_hz=rate.hz,
            gap_threshold_s=gap_threshold_s,
            ended=ended,
            samples=tuple(samples),
        )
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


def encode_demonstration(recording: Demonstration) -> bytes:
    """The recording's portable bytes, refusing one too large to reload.

    One encoder for the saved file and the panel's export: a schema bump in one
    of them would otherwise leave the other writing documents this build cannot
    read back, and the export used to stream a payload the loader would refuse.
    """
    data = json.dumps({"schema": 1, **asdict(recording)}, allow_nan=False).encode(
        "utf-8"
    )
    if len(data) > MAX_RECORDING_BYTES:
        raise ValueError("Recording exceeds the portable file size limit")
    return data


def save_demonstration(path: str | Path, recording: Demonstration) -> None:
    """Atomically save the explicit recording to a selected path."""
    data = encode_demonstration(recording)
    path = Path(path)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = output.name
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def load_demonstration(path: str | Path) -> Demonstration:
    """Load observations only; this never connects to or configures a robot."""
    with Path(path).open("rb") as source:
        data = source.read(MAX_RECORDING_BYTES + 1)
    if len(data) > MAX_RECORDING_BYTES:
        raise ValueError("Recording exceeds the portable file size limit")
    try:
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("Missing recording schema")
        schema = raw.pop("schema", None)
        if type(schema) is not int or schema != 1:
            raise ValueError("Unsupported recording schema")
        rows = raw.pop("samples")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_RECORDING_SAMPLES:
            raise ValueError("Invalid recording sample list")
        samples = []
        for row in rows:
            tool = row.pop("tool")
            samples.append(
                RecordedSample(
                    **row, tool=RecordedTool(**tool) if tool is not None else None
                )
            )
        return Demonstration(**raw, samples=tuple(samples))
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Invalid recording file") from error


# --- Conversion to an ordinary program ---------------------------------------

#: An encoder at rest wanders less than this between publications (degrees).
STILL_DEG = 0.05
#: A span whose path stays this close to its chord is one linear move (mm).
STRAIGHT_MM = 3.0
BLEND_MM = (1.0, 10.0)
#: Rows compared per span; a 50 Hz recording of a long span is decimated.
COMPARE_ROWS = 400


@dataclass(frozen=True)
class ConvertedSpan:
    """One statement group in the generated program and what it came from."""

    kind: Literal["approach", "move_l", "move_j", "delay", "tool", "replay"]
    start: int
    stop: int
    seconds: float
    lines: tuple[str, ...]
    waypoints: int = 0
    position_error_mm: float = 0.0
    orientation_error_deg: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class Conversion:
    """A program, and how far its previewed path sits from the recording.

    Errors are measured between the recorded joint path and the path the
    planner produces for the generated statements, both through the same
    forward kinematics, so the currently applied tool transform cancels out
    of the comparison.
    """

    source: str
    spans: tuple[ConvertedSpan, ...]
    position_error_mm: float
    orientation_error_deg: float
    recorded_duration_s: float
    planned_duration_s: float

    @property
    def replayed(self) -> tuple[ConvertedSpan, ...]:
        return tuple(span for span in self.spans if span.kind == "replay")

    def summary(self) -> str:
        moves = sum(1 for s in self.spans if s.kind in ("move_l", "move_j"))
        text = (
            f"{moves} moves from {self.recorded_duration_s:.1f} s of observations; "
            f"path within {self.position_error_mm:.1f} mm and "
            f"{self.orientation_error_deg:.1f}°, planned "
            f"{self.planned_duration_s:.1f} s"
        )
        if self.replayed:
            spans = ", ".join(f"{s.start}–{s.stop}" for s in self.replayed)
            text += f"; replayed as recorded: samples {spans}"
        return text


def _point_to_segment_mm(
    points: NDArray[np.float64],
    start: NDArray[np.float64],
    end: NDArray[np.float64],
) -> NDArray[np.float64]:
    span = end - start
    length = float(np.linalg.norm(span))
    if length < 1e-9:
        return np.linalg.norm(points - start, axis=1)
    t = np.clip((points - start) @ span / (length * length), 0.0, 1.0)
    return np.linalg.norm(points - (start + t[:, None] * span), axis=1)


def _distance_to_path(points: NDArray[np.float64], path: NDArray[np.float64]):
    if len(path) == 1:
        return np.linalg.norm(points - path[0], axis=1)
    best = np.full(len(points), np.inf)
    for index in range(len(path) - 1):
        best = np.minimum(
            best, _point_to_segment_mm(points, path[index], path[index + 1])
        )
    return best


def _path_gap_mm(first: NDArray[np.float64], second: NDArray[np.float64]) -> float:
    """The worst distance from either path to the other (mm).

    Both directions, because one path staying near the other is not the same
    claim: a generated move that cuts a corner is near the recording
    everywhere it goes, while the corner it skipped is near nothing.
    """
    return max(
        float(_distance_to_path(first, second).max()),
        float(_distance_to_path(second, first).max()),
    )


def _orientation_gap_deg(
    planned: NDArray[np.float64], recorded: NDArray[np.float64]
) -> float:
    """Worst per-axis orientation difference at the nearest recorded point.

    Per axis rather than as one rotation angle: it needs no convention for
    the triple, and it never under-reports the rotation it stands for.
    """
    distance = np.linalg.norm(planned[:, None, :3] - recorded[None, :, :3], axis=2)
    nearest = recorded[distance.argmin(axis=1), 3:]
    delta = (planned[:, 3:] - nearest + 180.0) % 360.0 - 180.0
    return float(np.abs(delta).max())


def _simplify(points: NDArray[np.float64], tolerance_mm: float) -> list[int]:
    """Douglas–Peucker: the indices a polyline within *tolerance_mm* keeps."""
    if len(points) <= 2:
        return list(range(len(points)))
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        first, last = pending.pop()
        if last - first < 2:
            continue
        inner = points[first + 1 : last]
        distance = _point_to_segment_mm(inner, points[first], points[last])
        offset = int(distance.argmax())
        if distance[offset] > tolerance_mm:
            split = first + 1 + offset
            keep.add(split)
            pending.extend(((first, split), (split, last)))
    return sorted(keep)


def _decimate(rows: NDArray[np.float64], limit: int = COMPARE_ROWS):
    if len(rows) <= limit:
        return rows
    step = int(np.ceil(len(rows) / limit))
    kept = rows[::step]
    return kept if np.array_equal(kept[-1], rows[-1]) else np.vstack([kept, rows[-1]])


def _poses(robot: Robot, joints_deg: NDArray[np.float64]) -> NDArray[np.float64]:
    """Tool poses for joint rows, in the program's units (mm and degrees)."""
    poses = np.asarray(robot.fk_batch(np.radians(np.atleast_2d(joints_deg))), float)
    out = poses.copy()
    out[:, :3] *= 1000.0
    out[:, 3:] = np.degrees(out[:, 3:])
    return out


def _numbers(values) -> str:
    return ", ".join(f"{float(v):.3f}" for v in values)


def _dwells(recording: Demonstration, dwell_s: float) -> list[tuple[int, int]]:
    """Sample ranges the arm held still for at least *dwell_s*, inclusive."""
    samples = recording.samples
    held: list[tuple[int, int]] = []
    index = 1
    while index < len(samples):
        if (
            max(
                abs(a - b)
                for a, b in zip(
                    samples[index].joints_deg, samples[index - 1].joints_deg
                )
            )
            > STILL_DEG
        ):
            index += 1
            continue
        start = index - 1
        while (
            index < len(samples)
            and max(
                abs(a - b)
                for a, b in zip(
                    samples[index].joints_deg, samples[index - 1].joints_deg
                )
            )
            <= STILL_DEG
        ):
            index += 1
        stop = index - 1
        seconds = (samples[stop].observed_ns - samples[start].observed_ns) / 1e9
        if seconds >= dwell_s:
            held.append((start, stop))
    return held


def _tool_position(sample: RecordedSample) -> float | None:
    """The gripper's normalized position, when the tool reports one."""
    if sample.tool is None or not sample.tool.positions:
        return None
    return float(sample.tool.positions[0])


def _seconds(recording: Demonstration, start: int, stop: int) -> float:
    samples = recording.samples
    return (samples[stop].observed_ns - samples[start].observed_ns) / 1e9


def _joint_gap_deg(
    planned_joints: NDArray[np.float64],
    planned_poses: NDArray[np.float64],
    recorded_joints: NDArray[np.float64],
    recorded_poses: NDArray[np.float64],
) -> float:
    """Worst per-joint difference between the planned and recorded postures.

    The tool pose is not the arm: a Cartesian move can trace the recorded path
    exactly through a flipped wrist, which is a different machine posture and
    a different sweep through the cell. Postures are matched at the nearest
    recorded tool position, and the final posture is compared outright.
    """
    distance = np.linalg.norm(
        planned_poses[:, None, :3] - recorded_poses[None, :, :3], axis=2
    )
    nearest = recorded_joints[distance.argmin(axis=1)]
    delta = (planned_joints - nearest + 180.0) % 360.0 - 180.0
    ending = (planned_joints[-1] - recorded_joints[-1] + 180.0) % 360.0 - 180.0
    return max(float(np.abs(delta).max()), float(np.abs(ending).max()))


def _candidates(
    recording: Demonstration,
    robot: Robot,
    start: int,
    stop: int,
) -> list[tuple[str, tuple[str, ...], int]]:
    """Ways to write one motion span, simplest first.

    A span whose tool path stays within ``STRAIGHT_MM`` of its chord is the
    straight line the demonstrator drew, and says so as a ``move_l``. Failing
    that -- or failing verification -- the joint positions it was recorded at,
    decimated to the waypoints that hold the path and blended so the arm does
    not stop at each one.
    """
    joints = np.array([recording.samples[i].joints_deg for i in range(start, stop + 1)])
    poses = _poses(robot, joints)
    seconds = _seconds(recording, start, stop)
    options: list[tuple[str, tuple[str, ...], int]] = []
    if len(_simplify(poses[:, :3], STRAIGHT_MM)) == 2:
        options.append(
            (
                "move_l",
                (f"rbt.move_l([{_numbers(poses[-1])}], duration={seconds:.3f})",),
                1,
            )
        )
    legs = [start + index for index in _simplify(poses[:, :3], STRAIGHT_MM / 3.0)]
    lengths = [
        float(np.linalg.norm(poses[b - start, :3] - poses[a - start, :3]))
        for a, b in zip(legs, legs[1:])
    ]
    blend = min(BLEND_MM[1], 0.25 * min(lengths)) if lengths else 0.0
    lines = []
    for number, (first, last) in enumerate(zip(legs, legs[1:])):
        target = recording.samples[last].joints_deg
        leg_s = _seconds(recording, first, last)
        final = number == len(legs) - 2
        tail = "" if final or blend < BLEND_MM[0] else f", r={blend:.1f}, wait=False"
        lines.append(f"rbt.move_j([{_numbers(target)}], duration={leg_s:.3f}{tail})")
    options.append(("move_j", tuple(lines), len(legs) - 1))
    return options


def _probe(
    robot: Robot, lines: tuple[str, ...], start_joints_deg
) -> tuple[NDArray[np.float64], float, str]:
    """Plan *lines* in a preview from *start_joints_deg*; return its joint path.

    The preview runs the backend's own planner, so the path compared against
    the recording is the one the controller would drive, not an interpolation
    of the generated waypoints.
    """
    from waldo_commander.services.path_preview_client import PathPreviewClient

    client = PathPreviewClient(
        dry_run_client_cls=lambda **kwargs: robot.create_dry_run_client(**kwargs),
        initial_joints=np.radians(start_joints_deg),
    )
    try:
        exec(
            compile("\n".join(lines), "converted_demonstration.py", "exec"),
            {"rbt": client},
        )
        client.close()
    except Exception as error:  # a refusal is a verdict, not a crash
        return (
            np.empty((0, len(start_joints_deg))),
            0.0,
            f"{type(error).__name__}: {error}",
        )
    rows: list[list[float]] = []
    planned = 0.0
    for segment in client.segment_collector:
        planned += float(segment.get("estimated_duration") or 0.0)
        trajectory = segment.get("joint_trajectory")
        if trajectory:
            rows.extend(trajectory)
    if client.accumulated_errors:
        return (
            np.empty((0, len(start_joints_deg))),
            planned,
            "; ".join(client.accumulated_errors),
        )
    return np.degrees(np.array(rows, dtype=float)), planned, ""


def to_program(
    recording: Demonstration,
    robot: Robot,
    *,
    name: str = "demonstration",
    source_path: str | Path | None = None,
    dwell_s: float = 0.3,
    tolerance_mm: float = 5.0,
    tolerance_deg: float = 2.0,
) -> Conversion:
    """Convert an uninterrupted demonstration into an ordinary Python program.

    The arm holding still separates the recording into moves: each still span
    of at least *dwell_s* becomes a ``delay``, a gripper position it changed
    in becomes a ``tool.set_position``, and the motion between them becomes
    one ``move_l`` where the tool travelled in a straight line, or the joint
    waypoints that hold its path otherwise.

    Every motion span is planned in the backend's preview and compared with
    the path it was recorded at. A span the planner cannot reproduce within
    *tolerance_mm* and *tolerance_deg* keeps its observations instead: it
    becomes a ``replay_demonstration`` call over that sample range, which
    needs *source_path* — the saved recording the program will load.
    """
    recording.require_continuous()
    for value, label in (
        (dwell_s, "Dwell"),
        (tolerance_mm, "Position tolerance"),
        (tolerance_deg, "Orientation tolerance"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{label} must be a positive finite number")
    validate_name(name)
    samples = recording.samples
    if len(samples) < 2:
        raise ValueError("A conversion needs at least two observations")

    held = _dwells(recording, dwell_s)
    spans: list[ConvertedSpan] = []
    position_error = 0.0
    orientation_error = 0.0
    planned_total = 0.0
    tool_position = _tool_position(samples[0])
    needs_tool = False

    first = samples[0].joints_deg
    spans.append(
        ConvertedSpan(
            kind="approach",
            start=0,
            stop=0,
            seconds=0.0,
            lines=(
                "# The demonstration starts here; this approach is not part of it.",
                f"rbt.move_j([{_numbers(first)}], speed=0.1)",
            ),
        )
    )

    def motion(start: int, stop: int) -> None:
        nonlocal position_error, orientation_error, planned_total
        if stop <= start:
            return
        recorded_joints = np.array(
            [samples[i].joints_deg for i in range(start, stop + 1)]
        )
        recorded = _decimate(_poses(robot, recorded_joints))
        recorded_decimated = _decimate(recorded_joints)
        reasons = []
        for kind, lines, waypoints in _candidates(recording, robot, start, stop):
            path, planned, failure = _probe(robot, lines, samples[start].joints_deg)
            if failure or not len(path):
                reasons.append(f"{kind}: {failure or 'planned no motion'}")
                continue
            planned_poses = _decimate(_poses(robot, path))
            planned_joints = _decimate(path)
            gap_mm = _path_gap_mm(planned_poses[:, :3], recorded[:, :3])
            gap_deg = _orientation_gap_deg(planned_poses, recorded)
            gap_joint = _joint_gap_deg(
                planned_joints, planned_poses, recorded_decimated, recorded
            )
            if (
                gap_mm > tolerance_mm
                or gap_deg > tolerance_deg
                or gap_joint > tolerance_deg
            ):
                reasons.append(
                    f"{kind}: {gap_mm:.1f} mm, {gap_deg:.1f}° and {gap_joint:.1f}° "
                    f"per joint from the recording"
                )
                continue
            position_error = max(position_error, gap_mm)
            orientation_error = max(orientation_error, max(gap_deg, gap_joint))
            planned_total += planned
            spans.append(
                ConvertedSpan(
                    kind=kind,
                    start=start,
                    stop=stop,
                    seconds=_seconds(recording, start, stop),
                    lines=lines,
                    waypoints=waypoints,
                    position_error_mm=gap_mm,
                    orientation_error_deg=max(gap_deg, gap_joint),
                )
            )
            return
        reason = "; ".join(reasons)
        if source_path is None:
            raise ValueError(
                f"Samples {start}–{stop} cannot be expressed as moves "
                f"({reason}); save the recording so the program can replay them"
            )
        spans.append(
            ConvertedSpan(
                kind="replay",
                start=start,
                stop=stop,
                seconds=_seconds(recording, start, stop),
                lines=(
                    f"replay_demonstration(rbt, recording.select({start}, {stop + 1}))",
                ),
                reason=reason,
            )
        )

    cursor = 0
    for number, (start, stop) in enumerate(held):
        motion(cursor, start)
        lines: list[str] = []
        for index in range(start, stop + 1):
            position = _tool_position(samples[index])
            if position is not None and (
                tool_position is None or abs(position - tool_position) > 1e-6
            ):
                lines.append(f"rbt.tool.set_position({position:.3f})")
                tool_position = position
                needs_tool = True
        seconds = _seconds(recording, start, stop)
        edge = (start == 0 and not lines) or (
            stop == len(samples) - 1 and number == len(held) - 1 and not lines
        )
        if lines:
            spans.append(
                ConvertedSpan(
                    kind="tool",
                    start=start,
                    stop=stop,
                    seconds=seconds,
                    lines=tuple(lines),
                )
            )
        # A hold at either end is the operator starting or stopping the
        # capture, not something the demonstration asked the arm to do; the
        # program says so rather than waiting it out.
        spans.append(
            ConvertedSpan(
                kind="delay",
                start=start,
                stop=stop,
                seconds=0.0 if edge else seconds,
                lines=(
                    (
                        f"# The recording holds still for {seconds:.1f} s here, "
                        f"before or after the demonstration.",
                    )
                    if edge
                    else (f"rbt.delay({seconds:.3f})",)
                ),
            )
        )
        if not edge:
            planned_total += seconds
        cursor = stop
    motion(cursor, len(samples) - 1)

    source = _program_source(
        recording,
        spans,
        name=name,
        source_path=source_path,
        needs_tool=needs_tool,
    )
    compile(source, f"{name}.py", "exec")
    return Conversion(
        source=source,
        spans=tuple(spans),
        position_error_mm=position_error,
        orientation_error_deg=orientation_error,
        recorded_duration_s=recording.duration_s,
        planned_duration_s=planned_total,
    )


def _program_source(
    recording: Demonstration,
    spans: list[ConvertedSpan],
    *,
    name: str,
    source_path: str | Path | None,
    needs_tool: bool,
) -> str:
    replayed = [span for span in spans if span.kind == "replay"]
    identity = recording.samples[0].tool
    header = [
        f'"""{name}: {recording.duration_s:.1f} s of recorded motion as a program.',
        "",
        "Waldo Commander generated this from recorded joint positions and checked",
        "each move against them in the backend's preview. Read it before running:",
        "the arm moves to the demonstration's first position at the start.",
        '"""',
        "",
        f"from {recording.backend} import RobotClient",
    ]
    if replayed:
        if source_path is None:
            raise ValueError("A replayed span needs the saved recording's path")
        header += [
            "from waldo_commander.demonstrations import load_demonstration",
            "from waldo_commander.skills import replay_demonstration",
            "",
            f"recording = load_demonstration({str(source_path)!r})",
        ]
    body: list[str] = []
    if needs_tool and identity is not None:
        call = f'rbt.select_tool("{identity.key}"'
        call += (
            f', variant_key="{identity.variant_key}")' if identity.variant_key else ")"
        )
        body.append(call)
    for span in spans:
        if span.kind == "replay":
            body.append(f"# Samples {span.start}–{span.stop}: {span.reason}.")
        body.extend(span.lines)
    indented = "\n".join(f"    {line}" for line in body)
    return "\n".join([*header, "", "with RobotClient() as rbt:", indented, ""])
