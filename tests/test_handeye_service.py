"""Hand-eye calibration math against synthetic ground truth.

Renders real ChArUco views with a known camera mount transform and checks
the full pipeline (detector → matchImagePoints → calibrateCamera →
calibrateHandEye) recovers it.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from tests.helpers.charuco_render import (
    look_at_target_pose,
    render_board_view,
    synthesize_views,
)
from waldo_commander.services import handeye

SPEC = handeye.BoardSpec()
IMAGE_SIZE = (640, 480)
K_TRUE = np.array([[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]])


def _transform(
    xyz_mm: tuple[float, float, float], rotvec_deg: tuple[float, float, float]
) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = Rotation.from_rotvec(np.radians(rotvec_deg)).as_matrix()
    T[:3, 3] = xyz_mm
    return T


X_TRUE = _transform((40.0, -25.0, 60.0), (8.0, -5.0, 92.0))  # camera → gripper
T_BASE_TARGET = _transform((320.0, 90.0, 40.0), (2.0, 179.0, 15.0))


def _samples(n_views: int = 12) -> list[handeye.HandEyeSample]:
    detector = handeye.make_detector(SPEC)
    samples: list[handeye.HandEyeSample] = []
    for i, (T_base_gripper, T_cam_target) in enumerate(
        synthesize_views(SPEC, X_TRUE, T_BASE_TARGET, n_views=n_views)
    ):
        image = render_board_view(SPEC, K_TRUE, T_cam_target, IMAGE_SIZE)
        detection = handeye.detect_board(image, detector)
        assert detection is not None, f"synthetic view {i} must contain the board"
        samples.append(handeye.HandEyeSample(T_base_gripper, detection, float(i)))
    return samples


@pytest.mark.unit
def test_solve_recovers_known_transform():
    result = handeye.solve_hand_eye(_samples(), SPEC, method="PARK")

    R_err = result.T_cam2gripper[:3, :3].T @ X_TRUE[:3, :3]
    rot_err_deg = math.degrees(np.linalg.norm(Rotation.from_matrix(R_err).as_rotvec()))
    trans_err_mm = float(np.linalg.norm(result.T_cam2gripper[:3, 3] - X_TRUE[:3, 3]))
    assert rot_err_deg < 0.5, f"rotation off by {rot_err_deg:.3f} deg"
    assert trans_err_mm < 2.0, f"translation off by {trans_err_mm:.3f} mm"

    K = result.intrinsics.camera_matrix
    assert abs(K[0, 0] - 800.0) / 800.0 < 0.01
    assert abs(K[1, 1] - 800.0) / 800.0 < 0.01
    assert result.intrinsics.reproj_rms_px < 1.0
    assert result.rot_residual_deg[0] < 0.5
    assert result.trans_residual_mm[0] < 3.0
    assert result.target_spread_mm < 3.0

    # Round-trips through the persistence format without losing the transform.
    stored = handeye.to_storage_dict(
        result, SPEC, "NONE", {"x": 0.0, "y": 0.0, "z": 0.0}, "2026-01-01T00:00:00Z"
    )
    restored = handeye.from_storage_dict(stored)
    np.testing.assert_allclose(restored["T_cam2gripper"], result.T_cam2gripper)
    assert restored["n_samples"] == result.n_views


def _view_samples(
    rolls: tuple[float, ...],
    tilts: tuple[float, ...] | None = None,
    azimuths: tuple[float, ...] | None = None,
) -> list[handeye.HandEyeSample]:
    """Rendered views at the given look-at angles, as the real capture path
    would produce them."""
    n = len(rolls)
    tilts = tilts or (20.0,) * n
    azimuths = azimuths or (0.0,) * n
    detector = handeye.make_detector(SPEC)
    samples: list[handeye.HandEyeSample] = []
    X_inv = np.linalg.inv(X_TRUE)
    for i, (roll, tilt, azimuth) in enumerate(zip(rolls, tilts, azimuths, strict=True)):
        T_cam_target = look_at_target_pose(SPEC, 450.0, tilt, azimuth, roll)
        T_base_gripper = T_BASE_TARGET @ np.linalg.inv(T_cam_target) @ X_inv
        image = render_board_view(SPEC, K_TRUE, T_cam_target, IMAGE_SIZE)
        detection = handeye.detect_board(image, detector)
        assert detection is not None, f"view {i} must contain the board"
        samples.append(handeye.HandEyeSample(T_base_gripper, detection, float(i)))
    return samples


def _roll_only_samples(n_views: int = 6) -> list[handeye.HandEyeSample]:
    """Views differing only by camera roll — all relative rotations share the
    optical axis, the degenerate case a look-at sweep without wrist roll hits."""
    step = 150.0 / max(n_views - 1, 1)
    return _view_samples(tuple(-75.0 + step * i for i in range(n_views)))


@pytest.mark.unit
def test_solve_rejections():
    samples = _samples(6)

    with pytest.raises(handeye.CalibrationError, match="at least 4"):
        handeye.solve_hand_eye(samples[:3], SPEC)

    # Pure translation: same orientation everywhere → rotation unobservable.
    translated = []
    for i, s in enumerate(samples[:4]):
        T = samples[0].T_base_gripper.copy()
        T[0, 3] += 20.0 * i
        translated.append(handeye.HandEyeSample(T, s.detection, s.timestamp))
    max_rot, _ = handeye.motion_diversity([s.T_base_gripper for s in translated])
    assert max_rot < handeye.DEGENERATE_ROTATION_DEG
    with pytest.raises(handeye.CalibrationError, match="[Pp]ure-translation"):
        handeye.solve_hand_eye(translated, SPEC)

    # Single-axis motion: plenty of total rotation, but every relative
    # rotation shares one axis, so the mount offset along it is unobservable.
    single_axis = _roll_only_samples()
    max_rot, max_axis = handeye.motion_diversity(
        [s.T_base_gripper for s in single_axis]
    )
    assert max_rot > handeye.DEGENERATE_ROTATION_DEG, (
        f"fixture must clear the rotation gate, got {max_rot:.1f} deg"
    )
    assert max_axis < handeye.AXIS_DIVERSITY_MIN_DEG
    with pytest.raises(handeye.CalibrationError, match="[Ss]ingle-axis"):
        handeye.solve_hand_eye(single_axis, SPEC)

    # Near-180° flips make the solve return a reflection (det = -1) rather
    # than a rotation, which OpenCV reports without raising.
    flipped = _view_samples(
        rolls=(-90.0, 0.0, 90.0, 180.0),
        tilts=(20.0, 30.0, 20.0, 30.0),
        azimuths=(0.0, 90.0, 180.0, 270.0),
    )
    with pytest.raises(handeye.CalibrationError, match="non-rigid"):
        handeye.solve_hand_eye(flipped, SPEC, method="PARK")

    # One near-duplicate capture must not fake axis diversity: its relative
    # rotation is too small for its axis to be anything but noise.
    dupe = single_axis[2]
    nudged = dupe.T_base_gripper.copy()
    nudged[:3, 3] += np.array([4.0, 3.0, 2.0])
    padded = [*single_axis, handeye.HandEyeSample(nudged, dupe.detection, 99.0)]
    with pytest.raises(handeye.CalibrationError, match="[Ss]ingle-axis"):
        handeye.solve_hand_eye(padded, SPEC)

    # Mixed capture resolutions invalidate a shared intrinsics solve.
    mixed = list(samples)
    shrunk = handeye.Detection(
        corners=mixed[0].detection.corners / 2.0,
        ids=mixed[0].detection.ids,
        image_size=(IMAGE_SIZE[0] // 2, IMAGE_SIZE[1] // 2),
        n_markers=mixed[0].detection.n_markers,
    )
    mixed[0] = handeye.HandEyeSample(mixed[0].T_base_gripper, shrunk, 0.0)
    with pytest.raises(handeye.CalibrationError, match="resolution"):
        handeye.solve_hand_eye(mixed, SPEC)

    with pytest.raises(handeye.CalibrationError, match="method"):
        handeye.solve_hand_eye(samples, SPEC, method="NOPE")


@pytest.mark.unit
def test_board_png_roundtrip():
    png = handeye.board_png(SPEC, dpi=150)
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    detection = handeye.detect_board(image, handeye.make_detector(SPEC))
    assert detection is not None
    assert len(detection.corners) == (SPEC.squares_x - 1) * (SPEC.squares_y - 1)
