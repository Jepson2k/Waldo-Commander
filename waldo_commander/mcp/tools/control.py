"""MCP tools for the single-controller lease — ``control.*``.

Actuation tools (motion, script execution) require holding the lease; reads
never do. ``take_control`` seizes it (anyone may, always visible),
``release_control`` drops it, ``get_controller`` reports the holder. The MCP
session is identified by FastMCP's per-request session id.
"""

from __future__ import annotations

import waldoctl
from fastmcp.server.dependencies import get_context

from waldo_commander.mcp.server import get_mcp
from waldo_commander.services.control_lease import MCP, control_lease

mcp = get_mcp()


def _session_id() -> str:
    """Stable id for the calling MCP session (FastMCP per-request context)."""
    ctx = get_context()
    return ctx.session_id or ctx.client_id or "mcp"


def _label(session_id: str) -> str:
    return f"MCP session {session_id[:8]}"


def require_mcp_control() -> None:
    """Gate an actuation tool on holding the control lease.

    Implicitly acquires a free lease (the first actuation claims it); refuses if
    a different live holder has it — the caller must ``take_control`` to seize.
    """
    sid = _session_id()
    if control_lease.held_by(MCP, sid):
        control_lease.touch(MCP, sid)
        return
    if control_lease.is_free():
        control_lease.seize(MCP, sid, _label(sid))
        return
    raise PermissionError(
        f"robot is controlled by {control_lease.describe()}; "
        "call control.take_control to take over"
    )


def require_motion_allowed() -> None:
    """Full actuation gate: the live ``allow_motion`` safety toggle plus the
    control lease. Used by every tool that physically moves the arm — direct
    motion verbs and program execution alike — so the user's "Allow motion via
    MCP" switch covers all of them, not just the motion.* namespace.
    """
    if not waldoctl.commander.settings.mcp.allow_motion:
        raise PermissionError(
            "motion is disabled in WC's MCP settings "
            "(commander.settings.mcp.allow_motion = False)"
        )
    require_mcp_control()


@mcp.tool(name="control.take_control")
async def take_control() -> dict:
    """Seize the single-controller lease for this MCP session.

    Anyone can take control; the displaced holder (a browser tab or another MCP
    session) is then blocked from actuating and can see that you hold it. Reads
    are never blocked for anyone.
    """
    sid = _session_id()
    control_lease.seize(MCP, sid, _label(sid))
    return {"holder": control_lease.describe(), "you_hold_it": True}


@mcp.tool(name="control.release_control")
async def release_control() -> dict:
    """Release the lease if this MCP session holds it."""
    control_lease.release(MCP, _session_id())
    return {"holder": control_lease.describe(), "you_hold_it": False}


@mcp.tool(name="control.get_controller")
async def get_controller() -> dict:
    """Report who currently holds control (read-only — never gated)."""
    return {
        "holder": control_lease.describe(),
        "you_hold_it": control_lease.held_by(MCP, _session_id()),
    }
