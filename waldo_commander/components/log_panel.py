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

    def toggle(self) -> None:
        if self._log_expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        self._log_expanded = True
        if self.editor_splitter:
            self.editor_splitter.set_value(self._splitter_value_when_expanded)
        if self.log_toggle_btn:
            self.log_toggle_btn.props("icon=expand_less")
            if self.log_toggle_btn_tooltip:
                self.log_toggle_btn_tooltip.text = "Hide Output"

    def collapse(self) -> None:
        self._log_expanded = False
        if self.editor_splitter:
            self.editor_splitter.set_value(94)  # 94% to editor (collapsed)
        if self.log_toggle_btn:
            self.log_toggle_btn.props("icon=expand_more")
            if self.log_toggle_btn_tooltip:
                self.log_toggle_btn_tooltip.text = "Show Output"

    def on_splitter_change(self, e) -> None:
        """Update expanded state when user drags the splitter directly."""
        value = e.value
        if value is None:
            return
        if value > 90:
            self._log_expanded = False
            if self.log_toggle_btn:
                self.log_toggle_btn.props("icon=expand_more")
                if self.log_toggle_btn_tooltip:
                    self.log_toggle_btn_tooltip.text = "Show Output"
        else:
            self._log_expanded = True
            self._splitter_value_when_expanded = value
            if self.log_toggle_btn:
                self.log_toggle_btn.props("icon=expand_less")
                if self.log_toggle_btn_tooltip:
                    self.log_toggle_btn_tooltip.text = "Hide Output"

    # ---- Log content ----

    def push(self, line: str) -> None:
        if self.program_log:
            self.program_log.push(line)

    def clear(self) -> None:
        if self.program_log:
            self.program_log.clear()


log_panel: LogPanelController = LogPanelController()
