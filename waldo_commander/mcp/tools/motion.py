"""MCP tools for direct motion commands — ``commander.client.*``.

Every **actuating** tool here passes :func:`require_motion_allowed` — the live
``commander.settings.mcp.allow_motion`` toggle (flippable from WC's Settings
without a restart) plus the single-controller lease. The deliberately ungated
tools are ``halt`` (stopping is always safe) and ``wait_motion`` (passive).

These wrappers are deliberately a flat subset of the full client surface: the
most common motion verbs an LLM is likely to issue. Advanced moves
(``move_c``, ``move_s``, ``move_p``, servo modes) are intentionally not exposed
for v1 — let the LLM compose them through ``programs.propose_edit`` +
``execution.run_active`` instead.
"""

from __future__ import annotations

import waldoctl
from waldoctl.types import Axis, Frame

from waldo_commander.mcp.server import get_mcp
from waldo_commander.mcp.tools.control import require_motion_allowed

mcp = get_mcp()


def _dispatched(index: int, verb: str) -> int:
    """Surface the client's in-band failure sentinel as a tool error.

    Queued motion commands return the command index (>= 0) on success and -1
    on failure / timeout. Returning -1 verbatim reads to the LLM as success, so
    a failed move would look accepted — raise instead.
    """
    if index < 0:
        raise RuntimeError(
            f"motion.{verb} was not accepted by the controller "
            "(the robot may be disconnected, e-stopped, or the target invalid)"
        )
    return index


@mcp.tool(name="motion.move_j")
async def move_j(
    angles: list[float],
    speed: float = 0.5,
    accel: float = 1.0,
    wait: bool = False,
) -> int:
    """Joint-space move to ``angles`` (degrees). Returns the command index."""
    require_motion_allowed()
    return _dispatched(
        await waldoctl.commander.client.move_j(
            angles, speed=speed, accel=accel, wait=wait
        ),
        "move_j",
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
    require_motion_allowed()
    return _dispatched(
        await waldoctl.commander.client.move_l(
            pose, frame=frame, speed=speed, accel=accel, wait=wait
        ),
        "move_l",
    )


@mcp.tool(name="motion.home")
async def home(wait: bool = False) -> int:
    """Move to the robot's home position."""
    require_motion_allowed()
    return _dispatched(await waldoctl.commander.client.home(wait=wait), "home")


@mcp.tool(name="motion.jog_j")
async def jog_j(joint: int, speed: float, duration: float = 0.1) -> int:
    """Velocity jog one joint for ``duration`` seconds."""
    require_motion_allowed()
    return _dispatched(
        await waldoctl.commander.client.jog_j(joint, speed, duration), "jog_j"
    )


@mcp.tool(name="motion.jog_l")
async def jog_l(
    frame: Frame,
    axis: Axis,
    speed: float,
    duration: float = 0.1,
) -> int:
    """Velocity jog one Cartesian axis for ``duration`` seconds."""
    require_motion_allowed()
    return _dispatched(
        await waldoctl.commander.client.jog_l(frame, axis, speed, duration), "jog_l"
    )


@mcp.tool(name="motion.halt")
async def halt() -> int:
    """Immediate stop — halt all motion and disable.

    Deliberately ungated: stopping is always safe, so ``halt`` works even when
    ``allow_motion`` is False (it and ``wait_motion`` are the exceptions).
    """
    return _dispatched(await waldoctl.commander.client.halt(), "halt")


@mcp.tool(name="motion.resume")
async def resume() -> int:
    """Re-enable the robot after halt / e-stop."""
    require_motion_allowed()
    return _dispatched(await waldoctl.commander.client.resume(), "resume")


@mcp.tool(name="motion.wait_motion")
async def wait_motion(timeout: float = 10.0) -> bool:
    """Block until the robot has stopped moving or ``timeout`` expires.

    Passive and deliberately ungated — it only waits, never actuates.
    """
    return await waldoctl.commander.client.wait_motion(timeout=timeout)
