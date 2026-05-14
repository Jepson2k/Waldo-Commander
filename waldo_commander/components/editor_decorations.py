"""CodeMirror decoration controller: flash, executing-line highlight, diagnostics, line tooltips, and target anchors.

Reads the active textarea from ``editor_tabs_state.active_textarea`` (which
``EditorPanel`` updates on tab switch), so it works for whichever tab is
currently focused without needing a back-reference into ``EditorPanel``.

Clears the executing-line highlight automatically when ``simulation_state.script_running``
transitions from True to False (via the state listener registered in __init__).
"""

from __future__ import annotations

import html
import logging
import re

from nicegui import Client, ui
from nicegui.elements.codemirror.codemirror import DecorationSpec, Diagnostic

from waldo_commander.state import editor_tabs_state, simulation_state, ui_state

logger = logging.getLogger(__name__)


_ERROR_LINE_RE = re.compile(
    r'(?:File "simulation_script\.py", line (\d+))|(?:^Line (\d+):)',
    re.MULTILINE,
)


class EditorDecorations:
    """Owns CodeMirror decoration state (flash + executing-line highlight) and
    diagnostics/tooltip/anchor pushes for whichever tab is currently active.

    Construction registers a ``simulation_state`` change listener that clears
    the executing-line highlight on the script-stop edge.
    """

    def __init__(self) -> None:
        self._active_flashes: list[tuple[int, set[int]]] = []
        self._flash_token: int = 0
        self._executing_line: int | None = None
        self._ui_client: Client | None = None
        self._last_script_running: bool = False
        simulation_state.add_change_listener(self._on_state_change)

    # ---- Wiring ----

    def set_ui_client(self, client: Client | None) -> None:
        """Store the page client for JS execution from background tasks."""
        self._ui_client = client

    def _on_state_change(self) -> None:
        running = simulation_state.script_running
        if self._last_script_running and not running:
            self.clear_executing_line_highlight()
        self._last_script_running = running

    # ---- Decoration application ----

    def _apply_decorations(self) -> None:
        """Single source of truth for editor decorations.

        Aggregates flash + executing-line state into one list and assigns to
        the flat ``decorations`` property in a single round-trip.
        """
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return
        specs: list[DecorationSpec] = []
        flash_lines: set[int] = set()
        for _, lines in self._active_flashes:
            flash_lines.update(lines)
        for ln in sorted(flash_lines):
            specs.append({"kind": "line", "line": ln, "class": "cm-line-flash"})
        if self._executing_line is not None:
            specs.append(
                {
                    "kind": "line",
                    "line": self._executing_line,
                    "class": "cm-highlighted",
                }
            )
        textarea.decorations[:] = specs

    def flash_editor_lines(self, line_numbers: list[int]) -> None:
        """Flash specific lines in the CodeMirror editor.

        When the editor panel is collapsed, flashes the editor tab via JS
        instead of applying decorations to an off-screen textarea.
        """
        textarea = editor_tabs_state.active_textarea
        if not textarea or not line_numbers:
            return
        if not ui_state.program_panel_visible:
            self.flash_editor_tab()
            return
        self._flash_token += 1
        token = self._flash_token
        self._active_flashes.append((token, set(line_numbers)))
        self._apply_decorations()
        textarea.reveal_line(max(line_numbers))
        ui.timer(1.5, lambda t=token: self._expire_flash(t), once=True)

    def _expire_flash(self, token: int) -> None:
        before = len(self._active_flashes)
        self._active_flashes = [
            (t, lns) for t, lns in self._active_flashes if t != token
        ]
        if len(self._active_flashes) != before:
            self._apply_decorations()

    def flash_editor_tab(self) -> None:
        """Flash the editor tab to indicate new content when panel is collapsed."""
        js_code = """
        (function() {
            const tabs = document.querySelectorAll('.q-tab');
            for (const tab of tabs) {
                const icon = tab.querySelector('i');
                if (icon && icon.innerText === 'code') {
                    tab.classList.add('tab-flash');
                    setTimeout(() => tab.classList.remove('tab-flash'), 2000);
                    break;
                }
            }
        })();
        """
        try:
            ui.run_javascript(js_code)
        except (RuntimeError, AssertionError):
            # No active client context — try the stored page client; if none,
            # we're likely in a unit test where the JS hook is moot.
            if self._ui_client:
                try:
                    self._ui_client.run_javascript(js_code)
                except (RuntimeError, AssertionError):
                    pass
            else:
                logger.debug("Cannot flash editor tab: no client available")

    # ---- Executing-line highlight ----

    def highlight_executing_line(self, step_index: int) -> None:
        """Highlight the source line corresponding to the current step."""
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return

        if simulation_state.path_segments and 0 <= step_index < len(
            simulation_state.path_segments
        ):
            segment = simulation_state.path_segments[step_index]
            if segment.line_number > 0:
                self._executing_line = segment.line_number
                self._apply_decorations()
                textarea.reveal_line(segment.line_number)
                return

        self._executing_line = None
        self._apply_decorations()

    def clear_executing_line_highlight(self) -> None:
        """Clear the executing line highlight decoration."""
        if self._executing_line is not None:
            self._executing_line = None
            self._apply_decorations()

    # ---- Diagnostics ----

    def apply_diagnostics(self, error: str | None = None) -> None:
        """Apply CM6 lint diagnostics for simulation errors and timing warnings."""
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return

        diagnostics: list[Diagnostic] = []

        if error:
            error_lines: set[int] = set()
            for m in _ERROR_LINE_RE.finditer(error):
                line_no = int(m.group(1) or m.group(2))
                error_lines.add(line_no)
            error_msg = error.strip().split("\n")[-1] if error.strip() else error
            for ln in sorted(error_lines):
                diagnostics.append(
                    {
                        "line": ln,
                        "severity": "error",
                        "message": error_msg,
                        "source": "simulation",
                    }
                )

        warned_lines: set[int] = set()
        for seg in simulation_state.path_segments:
            if seg.timing_feasible or seg.line_number <= 0:
                continue
            if seg.line_number in warned_lines:
                continue
            warned_lines.add(seg.line_number)
            if seg.estimated_duration is not None:
                diagnostics.append(
                    {
                        "line": seg.line_number,
                        "severity": "warning",
                        "message": f"Duration too short — minimum: {seg.estimated_duration:.2f}s",
                        "source": "timing",
                    }
                )

        textarea.diagnostics = diagnostics

    # ---- Line metadata + target anchors ----

    def push_line_metadata(self) -> None:
        """Push per-line metadata to CM6 for hover tooltips."""
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return
        tooltips: dict[int, str] = {}
        for seg in simulation_state.path_segments:
            if seg.line_number <= 0 or not seg.points:
                continue
            end = seg.points[-1]
            pos_str = html.escape(
                f"x: {end[0] * 1000:.1f}, y: {end[1] * 1000:.1f}, z: {end[2] * 1000:.1f} mm"
            )
            parts = [f"<div>{pos_str}</div>"]
            if seg.estimated_duration:
                parts.append(
                    f"<div>Duration: {html.escape(f'{seg.estimated_duration:.2f}s')}</div>"
                )
            if not seg.is_valid:
                parts.append('<div style="color:#f87171">Unreachable position</div>')
            if not seg.timing_feasible and seg.estimated_duration is not None:
                parts.append(
                    f'<div style="color:#fbbf24">Duration too short (min: {html.escape(f"{seg.estimated_duration:.2f}s")})</div>'
                )
            tooltips[seg.line_number] = "".join(parts)

        textarea._props["line-tooltips"] = tooltips

    def push_target_positions(self) -> None:
        """Push current target positions to CM6 line anchors for edit tracking."""
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return
        anchors = [
            {"id": t.id, "line": t.line_number}
            for t in simulation_state.targets
            if t.line_number > 0
        ]
        textarea.line_anchors[:] = anchors


decorations: EditorDecorations = EditorDecorations()
