"""Single-controller arbitration across browser tabs and MCP sessions.

Exactly one holder may issue actuation commands at a time; everyone else
observes (reads are never gated), and the holder is always visible so nobody
*unknowingly* drives the arm. The default holder is the active browser tab; an
MCP session seizes control with the ``control.take_control`` tool. Anyone may
seize — visibility, not permission, is what prevents unknowing dual control.

Liveness:
- a ``browser`` holder is live while its client id is in nicegui's
  ``Client.instances`` (same registry the multi-tab arbitration uses);
- an ``mcp`` holder is live while its last gated call was within
  :data:`MCP_TTL_SECONDS` — MCP has no per-connection registry on this side, so
  the holder refreshes a timestamp on every gated call and a crashed/disconnected
  session ages out.

A stale holder is dropped on the next query, so anyone can reclaim it. This is
host-application policy (the MCP server runs in WC's process and shares this
state), not part of the public ``commander`` surface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from nicegui import Client

BROWSER = "browser"
MCP = "mcp"

# How long an MCP holder stays "live" without a gated call before it ages out.
MCP_TTL_SECONDS = 30.0


class ControlMode(Enum):
    """How much per-action human approval an MCP-driven action needs.

    Global and human-set (the control-panel toggle / keybinding); mirrors
    Claude Code's ask → auto-accept-edits → bypass ladder. Governs whichever
    MCP session is driving. Independent of the lease (who holds control).
    """

    INSPECT = "inspect"  # approve every edit and every move
    AUTO_EDITS = "auto_edits"  # edits auto-apply; moves still approved per-action
    AUTOPILOT = "autopilot"  # edits + moves auto (hardware keeps a one-time floor)

    @property
    def auto_applies_edits(self) -> bool:
        return self in (ControlMode.AUTO_EDITS, ControlMode.AUTOPILOT)

    @property
    def auto_approves_motion(self) -> bool:
        return self is ControlMode.AUTOPILOT

    @property
    def label(self) -> str:
        return {
            ControlMode.INSPECT: "Inspect",
            ControlMode.AUTO_EDITS: "Auto-edits",
            ControlMode.AUTOPILOT: "Autopilot",
        }[self]


# Global control mode. Defaults to the safest rung; runtime-only (resets on
# restart, like Claude Code's per-session mode). Only a human changes it.
_control_mode: ControlMode = ControlMode.INSPECT


def control_mode() -> ControlMode:
    return _control_mode


def set_control_mode(mode: ControlMode) -> None:
    global _control_mode
    _control_mode = mode


def cycle_control_mode() -> ControlMode:
    """Advance Inspect → Auto-edits → Autopilot → Inspect and return the new
    mode. Backs the control-panel toggle and the keyboard shortcut."""
    global _control_mode
    order = list(ControlMode)
    _control_mode = order[(order.index(_control_mode) + 1) % len(order)]
    return _control_mode


@dataclass
class Holder:
    """The current controller. ``label`` is human-readable for the indicator."""

    channel: str  # BROWSER | MCP
    id: str
    label: str
    last_seen: float


class ControlLease:
    """Process-global single-controller lease (one driver, many observers)."""

    def __init__(self) -> None:
        self._holder: Holder | None = None

    def _live(self, h: Holder, now: float) -> bool:
        if h.channel == BROWSER:
            return h.id in Client.instances
        return (now - h.last_seen) <= MCP_TTL_SECONDS

    def holder(self) -> Holder | None:
        """The current live holder, or ``None``. Drops a stale holder so the
        slot is reclaimable."""
        h = self._holder
        if h is not None and not self._live(h, time.monotonic()):
            self._holder = None
        return self._holder

    def describe(self) -> str:
        """Human-readable holder label, or ``"no one"`` if free."""
        h = self.holder()
        return h.label if h is not None else "no one"

    def held_by(self, channel: str, id: str) -> bool:
        """True if ``(channel, id)`` currently holds a live lease."""
        h = self.holder()
        return h is not None and h.channel == channel and h.id == id

    def is_free(self) -> bool:
        return self.holder() is None

    def seize(self, channel: str, id: str, label: str) -> None:
        """Take control for ``(channel, id)``. Anyone may seize; the displaced
        holder finds out on its next query / actuation (always visible)."""
        self._holder = Holder(channel, id, label, time.monotonic())

    def touch(self, channel: str, id: str) -> None:
        """Refresh liveness if ``(channel, id)`` is the current holder."""
        h = self._holder
        if h is not None and h.channel == channel and h.id == id:
            h.last_seen = time.monotonic()

    def release(self, channel: str, id: str) -> None:
        """Release the lease if ``(channel, id)`` holds it; no-op otherwise."""
        h = self._holder
        if h is not None and h.channel == channel and h.id == id:
            self._holder = None

    def reset(self) -> None:
        """Drop any holder (used by ``reset_all_state`` between test sessions)."""
        self._holder = None
        _consented_sessions.clear()
        _pending_consent.clear()
        _denied_at.clear()
        _pending_action.clear()
        _approved_action.clear()
        _mcp_last_message.clear()
        set_control_mode(ControlMode.INSPECT)


control_lease = ControlLease()


# --------------------------------------------------------------------------
# MCP connection presence (any session, holder or not)
# --------------------------------------------------------------------------

MCP_CONNECTED_TTL_SECONDS = 300.0

_mcp_last_message: dict[str, float] = {}  # session_id -> monotonic last message


def mcp_touch(session_id: str) -> None:
    """Record MCP activity for ``session_id`` (any message, not just tools)."""
    _mcp_last_message[session_id] = time.monotonic()


def mcp_connected() -> bool:
    """Whether any MCP session has been heard from recently.

    Presence, not control: drives the faint ambient glow that says an AI
    client is attached even while the human holds the lease."""
    now = time.monotonic()
    stale = [
        s for s, t in _mcp_last_message.items() if now - t > MCP_CONNECTED_TTL_SECONDS
    ]
    for s in stale:
        del _mcp_last_message[s]
    return bool(_mcp_last_message)


def browser_try_acquire(client_id: str | None) -> bool:
    """Acquire the actuation lease for the active browser tab ``client_id``.

    Soft reclaim: human actuation always seizes — even from a live MCP holder.
    The controller cancels any in-flight motion when the human's command arrives,
    so the two never fight. Always returns ``True`` for a real client.
    """
    if client_id is None:
        return True  # pre-init / tests without a live client — don't block
    if control_lease.held_by(BROWSER, client_id):
        return True  # already holds — no re-seize (called on every jog tick)
    control_lease.seize(BROWSER, client_id, "Browser")
    return True


def browser_claim_if_unheld(client_id: str | None) -> None:
    """Claim the lease for a newly-loaded browser tab — but only when it's free
    or held by a (stale) prior browser tab.

    Unlike actuation (:func:`browser_try_acquire`), a page load carries no
    human intent to drive, so it must never take control away from a live MCP
    holder — an F5 while the AI is driving would silently kill its session.
    """
    if client_id is None:
        return
    h = control_lease.holder()  # drops a stale holder as a side effect
    if h is None or h.channel == BROWSER:
        control_lease.seize(BROWSER, client_id, "Browser")


def require_browser_control(client_id: str | None, *, notify: bool = True) -> bool:
    """Browser-side actuation gate used across the control / io / gripper /
    playback panels. The human is always allowed (soft reclaim); this just seizes
    the lease and, the first time it takes over from a live MCP session, surfaces
    a one-shot "you've taken control" toast. Pass ``notify=False`` on repeated
    stream ticks so the toast fires once per gesture, not per tick.
    """
    prior = control_lease.holder()
    seized_from_mcp = (
        client_id is not None
        and not control_lease.held_by(BROWSER, client_id)
        and prior is not None
        and prior.channel == MCP
    )
    browser_try_acquire(client_id)
    if seized_from_mcp and notify:
        from nicegui import ui

        ui.notify("You've taken control from the AI", color="positive")
    return True


# --- Per-session hardware-motion consent (MCP) ----------------------------
# The first tool that physically moves the arm in an MCP session must be
# acknowledged once by a human in the GUI (a brief safety gate). The gate is
# synchronous: an un-consented hardware move is refused and a prompt is armed;
# the GUI grants consent and the client retries. Keyed by FastMCP session id.
_consented_sessions: set[str] = set()
_pending_consent: dict[str, str] = {}  # session_id -> human label awaiting approval

# A denied prompt must not instantly re-arm (the AI's retry loop would re-open
# the dialog ~1s after every Deny). Within the cooldown, attempts get a
# terminal "denied" error; afterwards a fresh attempt may prompt again.
CONSENT_DENY_COOLDOWN_SECONDS = 30.0
_denied_at: dict[str, float] = {}  # session_id -> monotonic time of the deny


def session_consented(session_id: str) -> bool:
    return session_id in _consented_sessions


def arm_consent_prompt(session_id: str, label: str) -> None:
    """Record that *session_id* is awaiting GUI consent for hardware motion."""
    _pending_consent[session_id] = label


def pending_consents() -> dict[str, str]:
    """Sessions awaiting consent (session_id -> label), for the GUI to prompt."""
    return dict(_pending_consent)


def grant_consent(session_id: str) -> None:
    _consented_sessions.add(session_id)
    _pending_consent.pop(session_id, None)
    _denied_at.pop(session_id, None)


def deny_consent(session_id: str) -> None:
    _pending_consent.pop(session_id, None)
    _denied_at[session_id] = time.monotonic()


def recently_denied(session_id: str) -> bool:
    """True while *session_id* is inside the post-deny cooldown."""
    t = _denied_at.get(session_id)
    if t is None:
        return False
    if (time.monotonic() - t) > CONSENT_DENY_COOLDOWN_SECONDS:
        del _denied_at[session_id]
        return False
    return True


def reset_consent(session_id: str) -> None:
    _consented_sessions.discard(session_id)
    _pending_consent.pop(session_id, None)
    _denied_at.pop(session_id, None)
    _pending_action.pop(session_id, None)
    _approved_action.pop(session_id, None)


# --- Per-action motion approval (Inspect / Auto-edits modes) ---------------
# In Inspect and Auto-edits, every MCP motion command is approved individually
# (vs. the one-time per-session hardware consent used as Autopilot's floor).
# Same arm → refuse-with-retry → grant → retry flow, but the grant is one-shot
# and matched to the action's description so a stale approval can't ride a
# different move. Denials share the consent cooldown above.
_pending_action: dict[
    str, str
] = {}  # session_id -> action description awaiting approval
_approved_action: dict[
    str, str
] = {}  # session_id -> approved, not-yet-consumed description


def arm_action_prompt(session_id: str, description: str) -> None:
    """Record that *session_id* is awaiting GUI approval for a specific move."""
    _pending_action[session_id] = description


def pending_actions() -> dict[str, str]:
    """Sessions awaiting per-action approval (session_id -> description)."""
    return dict(_pending_action)


def grant_action(session_id: str) -> None:
    """Approve the session's pending action; the next matching gated call passes."""
    description = _pending_action.pop(session_id, None)
    if description is not None:
        _approved_action[session_id] = description
    _denied_at.pop(session_id, None)


def deny_action(session_id: str) -> None:
    _pending_action.pop(session_id, None)
    _denied_at[session_id] = time.monotonic()


def take_approved_action(session_id: str, description: str) -> bool:
    """Consume a one-shot approval iff it matches *description*. A retry of the
    same refused call matches; a different move does not (it re-prompts)."""
    if _approved_action.get(session_id) == description:
        del _approved_action[session_id]
        return True
    return False
