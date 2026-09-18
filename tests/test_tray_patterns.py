"""Pattern geometry, preview branches, and explicit progress persistence."""

import json
from dataclasses import asdict

import numpy as np
import pytest
from parol6 import Robot
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl.setup import Frame, Pose, SetupSnapshot
from waldoctl.signals import DigitalSignal
from waldoctl.skills import SkillError, UnresolvedPreview

from tests.test_skill_library import START, pose_of
from waldo_commander.patterns import (
    PatternProgress,
    grid_poses,
    load_progress,
    offset_poses,
    save_progress,
)
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.skills import SignalFixture, transfer, transfer_with_signal


def preview_with_gripper():
    tool = Robot().tools["PNEUMATIC"]
    preview = PathPreviewClient(
        DryRunRobotClient,
        initial_joints=np.radians(START),
        tool_meta_registry={
            "PNEUMATIC": {
                "motions": [{"type": "linear", **asdict(m)} for m in tool.motions],
                "activation_type": tool.activation_type.value,
            }
        },
    )
    preview.select_tool("PNEUMATIC")
    return preview


def test_tray_frames_transfer_preview_and_advisory_progress(tmp_path):
    setup = SetupSnapshot(frames={"tray": Frame((20, 30, 40, 0, 0, 90))})
    origin = Pose((1, 2, 3, 10, 20, 30), frame="tray")
    local = grid_poses(
        origin,
        rows=2,
        columns=2,
        layers=2,
        pitch_x_mm=10,
        pitch_y_mm=20,
        pitch_z_mm=30,
        serpentine=True,
    )
    poses = tuple(setup.resolve(pose) for pose in local)
    assert len(poses) == 8
    np.testing.assert_allclose(
        np.asarray(poses[1].values[:3]) - poses[0].values[:3], (0, 10, 0), atol=1e-8
    )
    np.testing.assert_allclose(
        np.asarray(poses[2].values[:3]) - poses[1].values[:3], (-20, 0, 0), atol=1e-8
    )
    np.testing.assert_allclose(
        np.asarray(poses[4].values[:3]) - poses[0].values[:3], (0, 0, 30), atol=1e-8
    )
    custom = offset_poses(origin, [(0, 0, 0), (10, 0, 0)])
    assert custom == local[:2]
    for changes in (
        {"rows": True},
        {"columns": 0},
        {"pitch_x_mm": 0},
        {"pitch_y_mm": float("nan")},
        {"layers": 100_000},
        # A tray built from configuration or CSV data arrives as text, and the
        # message promises a ValueError about the pitch rather than a TypeError
        # from inside a finiteness check.
        {"pitch_x_mm": "25"},
    ):
        args = {"rows": 2, "columns": 2, "pitch_x_mm": 1, "pitch_y_mm": 1, **changes}
        with pytest.raises(ValueError):
            grid_poses(origin, **args)
    for bad in ([("0", "25", "0")], [(0, 0, None)], [(0, 0)]):
        with pytest.raises(ValueError, match="millimeter"):
            offset_poses(origin, bad)

    progress = PatternProgress.for_poses(poses).mark(1).mark(3)
    progress = progress.mark(1, completed=False)
    assert progress.pending() == (0, 1, 2, 4, 5, 6, 7)
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(progress.to_dict()))
    restored = load_progress(path, poses)
    assert restored.completed == {3}
    with pytest.raises(ValueError, match="changed"):
        load_progress(path, list(reversed(poses)))
    invalid = progress.to_dict()
    invalid["completed"] = [8]
    path.write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match="indices"):
        load_progress(path, poses)
    path.write_text(json.dumps(progress.to_dict()))
    before = path.read_bytes()

    preview = preview_with_gripper()
    pick = pose_of(preview)
    targets = grid_poses(pick, rows=2, columns=2, pitch_x_mm=1, pitch_y_mm=1)
    for place in targets:
        transfer(preview, pick=pick, place=place, clearance_mm=2, speed=0.5)
    assert len(preview.segment_collector) == 24
    assert len(preview.tool_action_collector) == 8
    assert all(segment["is_valid"] for segment in preview.segment_collector)
    target = targets[-1].matrix()
    target[:3, 3] += 2 * target[:3, 2]
    np.testing.assert_allclose(pose_of(preview).matrix(), target, atol=0.1)
    assert not save_progress(path, restored.mark(0), client=preview)
    assert path.read_bytes() == before, "preview changed real completion records"

    signal = DigitalSignal("parol6", "output", 0, 2, 2)
    count = len(preview.segment_collector)
    with pytest.raises(UnresolvedPreview):
        transfer_with_signal(
            preview, pick=pick, place=pick, grip=signal, clearance_mm=2
        )
    assert len(preview.segment_collector) == count
    transfer_with_signal(
        preview,
        pick=pick,
        place=pick,
        grip=signal,
        clearance_mm=2,
        open_fixture=SignalFixture(False),
        closed_fixture=SignalFixture(True),
    )
    count = len(preview.segment_collector)
    with pytest.raises((ValueError, SkillError)):
        transfer(
            preview,
            pick=pick,
            place=Pose((0, 0, 0, 0, 0, 0), frame="tray"),
            clearance_mm=2,
        )
    assert len(preview.segment_collector) == count
