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


control_lease = ControlLease()
