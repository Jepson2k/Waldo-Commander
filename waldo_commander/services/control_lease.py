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

from nicegui import Client

BROWSER = "browser"
MCP = "mcp"

# How long an MCP holder stays "live" without a gated call before it ages out.
MCP_TTL_SECONDS = 30.0


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


control_lease = ControlLease()


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


def deny_consent(session_id: str) -> None:
    _pending_consent.pop(session_id, None)


def reset_consent(session_id: str) -> None:
    _consented_sessions.discard(session_id)
    _pending_consent.pop(session_id, None)
