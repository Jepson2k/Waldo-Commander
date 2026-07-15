"""Records the outcome of proposed program edits.

``EditFlow`` drops an edit from ``pending`` on approve/reject without keeping
a record, so an MCP client waiting on its proposal couldn't tell *how* it
left the queue. The editor's approve/reject handlers (and the MCP cancel
tool) record here; ``programs.wait_edit_decision`` reads it back.
"""

from __future__ import annotations

from typing import Literal

Outcome = Literal["applied", "rejected", "withdrawn"]

_MAX_DECISIONS = 256

_decisions: dict[str, Outcome] = {}


def record(edit_id: str, outcome: Outcome) -> None:
    """Remember how ``edit_id`` left the pending queue (bounded FIFO)."""
    while len(_decisions) >= _MAX_DECISIONS:
        del _decisions[next(iter(_decisions))]
    _decisions[edit_id] = outcome


def get(edit_id: str) -> Outcome | None:
    """The recorded outcome for ``edit_id``, or ``None`` if undecided."""
    return _decisions.get(edit_id)


def clear() -> None:
    """Drop all records (test isolation)."""
    _decisions.clear()
