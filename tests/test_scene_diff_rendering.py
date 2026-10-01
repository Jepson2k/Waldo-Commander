"""The fingerprints the scene diff keys its redraws on."""

from waldo_commander.state import PathSegment, ToolAction
from waldo_commander.services.urdf_scene.urdf_scene import (
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
):
    """Create a PathSegment with sensible defaults."""
    if points is None:
        points = [[0, 0, 0], [1, 0, 0]]
    return PathSegment(
        points=points,
        color=color,
        is_valid=is_valid,
        line_number=line_number,
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


# ── Fingerprints ─────────────────────────────────────────────────────────


def test_segment_fingerprint_changes_exactly_when_the_drawing_would():
    """The scene diff redraws a segment when its fingerprint changes: any
    drawn property or a neighbour's validity changes it, the line number
    does not, and a segment without points still has one."""
    base = _segment_fingerprint([_seg()], 0, 3)
    assert _segment_fingerprint([_seg()], 0, 3) == base
    for name, changed, redraws in (
        ("end point", _seg(points=[[0, 0, 0], [2, 0, 0]]), True),
        ("point count", _seg(points=[[0, 0, 0], [0.5, 0, 0], [1, 0, 0]]), True),
        ("colour", _seg(color="#FF0000"), True),
        ("validity", _seg(is_valid=False), True),
        ("dashes", _seg(is_dashed=False), True),
        ("arrows", _seg(show_arrows=False), True),
        ("line number", _seg(line_number=10), False),
    ):
        assert (_segment_fingerprint([changed], 0, 3) != base) is redraws, name

    valid = _seg(points=[[0, 0, 0], [1, 0, 0]])
    neighbour = [[2, 0, 0], [3, 0, 0]]
    assert _segment_fingerprint(
        [valid, _seg(points=neighbour)], 0, 3
    ) != _segment_fingerprint([valid, _seg(is_valid=False, points=neighbour)], 0, 3)

    empty = _segment_fingerprint([_seg(points=[])], 0, 3)
    assert empty[0] == () and empty[1] == ()


def test_tool_action_fingerprint_changes_with_what_the_jaws_draw():
    """A tool action redraws when its pose, targets, motions or path change,
    and one without a TCP pose still has a fingerprint."""
    base = _tool_action_fingerprint(_action())
    assert _tool_action_fingerprint(_action()) == base
    for name, changed in (
        ("position", _action(tcp_pose=[0.4, 0.5, 0.6, 0, 0, 0])),
        ("targets", _action(target_positions=(0.0,))),
        (
            "motions",
            _action(motions=[{"type": "linear", "axis": [1, 0, 0], "travel_m": 0.02}]),
        ),
        ("cascading path", _action(tcp_path=[[0, 0, 0], [1, 0, 0]])),
    ):
        assert _tool_action_fingerprint(changed) != base, name

    no_pose = ToolAction(
        tcp_pose=None,
        motions=[],
        target_positions=(1.0,),
        activation_type="immediate",
        line_number=1,
        method="close",
    )
    assert _tool_action_fingerprint(no_pose)[0] == ()
