"""MCP tools for user preferences — ``commander.settings.*``.

Tools are ``async`` so FastMCP runs them on WC's event loop rather than a
worker thread — settings writes fire ``notify_changed`` that propagates to
live NiceGUI bindings, which is loop-affine.
"""

from __future__ import annotations

import waldoctl
from waldoctl import EnvelopeMode

from waldo_commander.mcp.server import get_mcp

mcp = get_mcp()


@mcp.tool(name="settings.get_jog")
async def get_jog() -> dict:
    """Jog control preferences (speed %, accel %, step size)."""
    j = waldoctl.commander.settings.jog
    return {
        "speed": j.speed,
        "accel": j.accel,
        "incremental": j.incremental,
        "joint_step_deg": j.joint_step_deg,
    }


@mcp.tool(name="settings.set_jog")
async def set_jog(
    speed: int | None = None,
    accel: int | None = None,
    incremental: bool | None = None,
    joint_step_deg: float | None = None,
) -> None:
    """Update one or more jog preferences. ``None`` leaves a field unchanged."""
    j = waldoctl.commander.settings.jog
    if speed is not None:
        j.speed = speed
    if accel is not None:
        j.accel = accel
    if incremental is not None:
        j.incremental = incremental
    if joint_step_deg is not None:
        j.joint_step_deg = joint_step_deg


@mcp.tool(name="settings.get_gripper")
async def get_gripper() -> dict:
    """Gripper preferences."""
    g = waldoctl.commander.settings.gripper
    return {
        "speed_sync": g.speed_sync,
        "speed": g.speed,
        "current": g.current,
        "target_position": g.target_position,
    }


@mcp.tool(name="settings.set_gripper")
async def set_gripper(
    speed_sync: bool | None = None,
    speed: int | None = None,
    current: int | None = None,
    target_position: float | None = None,
) -> None:
    """Update one or more gripper preferences."""
    g = waldoctl.commander.settings.gripper
    if speed_sync is not None:
        g.speed_sync = speed_sync
    if speed is not None:
        g.speed = speed
    if current is not None:
        g.current = current
    if target_position is not None:
        g.target_position = target_position


@mcp.tool(name="settings.get_view")
async def get_view() -> dict:
    """3D-scene visualisation preferences."""
    v = waldoctl.commander.settings.view
    return {
        "gizmo_visible": v.gizmo_visible,
        "paths_visible": v.paths_visible,
        "envelope_mode": v.envelope_mode.value,
    }


@mcp.tool(name="settings.set_view")
async def set_view(
    gizmo_visible: bool | None = None,
    paths_visible: bool | None = None,
    envelope_mode: str | None = None,
) -> None:
    """Update one or more view preferences. ``envelope_mode`` accepts
    ``"auto"`` / ``"on"`` / ``"off"``."""
    v = waldoctl.commander.settings.view
    if gizmo_visible is not None:
        v.gizmo_visible = gizmo_visible
    if paths_visible is not None:
        v.paths_visible = paths_visible
    if envelope_mode is not None:
        v.envelope_mode = EnvelopeMode(envelope_mode)
