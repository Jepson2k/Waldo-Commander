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
    arm_action_prompt,
    arm_consent_prompt,
    control_lease,
    control_mode,
    recently_denied,
    reset_consent,
    session_consented,
    take_approved_action,
)
from waldo_commander.state import ui_state

mcp = get_mcp()


def _session_id() -> str:
    """Stable id for the calling MCP session (FastMCP per-request context)."""
    ctx = get_context()
    return ctx.session_id or ctx.client_id or "mcp"


def _label(session_id: str) -> str:
    """Human-facing holder label — the MCP client's self-reported name when
    the session sent one at initialize, else a generic tag."""
    params = get_context().session.client_params
    name = params.clientInfo.name if params is not None else ""
    if name:
        return f"{name} ({session_id[:8]})"
    return f"MCP session {session_id[:8]}"


def require_control() -> None:
    """Gate an action on holding the control lease (no hardware-motion consent).

    Implicitly acquires a free lease (the first action claims it), and
    inherits one held by another MCP session — session ids churn on every
    reconnect, so MCP sessions form one interchangeable holder class (two
    genuinely concurrent AI clients would trade the lease rather than fight;
    the per-session hardware-consent floor still applies to each). Refuses
    only when a live Browser holder has it — the caller must ``take_control``
    to seize from the human.
    """
    sid = _session_id()
    if control_lease.held_by(MCP, sid):
        control_lease.touch(MCP, sid)
        return
    h = control_lease.holder()
    if h is None or h.channel == MCP:
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


def require_action_approval(description: str) -> None:
    """Per-action approval gate (Inspect / Auto-edits modes): every move is
    OK'd individually in the GUI. Arms a prompt and refuses-with-retry until
    the human approves this specific action; the grant is one-shot and matched
    to ``description`` so the retry of the same call passes but a different move
    re-prompts."""
    sid = _session_id()
    if take_approved_action(sid, description):
        return
    if recently_denied(sid):
        raise PermissionError(
            f"the user just denied '{description}' — do not retry immediately; "
            "wait for the user or take a different approach"
        )
    cid = ui_state.active_client_id
    client = Client.instances.get(cid) if cid else None
    if client is None or client._deleted:
        raise PermissionError(
            "open the Waldo-Commander GUI and approve the action prompt first"
        )
    arm_action_prompt(sid, description)
    raise PermissionError(
        f"this action needs approval: {description} — approve the prompt in "
        "Waldo-Commander, then retry"
    )


def require_actuation(description: str) -> None:
    """Full actuation gate, mode-aware. Always requires the control lease, then:

    - **Autopilot**: motion is auto-approved; real hardware still needs the
      one-time per-session consent floor (simulator needs only the lease).
    - **Inspect / Auto-edits**: every move needs per-action GUI approval
      (this subsumes the hardware floor — a human is in the loop each time).
    """
    require_control()
    if control_mode().auto_approves_motion:
        if not waldoctl.commander.status.simulator_active:
            require_session_consent()
    else:
        require_action_approval(description)


@mcp.tool(name="control.take_control")
async def take_control() -> dict:
    """Seize the single-controller lease for this MCP session.

    Only needed to take over from the human's browser tab: a free lease is
    claimed by your first gated action, and a lease held by a previous MCP
    session transfers to you automatically. The displaced holder is blocked
    from actuating and can see that you hold it. Reads are never blocked for
    anyone.
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
    """Report who holds control and the human's current control mode (read-only,
    never gated). ``mode`` is one of ``inspect`` / ``auto_edits`` / ``autopilot``
    and is set by the human, not the LLM — it governs whether your edits apply
    immediately and whether each move needs per-action approval."""
    mode = control_mode()
    return {
        "holder": control_lease.describe(),
        "you_hold_it": control_lease.held_by(MCP, _session_id()),
        "mode": mode.value,
        "mode_auto_applies_edits": mode.auto_applies_edits,
        "mode_auto_approves_motion": mode.auto_approves_motion,
    }
