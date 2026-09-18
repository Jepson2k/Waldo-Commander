"""The pivot solve, measured TCP acquisition and confirmed application."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np
from waldoctl import RobotClient
from waldoctl.setup import Pose, PoseValues, TcpCalibration
from waldoctl.status import StatusBuffer


@dataclass(frozen=True)
class ToolBinding:
    tool_key: str
    variant_key: str


@dataclass(frozen=True)
class TcpObservation:
    nominal_tool: Pose
    binding: ToolBinding
    applied: PoseValues


Vector3 = tuple[float, float, float]


@dataclass(frozen=True)
class PivotCalibration:
    """A position-only result; this observation does not determine TCP axes.

    ``offset_mm`` is relative to the registered tool, before any user TCP
    transform. ``pivot_wrf_mm`` is the stationary contact point in WRF.
    """

    offset_mm: Vector3
    pivot_wrf_mm: Vector3
    rms_error_mm: float
    max_error_mm: float
    condition: float
    sample_count: int


def calibrate_tcp_position(
    nominal_tool_poses: Sequence[Pose],
    *,
    max_error_mm: float = 1.0,
    max_condition: float = 1000.0,
) -> PivotCalibration:
    """Fit a fixed tip from at least four diverse orientations at one pivot.

    Each pose describes the registered tool in WRF, with any existing user
    TCP transform removed. Acquisition must hold the same physical tip at
    the same stationary point while changing tool orientation. The caller
    chooses its measurement tolerance; every sample must meet it.

    Reject underdetermined, poorly conditioned, and inconsistent observations.
    This solve never estimates orientation or sends a configuration command.
    """
    if (
        not math.isfinite(max_error_mm)
        or max_error_mm <= 0
        or not math.isfinite(max_condition)
        or max_condition < 1
    ):
        raise ValueError("Require a positive error tolerance and condition limit >= 1")
    if len(nominal_tool_poses) < 4:
        raise ValueError("Pivot calibration requires at least four tool poses")
    if any(pose.frame != "WRF" for pose in nominal_tool_poses):
        raise ValueError("Pivot samples must be registered-tool poses in WRF")

    transforms = np.stack([pose.matrix() for pose in nominal_tool_poses])
    positions = transforms[:, :3, 3]
    origin = positions.mean(axis=0)
    design = np.concatenate(
        [transforms[:, :3, :3], np.broadcast_to(-np.eye(3), (len(transforms), 3, 3))],
        axis=2,
    ).reshape(-1, 6)
    solution, _, rank, singular = np.linalg.lstsq(
        design, -(positions - origin).reshape(-1), rcond=None
    )
    if rank != 6:
        raise ValueError(
            "Pivot samples are degenerate; vary orientation about multiple axes"
        )
    condition = float(singular[0] / singular[-1])
    if condition > max_condition:
        raise ValueError(
            f"Pivot samples are poorly conditioned ({condition:.1f}); use more diverse orientations"
        )
    residuals = (design @ solution + (positions - origin).reshape(-1)).reshape(-1, 3)
    errors = np.linalg.norm(residuals, axis=1)
    maximum = float(errors.max())
    if maximum > max_error_mm:
        raise ValueError(
            f"Pivot samples disagree by {maximum:.3f} mm (limit {max_error_mm:.3f} mm)"
        )
    return PivotCalibration(
        offset_mm=cast(Vector3, tuple(map(float, solution[:3]))),
        pivot_wrf_mm=cast(Vector3, tuple(map(float, solution[3:] + origin))),
        rms_error_mm=float(np.sqrt(np.mean(errors**2))),
        max_error_mm=maximum,
        condition=condition,
        sample_count=len(transforms),
    )


def teach_tcp_orientation(nominal_tool_pose: Pose, reference_axes: Pose) -> Vector3:
    """Align TCP axes with explicit reference axes at the observed tool pose.

    Both poses must be resolved to WRF. Positions do not affect this orientation
    teaching operation. Return intrinsic XYZ degrees relative to the registered
    tool; preserve the separately calibrated translation when applying them.
    """
    if nominal_tool_pose.frame != "WRF" or reference_axes.frame != "WRF":
        raise ValueError("Orientation teaching requires tool and reference axes in WRF")
    local = np.eye(4)
    local[:3, :3] = (
        nominal_tool_pose.matrix()[:3, :3].T @ reference_axes.matrix()[:3, :3]
    )
    return Pose.from_matrix(local).values[3:]


async def current_tool(client: RobotClient, *, timeout: float = 3.0) -> ToolBinding:
    binding: ToolBinding | None = None
    first = True

    def capture(status: StatusBuffer) -> bool:
        nonlocal binding, first
        if first:
            first = False
            return False
        binding = ToolBinding(
            status.tool_status.key, status.tool_status.variant_key or ""
        )
        return True

    if not await client.wait_status(capture, timeout=timeout) or binding is None:
        raise TimeoutError("No fresh tool status is available")
    return binding


async def read_applied_tcp(client: RobotClient) -> TcpCalibration:
    """Read the controller correction with a stable tool/variant binding."""
    for _ in range(3):
        binding = await current_tool(client)
        values = Pose(cast(PoseValues, tuple(await client.tcp_transform()))).values
        if await current_tool(client) == binding:
            return TcpCalibration(values, binding.tool_key, binding.variant_key)
    raise ValueError("The tool kept changing while reading its TCP transform")


async def observe_tcp(client: RobotClient, *, timeout: float = 3.0) -> TcpObservation:
    before = Pose(cast(PoseValues, tuple(await client.tcp_transform())))
    captured: tuple[Pose, ToolBinding] | None = None
    first = True

    def capture(status: StatusBuffer) -> bool:
        nonlocal captured, first
        if first:
            first = False
            return False
        speeds = np.asarray(status.speeds)
        if (
            not status.homed
            or not np.isfinite(speeds).all()
            or np.any(np.abs(speeds) > 0.01)
        ):
            return False
        observed = Pose.from_matrix(np.asarray(status.pose).reshape(4, 4).copy())
        captured = (
            observed,
            ToolBinding(status.tool_status.key, status.tool_status.variant_key or ""),
        )
        return True

    if not await client.wait_status(capture, timeout=timeout) or captured is None:
        raise TimeoutError("Reference the arm and hold the tip still before capturing")
    after = Pose(cast(PoseValues, tuple(await client.tcp_transform())))
    observed, binding = captured
    if (
        before.values != after.values
        or await current_tool(client, timeout=timeout) != binding
    ):
        raise ValueError(
            "The tool or TCP configuration changed during capture; capture again"
        )
    nominal = Pose.from_matrix(observed.matrix() @ np.linalg.inv(after.matrix()))
    return TcpObservation(nominal, binding, after.values)


async def apply_tcp_calibration(
    client: RobotClient, calibration: TcpCalibration, *, timeout: float = 15.0
) -> int:
    binding = ToolBinding(calibration.tool_key, calibration.variant_key)
    if await current_tool(client) != binding:
        raise ValueError("This calibration belongs to a different tool or variant")
    index = await client.set_tcp_transform(*calibration.values)
    if index < 0 or not await client.wait_command(index, timeout=timeout):
        raise TimeoutError("TCP calibration application was not confirmed")
    actual = Pose(cast(PoseValues, tuple(await client.tcp_transform())))
    if await current_tool(client) != binding or not np.allclose(
        actual.matrix(), calibration.matrix(), atol=1e-6, rtol=0
    ):
        raise ValueError(
            "The controller's tool or TCP changed before readback was confirmed"
        )
    return index
