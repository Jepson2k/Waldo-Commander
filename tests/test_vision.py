"""Localize real rendered board images and reject unusable observations."""

from dataclasses import replace

import cv2
import numpy as np
import pytest
from waldoctl.camera import CameraCalibration, CameraIntrinsics, CameraQuality
from waldoctl.setup import Pose, SetupSnapshot, TcpCalibration

from tests.helpers.charuco_render import look_at_target_pose, render_board_view
from tests.test_handeye_service import SPEC, IMAGE_SIZE, K_TRUE, X_TRUE
from waldo_commander.camera import CameraSnapshot
from waldo_commander.vision import LocalizationLimits, localize_board


def snapshot(image, *, camera_id="fixture-camera"):
    ok, jpeg = cv2.imencode(".jpg", image)
    assert ok
    return CameraSnapshot(jpeg.tobytes(), camera_id, 42.0, 1, "fixture-session")


def localization_scene(camera_id="fixture-camera"):
    camera_pose = Pose((100, -50, 200, 5, 10, 20))
    board_camera = look_at_target_pose(SPEC, 450, 30, 10, 20)
    image = render_board_view(SPEC, K_TRUE, board_camera, IMAGE_SIZE)
    calibration = CameraCalibration(
        camera_id,
        "parol6",
        "fixed",
        camera_pose,
        CameraIntrinsics(tuple(K_TRUE.ravel()), (0, 0, 0, 0, 0), IMAGE_SIZE),
        CameraQuality(12, "PARK", 0.2, (0.1, 0.3), (0.2, 0.5), 0.2),
        "2026-09-07T00:00:00Z",
        SPEC.to_dict(),
        reference_wrf=Pose((0, 0, 0, 0, 0, 0)),
    )
    setup = SetupSnapshot().with_camera("overhead", calibration)
    expected = camera_pose.matrix() @ board_camera
    return calibration, setup, image, expected


@pytest.mark.unit
def test_board_localization_in_world_and_tool_frames():
    calibration, setup, image, expected = localization_scene()
    camera_pose = calibration.pose
    frame = snapshot(image)
    found = localize_board(frame, calibration, setup, backend="parol6")
    assert found.outcome == "found", found
    assert found.pose is not None
    np.testing.assert_allclose(found.pose.matrix()[:3, 3], expected[:3, 3], atol=2)
    np.testing.assert_allclose(found.pose.matrix()[:3, :3], expected[:3, :3], atol=0.01)
    assert found.received_at == frame.received_at
    assert found.quality.rms_px < 1

    tool = TcpCalibration((1, 2, 3, 4, 5, 6), "MSG")
    wrist = replace(
        calibration,
        mount="tool",
        pose=Pose.from_matrix(X_TRUE, frame="TCP"),
        reference_wrf=None,
        tool=tool,
    )
    tcp_pose = Pose.from_matrix(camera_pose.matrix() @ np.linalg.inv(X_TRUE))
    same = localize_board(
        frame, wrist, setup, backend="parol6", tcp_pose=tcp_pose, tool=tool
    )
    np.testing.assert_allclose(same.pose.matrix(), found.pose.matrix(), atol=1e-8)
    with pytest.raises(ValueError, match="TCP transform"):
        localize_board(
            frame,
            wrist,
            setup,
            backend="parol6",
            tcp_pose=tcp_pose,
            tool=replace(tool, values=(1, 2, 3, 4, 5, 7)),
        )
    with pytest.raises(ValueError, match="source"):
        localize_board(
            replace(frame, camera_id="different"), calibration, setup, backend="parol6"
        )

    missing = localize_board(
        snapshot(np.full_like(image, 255)), calibration, setup, backend="parol6"
    )
    assert missing.outcome == "missing" and missing.pose is None
    corrupt = localize_board(
        replace(frame, jpeg=b"invalid image"), calibration, setup, backend="parol6"
    )
    assert corrupt.outcome == "rejected" and corrupt.pose is None
    small = localize_board(
        frame,
        calibration,
        setup,
        backend="parol6",
        limits=LocalizationLimits(min_image_fraction=0.8),
    )
    assert small.outcome == "rejected" and "cover" in small.reason
    ambiguous = localize_board(
        frame,
        calibration,
        setup,
        backend="parol6",
        limits=LocalizationLimits(ambiguity_gap_px=100),
    )
    assert ambiguous.outcome == "rejected" and "ambiguous" in ambiguous.reason


def test_localization_preview_requires_explicit_observations(tmp_path):
    from parol6.client.dry_run_client import DryRunRobotClient
    from waldoctl.skills import MissingCapability, UnresolvedPreview
    from waldo_commander.camera_sources import CommanderCameraSource, ImageFixture
    from waldo_commander.services.path_preview_client import PathPreviewClient
    from waldo_commander.skills import locate_board

    calibration, setup, image, expected = localization_scene()
    preview = PathPreviewClient(DryRunRobotClient)
    with pytest.raises(UnresolvedPreview, match="ImageFixture"):
        locate_board(preview, calibration, CommanderCameraSource(), setup)
    filename = tmp_path / "board.jpg"
    assert cv2.imwrite(str(filename), image)
    fixture = ImageFixture.from_file(filename, camera_id=calibration.camera_id)
    found = locate_board(preview, calibration, fixture, setup)
    assert found.outcome == "found"
    np.testing.assert_allclose(found.pose.matrix()[:3, 3], expected[:3, 3], atol=2)
    missing = locate_board(
        preview, calibration, ImageFixture(snapshot(np.full_like(image, 255))), setup
    )
    assert missing.outcome == "missing" and missing.pose is None
    wrist = replace(
        calibration,
        mount="tool",
        pose=Pose((0, 0, 0, 0, 0, 0), frame="TCP"),
        tool=TcpCalibration((0, 0, 0, 0, 0, 0), "NONE"),
        reference_wrf=None,
    )
    with pytest.raises(ValueError):
        locate_board(preview, wrist, fixture, setup)
    found = locate_board(
        preview,
        wrist,
        replace(fixture, tcp_pose=calibration.pose, tool=wrist.tool),
        setup,
    )
    np.testing.assert_allclose(found.pose.matrix()[:3, 3], expected[:3, 3], atol=2)
    assert not preview.segment_collector, "Localization must not create motion"

    # The skill localizes against the backend the client drives, not the one
    # the calibration names: comparing the calibration against itself made the
    # recalibrate-after-a-backend-change refusal unreachable.
    from dataclasses import replace as _replace

    other_backend = _replace(calibration, backend="par6")
    with pytest.raises(MissingCapability, match="par6"):
        locate_board(preview, other_backend, fixture, setup)
    from waldo_commander.skills.vision import _client_backend

    assert _client_backend(preview, "unused") == "parol6"
