"""CodeMirror decoration controller: flash, executing-line highlight, diagnostics, line tooltips, and target anchors.

Decoration writes are routed to a specific tab's textarea by tab_id. Callers that
own a tab context pass that tab_id; flash decorations always target the active tab
because their callers write to the user's current edit surface. The executing-line
highlight is cleared automatically on the ``is_any_program_running()`` True→False
edge, via the state listener registered in __init__.
"""

from __future__ import annotations

import html
import re

from nicegui import ui
from nicegui.elements.codemirror.codemirror import (
    DecorationSpec,
    Diagnostic,
)

import waldoctl

from waldo_commander.common.tab_flash import flash_tab
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.state import simulation_state, ui_state


_ERROR_LINE_RE = re.compile(
    r'(?:File "simulation_script\.py", line (\d+))|(?:^Line (\d+):)',
    re.MULTILINE,
)


class EditorDecorations:
    """Owns CodeMirror decoration state (flash + executing-line highlight) and
    diagnostics/tooltip/anchor pushes.

    Construction registers a ``simulation_state`` change listener that clears
    the executing-line highlight on the script-stop edge.
    """

    def __init__(self) -> None:
        self._active_flashes: list[tuple[int, set[int]]] = []
        self._flash_token: int = 0
        # Tracked per launching tab so the highlight persists when the user
        # switches away mid-run.
        self._executing_line_by_tab: dict[str, int] = {}
        self._last_script_running: bool = False
        simulation_state.add_change_listener(self._on_state_change)

    def cleanup(self) -> None:
        """Per-page cleanup. Clears in-flight decoration state so a flash timer
        that died with the client doesn't leave stale entries that the next page
        aggregates onto its textarea. The change listener stays registered
        (process-wide single instance — nothing to deregister)."""
        self._active_flashes.clear()
        self._executing_line_by_tab.clear()
        self._flash_token = 0

    def reset_for_test(self) -> None:
        """Restore field defaults by replaying ``__init__`` on this instance.
        Listener re-registration is idempotent via ``add_change_listener``'s
        ``not in`` check (relies on the bound-method equality fix in state.py)."""
        self.cleanup()
        type(self).__init__(self)

    def _on_state_change(self) -> None:
        running = is_any_program_running()
        if self._last_script_running and not running:
            # In practice there's at most one (only one script runs at a time),
            # but the dict is the source of truth.
            for tab_id in list(self._executing_line_by_tab):
                self.clear_executing_line_highlight(tab_id)
        self._last_script_running = running

    def _apply_decorations_to_tab(self, tab_id: str) -> None:
        """Write the aggregated decoration spec list for one tab's textarea.

        Combines whatever flash decorations are active (flashes are always
        on the active tab, so they only appear when tab_id == active) with
        that tab's executing-line highlight and any pending LLM-proposed
        edits. Result is assigned to the tab's CodeMirror ``decorations``
        in a single round-trip.
        """
        textarea = ui_state.textareas_by_tab.get(tab_id)
        if textarea is None:
            return
        specs: list[DecorationSpec] = []
        if tab_id == waldoctl.commander.programs.active_id:
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
        specs.extend(self._diff_decoration_specs(tab_id))
        textarea.decorations[:] = specs

    def _diff_decoration_specs(self, tab_id: str) -> list[DecorationSpec]:
        """Build decoration specs from this tab's pending LLM edits.

        For each pending edit:
        - lines marked ``-`` get a ``line`` decoration with
          ``cm-edit-remove`` (red strikethrough background).
        - each contiguous run of ``+`` lines becomes one ``widget``
          decoration at the position where the run occurs, classed
          ``cm-edit-add`` so the editor renders a green "+ <addition>"
          widget in place (interior insertions don't collapse to the
          hunk's end).

        Positions are computed against the diff's base source; once pushed,
        CodeMirror's decoration StateField maps them through subsequent
        document edits, so this must only be re-pushed when the pending-edits
        list itself changes.

        Pending diffs are validated at ``propose()`` time, so an unparseable
        diff can't reach this list; the ``except ValueError`` is cheap
        insurance for a directly-constructed PendingEdit.
        """
        tab = waldoctl.commander.programs.get(tab_id)
        if tab is None or not tab.edits.pending:
            return []
        # CodeMirror document positions are UTF-16 code-unit offsets, so the
        # widget anchor must accumulate UTF-16 lengths — Python's ``len`` counts
        # code points, which drifts one unit per astral-plane char (e.g. an
        # emoji) earlier in the source. Split on LF/CRLF/CR only and count
        # every break as ONE unit: CodeMirror normalizes documents to "\n"
        # (a CRLF counted as 2 would drift anchors +1 per preceding line) and,
        # unlike str.splitlines, doesn't break lines on \f/\x85/U+2028.
        line_starts = [0]
        for line in re.split(r"\r\n|\r|\n", tab.source):
            line_starts.append(line_starts[-1] + len(line.encode("utf-16-le")) // 2 + 1)
        specs: list[DecorationSpec] = []
        for edit in tab.edits.pending:
            try:
                hunks = waldoctl.parse_unified_diff(edit.diff)
            except ValueError:
                continue
            for h in hunks:
                # Shared with the apply path so preview and approve can't
                # diverge: a pure-insertion hunk anchors after old_start.
                cursor = h.start_index
                added: list[str] = []

                def _flush_added() -> None:
                    if added:
                        pos = line_starts[min(cursor, len(line_starts) - 1)]
                        specs.append(
                            {
                                "kind": "widget",
                                "position": pos,
                                "text": "\n".join("+ " + s for s in added),
                                "class": "cm-edit-add",
                                "side": 1,
                            }
                        )
                        added.clear()

                for op, content in h.body:
                    if op == " ":
                        _flush_added()
                        cursor += 1
                    elif op == "-":
                        _flush_added()
                        specs.append(
                            {
                                "kind": "line",
                                "line": cursor + 1,
                                "class": "cm-edit-remove",
                            }
                        )
                        cursor += 1
                    elif op == "+":
                        added.append(content)
                _flush_added()
        return specs

    def diff_touched_lines(
        self, tab_id: str, edit_ids: set[str] | None = None
    ) -> list[int]:
        """1-based line numbers touched by a tab's pending edits — each removed
        line and each addition's anchor line. Used to flash a freshly proposed
        edit the same way the motion recorder flashes an insert. ``edit_ids``
        (edit-id ``.value`` strings) limits the walk to specific edits; ``None``
        means all pending. Mirrors the cursor walk in ``_diff_decoration_specs``.
        """
        tab = waldoctl.commander.programs.get(tab_id)
        if tab is None or not tab.edits.pending:
            return []
        lines: set[int] = set()
        for edit in tab.edits.pending:
            if edit_ids is not None and edit.id.value not in edit_ids:
                continue
            try:
                hunks = waldoctl.parse_unified_diff(edit.diff)
            except ValueError:
                continue
            for h in hunks:
                cursor = h.start_index
                for op, _content in h.body:
                    if op == " ":
                        cursor += 1
                    elif op == "-":
                        lines.add(cursor + 1)
                        cursor += 1
                    elif op == "+":
                        lines.add(cursor + 1)
        return sorted(lines)

    def refresh_diff_overlay(self, tab_id: str) -> None:
        """Re-render decorations for ``tab_id`` after its pending-edits list
        changed. Public entry point for the editor's edit-listener wiring."""
        self._apply_decorations_to_tab(tab_id)

    def _apply_active_tab_decorations(self) -> None:
        """Re-render decorations on whichever tab is currently active.

        Used by the flash path, where the change is on the active tab and
        any executing-line highlight that happens to be on the same tab
        needs to be preserved in the single ``decorations`` write."""
        active = waldoctl.commander.programs.active_id
        if active is not None:
            self._apply_decorations_to_tab(active)

    def flash_editor_lines(self, line_numbers: list[int]) -> None:
        """Flash specific lines in the CodeMirror editor.

        Flashes always target the active tab — both callers
        (``EditorPanel.add_target_code`` and the motion recorder) write to
        the user's current edit surface. When the editor panel is
        collapsed, flashes the editor tab instead of applying decorations
        to an off-screen textarea.
        """
        textarea = ui_state.active_textarea
        if not textarea or not line_numbers:
            return
        if not ui_state.program_panel_visible:
            flash_tab(ui_state._program_tab)
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

    def highlight_executing_line(self, step_index: int, tab_id: str) -> None:
        """Highlight the source line on the launching tab for the current step.

        ``tab_id`` is the tab the script was launched from. Decorations
        stay on that tab even if the user switches away mid-run.
        """
        textarea = ui_state.textareas_by_tab.get(tab_id)
        if textarea is None:
            return

        new_line: int | None = None
        tab = waldoctl.commander.programs.get(tab_id)
        if tab and 0 <= step_index < len(tab.dry_run.path_segments):
            segment = tab.dry_run.path_segments[step_index]
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

    def apply_diagnostics(self, error: str | None, tab_id: str) -> None:
        """Apply CM6 lint diagnostics for simulation errors and timing
        warnings to the simulated tab's textarea."""
        textarea = ui_state.textareas_by_tab.get(tab_id)
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
        tab = waldoctl.commander.programs.get(tab_id)
        segments = tab.dry_run.path_segments if tab is not None else []
        for seg in segments:
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

    def push_line_metadata(self, tab_id: str) -> None:
        """Push per-line metadata to CM6 for hover tooltips on the
        simulated tab's textarea."""
        textarea = ui_state.textareas_by_tab.get(tab_id)
        if textarea is None:
            return
        tooltips: dict[int, str] = {}
        tab = waldoctl.commander.programs.get(tab_id)
        segments = tab.dry_run.path_segments if tab is not None else []
        for seg in segments:
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
        textarea = ui_state.textareas_by_tab.get(tab_id)
        if textarea is None:
            return
        tab = waldoctl.commander.programs.get(tab_id)
        targets = tab.dry_run.targets if tab is not None else []
        anchors = {t.id: t.line_number for t in targets if t.line_number > 0}
        if textarea is ui_state.active_textarea:
            # A full re-declare would drop the recording insertion cursor;
            # merging keeps it tracking at its browser-remapped position.
            anchors.update(motion_recorder.insertion_anchor())
        textarea.line_anchors = anchors


decorations: EditorDecorations = EditorDecorations()
