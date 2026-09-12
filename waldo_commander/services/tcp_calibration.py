"""Measured TCP acquisition and confirmed application on an explicit client."""

from __future__ import annotations

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
