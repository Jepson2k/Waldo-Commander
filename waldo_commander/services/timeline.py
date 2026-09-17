"""Time-based timeline over the program's records, for playback and scrubbing.

Rows of a record are evenly spaced in simulated time, so timing is
multiplication: a segment's window is its rows, and the pose at an instant
is the row at that instant rather than a guess between two waypoints. The
commanded record is the axis; when a predicted record is on screen the
pose played back is its row at the same point of the same command, so the
arm on screen is the arm the simulation says, and the scrub bar still
counts the program's own time.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from waldoctl import TickIndex, align_rows

from waldo_commander.state import PathSegment, ToolSelection


@dataclass(slots=True)
class TimelineSample:
    """Result of sampling the timeline at a given time."""

    segment_index: int
    joints: list[float] | None
    fraction: float  # 0..1 within segment
    time: float  # clamped input time


@dataclass(slots=True)
class ToolKeyframe:
    """A single tool animation keyframe."""

    time: float
    positions: tuple[float, ...]


@dataclass(slots=True)
class ToolSpan:
    """One stretch of tool travel, as the scrub bar draws it.

    The timeline reports these rather than leaving consumers to pair
    keyframes two at a time: a record's keyframes come from the jaw column
    and have no fixed structure to pair by.
    """

    start: float
    end: float
    blocking: bool
    """Whether the arm holds for the whole span (a full-height marker) or
    the tool moves under a motion already in flight (a mini one). The
    record shows the arm holding while the jaws travel, so every span it
    yields blocks."""


@dataclass(slots=True)
class ToolSelectionKeyframe:
    """Records which tool is active at a given point in the timeline."""

    time: float
    tool_key: str
    variant_key: str


@dataclass(slots=True)
class ObjectSample:
    """A world object's pose at an instant, from the predicted record."""

    pose: tuple[float, ...]
    physics: bool
    """Always true: a pose here was simulated, never guessed. Kept so the
    scene can go on drawing a guessed pose differently if one ever
    reaches it."""


@dataclass(slots=True)
class Checkpoint:
    """A point where playback pauses until a condition is met."""

    time: float  # Absolute time in timeline
    segment_index: int  # Which segment this checkpoint follows
    kind: str  # e.g. "home", "tool_idle"


def _tool_keyframes(
    closed: np.ndarray, dt: float
) -> tuple[list[ToolKeyframe], list[ToolSpan]]:
    """One keyframe pair per stretch of jaw travel in the record.

    Not one per changed row: at the record's rate a one-second grasp
    changes on fifty rows, and the scrub bar reads these two at a time.
    A tool that never moves contributes none.
    """
    keyframes: list[ToolKeyframe] = []
    spans: list[ToolSpan] = []
    moving_from: tuple[int, float] | None = None
    previous = float("nan")
    rows = len(closed)
    for i in range(rows + 1):
        v = float(closed[i]) if i < rows else float("nan")
        moved = v == v and previous == previous and abs(v - previous) >= 1e-4
        if moved and moving_from is None:
            moving_from = (i - 1, previous)
        elif not moved and moving_from is not None:
            row, start_pos = moving_from
            keyframes.append(ToolKeyframe(time=row * dt, positions=(start_pos,)))
            keyframes.append(ToolKeyframe(time=(i - 1) * dt, positions=(previous,)))
            spans.append(ToolSpan(start=row * dt, end=(i - 1) * dt, blocking=True))
            moving_from = None
        if v == v:
            previous = v
    return keyframes, spans


@dataclass(slots=True)
class Timeline:
    """Continuous time-based index over the program's segments.

    Maps a time on the commanded record's axis to the segment it falls in
    and the joint pose to show there — the commanded pose, or the
    predicted record's pose for the same point of the same command.
    """

    cumulative_times: list[float]  # len = num_segments + 1, starts with 0.0
    total_duration: float
    _segments: list[PathSegment]
    _commanded: TickIndex
    segment_durations: list[float] = field(default_factory=list)
    tool_keyframes: list[ToolKeyframe] = field(default_factory=list)
    _tool_times: list[float] = field(default_factory=list)
    tool_selection_keyframes: list[ToolSelectionKeyframe] = field(default_factory=list)
    _tool_sel_times: list[float] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    tool_spans: list[ToolSpan] = field(default_factory=list)
    _predicted: TickIndex | None = None
    #: For each commanded row, the predicted row at the same point of the
    #: same command. None without a predicted record.
    _align: NDArray[np.intp] | None = None

    @property
    def segments(self) -> list[PathSegment]:
        """The segments this timeline is indexed by — the program's, in
        program order, one per command that owns time (a delay between two
        moves is one) or carries a marker."""
        return self._segments

    @property
    def predicted(self) -> TickIndex | None:
        return self._predicted

    @classmethod
    def from_record(
        cls,
        commanded: TickIndex,
        segments: list[PathSegment],
        *,
        predicted: TickIndex | None = None,
        tool_selections: list[ToolSelection] | None = None,
    ) -> Timeline:
        """Build a timeline over the commanded record.

        Each segment's window is the rows it owns, in program order; a
        zero-row marker sits at its start row. Tool keyframes come from the
        jaw column, checkpoints from the segments, and *predicted* — when
        it answers this very plan — supplies the poses played back.
        """
        dt = commanded.row_dt_s
        cum = [seg.start_row * dt for seg in segments]
        cum.append(commanded.rows * dt)
        durs = [seg.rows * dt for seg in segments]
        total = commanded.duration_s

        tool_kf, tool_spans = _tool_keyframes(commanded.tool_closed, dt)

        sel_kf: list[ToolSelectionKeyframe] = []
        for sel in tool_selections or []:
            idx = sel.segment_index
            t_sel = 0.0 if idx < 0 else cum[min(idx + 1, len(cum) - 1)]
            sel_kf.append(
                ToolSelectionKeyframe(
                    time=t_sel,
                    tool_key=sel.tool_key,
                    variant_key=sel.variant_key,
                )
            )

        cps = [
            Checkpoint(time=cum[i] + durs[i], segment_index=i, kind=seg.checkpoint)
            for i, seg in enumerate(segments)
            if seg.checkpoint
        ]

        align = None
        if predicted is not None and predicted.rows > 0 and commanded.rows > 0:
            align = align_rows(commanded, predicted)

        return cls(
            cumulative_times=cum,
            total_duration=total,
            _segments=segments,
            _commanded=commanded,
            segment_durations=durs,
            tool_keyframes=tool_kf,
            _tool_times=[k.time for k in tool_kf],
            tool_selection_keyframes=sel_kf,
            _tool_sel_times=[k.time for k in sel_kf],
            checkpoints=cps,
            tool_spans=tool_spans,
            _predicted=predicted if align is not None else None,
            _align=align,
        )

    def _row(self, t: float) -> int:
        """The commanded row at time *t*, clamped to the record."""
        rows = self._commanded.rows
        if rows == 0:
            return 0
        return max(0, min(self._commanded.row_at(t), rows - 1))

    def predicted_row(self, t: float) -> int:
        """The predicted record's row for time *t*: the row at the same
        point of the same command, or the commanded row itself without a
        predicted record."""
        row = self._row(t)
        if self._align is None:
            return row
        return int(self._align[row])

    def sample(self, t: float) -> TimelineSample:
        """Sample the timeline at time t (seconds).

        Returns the pose to show, the segment index, and the fractional
        position within the segment. Uses binary search for O(log N) lookup.
        """
        if not self._segments or self._commanded.rows == 0:
            return TimelineSample(segment_index=0, joints=None, fraction=0.0, time=0.0)

        t = max(0.0, min(t, self.total_duration))

        # Binary search: find rightmost cum_time <= t
        idx = bisect.bisect_right(self.cumulative_times, t) - 1
        idx = max(0, min(idx, len(self._segments) - 1))

        seg_start = self.cumulative_times[idx]
        motion_dur = self.segment_durations[idx]
        fraction = (t - seg_start) / motion_dur if motion_dur > 0 else 1.0
        fraction = max(0.0, min(1.0, fraction))

        # Measured, not interpolated: the row IS where the arm is.
        if self._predicted is not None:
            source = self._predicted.joints_rad[self.predicted_row(t)]
        else:
            source = self._commanded.joints_rad[self._row(t)]
        joints = [float(v) for v in source]

        return TimelineSample(
            segment_index=idx,
            joints=joints,
            fraction=fraction,
            time=t,
        )

    def sample_tool(self, t: float) -> tuple[float, ...]:
        """Interpolate tool position at time t from keyframes."""
        kf = self.tool_keyframes
        if not kf:
            return ()

        if t <= kf[0].time:
            return kf[0].positions
        if t >= kf[-1].time:
            return kf[-1].positions

        idx = bisect.bisect_right(self._tool_times, t) - 1
        idx = max(0, min(idx, len(kf) - 2))

        k0 = kf[idx]
        k1 = kf[idx + 1]
        dt = k1.time - k0.time
        if dt < 1e-9 or len(k0.positions) != len(k1.positions):
            return k1.positions

        frac = (t - k0.time) / dt
        frac = max(0.0, min(1.0, frac))
        return tuple(a + (b - a) * frac for a, b in zip(k0.positions, k1.positions))

    def sample_objects(self, t: float) -> dict[str, ObjectSample]:
        """Every tracked object's pose at time t, off the predicted record:
        an object that never moved holds its one row, one that did is read
        at the row playback is on. A NaN row is an object the simulation
        lost, and is left out."""
        predicted = self._predicted
        if predicted is None:
            return {}
        row = self.predicted_row(t)
        out: dict[str, ObjectSample] = {}
        for obj in predicted.objects:
            poses = obj.poses
            if poses.shape[0] == 0:
                continue
            pose = poses[min(row, poses.shape[0] - 1)]
            if not np.all(np.isfinite(pose)):
                continue
            out[obj.name] = ObjectSample(tuple(float(v) for v in pose), True)
        return out

    def sample_tool_selection(self, t: float) -> ToolSelectionKeyframe | None:
        """Return the active tool selection at time t.

        Finds the last tool selection keyframe with time <= t.
        Returns None if no tool selections exist.
        """
        kf = self.tool_selection_keyframes
        if not kf:
            return None
        idx = bisect.bisect_right(self._tool_sel_times, t) - 1
        if idx < 0:
            return kf[0] if kf[0].time <= t + 1e-6 else None
        return kf[idx]

    def next_checkpoint(self, t: float) -> Checkpoint | None:
        """Find the first checkpoint at or after time t, or None."""
        for cp in self.checkpoints:
            if cp.time >= t - 1e-6:
                return cp
        return None
