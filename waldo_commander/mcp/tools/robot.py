"""MCP tools exposing the robot's static capabilities — ``commander.robot.*``."""

from __future__ import annotations

import waldoctl

from waldo_commander.mcp.server import get_mcp

mcp = get_mcp()


@mcp.tool(name="robot.get_capabilities")
async def get_capabilities() -> dict:
    """Static robot configuration: joints, tools, limits, frames, IO width.

    Read once at start of session and cache — these don't change unless the
    backend is swapped.
    """
    r = waldoctl.commander.robot
    return {
        "name": r.name,
        "position_unit": r.position_unit,
        "has_force_torque": r.has_force_torque,
        "has_freedrive": r.has_freedrive,
        "digital_outputs": r.digital_outputs,
        "digital_inputs": r.digital_inputs,
        "joints": {
            "count": r.joints.count,
            "names": list(r.joints.names),
            "home_deg": list(r.joints.home.deg),
        },
        "tools": [
            {
                "key": s.key,
                "display_name": s.display_name,
                "tool_type": str(s.tool_type),
            }
            for s in r.tools.available
        ],
        "cartesian_frames": list(r.cartesian_frames),
        "motion_profiles": list(r.motion_profiles),
    }
