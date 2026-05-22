"""Log panel controller: owns the shared output log widget, toggle button,
splitter, and the show/hide semantics around them.

Module-level singleton ``log_panel`` is constructed at import time. Widgets
are created lazily via the ``build_toggle_button`` / ``build_log_area`` /
``attach_splitter`` factory methods called from the editor build.
"""

from __future__ import annotations

import logging

from nicegui import ui

from waldo_commander.state import simulation_state

logger = logging.getLogger(__name__)

# Splitter values: the editor-vs-log splitter is sized as % to the editor side.
# Collapsed pins the log to a thin strip; the threshold determines whether a
# user drag should be treated as a collapse vs an expand.
LOG_COLLAPSED_VALUE: float = 94.0
LOG_EXPAND_THRESHOLD: float = 90.0


class LogPanelController:
    """Owns the editor log widget + splitter + toggle button."""

    def __init__(self) -> None:
        self.program_log: ui.log | None = None
        self.log_toggle_btn: ui.button | None = None
        self.log_toggle_btn_tooltip: ui.tooltip | None = None
        self.editor_splitter: ui.splitter | None = None
        self._log_expanded: bool = False
        self._splitter_value_when_expanded: float = 70.0
        self._last_script_running: bool = False
        simulation_state.add_change_listener(self._on_state_change)

    def cleanup(self) -> None:
        """Per-page cleanup. No-op: the change listener was registered in
        ``__init__`` (process-wide, single instance), not in a per-page
        setup, so there's nothing to deregister. Method exists for symmetry
        with the other singletons."""

    def reset_for_test(self) -> None:
        """Restore field defaults by replaying ``__init__`` on this instance."""
        self.cleanup()
        type(self).__init__(self)

    # ---- State listener: auto-expand on script start ----

    def _on_state_change(self) -> None:
        running = simulation_state.script_running
        if running and not self._last_script_running and not self._log_expanded:
            self.expand()
        self._last_script_running = running

    # ---- Widget construction ----

    def build_toggle_button(self) -> ui.button:
        """Create the show/hide toggle button. Call inside the playback bar."""
        # Reset transient state for a fresh page build (new client / test).
        self._log_expanded = False
        self._last_script_running = simulation_state.script_running
        self.log_toggle_btn = (
            ui.button(icon="expand_more", on_click=self.toggle)
            .props("round dense flat")
            .classes("text-white")
        )
        with self.log_toggle_btn:
            self.log_toggle_btn_tooltip = ui.tooltip("Show Output")
        self.log_toggle_btn.mark("editor-log-toggle")
        # Page-reload-during-script: the listener's idle→running edge won't
        # fire (baseline is already running), so seed the visual to match.
        if self._last_script_running:
            self.expand()
        return self.log_toggle_btn

    def build_log_area(self) -> ui.log:
        """Create the shared ui.log widget. Call inside the splitter's after slot."""
        self.program_log = (
            ui.log(max_lines=1000)
            .classes("w-full h-full whitespace-pre-wrap break-words")
            .style("min-height: 0;")
        )
        return self.program_log

    def attach_splitter(self, splitter: ui.splitter) -> None:
        self.editor_splitter = splitter

    # ---- Toggle semantics ----

    def _set_toggle_visual(self, expanded: bool) -> None:
        if not self.log_toggle_btn:
            return
        self.log_toggle_btn.props(
            f"icon={'expand_less' if expanded else 'expand_more'}"
        )
        if self.log_toggle_btn_tooltip:
            self.log_toggle_btn_tooltip.text = (
                "Hide Output" if expanded else "Show Output"
            )

    def toggle(self) -> None:
        if self._log_expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        self._log_expanded = True
        if self.editor_splitter:
            self.editor_splitter.set_value(self._splitter_value_when_expanded)
        self._set_toggle_visual(True)

    def collapse(self) -> None:
        self._log_expanded = False
        if self.editor_splitter:
            self.editor_splitter.set_value(LOG_COLLAPSED_VALUE)
        self._set_toggle_visual(False)

    def on_splitter_change(self, e) -> None:
        """Update expanded state when user drags the splitter directly."""
        value = e.value
        if value is None:
            return
        if value > LOG_EXPAND_THRESHOLD:
            self._log_expanded = False
            self._set_toggle_visual(False)
        else:
            self._log_expanded = True
            self._splitter_value_when_expanded = value
            self._set_toggle_visual(True)

    # ---- Log content ----

    def push(self, line: str) -> None:
        if self.program_log:
            self.program_log.push(line)

    def clear(self) -> None:
        if self.program_log:
            self.program_log.clear()


log_panel: LogPanelController = LogPanelController()
