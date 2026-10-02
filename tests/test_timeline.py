"""The timeline over a program's records: windows from the commanded
record's blocks, poses from whichever record plays back, tool keyframes
from the jaw column. Records come from parol6's own dry run."""

from dataclasses import replace

import numpy as np
import pytest
from parol6.client.dry_run_client import DryRunRobotClient
from parol6.config import HOME_ANGLES_DEG
from waldoctl import ObjectTicks, TickIndex

from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.preview_segments import (
    index_boundaries,
    segments_from_record,
)
from waldo_commander.services.timeline import Timeline

START = [85, -85, 135, 10, 45, 170]
AWAY = [90, -90, 140, 15, 50, 175]


def _preview() -> PathPreviewClient:
    return PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient, initial_joints=np.radians(START)
    )


def _planned(client: PathPreviewClient):
    client.close()
    record = client.plan()
    segments = segments_from_record(record, client.notes)
    return record, segments


def test_segments_window_the_commanded_record_and_samples_read_its_rows():
    client = _preview()
    client.move_j(AWAY, speed=1.0)
    client.delay(0.5)
    client.checkpoint("mid")
    client.move_j(START, speed=1.0)
    record, segments = _planned(client)
    tl = Timeline.from_record(record, segments)

    dt = record.row_dt_s
    assert [s.move_type for s in segments] == [
        "joints",
        "delay",
        "checkpoint",
        "joints",
    ]
    assert tl.total_duration == pytest.approx(record.duration_s)
    assert tl.cumulative_times == pytest.approx(
        [s.start_row * dt for s in segments] + [record.rows * dt]
    )
    assert tl.segment_durations[1] == pytest.approx(0.5, abs=dt)
    assert tl.segment_durations[2] == 0.0, "a checkpoint owns no time"

    # A hold is time on the bar: the arm stands where the move left it.
    t = tl.cumulative_times[1] + 0.25
    sample = tl.sample(t)
    assert sample.segment_index == 1
    assert sample.joints == pytest.approx(record.joints_rad[record.row_at(t)].tolist())
    assert np.degrees(sample.joints) == pytest.approx(AWAY, abs=0.5)
    assert 0.0 < sample.fraction < 1.0

    assert tl.sample(-1.0).time == 0.0
    end = tl.sample(999.0)
    assert end.time == pytest.approx(tl.total_duration)
    assert end.segment_index == 3
    assert end.joints == pytest.approx(record.joints_rad[-1].tolist())

    assert [(cp.kind, cp.segment_index) for cp in tl.checkpoints] == [("mid", 2)]
    assert tl.checkpoints[0].time == pytest.approx(tl.cumulative_times[2])
    assert tl.next_checkpoint(0.0) is tl.checkpoints[0]
    assert tl.next_checkpoint(tl.total_duration + 1.0) is None

    empty, none = _planned(_preview())
    assert none == []
    assert Timeline.from_record(empty, none).sample(0.5).joints is None


def test_home_is_a_planned_move_when_referenced_and_a_snap_when_not():
    client = _preview()
    client.move_j(AWAY, speed=1.0)
    client.home()
    record, segments = _planned(client)
    tl = Timeline.from_record(record, segments)
    assert [s.checkpoint for s in segments] == [None, "home"]
    assert tl.segment_durations[1] > 0.0
    assert np.degrees(tl.sample(tl.total_duration).joints) == pytest.approx(
        HOME_ANGLES_DEG, abs=0.6
    )

    client = PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient,
        initial_joints=np.radians(START),
        initial_homed=False,
    )
    client.home()
    record, segments = _planned(client)
    assert record.rows <= 1, "an unreferenced arm references itself without a move"
    assert [(s.checkpoint, s.rows) for s in segments] == [("home", record.rows)]
    assert Timeline.from_record(record, segments).total_duration <= record.row_dt_s
    assert client.angles() == pytest.approx(HOME_ANGLES_DEG, abs=0.6)


def test_a_predicted_record_plays_its_own_rows_at_the_same_point_of_each_command():
    client = _preview()
    client.move_j(AWAY, speed=1.0)
    client.move_j(START, speed=1.0)
    commanded, segments = _planned(client)
    first, second = commanded.blocks
    dt = commanded.row_dt_s

    # The simulated arm settles for five rows after the first move and runs
    # a hair behind its command throughout; an object rides along, and the
    # simulation loses it on one row.
    settle = 5
    head = commanded.joints_rad[: first.rows]
    hold = np.repeat(commanded.joints_rad[first.rows - 1 : first.rows], settle, axis=0)
    tail = commanded.joints_rad[first.rows :]
    joints = np.concatenate([head, hold, tail]) + np.float32(0.01)
    rows = len(joints)
    poses = np.zeros((rows, 7), dtype=np.float32)
    poses[:, 0] = np.linspace(0.3, 0.4, rows)
    poses[:, 3] = 1.0
    lost = first.rows + settle + 3
    poses[lost] = np.nan
    predicted = TickIndex(
        row_dt_s=dt,
        joints_rad=joints,
        tcp=np.concatenate(
            [
                commanded.tcp[: first.rows],
                np.repeat(commanded.tcp[first.rows - 1 : first.rows], settle, axis=0),
                commanded.tcp[first.rows :],
            ]
        ),
        tool_closed=np.zeros(rows, dtype=np.float32),
        tool_gripping=np.zeros(rows, dtype=np.bool_),
        blocks=(
            replace(first, rows=first.rows + settle),
            replace(second, start_row=first.rows + settle),
        ),
        objects=(ObjectTicks(name="block", poses=poses),),
        digest=b"predicted",
    )
    tl = Timeline.from_record(commanded, segments, predicted=predicted)

    assert tl.predicted is predicted
    assert tl.total_duration == pytest.approx(commanded.duration_s), (
        "the scrub bar counts the program's own time"
    )
    early = 2 * dt
    late = commanded.duration_s - 2 * dt
    assert tl.predicted_row(early) == commanded.row_at(early)
    assert tl.predicted_row((first.rows - 1) * dt) == first.rows - 1
    assert tl.predicted_row(late) == commanded.row_at(late) + settle
    sample = tl.sample(late)
    assert sample.segment_index == 1
    assert sample.joints == pytest.approx(
        joints[commanded.row_at(late) + settle].tolist(), abs=1e-6
    )
    assert sample.joints != pytest.approx(
        commanded.joints_rad[commanded.row_at(late)].tolist(), abs=1e-6
    ), "the pose played is the prediction, not the command"

    block = tl.sample_objects(early)["block"]
    assert block.physics
    assert block.pose[0] == pytest.approx(float(poses[commanded.row_at(early), 0]))
    assert "block" not in tl.sample_objects((lost - settle) * dt)
    assert Timeline.from_record(commanded, segments).sample_objects(early) == {}


def test_tool_keyframes_and_selections_come_from_the_record():
    client = _preview()
    client.select_tool("SSG-48", "pinch")
    client.move_j(AWAY, speed=1.0)
    client.tool.close()
    client.move_j(START, speed=1.0)
    client.tool.open()
    record, segments = _planned(client)
    selections = client.tool_selection_collector
    index_boundaries(segments, selections)
    tl = Timeline.from_record(record, segments, tool_selections=selections)

    assert len(tl.tool_spans) == 2 and all(span.blocking for span in tl.tool_spans)
    close, open_ = tl.tool_spans
    assert 0.0 < close.start < close.end < open_.start < open_.end
    assert tl.sample_tool(0.0) == pytest.approx((0.0,))
    assert tl.sample_tool((close.start + close.end) / 2) == pytest.approx(
        (0.5,), abs=0.1
    )
    assert tl.sample_tool((close.end + open_.start) / 2) == pytest.approx((1.0,))
    assert tl.sample_tool(tl.total_duration) == pytest.approx((0.0,), abs=0.05)

    hold = next(s for s in segments if s.move_type == "tool_action")
    assert hold.rows > 0 and hold.points[0] == hold.points[-1], (
        "a jaw move holds the arm: time on the bar, no length in the scene"
    )
    assert hold.start_row * record.row_dt_s == pytest.approx(
        close.start, abs=2 * record.row_dt_s
    )

    assert [
        (k.time, k.tool_key, k.variant_key) for k in tl.tool_selection_keyframes
    ] == [(0.0, "SSG-48", "pinch")]
    selected = tl.sample_tool_selection(tl.total_duration / 2)
    assert selected is not None and selected.tool_key == "SSG-48"
