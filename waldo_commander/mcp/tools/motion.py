"""MCP tools for direct motion commands — ``commander.client.*``.

**All tools in this module are gated by**
``commander.settings.mcp.allow_motion``. If that flag is False, every
tool here raises ``PermissionError`` cleanly. The user can flip it from
WC's Settings panel at any time without restarting the server.

These wrappers are deliberately a flat subset of the full client
surface: the most common motion verbs an LLM is likely to issue.
Advanced moves (``move_c``, ``move_s``, ``move_p``, servo modes) are
intentionally not exposed for v1 — let the LLM compose them through
``programs.propose_edit`` + ``execution.run_active`` instead.
"""

from __future__ import annotations

import waldoctl
from waldoctl.types import Axis, Frame

from waldo_commander.mcp.server import get_mcp

mcp = get_mcp()


def _motion_allowed() -> None:
    if not waldoctl.commander.settings.mcp.allow_motion:
        raise PermissionError(
            "motion commands are disabled in WC's MCP settings "
            "(commander.settings.mcp.allow_motion = False)"
        )


@mcp.tool(name="motion.move_j")
async def move_j(
    angles: list[float],
    speed: float = 0.5,
    accel: float = 1.0,
    wait: bool = False,
) -> int:
    """Joint-space move to ``angles`` (degrees). Returns the command index."""
    _motion_allowed()
    return await waldoctl.commander.client.move_j(
        angles, speed=speed, accel=accel, wait=wait
    )


@mcp.tool(name="motion.move_l")
async def move_l(
    pose: list[float],
    frame: Frame = "WRF",
    speed: float = 0.5,
    accel: float = 1.0,
    wait: bool = False,
) -> int:
    """Linear Cartesian move to ``pose = [x,y,z,rx,ry,rz]`` (mm, deg)."""
    _motion_allowed()
    return await waldoctl.commander.client.move_l(
        pose, frame=frame, speed=speed, accel=accel, wait=wait
    )


@mcp.tool(name="motion.home")
async def home(wait: bool = False) -> int:
    """Move to the robot's home position."""
    _motion_allowed()
    return await waldoctl.commander.client.home(wait=wait)


@mcp.tool(name="motion.jog_j")
async def jog_j(joint: int, speed: float, duration: float = 0.1) -> int:
    """Velocity jog one joint for ``duration`` seconds."""
    _motion_allowed()
    return await waldoctl.commander.client.jog_j(joint, speed, duration)


@mcp.tool(name="motion.jog_l")
async def jog_l(
    frame: Frame,
    axis: Axis,
    speed: float,
    duration: float = 0.1,
) -> int:
    """Velocity jog one Cartesian axis for ``duration`` seconds."""
    _motion_allowed()
    return await waldoctl.commander.client.jog_l(frame, axis, speed, duration)


@mcp.tool(name="motion.halt")
async def halt() -> int:
    """Immediate stop — halt all motion and disable.

    Note: halt is allowed even when ``allow_motion`` is False, because
    "stop" is always safe to invoke. This is the one exception to the
    gate.
    """
    return await waldoctl.commander.client.halt()


@mcp.tool(name="motion.resume")
async def resume() -> int:
    """Re-enable the robot after halt / e-stop."""
    _motion_allowed()
    return await waldoctl.commander.client.resume()


@mcp.tool(name="motion.wait_motion")
async def wait_motion(timeout: float = 10.0) -> bool:
    """Block until the robot has stopped moving or ``timeout`` expires."""
    return await waldoctl.commander.client.wait_motion(timeout=timeout)
