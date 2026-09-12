"""Image acquisition and localization using explicit camera and robot clients."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from scipy.spatial.transform import Rotation
from waldoctl import RobotClient
from waldoctl.camera import CameraCalibration
from waldoctl.setup import Pose, SetupSnapshot, TcpCalibration
from waldoctl.skills import MissingCapability, UnresolvedPreview, report_progress, skill

from waldo_commander.camera import CameraUnavailable
from waldo_commander.camera_sources import FrameSource, ImageFixture, check_timeout
from waldo_commander.services.tcp_calibration import observe_tcp
from waldo_commander.vision import (
    LocalizationLimits,
    LocalizationResult,
    localize_board,
)


#: Budget for the arm observations a tool camera's localization brackets its
#: capture with: two status frames each plus two transform reads, which a short
#: image-acquisition window cannot cover.
_OBSERVE_TCP_S = 3.0


def _client_backend(rbt: RobotClient, fallback: str) -> str:
    """The backend this client drives, from the capabilities it advertises."""
    for capability in rbt.skill_capabilities:
        if capability.startswith("backend."):
            return capability.removeprefix("backend.")
    return fallback


@skill(id="waldo.locate_board", version="1.0.0")
async def locate_board(
    rbt: RobotClient,
    calibration: CameraCalibration,
    source: FrameSource,
    setup: SetupSnapshot,
    *,
    timeout_s: float = 2.0,
    limits: LocalizationLimits | None = None,
) -> LocalizationResult:
    """Locate a calibrated ChArUco board in WRF; hold a tool camera still.

    Preview requires an explicit ImageFixture. Live programs request a fresh
    image from their FrameSource. Missing and rejected detections return no
    pose; unavailable sources and invalid calibration bindings raise errors.

    ``timeout_s`` bounds the image acquisition. The arm observations a tool
    camera needs before and after it get their own budget: several status
    frames and two transform reads do not fit in a short acquisition window,
    and a localization that failed for that reason used to report that the arm
    had moved.
    """
    check_timeout(timeout_s)
    if f"backend.{calibration.backend}" not in rbt.skill_capabilities:
        raise MissingCapability(f"This calibration belongs to {calibration.backend}")
    preview = "execution.preview" in rbt.skill_capabilities
    tcp_pose, tool = None, None
    if preview:
        if not isinstance(source, ImageFixture):
            raise UnresolvedPreview(
                "Camera localization needs an explicit ImageFixture in preview"
            )
        observation = await source.snapshot(timeout_s=timeout_s)
        tcp_pose, tool = source.tcp_pose, source.tool
    else:
        if isinstance(source, ImageFixture):
            raise ValueError("Image fixtures require a preview client")
        observe_s = max(timeout_s, _OBSERVE_TCP_S)
        before = (
            await observe_tcp(rbt, timeout=observe_s)
            if calibration.mount == "tool"
            else None
        )
        requested_at = time.time()
        async with asyncio.timeout(timeout_s + 0.5):
            observation = await source.snapshot(timeout_s=timeout_s)
        if not requested_at - 0.05 <= observation.received_at <= time.time() + 0.05:
            raise CameraUnavailable(
                "The source did not return an image received during this acquisition"
            )
        if before is not None:
            after = await observe_tcp(rbt, timeout=observe_s)
            relative = (
                np.linalg.inv(before.nominal_tool.matrix())
                @ after.nominal_tool.matrix()
            )
            translation = float(np.linalg.norm(relative[:3, 3]))
            rotation = float(
                np.rad2deg(
                    np.linalg.norm(Rotation.from_matrix(relative[:3, :3]).as_rotvec())
                )
            )
            if (
                before.binding != after.binding
                or before.applied != after.applied
                or translation > 0.5
                or rotation > 0.25
            ):
                raise ValueError(
                    "Tool or robot changed during capture; hold still and acquire again"
                )
            tool = TcpCalibration(
                before.applied, before.binding.tool_key, before.binding.variant_key
            )
            tcp_pose = Pose.from_matrix(before.nominal_tool.matrix() @ tool.matrix())
    result = await asyncio.to_thread(
        localize_board,
        observation,
        calibration,
        setup,
        # The client's own backend, not the calibration's: comparing the
        # calibration against itself made `validate`'s recalibrate-after-a-
        # backend-change check unreachable.
        backend=_client_backend(rbt, calibration.backend),
        tcp_pose=tcp_pose,
        tool=tool,
        limits=limits if limits is not None else LocalizationLimits(),
    )
    if result.pose is not None and result.quality is not None:
        report_progress(
            f"Board found at WRF {result.pose.values}; corner RMS {result.quality.rms_px:.3f} px"
        )
    else:
        report_progress(f"Board {result.outcome}: {result.reason}")
    return result
