"""Action-log dedup service — feeds ``commander.status.action``.

Action state lives on ``commander.status.action`` (``history``, ``state``,
``current_name``). This service holds the per-session bookkeeping needed to
coalesce repeated commands and detect command transitions from the raw
``StatusBuffer`` fields.

``process_status`` is called from the status consumer once per tick; on any
change it reassigns ``commander.status.action.history`` wholesale and calls
``Action.notify_changed()`` so the readout panel's change listener rebuilds
its HTML. (Nothing value-binds ``Action.history``; the listener is what drives
the refresh.)
"""

from __future__ import annotations

import time
from collections import deque

import waldoctl
from waldoctl import ActionLogEntry, ActionState, ActionStatus


class ActionLogService:
    """Dedup + coalesce raw status fields into ``commander.status.action``."""

    def __init__(self, max_entries: int = 200) -> None:
        self._max_entries = max_entries
        self._last_executing_index: int = -1
        self._last_completed_index: int = -1
        # True when the newest history entry is still EXECUTING — i.e. a
        # FAILED transition is still possible without an index advance.
        self._tail_executing: bool = False

    def process_status(
        self,
        action_current: str,
        action_params: str,
        action_state: ActionState,
        executing_index: int,
        completed_index: int,
    ) -> bool:
        """Process a status update, returning True if the log changed."""
        # Nothing can change unless an index advanced or a still-EXECUTING
        # entry can transition to FAILED. Skip the per-tick deque copy in the
        # common idle / steady-EXECUTING case — this runs at 20-50 Hz.
        if (
            executing_index == self._last_executing_index
            and completed_index == self._last_completed_index
            and not (self._tail_executing and action_state != ActionState.EXECUTING)
        ):
            return False

        action = waldoctl.commander.status.action
        entries = deque(action.history, maxlen=self._max_entries)
        changed = False

        # New command starting (executing index advanced + state is EXECUTING).
        if (
            executing_index > self._last_executing_index
            and action_state == ActionState.EXECUTING
        ):
            name = action_current.removesuffix("Command")
            latest = entries[-1] if entries else None
            if (
                latest
                and latest.command_name == name
                and latest.params == action_params
                and latest.status == ActionStatus.COMPLETED
            ):
                latest.count += 1
                latest.status = ActionStatus.EXECUTING
                latest.command_index = executing_index
                latest.timestamp = time.time()
            else:
                entries.append(
                    ActionLogEntry(
                        command_name=name,
                        params=action_params,
                        command_index=executing_index,
                        timestamp=time.time(),
                    )
                )
            self._last_executing_index = executing_index
            changed = True

        # Command completion.
        if completed_index > self._last_completed_index:
            matched = False
            for entry in reversed(entries):
                if entry.command_index == completed_index:
                    entry.status = ActionStatus.COMPLETED
                    matched = True
                    break
            # Coalesced entries may have overwritten command_index — fall back
            # to marking the latest still-EXECUTING entry as completed.
            if not matched and entries:
                for entry in reversed(entries):
                    if entry.status == ActionStatus.EXECUTING:
                        entry.status = ActionStatus.COMPLETED
                        break
            self._last_completed_index = completed_index
            changed = True

        # Failure: action goes non-EXECUTING but completed_index didn't advance.
        if (
            action_state != ActionState.EXECUTING
            and entries
            and entries[-1].status == ActionStatus.EXECUTING
            and completed_index == self._last_completed_index
            and executing_index == self._last_executing_index
        ):
            entries[-1].status = ActionStatus.FAILED
            changed = True

        if changed:
            # Reassign wholesale (a new list) and notify change listeners so
            # the readout's listener rebuilds its HTML.
            action.history = list(entries)
            action.notify_changed()
            self._tail_executing = (
                bool(entries) and entries[-1].status == ActionStatus.EXECUTING
            )
        return changed

    def clear(self) -> None:
        """Drop all history and reset the dedup cursor.

        Called from ``reset_all_state`` between tests; tolerates being run
        before the Commander locator has been registered (early-fixture
        setup) by skipping the surface write — there's no state to clear
        when no Commander exists yet.
        """
        self._last_executing_index = -1
        self._last_completed_index = -1
        self._tail_executing = False
        try:
            action = waldoctl.commander.status.action
        except RuntimeError:
            return
        action.history = []
        action.notify_changed()


action_log_service: ActionLogService = ActionLogService()
