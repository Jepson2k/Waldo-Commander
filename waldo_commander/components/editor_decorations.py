"""CodeMirror decoration controller: flash, executing-line highlight, diagnostics, line tooltips, and target anchors.

Decoration writes are routed to a specific tab's textarea by tab_id (looked
up via ``editor_tabs_state.get_tab_textarea``). Sub-controllers that own a
tab context pass that tab_id — simulation_engine for diagnostics / line
metadata / target anchors (the simulated tab), script_exec for the
executing-line highlight (the launching tab). Flash decorations stay on
the active tab because their callers (insert-command, motion recorder)
always target the user's current edit surface.

Clears the executing-line highlight automatically when ``simulation_state.script_running``
transitions from True to False (via the state listener registered in __init__).
"""

from __future__ import annotations

import html
import logging
import re

from nicegui import Client, ui
from nicegui.elements.codemirror.codemirror import (
    DecorationSpec,
    Diagnostic,
    LineAnchor,
)

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
        # Executing-line is tracked per launching tab so the highlight
        # persists on that tab even when the user switches away while a
        # script is running.
        self._executing_line_by_tab: dict[str, int] = {}
        self._ui_client: Client | None = None
        self._last_script_running: bool = False
        simulation_state.add_change_listener(self._on_state_change)

    def cleanup(self) -> None:
        """Per-page cleanup. Clears in-flight decoration state so a flash
        timer that died with the client doesn't leave stale entries that
        the next page's apply routine would aggregate onto the new
        textarea. The change listener stays registered (process-wide,
        single instance — nothing to deregister)."""
        self._active_flashes.clear()
        self._executing_line_by_tab.clear()
        self._flash_token = 0

    def reset_for_test(self) -> None:
        """Restore field defaults by replaying ``__init__`` on this instance.
        Listener re-registration is idempotent via ``add_change_listener``'s
        ``not in`` check (bound-method equality fixed in state.py)."""
        self.cleanup()
        type(self).__init__(self)

    # ---- Wiring ----

    def set_ui_client(self, client: Client | None) -> None:
        """Store the page client for JS execution from background tasks."""
        self._ui_client = client

    def _on_state_change(self) -> None:
        running = simulation_state.script_running
        if self._last_script_running and not running:
            # Script stopped — clear every tracked executing-line highlight
            # (in practice there's at most one, since only one script can
            # run at a time, but the dict is the source of truth).
            for tab_id in list(self._executing_line_by_tab):
                self.clear_executing_line_highlight(tab_id)
        self._last_script_running = running

    # ---- Decoration application ----

    def _apply_decorations_to_tab(self, tab_id: str) -> None:
        """Write the aggregated decoration spec list for one tab's textarea.

        Combines whatever flash decorations are active (flashes are always
        on the active tab, so they only appear when tab_id == active) with
        that tab's executing-line highlight. Result is assigned to the
        tab's CodeMirror ``decorations`` in a single round-trip.
        """
        textarea = editor_tabs_state.get_tab_textarea(tab_id)
        if textarea is None:
            return
        specs: list[DecorationSpec] = []
        if tab_id == editor_tabs_state.active_tab_id:
            flash_lines: set[int] = set()
            for _, lines in self._active_flashes:
                flash_lines.update(lines)
            for ln in sorted(flash_lines):
                specs.append({"kind": "line", "line": ln, "class": "cm-line-flash"})
        executing_line = self._executing_line_by_tab.get(tab_id)
        if executing_line is not None:
            specs.append(
                {
                    "kind": "line",
                    "line": executing_line,
                    "class": "cm-highlighted",
                }
            )
        textarea.decorations[:] = specs

    def _apply_active_tab_decorations(self) -> None:
        """Re-render decorations on whichever tab is currently active.

        Used by the flash path, where the change is on the active tab and
        any executing-line highlight that happens to be on the same tab
        needs to be preserved in the single ``decorations`` write."""
        active = editor_tabs_state.active_tab_id
        if active is not None:
            self._apply_decorations_to_tab(active)

    def flash_editor_lines(self, line_numbers: list[int]) -> None:
        """Flash specific lines in the CodeMirror editor.

        Flashes always target the active tab — the only callers are
        insert-command and motion recorder, both of which write to the
        user's current edit surface. When the editor panel is collapsed,
        flashes the editor tab via JS instead of applying decorations to
        an off-screen textarea.
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
        self._apply_active_tab_decorations()
        textarea.reveal_line(max(line_numbers))
        ui.timer(1.5, lambda t=token: self._expire_flash(t), once=True)

    def _expire_flash(self, token: int) -> None:
        before = len(self._active_flashes)
        self._active_flashes = [
            (t, lns) for t, lns in self._active_flashes if t != token
        ]
        if len(self._active_flashes) != before:
            self._apply_active_tab_decorations()

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

    def highlight_executing_line(self, step_index: int, tab_id: str) -> None:
        """Highlight the source line on the launching tab for the current step.

        ``tab_id`` is the tab the script was launched from. Decorations
        stay on that tab even if the user switches away mid-run.
        """
        textarea = editor_tabs_state.get_tab_textarea(tab_id)
        if textarea is None:
            return

        new_line: int | None = None
        if simulation_state.path_segments and 0 <= step_index < len(
            simulation_state.path_segments
        ):
            segment = simulation_state.path_segments[step_index]
            if segment.line_number > 0:
                new_line = segment.line_number

        current = self._executing_line_by_tab.get(tab_id)
        if new_line == current:
            if new_line is not None:
                textarea.reveal_line(new_line)
            return

        if new_line is None:
            self._executing_line_by_tab.pop(tab_id, None)
        else:
            self._executing_line_by_tab[tab_id] = new_line
        self._apply_decorations_to_tab(tab_id)
        if new_line is not None:
            textarea.reveal_line(new_line)

    def clear_executing_line_highlight(self, tab_id: str) -> None:
        """Clear the executing-line highlight from the given tab."""
        if tab_id in self._executing_line_by_tab:
            del self._executing_line_by_tab[tab_id]
            self._apply_decorations_to_tab(tab_id)

    # ---- Diagnostics ----

    def apply_diagnostics(self, error: str | None, tab_id: str) -> None:
        """Apply CM6 lint diagnostics for simulation errors and timing
        warnings to the simulated tab's textarea."""
        textarea = editor_tabs_state.get_tab_textarea(tab_id)
        if textarea is None:
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

    def push_line_metadata(self, tab_id: str) -> None:
        """Push per-line metadata to CM6 for hover tooltips on the
        simulated tab's textarea."""
        textarea = editor_tabs_state.get_tab_textarea(tab_id)
        if textarea is None:
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

    def push_target_positions(self, tab_id: str) -> None:
        """Push current target positions to CM6 line anchors on the
        simulated tab's textarea for edit tracking."""
        textarea = editor_tabs_state.get_tab_textarea(tab_id)
        if textarea is None:
            return
        anchors: list[LineAnchor] = [
            {"id": t.id, "line": t.line_number}
            for t in simulation_state.targets
            if t.line_number > 0
        ]
        textarea.line_anchors[:] = anchors


decorations: EditorDecorations = EditorDecorations()
