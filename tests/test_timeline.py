"""Unit tests for the simulation timeline engine."""

import math

import pytest

from waldo_commander.services.timeline import (
    DEFAULT_SEGMENT_DURATION,
    Timeline,
)
from waldo_commander.state import PathSegment, ToolAction


def _seg(
    duration: float | None = None,
    joints: list[float] | None = None,
    joint_trajectory: list[list[float]] | None = None,
    checkpoint: str | None = None,
) -> PathSegment:
    """Create a minimal PathSegment for timeline testing."""
    return PathSegment(
        points=[[0, 0, 0]],
        color="#00ff00",
        is_valid=True,
        line_number=1,
        joints=joints,
        estimated_duration=duration,
        joint_trajectory=joint_trajectory,
        checkpoint=checkpoint,
    )


class TestTimelineConstruction:
    def test_from_segments_basic(self):
        segs = [_seg(duration=1.0), _seg(duration=2.0), _seg(duration=0.5)]
        tl = Timeline.from_segments(segs)

        assert tl.total_duration == pytest.approx(3.5)
        assert tl.cumulative_times == pytest.approx([0.0, 1.0, 3.0, 3.5])

    def test_empty_segments(self):
        tl = Timeline.from_segments([])
        assert tl.total_duration == 0.0
        assert tl.cumulative_times == [0.0]

    def test_none_duration_fallback(self):
        segs = [_seg(duration=None), _seg(duration=1.0)]
        tl = Timeline.from_segments(segs)

        assert tl.total_duration == pytest.approx(DEFAULT_SEGMENT_DURATION + 1.0)
        assert tl.cumulative_times[1] == pytest.approx(DEFAULT_SEGMENT_DURATION)


class TestTimelineSampling:
    def test_sample_at_zero(self):
        segs = [_seg(duration=2.0, joints=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0])]
        tl = Timeline.from_segments(segs)
        s = tl.sample(0.0)

        assert s.segment_index == 0
        assert s.fraction == pytest.approx(0.0)
        assert s.time == pytest.approx(0.0)

    def test_sample_at_end(self):
        segs = [
            _seg(duration=1.0, joints=[1.0] * 6),
            _seg(duration=1.0, joints=[2.0] * 6),
        ]
        tl = Timeline.from_segments(segs)
        s = tl.sample(2.0)

        assert s.segment_index == 1
        assert s.fraction == pytest.approx(1.0)
        assert s.joints == [2.0] * 6

    def test_sample_at_segment_boundary(self):
        segs = [
            _seg(duration=1.0, joints=[1.0] * 6),
            _seg(duration=1.0, joints=[2.0] * 6),
        ]
        tl = Timeline.from_segments(segs)
        s = tl.sample(1.0)

        assert s.segment_index == 1
        assert s.fraction == pytest.approx(0.0)

    def test_sample_mid_segment_no_trajectory(self):
        segs = [_seg(duration=2.0, joints=[10.0] * 6)]
        tl = Timeline.from_segments(segs)
        s = tl.sample(1.0)

        assert s.segment_index == 0
        assert s.fraction == pytest.approx(0.5)
        # Without trajectory, returns endpoint joints
        assert s.joints == [10.0] * 6

    def test_sample_interpolation_with_trajectory(self):
        traj = [
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        ]
        segs = [_seg(duration=2.0, joint_trajectory=traj)]
        tl = Timeline.from_segments(segs)
        s = tl.sample(1.0)  # midpoint

        assert s.segment_index == 0
        assert s.fraction == pytest.approx(0.5)
        assert s.joints is not None
        assert s.joints == pytest.approx([5.0, 10.0, 15.0, 20.0, 25.0, 30.0])

    def test_sample_interpolation_three_waypoints(self):
        traj = [
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [10.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [20.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        ]
        segs = [_seg(duration=4.0, joint_trajectory=traj)]
        tl = Timeline.from_segments(segs)

        # At 25% → fraction=0.25 → float_idx=0.5 → lerp between rows 0 and 1
        s = tl.sample(1.0)
        assert s.joints is not None
        assert s.joints[0] == pytest.approx(5.0)

        # At 75% → fraction=0.75 → float_idx=1.5 → lerp between rows 1 and 2
        s = tl.sample(3.0)
        assert s.joints is not None
        assert s.joints[0] == pytest.approx(15.0)

    def test_sample_clamps_negative(self):
        segs = [_seg(duration=1.0, joints=[1.0] * 6)]
        tl = Timeline.from_segments(segs)
        s = tl.sample(-5.0)

        assert s.time == pytest.approx(0.0)
        assert s.segment_index == 0

    def test_sample_clamps_past_end(self):
        segs = [_seg(duration=1.0, joints=[1.0] * 6)]
        tl = Timeline.from_segments(segs)
        s = tl.sample(100.0)

        assert s.time == pytest.approx(1.0)
        assert s.segment_index == 0
        assert s.fraction == pytest.approx(1.0)

    def test_sample_empty_timeline(self):
        tl = Timeline.from_segments([])
        s = tl.sample(1.0)

        assert s.segment_index == 0
        assert s.joints is None


class TestTimelineCheckpoints:
    """Test zero-duration segments (home/checkpoint) and checkpoint detection."""

    def test_zero_duration_segment_contributes_no_time(self):
        """A zero-width home segment between two normal segments adds no duration."""
        home = [0.0] * 6
        segs = [
            _seg(duration=1.0, joints=[10.0] * 6),
            _seg(duration=0.0, joints=home, checkpoint="home"),
            _seg(duration=2.0, joints=[20.0] * 6),
        ]
        tl = Timeline.from_segments(segs)

        assert tl.total_duration == pytest.approx(3.0)
        # Cumulative: [0.0, 1.0, 1.0, 3.0]  (home at t=1.0, zero width)
        assert tl.cumulative_times == pytest.approx([0.0, 1.0, 1.0, 3.0])

    def test_checkpoint_extracted_at_correct_time(self):
        """Checkpoints are placed at the end of the checkpoint segment."""
        segs = [
            _seg(duration=1.0),
            _seg(duration=0.0, checkpoint="home"),
            _seg(duration=2.0),
        ]
        tl = Timeline.from_segments(segs)

        assert len(tl.checkpoints) == 1
        cp = tl.checkpoints[0]
        assert cp.kind == "home"
        assert cp.time == pytest.approx(1.0)
        assert cp.segment_index == 1

    def test_next_checkpoint_search(self):
        """next_checkpoint() finds upcoming checkpoints and returns None when past all."""
        segs = [
            _seg(duration=1.0),
            _seg(duration=0.0, checkpoint="pick"),
            _seg(duration=1.0),
            _seg(duration=0.0, checkpoint="place"),
            _seg(duration=1.0),
        ]
        tl = Timeline.from_segments(segs)

        assert len(tl.checkpoints) == 2

        # Before first checkpoint
        cp = tl.next_checkpoint(0.5)
        assert cp is not None and cp.kind == "pick"

        # Between checkpoints
        cp = tl.next_checkpoint(1.5)
        assert cp is not None and cp.kind == "place"

        # After all checkpoints
        cp = tl.next_checkpoint(2.5)
        assert cp is None

    def test_sample_at_zero_width_segment_returns_its_joints(self):
        """Sampling at a zero-width segment boundary returns that segment's joints."""
        home_joints = [0.0, -90.0, 0.0, 0.0, 0.0, 0.0]
        segs = [
            _seg(duration=1.0, joints=[10.0] * 6),
            _seg(duration=0.0, joints=home_joints, checkpoint="home"),
            _seg(duration=2.0, joints=[20.0] * 6),
        ]
        tl = Timeline.from_segments(segs)

        # At t=1.0 we're at the boundary where home segment starts (and ends)
        s = tl.sample(1.0)
        # Should be in the third segment (idx=2) since home is zero-width
        assert s.segment_index == 2
        assert s.fraction == pytest.approx(0.0)


class TestTimelineToolKeyframes:
    """Test tool action keyframe construction and interpolation."""

    def test_tool_keyframes_from_actions(self):
        """Blocking tool action keyframes start at the end of segment motion."""
        segs = [_seg(duration=2.0), _seg(duration=3.0)]
        actions = [
            ToolAction(
                tcp_pose=[0, 0, 0],
                motions=[],
                target_positions=(1.0,),
                start_positions=(0.0,),
                activation_type="electric",
                line_number=1,
                method="close",
                estimated_duration=0.5,
                segment_index=0,
                sleep_offset=0.0,
            ),
        ]
        tl = Timeline.from_segments(segs, tool_actions=actions)

        # Blocking tool fires at end of segment 0's motion (t=2.0)
        assert len(tl.tool_keyframes) == 2
        assert tl.tool_keyframes[0].time == pytest.approx(2.0)
        assert tl.tool_keyframes[0].positions == (0.0,)
        assert tl.tool_keyframes[1].time == pytest.approx(2.5)
        assert tl.tool_keyframes[1].positions == (1.0,)

    def test_sample_tool_interpolation(self):
        """sample_tool() linearly interpolates between keyframes."""
        segs = [_seg(duration=2.0)]
        actions = [
            ToolAction(
                tcp_pose=[0, 0, 0],
                motions=[],
                target_positions=(1.0,),
                start_positions=(0.0,),
                activation_type="electric",
                line_number=1,
                method="close",
                estimated_duration=1.0,
                segment_index=0,
                sleep_offset=0.5,
            ),
        ]
        tl = Timeline.from_segments(segs, tool_actions=actions)

        # Action starts at t=0.5, ends at t=1.5
        # Before action: position = start (0.0)
        pos = tl.sample_tool(0.0)
        assert pos == pytest.approx((0.0,))

        # Midpoint of action: t=1.0
        pos = tl.sample_tool(1.0)
        assert pos == pytest.approx((0.5,))

        # After action: position = target (1.0)
        pos = tl.sample_tool(2.0)
        assert pos == pytest.approx((1.0,))

    def test_tool_action_past_segments_extends_duration(self):
        """Tool action duration extends total timeline if it goes past last segment."""
        segs = [_seg(duration=1.0)]
        actions = [
            ToolAction(
                tcp_pose=[0, 0, 0],
                motions=[],
                target_positions=(1.0,),
                start_positions=(0.0,),
                activation_type="electric",
                line_number=1,
                method="close",
                estimated_duration=2.0,
                segment_index=0,
                sleep_offset=0.5,
            ),
        ]
        tl = Timeline.from_segments(segs, tool_actions=actions)

        # Tool ends at 0.5 + 2.0 = 2.5, which exceeds segment duration of 1.0
        assert tl.total_duration == pytest.approx(2.5)


class TestTimelineBlockingGap:
    """Test that arm holds still during blocking tool action gaps."""

    def test_arm_holds_at_fraction_1_during_gap(self):
        """After a blocking move, sample() returns fraction=1.0 during the gap."""
        traj = [
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        ]
        segs = [_seg(duration=2.0, joint_trajectory=traj)]
        actions = [
            ToolAction(
                tcp_pose=[0, 0, 0],
                motions=[],
                target_positions=(1.0,),
                start_positions=(0.0,),
                activation_type="electric",
                line_number=1,
                method="close",
                estimated_duration=1.0,
                segment_index=0,
                sleep_offset=0.0,
            ),
        ]
        tl = Timeline.from_segments(segs, tool_actions=actions)

        # Total = 2.0 (motion) + 1.0 (blocking gap) = 3.0
        assert tl.total_duration == pytest.approx(3.0)

        # At t=1.0: mid-motion, fraction=0.5
        s = tl.sample(1.0)
        assert s.fraction == pytest.approx(0.5)
        assert s.joints == pytest.approx([5.0, 10.0, 15.0, 20.0, 25.0, 30.0])

        # At t=2.0: motion done, fraction=1.0
        s = tl.sample(2.0)
        assert s.fraction == pytest.approx(1.0)
        assert s.joints == pytest.approx([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])

        # At t=2.5: mid-gap (tool animating), arm still at fraction=1.0
        s = tl.sample(2.5)
        assert s.fraction == pytest.approx(1.0)
        assert s.joints == pytest.approx([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])


class TestGradientBlending:
    """Test that the path gradient blends green→red sensibly."""

    def test_green_red_midpoint(self):
        """Blending green and red at 50% produces a mix, not an unchanged color."""
        from waldo_commander.services.urdf_scene.urdf_scene import _lerp_hex

        green = (0x10, 0xB9, 0x81)  # #10b981
        red = (0xEF, 0x44, 0x44)  # #ef4444

        result = _lerp_hex(green, red, 0.5)
        r8 = int(result[1:3], 16)
        g8 = int(result[3:5], 16)
        b8 = int(result[5:7], 16)

        # Midpoint should have moderate red and green, low blue
        assert r8 > 100, f"Red channel should be moderate at midpoint, got {r8}"
        assert g8 > 80, f"Green channel should be moderate at midpoint, got {g8}"
        assert b8 < 120, f"Blue channel should be low-ish at midpoint, got {b8}"

    def test_low_factor_stays_greenish(self):
        """At factor=0.1 (far from invalid), green still dominates."""
        from waldo_commander.services.urdf_scene.urdf_scene import _lerp_hex

        green = (0x10, 0xB9, 0x81)
        red = (0xEF, 0x44, 0x44)

        result = _lerp_hex(green, red, 0.1)
        r8 = int(result[1:3], 16)
        g8 = int(result[3:5], 16)
        # Green channel should still be dominant at low blend factor
        assert g8 > r8, f"Green should dominate at low factor, got r={r8} g={g8}"


def _track(
    name: str, poses: list[list[float]], physics: bool = True, carried: bool = False
) -> dict:
    return {"name": name, "poses": poses, "carried": carried, "physics": physics}


_Q_ID = [1.0, 0.0, 0.0, 0.0]
_Q_Z90 = [0.7071067811865476, 0.0, 0.0, 0.7071067811865476]


class TestObjectTracks:
    """World objects ride the timeline: held while parked, interpolated while
    moving, slerped through a turn, and carried through a tool action."""

    def test_objects_hold_before_between_and_after_their_tracks(self):
        parked = _seg(duration=2.0, joints=[0] * 6)
        parked.object_tracks = [_track("block", [[0.3, 0.0, 0.04, *_Q_ID]])]
        carry = _seg(duration=4.0, joints=[0] * 6)
        carry.object_tracks = [
            _track(
                "block",
                [[0.3, 0.0, 0.04, *_Q_ID], [0.3, 0.0, 0.24, *_Q_Z90]],
                carried=True,
            )
        ]
        tl = Timeline.from_segments([parked, carry])

        assert tl.sample_objects(0.0)["block"].pose == (0.3, 0.0, 0.04, *_Q_ID)
        assert tl.sample_objects(1.5)["block"].pose == (0.3, 0.0, 0.04, *_Q_ID)
        mid = tl.sample_objects(4.0)["block"].pose  # half way through the carry
        assert mid[:3] == pytest.approx((0.3, 0.0, 0.14))
        # A slerp half way through a 90 degree yaw is a 45 degree yaw: the
        # quaternion stays unit length, which a componentwise lerp would not.
        assert mid[3] == pytest.approx(math.cos(math.pi / 8))
        assert mid[6] == pytest.approx(math.sin(math.pi / 8))
        assert sum(c * c for c in mid[3:]) == pytest.approx(1.0)
        assert tl.sample_objects(99.0)["block"].pose == (0.3, 0.0, 0.24, *_Q_Z90)
        assert tl.sample_objects(4.0)["block"].physics is True

    def test_tool_action_tracks_span_the_action_and_flag_a_guess(self):
        seg = _seg(duration=2.0, joints=[0] * 6)
        drop = ToolAction(
            tcp_pose=None,
            motions=[],
            target_positions=(0.0,),
            activation_type="gripper",
            line_number=1,
            method="open",
            estimated_duration=1.0,
            segment_index=0,
            object_tracks=[
                _track(
                    "block",
                    [[0.3, 0.0, 0.24, *_Q_ID], [0.3, 0.0, 0.04, *_Q_ID]],
                    physics=False,
                )
            ],
        )
        tl = Timeline.from_segments([seg], [drop])

        # The release starts when the segment's motion ends and lasts the action.
        assert tl.sample_objects(2.0)["block"].pose[2] == pytest.approx(0.24)
        falling = tl.sample_objects(2.5)["block"]
        assert falling.pose[2] == pytest.approx(0.14)
        assert falling.physics is False, (
            "a geometric fallback must be marked as a guess"
        )
        assert tl.sample_objects(3.0)["block"].pose[2] == pytest.approx(0.04)

    def test_no_tracks_means_no_objects(self):
        tl = Timeline.from_segments([_seg(duration=1.0, joints=[0] * 6)])
        assert tl.sample_objects(0.5) == {}
