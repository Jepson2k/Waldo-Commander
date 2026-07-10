"""Time-based timeline over path segments for smooth simulation playback."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from waldo_commander.state import PathSegment, ToolAction, ToolSelection

DEFAULT_SEGMENT_DURATION = 0.5  # seconds, for segments without timing data


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
class ToolSelectionKeyframe:
    """Records which tool is active at a given point in the timeline."""

    time: float
    tool_key: str
    variant_key: str


@dataclass(slots=True)
class Checkpoint:
    """A point where playback pauses until a condition is met."""

    time: float  # Absolute time in timeline
    segment_index: int  # Which segment this checkpoint follows
    kind: str  # e.g. "home", "tool_idle"


@dataclass(slots=True)
class Timeline:
    """Continuous time-based index over path segments.

    Enables smooth playback and scrubbing by mapping wall-clock time
    to interpolated joint poses within the segment sequence.
    """

    cumulative_times: list[float]  # len = num_segments + 1, starts with 0.0
    total_duration: float
    _segments: list[PathSegment]
    segment_durations: list[float] = field(default_factory=list)  # motion-only (no gap)
    tool_keyframes: list[ToolKeyframe] = field(default_factory=list)
    _tool_times: list[float] = field(default_factory=list)
    tool_selection_keyframes: list[ToolSelectionKeyframe] = field(default_factory=list)
    _tool_sel_times: list[float] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)

    @classmethod
    def from_segments(
        cls,
        segments: list[PathSegment],
        tool_actions: list[ToolAction] | None = None,
        tool_selections: list[ToolSelection] | None = None,
    ) -> Timeline:
        """Build a timeline from path segments and optional tool actions.

        Each segment's width in the timeline is proportional to its
        estimated_duration.  Blocking tool actions (sleep_offset == 0)
        insert a gap after their segment so the next move doesn't start
        until the tool finishes.  Non-blocking tool actions (sleep_offset > 0)
        overlap with the preceding segment.
        """
        blocking_gap: dict[int, float] = {}
        if tool_actions:
            for act in tool_actions:
                if act.sleep_offset == 0 and act.segment_index >= 0:
                    # Multiple blocking actions on the same segment: sum durations
                    blocking_gap[act.segment_index] = blocking_gap.get(
                        act.segment_index, 0.0
                    ) + max(act.estimated_duration, 0.01)

        # Build cumulative times with gaps for blocking tool actions
        cum = [0.0]
        seg_durs: list[float] = []
        for i, seg in enumerate(segments):
            seg_dur = (
                seg.estimated_duration
                if seg.estimated_duration is not None
                else DEFAULT_SEGMENT_DURATION
            )
            seg_durs.append(seg_dur)
            cum.append(cum[-1] + seg_dur + blocking_gap.get(i, 0.0))
        total = cum[-1] if segments else 0.0

        # Build tool keyframes from actions
        tool_kf: list[ToolKeyframe] = []
        if tool_actions:
            n_dof = len(tool_actions[0].target_positions)
            current: tuple[float, ...] = tuple(0.0 for _ in range(n_dof))
            # Track accumulated time for sequential blocking actions on same segment
            blocking_accum: dict[int, float] = {}

            for act in tool_actions:
                dur = max(act.estimated_duration, 0.01)
                if act.segment_index >= 0 and act.segment_index < len(segments):
                    if act.sleep_offset > 0:
                        # Mid-motion: offset from start of preceding segment
                        t = cum[act.segment_index] + act.sleep_offset
                    else:
                        # End-of-move: tool fires after segment motion ends.
                        # Sequential blocking actions on the same segment are
                        # placed one after another (not all at the same time).
                        seg_dur = segments[act.segment_index].estimated_duration
                        if seg_dur is None:
                            seg_dur = DEFAULT_SEGMENT_DURATION
                        prev = blocking_accum.get(act.segment_index, 0.0)
                        t = cum[act.segment_index] + seg_dur + prev
                        blocking_accum[act.segment_index] = prev + dur
                else:
                    t = total
                tool_kf.append(ToolKeyframe(time=t, positions=current))
                current = act.target_positions
                tool_kf.append(ToolKeyframe(time=t + dur, positions=current))

            # Extend total duration if tool actions go past last segment
            if tool_kf:
                total = max(total, tool_kf[-1].time)

        # Extract checkpoints from segments
        cps: list[Checkpoint] = []
        for idx, seg in enumerate(segments):
            if seg.checkpoint:
                # Place checkpoint at segment motion end (before any
                # blocking tool gap) so it appears before tool markers.
                cp_time = cum[idx] + seg_durs[idx]
                cps.append(
                    Checkpoint(
                        time=cp_time,
                        segment_index=idx,
                        kind=seg.checkpoint,
                    )
                )

        # Build tool selection keyframes
        sel_kf: list[ToolSelectionKeyframe] = []
        if tool_selections:
            for sel in tool_selections:
                if sel.segment_index < 0:
                    t_sel = 0.0
                elif sel.segment_index < len(cum) - 1:
                    t_sel = cum[sel.segment_index + 1]
                else:
                    t_sel = total
                sel_kf.append(
                    ToolSelectionKeyframe(
                        time=t_sel,
                        tool_key=sel.tool_key,
                        variant_key=sel.variant_key,
                    )
                )

        return cls(
            cumulative_times=cum,
            total_duration=total,
            _segments=segments,
            segment_durations=seg_durs,
            tool_keyframes=tool_kf,
            _tool_times=[k.time for k in tool_kf],
            tool_selection_keyframes=sel_kf,
            _tool_sel_times=[k.time for k in sel_kf],
            checkpoints=cps,
        )

    def sample(self, t: float) -> TimelineSample:
        """Sample the timeline at time t (seconds).

        Returns interpolated joints, segment index, and fractional position.
        Uses binary search for O(log N) lookup.
        """
        if not self._segments:
            return TimelineSample(segment_index=0, joints=None, fraction=0.0, time=0.0)

        t = max(0.0, min(t, self.total_duration))

        # Binary search: find rightmost cum_time <= t
        idx = bisect.bisect_right(self.cumulative_times, t) - 1
        idx = max(0, min(idx, len(self._segments) - 1))

        seg = self._segments[idx]
        seg_start = self.cumulative_times[idx]
        # Use motion-only duration so the arm holds at fraction=1.0
        # during any blocking tool-action gap after the segment.
        motion_dur = (
            self.segment_durations[idx]
            if self.segment_durations
            else self.cumulative_times[idx + 1] - seg_start
        )

        fraction = (t - seg_start) / motion_dur if motion_dur > 0 else 1.0
        fraction = max(0.0, min(1.0, fraction))

        joints = self._interpolate_joints(seg, fraction)

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

    @staticmethod
    def _interpolate_joints(seg: PathSegment, fraction: float) -> list[float] | None:
        """Interpolate joint angles within a segment at the given fraction."""
        traj = seg.joint_trajectory
        if traj and len(traj) >= 2:
            # Float-index lerp between nearest rows
            f_idx = fraction * (len(traj) - 1)
            lo = int(f_idx)
            hi = min(lo + 1, len(traj) - 1)
            alpha = f_idx - lo
            row_lo = traj[lo]
            row_hi = traj[hi]
            return [a + alpha * (b - a) for a, b in zip(row_lo, row_hi)]

        if traj and len(traj) == 1:
            return list(traj[0])

        # Fallback: endpoint-only (discrete jump to end pose)
        return list(seg.joints) if seg.joints else None
