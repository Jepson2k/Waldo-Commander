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
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from waldoctl import TickBlock, TickIndex, align_rows

from waldo_commander.state import PathSegment, ToolSelection


def distinct_prediction(
    commanded: TickIndex, predicted: TickIndex | None
) -> TickIndex | None:
    """*predicted*, unless it is the commanded record itself or its copy."""
    if predicted is commanded or (
        predicted is not None
        and predicted.digest
        and predicted.digest == commanded.digest
    ):
        return None
    return predicted


def selection_before(
    selections: Sequence[ToolSelection], command: int
) -> ToolSelection | None:
    """The tool selection in effect when command *command* runs."""
    chosen = None
    for selection in selections:
        if selection.command <= command and (
            chosen is None or selection.command >= chosen.command
        ):
            chosen = selection
    return chosen


def _occurrences(commands: Iterable[int]) -> list[tuple[int, int]]:
    """Each command with how many times it ran before: a run's identity
    across records that number their rows differently."""
    seen: defaultdict[int, int] = defaultdict(int)
    keys = []
    for command in commands:
        keys.append((command, seen[command]))
        seen[command] += 1
    return keys


@dataclass(frozen=True, slots=True)
class Cursor:
    """A point of the timeline: which record shows it (0 commanded, 1
    predicted), that record's row, and the segment it belongs to."""

    record: int
    row: int
    segment: int


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
    #: Each segment's (command, occurrence), and the reverse.
    _segment_keys: list[tuple[int, int]] = field(default_factory=list)
    _segment_of: dict[tuple[int, int], int] = field(default_factory=dict)
    #: Per record (commanded, predicted), its blocks by (command, occurrence).
    _blocks: tuple[dict[tuple[int, int], TickBlock], ...] = field(
        default_factory=lambda: ({}, {})
    )

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
        predicted = distinct_prediction(commanded, predicted)
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
        if align is None:
            predicted = None
        segment_keys = _occurrences(seg.command for seg in segments)

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
            _predicted=predicted,
            _align=align,
            _segment_keys=segment_keys,
            _segment_of={key: i for i, key in enumerate(segment_keys)},
            _blocks=tuple(
                dict(zip(_occurrences(b.command for b in r.blocks), r.blocks))
                if r is not None
                else {}
                for r in (commanded, predicted)
            ),
        )

    def commanded_row(self, t: float) -> int:
        """The commanded row at time *t*, clamped to the record."""
        rows = self._commanded.rows
        if rows == 0:
            return 0
        return max(0, min(self._commanded.row_at(t), rows - 1))

    def predicted_row(self, t: float) -> int:
        """The predicted record's row for time *t*: the row at the same
        point of the same command, or the commanded row itself without a
        predicted record."""
        row = self.commanded_row(t)
        if self._align is None:
            return row
        return int(self._align[row])

    def record(self, index: int) -> TickIndex | None:
        """The commanded record (0) or the predicted one (1)."""
        return self._predicted if index else self._commanded

    def _segment_at(self, t: float) -> int:
        idx = bisect.bisect_right(self.cumulative_times, t) - 1
        return max(0, min(idx, len(self._segments) - 1))

    def cursor_at(self, t: float) -> Cursor:
        """Where playback is at *t*: the predicted row when there is one."""
        index = int(self._predicted is not None)
        row = self.predicted_row(t) if index else self.commanded_row(t)
        return Cursor(index, row, self._segment_at(t))

    def segment_key(self, segment: int) -> tuple[int, int]:
        """*segment*'s command and how many times that command ran before."""
        return self._segment_keys[segment]

    def segment_start(self, segment: int) -> Cursor:
        """The first row of *segment*'s command run, in the record playback
        shows; a segment several share an instant with stays itself."""
        key = self.segment_key(segment)
        for index in dict.fromkeys((int(self._predicted is not None), 0)):
            record = self.record(index)
            block = self._blocks[index].get(key)
            if record is not None and block is not None and record.rows:
                return Cursor(index, min(block.start_row, record.rows - 1), segment)
        cursor = self.cursor_at(self.cumulative_times[segment])
        return Cursor(cursor.record, cursor.row, segment)

    def cursor_of(
        self, index: int, row: int, command: int, occurrence: int
    ) -> tuple[Cursor, float]:
        """A picked row of *command*'s *occurrence*-th run, and its playback
        time: a predicted row maps to the same elapsed point of that run's
        commanded rows."""
        key = (command, occurrence)
        record = self.record(index)
        assert record is not None
        own = self._blocks[index].get(key)
        peer = self._blocks[0].get(key) if index else None
        dt = self._commanded.row_dt_s
        if own is not None and peer is not None:
            if own.rows:
                elapsed = (row - min(own.start_row, record.rows - 1)) * record.row_dt_s
                start = min(peer.start_row, self._commanded.rows - 1)
                t = start * dt + min((max(1, peer.rows) - 1) * dt, elapsed)
            else:
                t = peer.start_row * dt
        elif own is not None and not own.rows:
            t = own.start_row * record.row_dt_s
        else:
            t = row * record.row_dt_s
        segment = self._segment_of.get(key)
        return Cursor(
            index, row, self._segment_at(t) if segment is None else segment
        ), t

    def sample(self, t: float, cursor: Cursor | None = None) -> TimelineSample:
        """Sample the timeline at time t (seconds), at *cursor* when the row
        shown is not simply the one playback reaches at t.

        Returns the pose to show, the segment index, and the fractional
        position within the segment. Uses binary search for O(log N) lookup.
        """
        if not self._segments or self._commanded.rows == 0:
            return TimelineSample(segment_index=0, joints=None, fraction=0.0, time=0.0)

        t = max(0.0, min(t, self.total_duration))
        if cursor is None:
            cursor = self.cursor_at(t)
        idx = cursor.segment

        seg_start = self.cumulative_times[idx]
        motion_dur = self.segment_durations[idx]
        fraction = (t - seg_start) / motion_dur if motion_dur > 0 else 1.0
        fraction = max(0.0, min(1.0, fraction))

        # Measured, not interpolated: the row IS where the arm is.
        record = self.record(cursor.record)
        assert record is not None
        joints = [float(v) for v in record.joints_rad[cursor.row]]

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

    def sample_objects(
        self, t: float, cursor: Cursor | None = None
    ) -> dict[str, ObjectSample]:
        """Every tracked object's pose at time t off the predicted record, or
        at *cursor*: an object that never moved holds its one row, one that
        did is read at the row shown. A NaN row is an object the simulation
        lost, and is left out."""
        if cursor is None:
            record, row = self._predicted, self.predicted_row(t)
        else:
            record, row = self.record(cursor.record), cursor.row
        if record is None:
            return {}
        out: dict[str, ObjectSample] = {}
        for obj in record.objects:
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
