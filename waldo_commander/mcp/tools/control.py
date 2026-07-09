"""MCP tools for the single-controller lease — ``control.*``.

Actuation tools (motion, script execution) require holding the lease; reads
never do. ``take_control`` seizes it (anyone may, always visible),
``release_control`` drops it, ``get_controller`` reports the holder. The MCP
session is identified by FastMCP's per-request session id.
"""

from __future__ import annotations

import waldoctl
from fastmcp.server.dependencies import get_context
from nicegui import Client

from waldo_commander.mcp.server import get_mcp
from waldo_commander.services.control_lease import (
    MCP,
    arm_consent_prompt,
    control_lease,
    recently_denied,
    reset_consent,
    session_consented,
)
from waldo_commander.state import ui_state

mcp = get_mcp()


def _session_id() -> str:
    """Stable id for the calling MCP session (FastMCP per-request context)."""
    ctx = get_context()
    return ctx.session_id or ctx.client_id or "mcp"


def _label(session_id: str) -> str:
    return f"MCP session {session_id[:8]}"


def require_control() -> None:
    """Gate an action on holding the control lease (no hardware-motion consent).

    Implicitly acquires a free lease (the first action claims it); refuses if a
    different live holder has it — the caller must ``take_control`` to seize.
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


def require_session_consent() -> None:
    """Gate the first hardware (non-simulator) move of an MCP session on a
    one-time human acknowledgement in the GUI.

    Un-consented moves are refused and a prompt is armed; the user approves it
    and the client retries. Refused outright when no GUI page is connected — no
    one could consent, so a hardware-affecting action must not proceed.
    """
    sid = _session_id()
    if session_consented(sid):
        return
    if recently_denied(sid):
        # Terminal for the cooldown: no prompt is re-armed, so the deny can't
        # be nagged away by an immediate retry loop.
        raise PermissionError(
            "the user denied hardware motion for this session just now — do "
            "not retry immediately; work in simulator mode or wait for the "
            "user to initiate"
        )
    cid = ui_state.active_client_id
    client = Client.instances.get(cid) if cid else None
    if client is None or client._deleted:
        raise PermissionError(
            "open the Waldo-Commander GUI and approve the hardware-motion prompt first"
        )
    arm_consent_prompt(sid, _label(sid))
    raise PermissionError(
        "first hardware move of this session needs GUI consent — approve the "
        "prompt in Waldo-Commander, then retry"
    )


def require_actuation() -> None:
    """Full actuation gate: the control lease always, plus per-session consent
    when driving real hardware. In simulator mode only the lease is required
    (sim playback is an informal handoff, not safety-critical)."""
    require_control()
    if not waldoctl.commander.status.simulator_active:
        require_session_consent()


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
    """Release the lease if this MCP session holds it (and clear its consent)."""
    sid = _session_id()
    control_lease.release(MCP, sid)
    reset_consent(sid)
    return {"holder": control_lease.describe(), "you_hold_it": False}


@mcp.tool(name="control.get_controller")
async def get_controller() -> dict:
    """Report who currently holds control (read-only — never gated)."""
    return {
        "holder": control_lease.describe(),
        "you_hold_it": control_lease.held_by(MCP, _session_id()),
    }
