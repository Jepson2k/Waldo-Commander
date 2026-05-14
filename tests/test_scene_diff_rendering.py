"""Unit tests for scene diff rendering: fingerprinting, diffing, and opacity."""

from waldo_commander.state import PathSegment, ProgramTarget, ToolAction
from waldo_commander.services.urdf_scene.urdf_scene import (
    RenderedSegment,
    RenderedItem,
    _segment_fingerprint,
    _tool_action_fingerprint,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _seg(
    points=None,
    color="#00FF00",
    is_valid=True,
    line_number=1,
    is_dashed=True,
    show_arrows=True,
    is_travel=False,
    joints=None,
):
    """Create a PathSegment with sensible defaults."""
    if points is None:
        points = [[0, 0, 0], [1, 0, 0]]
    return PathSegment(
        points=points,
        color=color,
        is_valid=is_valid,
        line_number=line_number,
        joints=joints or [0.0] * 6,
        is_dashed=is_dashed,
        show_arrows=show_arrows,
        is_travel=is_travel,
    )


def _action(
    tcp_pose=None,
    motions=None,
    target_positions=(1.0,),
    start_positions=(0.0,),
    segment_index=0,
    tcp_path=None,
):
    """Create a ToolAction with sensible defaults."""
    return ToolAction(
        tcp_pose=tcp_pose or [0.1, 0.2, 0.3, 0, 0, 0],
        motions=motions or [{"type": "linear", "axis": [0, 0, 1], "travel_m": 0.01}],
        target_positions=target_positions,
        activation_type="immediate",
        line_number=1,
        method="close",
        start_positions=start_positions,
        segment_index=segment_index,
        tcp_path=tcp_path,
    )


# ── Segment Fingerprint Tests ───────────────────────────────────────────


class TestSegmentFingerprint:
    def test_same_segment_same_fingerprint(self):
        segments = [_seg()]
        fp1 = _segment_fingerprint(segments, 0, 3)
        fp2 = _segment_fingerprint(segments, 0, 3)
        assert fp1 == fp2

    def test_different_endpoints_different_fingerprint(self):
        s1 = _seg(points=[[0, 0, 0], [1, 0, 0]])
        s2 = _seg(points=[[0, 0, 0], [2, 0, 0]])
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 != fp2

    def test_same_endpoints_different_count_different_fingerprint(self):
        s1 = _seg(points=[[0, 0, 0], [1, 0, 0]])
        s2 = _seg(points=[[0, 0, 0], [0.5, 0, 0], [1, 0, 0]])
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 != fp2

    def test_line_number_excluded(self):
        s1 = _seg(line_number=5)
        s2 = _seg(line_number=10)
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 == fp2

    def test_color_change_different_fingerprint(self):
        s1 = _seg(color="#00FF00")
        s2 = _seg(color="#FF0000")
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 != fp2

    def test_validity_change_different_fingerprint(self):
        s1 = _seg(is_valid=True)
        s2 = _seg(is_valid=False)
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 != fp2

    def test_neighbor_validity_affects_fingerprint(self):
        """A segment's fingerprint changes when a neighbor's validity changes."""
        valid = _seg(is_valid=True, points=[[0, 0, 0], [1, 0, 0]])
        invalid = _seg(is_valid=False, points=[[2, 0, 0], [3, 0, 0]])
        valid2 = _seg(is_valid=True, points=[[2, 0, 0], [3, 0, 0]])

        # Segment 0 with valid neighbor at index 1
        fp_valid_neighbor = _segment_fingerprint([valid, valid2], 0, 3)
        # Segment 0 with invalid neighbor at index 1
        fp_invalid_neighbor = _segment_fingerprint([valid, invalid], 0, 3)
        assert fp_valid_neighbor != fp_invalid_neighbor

    def test_dashed_change(self):
        s1 = _seg(is_dashed=True)
        s2 = _seg(is_dashed=False)
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 != fp2

    def test_show_arrows_change(self):
        s1 = _seg(show_arrows=True)
        s2 = _seg(show_arrows=False)
        fp1 = _segment_fingerprint([s1], 0, 3)
        fp2 = _segment_fingerprint([s2], 0, 3)
        assert fp1 != fp2

    def test_empty_points(self):
        s = _seg(points=[])
        fp = _segment_fingerprint([s], 0, 3)
        assert fp[0] == ()  # first endpoint
        assert fp[1] == ()  # last endpoint


# ── Tool Action Fingerprint Tests ────────────────────────────────────────


class TestToolActionFingerprint:
    def test_same_action_same_fingerprint(self):
        a = _action()
        assert _tool_action_fingerprint(a) == _tool_action_fingerprint(a)

    def test_different_position_different_fingerprint(self):
        a1 = _action(tcp_pose=[0.1, 0.2, 0.3, 0, 0, 0])
        a2 = _action(tcp_pose=[0.4, 0.5, 0.6, 0, 0, 0])
        assert _tool_action_fingerprint(a1) != _tool_action_fingerprint(a2)

    def test_different_target_positions(self):
        a1 = _action(target_positions=(0.0,))
        a2 = _action(target_positions=(1.0,))
        assert _tool_action_fingerprint(a1) != _tool_action_fingerprint(a2)

    def test_different_motions(self):
        a1 = _action(motions=[{"type": "linear", "axis": [0, 0, 1], "travel_m": 0.01}])
        a2 = _action(motions=[{"type": "linear", "axis": [1, 0, 0], "travel_m": 0.02}])
        assert _tool_action_fingerprint(a1) != _tool_action_fingerprint(a2)

    def test_cascading_vs_single(self):
        a1 = _action(tcp_path=None)
        a2 = _action(tcp_path=[[0, 0, 0], [1, 0, 0]])
        assert _tool_action_fingerprint(a1) != _tool_action_fingerprint(a2)

    def test_none_tcp_pose(self):
        a = ToolAction(
            tcp_pose=None,
            motions=[],
            target_positions=(1.0,),
            activation_type="immediate",
            line_number=1,
            method="close",
        )
        fp = _tool_action_fingerprint(a)
        assert fp[0] == ()


# ── PathSegment.is_travel Tests ──────────────────────────────────────────


class TestIsTravel:
    def test_default_false(self):
        s = _seg()
        assert s.is_travel is False

    def test_from_dict_with_is_travel(self):
        d = {
            "points": [[0, 0, 0]],
            "color": "#00FF00",
            "is_valid": True,
            "line_number": 1,
            "is_travel": True,
        }
        s = PathSegment.from_dict(d)
        assert s.is_travel is True

    def test_from_dict_without_is_travel(self):
        d = {
            "points": [[0, 0, 0]],
            "color": "#00FF00",
            "is_valid": True,
            "line_number": 1,
        }
        s = PathSegment.from_dict(d)
        assert s.is_travel is False


# ── RenderedSegment / RenderedItem Tests ─────────────────────────────────


class TestRenderedTypes:
    def test_rendered_segment_replace_line_number(self):
        rs = RenderedSegment(
            objects=["obj1"],
            colors=["#fff"],
            uses_vc=False,
            fingerprint=("fp",),
            line_number=5,
        )
        rs2 = rs._replace(line_number=10)
        assert rs2.line_number == 10
        assert rs2.objects is rs.objects  # same reference, no copy

    def test_rendered_item_segment_index(self):
        ri = RenderedItem(objects=["arrow"], fingerprint=("fp",), segment_index=3)
        assert ri.segment_index == 3


# ── ProgramTarget.is_valid Tests ─────────────────────────────────────────


class TestProgramTargetValidity:
    def test_default_valid(self):
        t = ProgramTarget(
            id="t1",
            line_number=1,
            pose=[0, 0, 0, 0, 0, 0],
            move_type="cartesian",
            scene_object_id="",
        )
        assert t.is_valid is True

    def test_invalid_from_dict(self):
        t = ProgramTarget.from_dict(
            {
                "id": "t1",
                "line_number": 1,
                "pose": [0, 0, 0],
                "move_type": "cartesian",
                "scene_object_id": "",
                "is_valid": False,
            }
        )
        assert t.is_valid is False

    def test_valid_from_dict_default(self):
        t = ProgramTarget.from_dict(
            {
                "id": "t1",
                "line_number": 1,
                "pose": [0, 0, 0],
                "move_type": "cartesian",
                "scene_object_id": "",
            }
        )
        assert t.is_valid is True


# ── Failed Move Target Collection Tests ──────────────────────────────────


class TestCollectFailedTarget:
    """Test that _collect_failed_target creates targets for failed moves."""

    def _make_client(self):
        """Create a PathPreviewClient with a mock dry-run client."""
        from unittest.mock import MagicMock
        from waldo_commander.services.path_preview_client import PathPreviewClient

        mock_cls = MagicMock()
        mock_cls.return_value = MagicMock()
        targets: list = []
        client = PathPreviewClient(
            dry_run_client_cls=mock_cls,
            target_collector=targets,
        )
        return client, targets

    def test_cartesian_move_failed_creates_target(self):
        client, targets = self._make_client()
        # Simulate source line with literal args
        import linecache

        linecache.cache["simulation_script.py"] = (
            100,
            None,
            ["robot.move_l([100, 200, 300, 0, 0, 0])\n"],
            "simulation_script.py",
        )
        try:
            client._collect_failed_target(
                line_no=1,
                move_type="cartesian",
                args=([100.0, 200.0, 300.0, 0.0, 0.0, 0.0],),
                kwargs={},
            )
            assert len(targets) == 1
            t = targets[0]
            assert t["id"] == "auto_1"
            assert t["is_valid"] is False
            assert abs(t["pose"][0] - 0.1) < 1e-6  # 100mm -> 0.1m
            assert abs(t["pose"][1] - 0.2) < 1e-6
            assert abs(t["pose"][2] - 0.3) < 1e-6
        finally:
            linecache.cache.pop("simulation_script.py", None)

    def test_pose_kwarg_creates_target(self):
        client, targets = self._make_client()
        import linecache

        linecache.cache["simulation_script.py"] = (
            100,
            None,
            ["robot.move_j(pose=[100, 200, 300, 0, 0, 0])\n"],
            "simulation_script.py",
        )
        try:
            client._collect_failed_target(
                line_no=1,
                move_type="joints",
                args=(),
                kwargs={"pose": [100.0, 200.0, 300.0, 0.0, 0.0, 0.0]},
            )
            assert len(targets) == 1
            assert targets[0]["id"] == "auto_1"
            assert targets[0]["is_valid"] is False
        finally:
            linecache.cache.pop("simulation_script.py", None)

    def test_joint_angles_no_target(self):
        """move_j with joint angles (no pose) can't determine TCP position."""
        client, targets = self._make_client()
        import linecache

        linecache.cache["simulation_script.py"] = (
            100,
            None,
            ["robot.move_j([0, 0, 0, 0, 0, 0])\n"],
            "simulation_script.py",
        )
        try:
            client._collect_failed_target(
                line_no=1,
                move_type="joints",
                args=([0, 0, 0, 0, 0, 0],),
                kwargs={},
            )
            assert len(targets) == 0  # No target — can't derive TCP pose
        finally:
            linecache.cache.pop("simulation_script.py", None)

    def test_variable_args_creates_target(self):
        """Computed/variable-arg pose calls (e.g. move_l(circle_pt(...))) still
        produce a red marker so the user can see WHERE a failed IK was attempted.
        Drag-edit-back-to-source isn't supported for non-literal sources, but
        that's a UI concern — the marker should still render."""
        client, targets = self._make_client()
        import linecache

        linecache.cache["simulation_script.py"] = (
            100,
            None,
            ["robot.move_l(some_variable)\n"],
            "simulation_script.py",
        )
        try:
            client._collect_failed_target(
                line_no=1,
                move_type="cartesian",
                args=([100, 200, 300, 0, 0, 0],),
                kwargs={},
            )
            assert len(targets) == 1
            assert targets[0]["is_valid"] is False
        finally:
            linecache.cache.pop("simulation_script.py", None)
