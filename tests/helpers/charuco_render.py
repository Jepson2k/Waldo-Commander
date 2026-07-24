"""Synthetic ChArUco views for hand-eye calibration tests.

Renders what a pinhole camera at a known pose would see of a ChArUco board,
by warping the board's own generated image with a homography built from
OpenCV's detected corners — so the board-image ↔ object-frame mapping comes
from OpenCV itself and no corner-origin convention is assumed.

Rendering is exact for zero lens distortion (a plane-to-plane map is a
homography only then); tests use dist=0 ground truth.

Units match the app: millimeters, 4x4 homogeneous transforms.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from waldo_commander.services import handeye

_FLAT_PX_PER_MM = 8


def _flat_board(spec: handeye.BoardSpec) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(flat image, corner pixels (N,2), corner object points mm (N,3)) by id."""
    board = handeye.make_board(spec)
    margin_px = round(spec.square_mm * _FLAT_PX_PER_MM)
    w = round(spec.squares_x * spec.square_mm * _FLAT_PX_PER_MM) + 2 * margin_px
    h = round(spec.squares_y * spec.square_mm * _FLAT_PX_PER_MM) + 2 * margin_px
    flat = board.generateImage((w, h), marginSize=margin_px)

    detection = handeye.detect_board(
        cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR), handeye.make_detector(spec)
    )
    assert detection is not None, "board must be detectable in its own image"
    all_obj = np.asarray(board.getChessboardCorners(), dtype=np.float64)
    ids = detection.ids.ravel()
    return flat, detection.corners.reshape(-1, 2).astype(np.float64), all_obj[ids]


def render_board_view(
    spec: handeye.BoardSpec,
    K: np.ndarray,
    T_cam_target: np.ndarray,
    image_size: tuple[int, int],
) -> np.ndarray:
    """BGR image of the board seen from ``T_cam_target`` (target→camera, mm)."""
    flat, flat_px, obj_mm = _flat_board(spec)
    rvec = cv2.Rodrigues(T_cam_target[:3, :3])[0]
    tvec = T_cam_target[:3, 3]
    projected, _ = cv2.projectPoints(obj_mm, rvec, tvec, K, np.zeros(5))
    H, _ = cv2.findHomography(flat_px, projected.reshape(-1, 2))
    warped = cv2.warpPerspective(
        flat,
        H,
        image_size,
        flags=cv2.INTER_AREA,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    return cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)


def board_center(spec: handeye.BoardSpec) -> np.ndarray:
    return np.array(
        [spec.squares_x * spec.square_mm / 2, spec.squares_y * spec.square_mm / 2, 0.0]
    )


def look_at_target_pose(
    spec: handeye.BoardSpec,
    distance_mm: float,
    tilt_deg: float,
    azimuth_deg: float,
    roll_deg: float,
) -> np.ndarray:
    """T_cam_target (target→camera) for a camera on a sphere around the board
    center, optical axis through the center, rolled about it."""
    center = board_center(spec)
    tilt = math.radians(tilt_deg)
    azim = math.radians(azimuth_deg)
    # Camera position in the target frame; board normal is -z side.
    offset = distance_mm * np.array(
        [
            math.sin(tilt) * math.cos(azim),
            math.sin(tilt) * math.sin(azim),
            -math.cos(tilt),
        ]
    )
    pos = center + offset

    z_cam = center - pos
    z_cam = z_cam / np.linalg.norm(z_cam)
    up = np.array([0.0, 1.0, 0.0])
    x_cam = np.cross(up, z_cam)
    x_cam = x_cam / np.linalg.norm(x_cam)
    y_cam = np.cross(z_cam, x_cam)

    T_target_cam = np.eye(4)
    T_target_cam[:3, 0] = x_cam
    T_target_cam[:3, 1] = y_cam
    T_target_cam[:3, 2] = z_cam
    T_target_cam[:3, 3] = pos

    roll = math.radians(roll_deg)
    R_roll = np.array(
        [
            [math.cos(roll), -math.sin(roll), 0, 0],
            [math.sin(roll), math.cos(roll), 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ]
    )
    return np.linalg.inv(T_target_cam @ R_roll)


def synthesize_views(
    spec: handeye.BoardSpec,
    T_cam2gripper_true: np.ndarray,
    T_base_target: np.ndarray,
    n_views: int = 12,
    distance_mm: float = 450.0,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """(T_base_gripper, T_cam_target) pairs with rotation-diverse viewpoints.

    Gripper poses are derived so that a camera mounted at
    ``T_cam2gripper_true`` on that gripper sees exactly ``T_cam_target``:
    T_base_gripper = T_base_target @ inv(T_cam_target) @ inv(X).
    """
    X_inv = np.linalg.inv(T_cam2gripper_true)
    views: list[tuple[np.ndarray, np.ndarray]] = []
    for i in range(n_views):
        # Distance and tilt both vary: single-distance shallow views leave
        # focal length and depth nearly degenerate in the intrinsics solve.
        distance = distance_mm * (0.75 + 0.5 * ((i % 3) / 2.0))
        tilt = 12.0 + 26.0 * ((i % 4) / 3.0)
        azimuth = (360.0 / n_views) * i
        roll = -35.0 + (70.0 / max(n_views - 1, 1)) * i
        T_cam_target = look_at_target_pose(spec, distance, tilt, azimuth, roll)
        T_base_gripper = T_base_target @ np.linalg.inv(T_cam_target) @ X_inv
        views.append((T_base_gripper, T_cam_target))
    return views
