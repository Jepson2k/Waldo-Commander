"""MCP tools for live robot status — wraps ``commander.status.*``."""

from __future__ import annotations

import waldoctl

from waldo_commander.mcp.server import get_mcp

mcp = get_mcp()


@mcp.tool(name="status.get_pose")
async def get_pose() -> dict:
    """Current Cartesian pose in the world frame.

    Returns translation in mm, rotation as RPY in degrees, and live TCP
    speed in mm/s.
    """
    p = waldoctl.commander.status.pose
    return {
        "x": p.x,
        "y": p.y,
        "z": p.z,
        "rx": p.rx,
        "ry": p.ry,
        "rz": p.rz,
        "tcp_speed": p.tcp_speed,
    }


@mcp.tool(name="status.get_joints")
async def get_joints() -> dict:
    """Current joint angles (deg + rad), joint speeds (rad/s), and per-joint
    jog availability."""
    j = waldoctl.commander.status.joints
    return {
        "angles_deg": list(j.angles.deg),
        "angles_rad": list(j.angles.rad),
        "speeds_rad_s": list(j.speeds),
        "can_jog_pos": list(j.can_jog_pos),
        "can_jog_neg": list(j.can_jog_neg),
    }


@mcp.tool(name="status.get_io")
async def get_io() -> dict:
    """Digital input/output bitmask state plus the e-stop signal (1=OK, 0=triggered)."""
    io = waldoctl.commander.status.io
    return {
        "inputs": list(io.inputs),
        "outputs": list(io.outputs),
        "estop": io.estop,
    }


@mcp.tool(name="status.get_action_state")
async def get_action_state() -> dict:
    """Currently-executing controller action plus recent action history."""
    a = waldoctl.commander.status.action
    return {
        "state": a.state.name,
        "current_name": a.current_name,
        "history": [
            {
                "command_name": e.command_name,
                "params": e.params,
                "status": e.status.name,
                "command_index": e.command_index,
                "count": e.count,
                "timestamp": e.timestamp,
            }
            for e in a.history
        ],
    }


@mcp.tool(name="status.get_tool_status")
async def get_tool_status() -> dict:
    """Active tool key + variant + live per-tool state (positions, currents)."""
    t = waldoctl.commander.status.tool
    return {
        "key": t.key,
        "variant_key": t.variant_key,
        "state": t.state.name,
        "engaged": t.engaged,
        "part_detected": t.part_detected,
        "fault_code": t.fault_code,
        "positions": list(t.positions),
        "channels": list(t.channels),
    }


@mcp.tool(name="status.get_connected")
async def get_connected() -> dict:
    """Whether the controller link is up and whether the simulator is in use."""
    s = waldoctl.commander.status
    return {"connected": s.connected, "simulator_active": s.simulator_active}
