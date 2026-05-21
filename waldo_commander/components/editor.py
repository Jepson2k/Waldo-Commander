"""Program editor component with script execution and command palette."""

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Callable

from nicegui import context, ui, Client
from waldo_commander.common.theme import get_theme
from waldo_commander.constants import REPO_ROOT
from waldo_commander.state import (
    robot_state,
    simulation_state,
    ui_state,
    EditorTab,
    editor_tabs_state,
    recording_state,
)
from waldo_commander.services.command_discovery import (
    discover_robot_commands,
    generate_completions_from_commands,
)
from waldo_commander.components.editor_decorations import decorations
from waldo_commander.components.log_panel import LOG_COLLAPSED_VALUE, log_panel
from waldo_commander.components.simulation_engine import (
    default_python_snippet,
    get_home_joints_rad,
    is_default_script,
    simulation,
)
from waldo_commander.components.script_execution import script_exec
from waldo_commander.components.playback import playback
from waldo_commander.components.file_operations import FileOperationsMixin

logger = logging.getLogger(__name__)


class EditorPanel(FileOperationsMixin):
    """Program editor panel with script execution and command palette."""

    def __init__(self) -> None:
        """Initialize editor panel with state and UI references."""
        self._ui_client: Client | None = None  # NiceGUI client for JS execution
        # Program directory
        self.PROGRAM_DIR = (
            REPO_ROOT / "PAROL-commander-software" / "GUI" / "files" / "Programs"
        )
        if not self.PROGRAM_DIR.exists():
            self.PROGRAM_DIR = REPO_ROOT / "programs"
            self.PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
        script_exec.set_program_dir(self.PROGRAM_DIR)

        # Multi-tab management
        self.tabs_container: ui.tabs | None = None
        self.tab_panels_container: ui.tab_panels | None = None
        self._tab_widgets: dict[
            str, dict
        ] = {}  # tab_id -> {tab_element, filename_input, dirty_dot, panel, textarea}

        # Active tab's widgets live on editor_tabs_state.active_textarea /
        # active_filename_input — sub-controllers (decorations, simulation,
        # motion recorder, script execution) read them from there directly.

        # Playback singleton (owns bottom bar UI, playback logic, and recording).
        # Kept as an instance attribute so external callers (and tests) can
        # still read editor.playback.X.
        self.playback = playback

        # Debounce for tab-switch path rendering
        self._tab_switch_render_task: asyncio.Task | None = None

    def _insert_command(self, method_name: str) -> None:
        """Build a snippet for ``method_name`` (pre-filled with the robot's
        current position for move_j / move_l) and append it to the active
        textarea."""
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return

        utility_snippets = {
            "delay": "time.sleep(1.0)",
            "comment": "# Add your robot commands here",
        }
        if method_name in utility_snippets:
            snippet = utility_snippets[method_name]
        elif method_name == "move_j":
            speed = max(0.01, min(1.0, ui_state.jog_speed / 100.0))
            accel = max(0.01, min(1.0, ui_state.jog_accel / 100.0))
            angles = list(robot_state.angles.deg)
            snippet = f"rbt.move_j({angles}, speed={speed}, accel={accel})"
        elif method_name == "move_l":
            speed = max(0.01, min(1.0, ui_state.jog_speed / 100.0))
            accel = max(0.01, min(1.0, ui_state.jog_accel / 100.0))
            x, y, z = robot_state.x, robot_state.y, robot_state.z
            rx, ry, rz = robot_state.rx, robot_state.ry, robot_state.rz
            snippet = (
                f"rbt.move_l([{x:.3f}, {y:.3f}, {z:.3f}, "
                f"{rx:.3f}, {ry:.3f}, {rz:.3f}], speed={speed}, accel={accel})"
            )
        else:
            all_commands = discover_robot_commands()
            snippet = all_commands.get(method_name, {}).get(
                "snippet", f"rbt.{method_name}(...)"
            )

        val = textarea.value
        if val and not val.endswith("\n"):
            val += "\n"
        textarea.value = val + snippet + "\n"
        logger.info("Added Python snippet: %s", snippet)

    def sync_code_from_target(
        self,
        target_id: str,
        pose: list[float],
        *,
        move_type: str | None = None,
        joint_angles_deg: list[float] | None = None,
    ) -> None:
        """Update the program code with the new pose for a specific target.

        Uses CM6 StateField position tracking to find the target line.
        Positions are tracked through edits, so this works even after
        the user inserts/deletes lines.

        Note: pose is in scene units (meters for position, degrees for rotation).
        Code uses user units (mm for position, degrees for rotation).

        If move_type is provided (e.g. "joints"), the move command is also
        converted (move_l→move_j or vice versa). joint_angles_deg must be
        provided when converting to move_j.
        """
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return

        # Check if codemirror is properly initialized
        try:
            current_value = textarea.value
            if current_value is None:
                logger.debug("Sync skipped: codemirror value is None")
                return
        except (AttributeError, RuntimeError) as e:
            logger.debug("Sync skipped: codemirror not ready - %s", e)
            return

        line_number = textarea.line_anchor_positions.get(target_id)
        if line_number is None:
            logger.warning("Sync failed: Target %s not found", target_id)
            return

        content = current_value
        lines = content.splitlines()
        found_line_idx = line_number - 1  # Convert to 0-indexed

        if found_line_idx < 0 or found_line_idx >= len(lines):
            logger.warning("Sync failed: Line %d out of range", line_number)
            return

        line = lines[found_line_idx]

        # Replace the coordinate list in the line
        # Match a list of numbers: [...]
        match = re.search(r"(\[[\d\.\,\-\s]+\])", line)

        if match:
            # Convert move type if requested (e.g. move_l → move_j)
            if move_type == "joints" and joint_angles_deg is not None:
                new_values_str = (
                    "[" + ", ".join(f"{v:.3f}" for v in joint_angles_deg) + "]"
                )
                new_line = line[: match.start()] + new_values_str + line[match.end() :]
                new_line = new_line.replace("rbt.move_l(", "rbt.move_j(")
                new_line = new_line.replace("rbt.move_c(", "rbt.move_j(")
            else:
                # Convert from scene units (meters) to user units (mm) for position
                pose_mm = [
                    pose[0] * 1000.0 if len(pose) > 0 else 0.0,
                    pose[1] * 1000.0 if len(pose) > 1 else 0.0,
                    pose[2] * 1000.0 if len(pose) > 2 else 0.0,
                    pose[3] if len(pose) > 3 else 0.0,
                    pose[4] if len(pose) > 4 else 0.0,
                    pose[5] if len(pose) > 5 else 0.0,
                ]
                new_values_str = "[" + ", ".join(f"{v:.3f}" for v in pose_mm) + "]"
                new_line = line[: match.start()] + new_values_str + line[match.end() :]

            lines[found_line_idx] = new_line
            textarea.value = "\n".join(lines)
            logger.info(
                "Synced code for target %s at line %d: %s",
                target_id,
                line_number,
                new_values_str,
            )
        else:
            logger.warning(
                "Sync failed: Could not find coordinate list in line: %s", line
            )

    def delete_target_code(self, target_id: str) -> None:
        """Delete the code line corresponding to the target and re-simulate.

        Uses CM6 StateField position tracking to find the line.
        """
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return

        line_number = textarea.line_anchor_positions.get(target_id)
        if line_number is None:
            logger.warning("Target %s not found for deletion", target_id)
            return

        content = textarea.value or ""
        lines = content.splitlines()
        line_idx = line_number - 1

        if 0 <= line_idx < len(lines):
            del lines[line_idx]
            textarea.value = "\n".join(lines)
            logger.info("Deleted target %s from code (line %d)", target_id, line_number)
            # Re-simulation will trigger automatically via debounced on_change
        else:
            logger.warning("Target %s line %d out of range", target_id, line_number)

    def add_target_code(self, pose: list[float], move_type: str) -> int | None:
        """Add a move command to the editor.

        Generates clean code without any internal markers.
        The CM6 StateField will track the line position after the
        next simulation run produces targets.

        Args:
            pose: [x, y, z, rx, ry, rz] position and orientation
            move_type: Type of movement ("pose", "cartesian", "joints")

        Returns:
            1-indexed line number of the new line, or None on failure.
        """
        textarea = editor_tabs_state.active_textarea
        if not textarea:
            return None

        speed = max(0.01, min(1.0, ui_state.jog_speed / 100.0))
        accel = max(0.01, min(1.0, ui_state.jog_accel / 100.0))

        pose_str = "[" + ", ".join(f"{v:.3f}" for v in pose) + "]"

        if move_type == "joints":
            code_line = f"rbt.move_j({pose_str}, speed={speed}, accel={accel})"
        else:
            code_line = f"rbt.move_l({pose_str}, speed={speed}, accel={accel})"

        content = textarea.value or ""

        # Count lines before adding
        lines_before = len(content.splitlines()) if content else 0

        # Ensure content ends with newline
        if content and not content.endswith("\n"):
            content += "\n"

        # Append new code (will trigger debounced simulation)
        new_content = content + code_line + "\n"
        textarea.value = new_content

        # Flash the newly added line
        new_line_number = lines_before + 1
        decorations.flash_editor_lines([new_line_number])

        logger.info("Added target code at line %d: %s", new_line_number, code_line)
        return new_line_number

    def add_joint_target_code(self, joint_angles: list[float]) -> int | None:
        """Add joint target code to the editor.

        Args:
            joint_angles: [j1, j2, j3, j4, j5, j6] joint angles in degrees

        Returns:
            1-indexed line number of the new line, or None on failure.
        """
        return self.add_target_code(joint_angles, move_type="joints")

    def _build_command_menu(self) -> None:
        """Build command palette as a dropdown menu with nested submenus."""
        # Discover all commands dynamically
        all_commands = discover_robot_commands()

        # Group by category
        categories: dict[str, list[dict[str, Any]]] = {}
        for key, cmd in all_commands.items():
            cat = cmd["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append({"key": key, **cmd})

        # Build menu structure with nested submenus (following NiceGUI docs pattern)
        with ui.menu():
            for category_name, commands in sorted(categories.items()):
                # Category as submenu parent - must disable auto_close to keep open while navigating
                with ui.menu_item(category_name, auto_close=False).classes(
                    "text-sm font-medium"
                ):
                    # Arrow indicator on the right side
                    with ui.item_section().props("side"):
                        ui.icon("keyboard_arrow_right")
                    # Nested submenu with auto-close
                    with (
                        ui.menu()
                        .props('anchor="top end" self="top start" auto-close')
                        .classes("max-h-80 overflow-y-auto")
                    ):
                        for cmd in sorted(commands, key=lambda c: c["title"]):
                            # Command menu item
                            item = ui.menu_item(
                                cmd["title"],
                                on_click=lambda e, k=cmd["key"]: self._insert_command(
                                    k
                                ),
                            ).classes("text-sm")

                            # Add tooltip
                            with item:
                                tooltip_text = f"{cmd['signature']}"
                                if cmd["docstring"]:
                                    tooltip_text += f"\n\n{cmd['docstring']}"
                                ui.tooltip(tooltip_text).classes("text-xs").style(
                                    "max-width: 300px; white-space: pre-wrap;"
                                )

    def cleanup(self) -> None:
        """Per-page cleanup — remove listeners and cancel timers registered
        during ``build()``. Idempotent: safe to call from both
        ``_on_disconnect`` and ``_on_shutdown``."""
        if self._tab_switch_render_task is not None:
            self._tab_switch_render_task.cancel()
            self._tab_switch_render_task = None
        # Only playback owns a per-page simulation_state listener; remove it
        # first. The other cleanups are independent (decorations / log_panel
        # are no-ops, simulation/script_exec only cancel their own resources).
        self.playback.cleanup()
        decorations.cleanup()
        log_panel.cleanup()
        simulation.cleanup()
        script_exec.cleanup()

    # ---- Tab Management Methods ----

    def _new_tab(
        self, filename: str = "untitled.py", content: str | None = None
    ) -> EditorTab:
        """Create a new tab and switch to it."""
        tab = EditorTab(
            id=uuid.uuid4().hex[:8],
            filename=filename,
            file_path=None,
            content=content if content is not None else default_python_snippet(),
            saved_content=content if content is not None else default_python_snippet(),
            path_segments=[],
            targets=[],
            created_at=time.time(),
        )

        editor_tabs_state.add_tab(tab)
        self._create_tab_widget(tab)
        self._create_tab_panel(tab)
        self._switch_to_tab(tab.id)

        # Trigger simulation at tab creation (with default script optimization)
        if is_default_script(tab.content):
            # Default script ends at home position - skip simulation;
            # other tab list fields default to [] so no further reset needed.
            tab.final_joints_rad = list(get_home_joints_rad())
        elif tab.content.strip():
            simulation.schedule_debounced_simulation(tab_id=tab.id)

        return tab

    def _close_tab(self, tab: EditorTab) -> None:
        """Close a tab, prompting to save if dirty.

        Uses deferred execution via ui.timer to avoid modifying UI
        during NiceGUI's event listener iteration.
        """
        # The subprocess outlives the page (script_exec.cleanup doesn't kill
        # script_handle), so closing its launching tab would orphan the output:
        # _record_line silently drops every line once find_tab_by_id returns None.
        if simulation_state.script_running and script_exec.is_launching_tab(tab.id):
            ui.notify(
                "Cannot close the tab whose script is running. Stop the script first.",
                color="warning",
            )
            return

        def do_close():
            if tab.is_dirty:
                self._show_save_confirmation(tab)
            else:
                self._do_close_tab(tab)

        # Defer to avoid "dictionary changed size during iteration" in tests
        ui.timer(0, do_close, once=True)

    def _show_save_confirmation(self, tab: EditorTab) -> None:
        """Show save confirmation dialog for dirty tab."""
        dlg = ui.dialog().classes("save-dialog")

        def dont_save():
            dlg.close()
            self._do_close_tab(tab)

        with dlg, ui.card().classes("overlay-card w-80"):
            ui.label(f"Save changes to {tab.filename}?").classes(
                "text-lg font-medium mb-2"
            )
            ui.label("Your changes will be lost if you don't save.").classes(
                "text-sm text-gray-500 mb-4"
            )
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button(
                    "Don't Save",
                    on_click=dont_save,
                ).props("flat color=negative")
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button(
                    "Save", on_click=lambda: self._save_tab_and_close(tab, dlg)
                ).props("color=primary")
        dlg.open()

    def _do_close_tab(self, tab: EditorTab) -> None:
        """Actually close the tab and clean up UI."""
        tab_id = tab.id

        # Determine which tab to switch to BEFORE removing
        tabs = editor_tabs_state.tabs
        closed_idx = next((i for i, t in enumerate(tabs) if t.id == tab_id), -1)
        new_active_id = None

        if len(tabs) > 1:
            if closed_idx > 0:
                new_active_id = tabs[closed_idx - 1].id  # Previous tab
            else:
                new_active_id = tabs[1].id  # Next tab if closing first

        # Remove tab widget from tabs container
        if tab_id in self._tab_widgets:
            widgets = self._tab_widgets[tab_id]
            # Delete the tab widget element
            if "tab_element" in widgets and widgets["tab_element"]:
                widgets["tab_element"].delete()
            # Delete the panel element
            if "panel" in widgets and widgets["panel"]:
                widgets["panel"].delete()
            del self._tab_widgets[tab_id]
        editor_tabs_state.textareas_by_tab.pop(tab_id, None)

        # Remove from state
        editor_tabs_state.remove_tab(tab_id)

        # Create new tab if all tabs closed
        if not editor_tabs_state.tabs:
            self._new_tab()
        elif new_active_id:
            editor_tabs_state.active_tab_id = new_active_id
            self._switch_to_tab(new_active_id)

    def _switch_to_tab(self, tab_id: str) -> None:
        """Switch to a specific tab (blocked during recording/playback)."""

        # Block tab switching during recording or playback
        if recording_state.is_recording:
            ui.notify("Cannot switch tabs while recording", color="warning")
            # Reset UI to current active tab since the click already changed it visually
            if self.tabs_container and editor_tabs_state.active_tab_id:
                self.tabs_container.set_value(editor_tabs_state.active_tab_id)
            return
        if simulation_state.script_running and simulation_state.is_playing:
            ui.notify("Cannot switch tabs during script playback", color="warning")
            if self.tabs_container and editor_tabs_state.active_tab_id:
                self.tabs_container.set_value(editor_tabs_state.active_tab_id)
            return

        # Stop simulation playback on tab switch (non-blocking)
        self.playback.stop_playback()
        self.playback.invalidate_timeline()

        tab = editor_tabs_state.find_tab_by_id(tab_id)
        if not tab:
            return

        # Save current tab's simulation context. The log doesn't need saving
        # here — script_execution / simulation_engine append to the owning
        # tab's output_log incrementally during writes.
        current_tab = editor_tabs_state.get_active_tab()
        if current_tab and current_tab.id != tab_id:
            self._save_simulation_context(current_tab)

        # Update active tab
        editor_tabs_state.active_tab_id = tab_id
        simulation_state.active_cursor_line = 0

        # Update tab panels value
        if self.tab_panels_container:
            self.tab_panels_container.set_value(tab_id)

        # Update tabs container value
        if self.tabs_container:
            self.tabs_container.set_value(tab_id)

        # Load this tab's simulation context
        self._load_simulation_context(tab)

        # Swap log content: load new tab's log entries into shared log
        log_panel.clear()
        for entry in tab.output_log:
            log_panel.push(entry)

        widgets = self._tab_widgets.get(tab_id, {})
        editor_tabs_state.active_textarea = widgets.get("textarea")
        editor_tabs_state.active_filename_input = widgets.get("filename_input")

    def _save_simulation_context(self, tab: EditorTab) -> None:
        """Save current simulation state to tab."""
        tab.path_segments = list(simulation_state.path_segments)
        tab.targets = list(simulation_state.targets)
        tab.tool_actions = list(simulation_state.tool_actions)
        tab.tool_selections = list(simulation_state.tool_selections)

    def _load_simulation_context(self, tab: EditorTab) -> None:
        """Load tab's simulation state into global simulation_state.

        Updates simulation_state synchronously so _save_simulation_context on
        the *next* tab switch reads consistent data. Only defers the expensive
        path invalidation and re-render to an async task.
        """
        # Cancel previous tab-switch render if still pending
        if self._tab_switch_render_task is not None:
            self._tab_switch_render_task.cancel()

        # Update global state synchronously to avoid races with _save
        simulation_state.path_segments = list(tab.path_segments)
        simulation_state.targets = list(tab.targets)
        simulation_state.tool_actions = list(tab.tool_actions)
        simulation_state.tool_selections = list(tab.tool_selections)
        simulation_state.current_step_index = 0
        simulation_state.total_steps = len(tab.path_segments)

        # Capture client context before creating task (asyncio.create_task
        # doesn't propagate NiceGUI context)
        try:
            client = context.client
        except RuntimeError:
            client = None

        async def _apply():
            try:
                await asyncio.sleep(0)  # yield so UI updates first
                if ui_state.urdf_scene:
                    ui_state.urdf_scene.invalidate_paths()
                if client is not None:
                    with client:
                        self.playback.update_scrub_segments()
                simulation_state.notify_changed()
            finally:
                if self._tab_switch_render_task is task:
                    self._tab_switch_render_task = None

        task = asyncio.create_task(_apply())
        self._tab_switch_render_task = task

    def _create_tab_widget(self, tab: EditorTab) -> ui.tab | None:
        """Create a single tab widget with filename input, save button, close button."""
        if not self.tabs_container:
            return None

        with self.tabs_container:
            tab_element = ui.tab(name=tab.id, label="").classes("editor-tab")
            tab_element.mark(f"editor-tab-{tab.id}")
            with tab_element:
                with ui.row().classes("items-center gap-1 no-wrap"):
                    # Dirty indicator (orange dot)
                    dirty_dot = (
                        ui.icon("fiber_manual_record", size="xs")
                        .classes("text-amber-500")
                        .style("font-size: 8px;")
                    )
                    # Bind visibility to dirty state - update on content change
                    dirty_dot.bind_visibility_from(tab, "is_dirty", lambda d: d)

                    # Filename input (compact)
                    filename_input = (
                        ui.input(value=tab.filename)
                        .props("dense borderless")
                        .classes("text-sm w-28")
                        .on("change", lambda e, t=tab: setattr(t, "filename", e.args))
                    )
                    filename_input.mark(f"editor-tab-filename-{tab.id}")

                    # Close button
                    close_btn = (
                        ui.button(
                            icon="close", on_click=lambda _e, t=tab: self._close_tab(t)
                        )
                        .props("flat round dense size=xs")
                        .classes("text-white")
                        .tooltip("Close tab")
                    )
                    close_btn.mark(f"editor-tab-close-{tab.id}")

            # Store tab element reference
            if tab.id not in self._tab_widgets:
                self._tab_widgets[tab.id] = {}
            self._tab_widgets[tab.id]["tab_element"] = tab_element
            self._tab_widgets[tab.id]["filename_input"] = filename_input
            self._tab_widgets[tab.id]["dirty_dot"] = dirty_dot

        return tab_element

    def _create_tab_panel(self, tab: EditorTab) -> ui.tab_panel | None:
        """Create content panel for a tab (CodeMirror only, log is shared)."""
        if not self.tab_panels_container:
            return None

        with self.tab_panels_container:
            panel = (
                ui.tab_panel(name=tab.id)
                .classes("editor-tab-panel")
                .style("padding: 0; width: 100%; height: 100%;")
            )
            with panel:
                # Generate completions
                completions = generate_completions_from_commands()

                # CodeMirror editor - fill entire panel (uses its own internal scrolling)
                textarea = (
                    ui.codemirror(
                        value=tab.content,
                        language="Python",
                        line_wrapping=True,
                        on_change=lambda e, t=tab: self._on_tab_content_change(
                            t, e.value
                        ),
                        on_selection_change=lambda e, t=tab: self._on_cursor_line(t, e),
                        completions=completions,
                        keybindings={
                            "Mod-s": lambda _e, t=tab: self._save_tab(t),
                        },
                        line_tooltip_html=True,
                    )
                    .classes("w-full h-full")
                    .style("min-height: 100%;")
                )

                # Initialize theme
                try:
                    mode = get_theme()
                    effective = "light" if mode == "light" else "dark"
                    textarea.theme = "basicLight" if effective == "light" else "oneDark"
                except (KeyError, ValueError):
                    textarea.theme = "oneDark"

            # Store references
            self._tab_widgets[tab.id]["panel"] = panel
            self._tab_widgets[tab.id]["textarea"] = textarea
            editor_tabs_state.textareas_by_tab[tab.id] = textarea

        return panel

    def _on_cursor_line(self, tab: EditorTab, e) -> None:
        """Handle cursor line change from CodeMirror."""
        if tab.id != editor_tabs_state.active_tab_id:
            return
        simulation_state.active_cursor_line = e.line
        if ui_state.urdf_scene and simulation_state.paths_visible:
            ui_state.urdf_scene.update_cursor_line_highlight()

    def _on_tab_content_change(self, tab: EditorTab, new_value: str) -> None:
        """Handle content change for a tab."""
        tab.content = new_value

        self._update_dirty_dot(tab)

        # Only run simulation for active tab
        if tab.id == editor_tabs_state.active_tab_id:
            simulation.schedule_debounced_simulation()

    def build(self, close_callback: Callable | None = None) -> None:
        """Build the program editor content with multi-tab support."""
        # Store NiceGUI client reference for JS execution from background tasks
        try:
            self._ui_client = ui.context.client
        except RuntimeError:
            pass  # No client context during build (shouldn't happen)
        decorations.set_ui_client(self._ui_client)
        simulation.set_ui_client(self._ui_client)
        playback.set_ui_client(self._ui_client)
        script_exec.set_ui_client(self._ui_client)

        # Periodic check: re-run path preview when robot position changes
        ui.timer(1.0, simulation.check_position_changed)

        # Main editor container
        with (
            ui.column()
            .classes("w-full h-full gap-0")
            .style("height: 100%; min-height: 0; padding-bottom: 16px;")
        ):
            # ---- Header Row (title + tabs + cmd + X) ----
            with (
                ui.row()
                .classes("w-full items-center gap-2 px-2")
                .style("height: 42px;")
            ):
                # Title
                ui.label("Program").classes("text-lg font-medium whitespace-nowrap")

                # Tabs area (horizontal scroll)
                with (
                    ui.scroll_area()
                    .classes("flex-1 no-wrap items-start editor-tabs-scroll")
                    .style("height: 42px;")
                ):
                    with ui.row().classes("items-center gap-0 flex-nowrap"):
                        # Tabs container
                        self.tabs_container = (
                            ui.tabs()
                            .props("dense inline-label")
                            .classes("editor-tabs")
                            .on(
                                "update:model-value",
                                lambda e: self._switch_to_tab(e.args),
                            )
                        )

                        # New tab button (last element in scrollable area)
                        new_tab_btn = (
                            ui.button(icon="add", on_click=lambda: self._new_tab())
                            .props("flat dense color=white")
                            .classes("ml-2")
                            .tooltip("New Tab")
                        )
                        new_tab_btn.mark("editor-new-tab-btn")

                # Open button
                open_btn = (
                    ui.button(icon="folder", on_click=self._show_open_dialog)
                    .props("flat dense color=white")
                    .tooltip("Open")
                )
                open_btn.mark("editor-open-btn")

                # Save button
                save_btn = (
                    ui.button(icon="save", on_click=self._show_save_dialog)
                    .props("flat dense color=white")
                    .tooltip("Save")
                )
                save_btn.mark("editor-save-btn")

                # Command palette menu
                commands_btn = (
                    ui.button(icon="library_add")
                    .props("flat dense color=white")
                    .tooltip("Insert Command")
                )
                commands_btn.mark("editor-commands-btn")
                with commands_btn:
                    self._build_command_menu()

                # X close button
                if close_callback:
                    ui.button(icon="close", on_click=close_callback).props(
                        "flat round dense color=white"
                    )

            # ---- Splitter: Editor (before) | Playbar (separator) | Log (after) ----
            # horizontal=True means vertical stacking (column layout)
            with (
                ui.splitter(
                    horizontal=True,
                    value=LOG_COLLAPSED_VALUE,
                    limits=(50, LOG_COLLAPSED_VALUE),
                    on_change=log_panel.on_splitter_change,
                )
                .classes("w-full flex-1 editor-splitter")
                .style("overflow: hidden;") as splitter
            ):
                log_panel.attach_splitter(splitter)

                # ---- Tab Panels Area (CodeMirror) in splitter.before ----
                with splitter.before:
                    self.tab_panels_container = (
                        ui.tab_panels(self.tabs_container)
                        .classes("w-full h-full")
                        .props("animated")
                        .style("padding: 0; overflow: hidden;")
                    )

                # ---- Playbar in splitter.separator (acts as handle) ----
                with splitter.separator:
                    self.playback.build_bar()

                # ---- Shared Log Area in splitter.after ----
                with splitter.after:
                    log_panel.build_log_area()

        # Set up playback timers and listeners
        self.playback.setup_timers()

        # Restore tabs from existing state (page refresh) or create initial tab
        if editor_tabs_state.tabs:
            # Clear stale UI references from previous page load
            self._tab_widgets.clear()

            # Rebuild UI for each existing tab
            for tab in editor_tabs_state.tabs:
                self._create_tab_widget(tab)
                self._create_tab_panel(tab)

            # Activate the previously active tab (or first tab if none active).
            # Set references directly instead of calling _switch_to_tab() which
            # blocks during recording/playback — those guards are for user-initiated
            # switches, not page-load restoration.
            active_id = editor_tabs_state.active_tab_id or editor_tabs_state.tabs[0].id
            editor_tabs_state.active_tab_id = active_id
            if self.tab_panels_container:
                self.tab_panels_container.set_value(active_id)
            if self.tabs_container:
                self.tabs_container.set_value(active_id)
            widgets = self._tab_widgets.get(active_id, {})
            editor_tabs_state.active_textarea = widgets.get("textarea")
            editor_tabs_state.active_filename_input = widgets.get("filename_input")

            # Restore simulation state from active tab
            active_tab = editor_tabs_state.get_active_tab()
            if active_tab:
                self._load_simulation_context(active_tab)
        else:
            # No existing tabs - create initial tab
            self._new_tab()
