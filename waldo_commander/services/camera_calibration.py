"""Convert acquired and previously saved camera measurements into setup data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import numpy as np
from waldoctl.camera import CameraCalibration, CameraIntrinsics, CameraQuality
from waldoctl.setup import Pose, PoseValues, SetupSnapshot, TcpCalibration

from waldo_commander.services import handeye


@dataclass(frozen=True)
class CaptureBinding:
    camera_id: str
    session_id: str
    backend: str
    tool: TcpCalibration


def calibration_from_result(
    result: handeye.HandEyeResult,
    spec: handeye.BoardSpec,
    binding: CaptureBinding,
    setup: SetupSnapshot,
    reference_frame: str = "WRF",
) -> CameraCalibration:
    intrinsics = result.intrinsics
    parent = setup.frame_matrix(reference_frame) if result.mount == "fixed" else None
    pose = Pose.from_matrix(
        np.linalg.inv(parent) @ result.T_camera_parent
        if parent is not None
        else result.T_camera_parent,
        frame=reference_frame if result.mount == "fixed" else "TCP",
    )
    return CameraCalibration(
        camera_id=binding.camera_id,
        backend=binding.backend,
        mount=result.mount,
        pose=pose,
        intrinsics=CameraIntrinsics(
            tuple(intrinsics.camera_matrix.ravel().tolist()),
            tuple(intrinsics.dist_coeffs.ravel().tolist()),
            intrinsics.image_size,
        ),
        quality=CameraQuality(
            result.n_views,
            result.method,
            intrinsics.reproj_rms_px,
            result.rot_residual_deg,
            result.trans_residual_mm,
            result.target_spread_mm,
        ),
        calibrated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        board=spec.to_dict(),
        tool=binding.tool if result.mount == "tool" else None,
        reference_wrf=Pose.from_matrix(parent) if parent is not None else None,
    )


def import_saved_handeye(
    document: dict[str, Any],
    *,
    camera_id: str,
    image_size: tuple[int, int],
    backend: str,
    tool: TcpCalibration,
) -> CameraCalibration:
    """Bind an existing per-tool result explicitly, preserving its measurements.

    The original format did not identify the camera or tool variant. The caller
    must confirm the same physical camera, lens and mount before importing.
    Software checks the recorded tool, TCP and image dimensions.
    """
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("Unsupported saved hand-eye measurement")
    try:
        spec = handeye.BoardSpec.from_dict(document["board"])
        spec.validate()
        offset = document["tcp_offset_snapshot"]
        measured_tool = TcpCalibration(
            cast(
                PoseValues,
                tuple(
                    offset.get(k, 0) for k in ("x", "y", "z", "roll", "pitch", "yaw")
                ),
            ),
            document["tool_key"],
            tool.variant_key,
        )
        calibration = CameraCalibration(
            camera_id=camera_id,
            backend=backend,
            mount="tool",
            pose=Pose.from_matrix(
                np.asarray(document["T_cam2gripper_mm"]).reshape(4, 4), frame="TCP"
            ),
            intrinsics=CameraIntrinsics(
                tuple(document["camera_matrix"]),
                tuple(document["dist_coeffs"]),
                tuple(document["image_size"]),
            ),
            quality=CameraQuality(
                document["n_samples"],
                document["method"],
                document["reproj_rms_px"],
                (
                    document["rot_residual_deg"]["mean"],
                    document["rot_residual_deg"]["max"],
                ),
                (
                    document["trans_residual_mm"]["mean"],
                    document["trans_residual_mm"]["max"],
                ),
                document["target_spread_mm"],
            ),
            calibrated_at=document["timestamp"],
            board=document["board"],
            tool=measured_tool,
        )
        calibration.validate(
            SetupSnapshot(),
            camera_id=camera_id,
            image_size=image_size,
            backend=backend,
            tool=tool,
        )
        return calibration
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(
            f"Saved hand-eye measurement is incomplete: {error}"
        ) from error
