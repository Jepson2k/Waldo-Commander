"""Planar fiducial localization from explicit images and measured setup data."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np
from scipy.spatial.transform import Rotation
from waldoctl.camera import CameraCalibration
from waldoctl.setup import Pose, SetupSnapshot, TcpCalibration

from waldo_commander.camera import CameraSnapshot
from waldo_commander.services import handeye


@dataclass(frozen=True)
class LocalizationLimits:
    """Image-space acceptance limits; these are not an accuracy guarantee."""

    max_rms_px: float = 2.0
    max_corner_error_px: float = 4.0
    min_image_fraction: float = 0.002
    ambiguity_gap_px: float = 0.2
    ambiguity_rotation_deg: float = 2.0
    ambiguity_translation_mm: float = 2.0

    def __post_init__(self) -> None:
        for value in (
            self.max_rms_px,
            self.max_corner_error_px,
            self.min_image_fraction,
            self.ambiguity_gap_px,
            self.ambiguity_rotation_deg,
            self.ambiguity_translation_mm,
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Localization limits must be finite and positive")
        if self.min_image_fraction > 1:
            raise ValueError("Minimum image fraction cannot exceed one")


@dataclass(frozen=True)
class DetectionQuality:
    corners: int
    rms_px: float
    max_corner_error_px: float
    image_fraction: float
    alternative_gap_px: float | None


@dataclass(frozen=True)
class LocalizationResult:
    """A found pose is board→WRF; a missing/rejected observation has no pose."""

    outcome: Literal["found", "missing", "rejected"]
    pose: Pose | None
    received_at: float
    camera_id: str
    quality: DetectionQuality | None
    reason: str = ""


def localize_board(
    observation: CameraSnapshot,
    calibration: CameraCalibration,
    setup: SetupSnapshot,
    *,
    backend: str,
    tcp_pose: Pose | None = None,
    tool: TcpCalibration | None = None,
    board: handeye.BoardSpec | None = None,
    limits: LocalizationLimits = LocalizationLimits(),
) -> LocalizationResult:
    """Locate a known ChArUco board without acquiring images or commanding motion.

    Acquisition supplies a fresh image and, for a tool camera, the stationary
    or independently synchronized TCP pose and binding. Board axes follow
    OpenCV's board coordinates: origin at its first outer corner, X along
    squares_x, Y along squares_y. Planar pose alternatives are checked rather
    than silently selecting a substantially different, equally plausible pose.
    """

    def result(outcome, reason, *, pose=None, quality=None):
        return LocalizationResult(
            outcome,
            pose,
            observation.received_at,
            observation.camera_id,
            quality,
            reason,
        )

    image = handeye.decode_jpeg(observation.jpeg)
    if image is None:
        return result("rejected", "Image cannot be decoded")
    size = (image.shape[1], image.shape[0])
    camera_wrf = calibration.world_pose(
        setup,
        camera_id=observation.camera_id,
        image_size=size,
        backend=backend,
        tcp_pose=tcp_pose,
        tool=tool,
    )
    spec = (
        board
        if board is not None
        else handeye.BoardSpec.from_dict(dict(calibration.board))
    )
    if any(
        type(n) is not int or not 3 <= n <= 100
        for n in (spec.squares_x, spec.squares_y)
    ):
        raise ValueError("Localization board needs 3–100 squares on each axis")
    if not math.isfinite(spec.square_mm) or not math.isfinite(spec.marker_mm):
        raise ValueError("Board dimensions must be finite")
    detector = handeye.make_detector(spec)
    detection = handeye.detect_board(image, detector)
    if detection is None:
        return result("missing", "No usable board detected")
    object_points = np.asarray(
        detector.getBoard().getChessboardCorners()[detection.ids.reshape(-1)],
        dtype=np.float64,
    ).reshape(-1, 3)
    image_points = np.asarray(detection.corners, dtype=np.float64).reshape(-1, 2)
    fraction = float(
        cv2.contourArea(cv2.convexHull(image_points.astype(np.float32)))
    ) / (size[0] * size[1])
    k = np.asarray(calibration.intrinsics.camera_matrix).reshape(3, 3)
    distortion = np.asarray(calibration.intrinsics.dist_coeffs)
    try:
        solved, rotations, translations, _ = cv2.solvePnPGeneric(
            object_points,
            image_points,
            k,
            distortion,
            flags=cv2.SOLVEPNP_IPPE,
        )
    except cv2.error:
        return result("rejected", "The detected corners do not determine a planar pose")
    if not solved:
        return result("rejected", "No planar pose solution")
    candidates: list[tuple[float, float, Pose]] = []
    for rotation, translation in zip(rotations, translations, strict=True):
        transform = np.eye(4)
        transform[:3, :3] = cv2.Rodrigues(rotation)[0]
        transform[:3, 3] = np.asarray(translation).reshape(3)
        if not np.isfinite(transform).all():
            continue
        if np.any((object_points @ transform[:3, :3].T + transform[:3, 3])[:, 2] <= 0):
            continue
        projected, _ = cv2.projectPoints(
            object_points, rotation, translation, k, distortion
        )
        errors = np.linalg.norm(projected.reshape(-1, 2) - image_points, axis=1)
        candidates.append(
            (
                float(np.sqrt(np.mean(errors**2))),
                float(errors.max()),
                Pose.from_matrix(transform),
            )
        )
    if not candidates:
        return result(
            "rejected", "No finite pose places the board in front of the camera"
        )
    candidates.sort(key=lambda candidate: candidate[0])
    rms, maximum, camera_board = candidates[0]
    gap = candidates[1][0] - rms if len(candidates) > 1 else None
    quality = DetectionQuality(len(image_points), rms, maximum, fraction, gap)
    if fraction < limits.min_image_fraction:
        return result(
            "rejected",
            "Detected corners cover too little of the image",
            quality=quality,
        )
    if rms > limits.max_rms_px or maximum > limits.max_corner_error_px:
        return result(
            "rejected", "Corner reprojection error exceeds the limit", quality=quality
        )
    if gap is not None and gap < limits.ambiguity_gap_px:
        relative = np.linalg.inv(camera_board.matrix()) @ candidates[1][2].matrix()
        rotation_difference = float(
            np.linalg.norm(Rotation.from_matrix(relative[:3, :3]).as_rotvec())
            * 180
            / np.pi
        )
        if (
            rotation_difference > limits.ambiguity_rotation_deg
            or np.linalg.norm(relative[:3, 3]) > limits.ambiguity_translation_mm
        ):
            return result(
                "rejected",
                "Planar pose is ambiguous; use a more oblique view",
                quality=quality,
            )
    return result(
        "found",
        "",
        pose=Pose.from_matrix(camera_wrf.matrix() @ camera_board.matrix()),
        quality=quality,
    )
