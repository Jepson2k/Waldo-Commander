"""Script execution controller: subprocess lifecycle + GUI step controller.

Owns the python-subprocess script handle, the GUI step controller, and the
event-watcher / completion-monitor tasks. Sub-controller call sites in the
editor delegate to this singleton instead of holding the state themselves.

Communication with other controllers is one-way through ``simulation_state``:
every state transition that should redraw the playback bar mutates the
relevant fields and calls ``simulation_state.notify_changed()`` so registered
listeners fire. ``bindable_dataclass`` field assignment alone does not fire
``ChangeNotifierMixin._change_listeners`` (the descriptor never chains to
``super().__setattr__``), so the explicit ``notify_changed()`` call is
required after each mutation. This module never imports ``playback``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path

from nicegui import Client, context, ui

from waldo_commander.components.log_panel import log_panel
from waldo_commander.constants import REPO_ROOT
from waldo_commander.services.script_runner import (
    ScriptProcessHandle,
    create_default_config,
    run_script,
    stop_script,
)
from waldo_commander.services.stepping_client import GUIStepController
from waldo_commander.state import editor_tabs_state, simulation_state

logger = logging.getLogger(__name__)


class ScriptExecutionController:
    """Owns the script subprocess lifecycle and GUI step controller.

    Reached via the module-level ``script_exec`` singleton. The program
    directory is supplied later via ``set_program_dir()`` once
    ``EditorPanel.__init__`` has chosen it.
    """

    def __init__(self) -> None:
        self._program_dir: Path | None = None
        self.script_handle: ScriptProcessHandle | None = None
        self._step_session_id: str | None = None
        self._step_controller: GUIStepController | None = None
        self._event_watcher_task: asyncio.Task | None = None
        self._ui_client: Client | None = None
        # Tab whose content was launched. Output is appended to that tab's
        # output_log so switching tabs preserves the originating tab's log.
        self._script_tab_id: str | None = None

    def cleanup(self) -> None:
        """Per-page cleanup — cancel the event watcher bound to this page.
        Does NOT touch ``script_handle`` OR the stepping IPC: the
        subprocess outlives the page (``_on_shutdown`` reaps it), and the
        step controller / IPC files are preserved so the subprocess can
        keep stepping. The next page's ``set_ui_client`` rebinds the
        watcher to the new client."""
        self._cancel_watcher()

    def reset_for_test(self) -> None:
        """Restore field defaults by replaying ``__init__`` on this instance.
        Calls the FULL stepping teardown (deleting IPC files) — unlike
        per-page ``cleanup()`` which preserves IPC across reloads, tests
        want a fully clean slate between runs."""
        self.cleanup_stepping()
        type(self).__init__(self)

    def set_program_dir(self, program_dir: Path) -> None:
        self._program_dir = program_dir

    def set_ui_client(self, client: Client | None) -> None:
        self._ui_client = client
        # If a stepping subprocess outlived the previous page, its IPC
        # files and step controller were preserved by ``cleanup()`` (see
        # docstring). Rebind the event watcher to the new client so step
        # progress resumes on this page.
        if (
            client is not None
            and simulation_state.script_running
            and self._step_controller is not None
            and (self._event_watcher_task is None or self._event_watcher_task.done())
        ):
            self._event_watcher_task = asyncio.create_task(
                self._watch_script_events(client)
            )

    def is_launching_tab(self, tab_id: str) -> bool:
        """True if this tab launched the currently running script."""
        return self._script_tab_id == tab_id

    @property
    def launching_tab_id(self) -> str | None:
        """ID of the tab whose content launched the current script (or None
        if no script is running)."""
        return self._script_tab_id

    def _record_line(self, line: str, ui_client: Client | None = None) -> None:
        """Append a log line to the launching tab's output_log; push to the
        visible log_panel only when the launching tab is currently active.

        ``ui_client`` is the page client captured at ``start()`` so the
        push runs in the right NiceGUI context when called from a script
        subprocess callback. Tests may pass ``None`` — ``log_panel.push``
        no-ops when the log widget hasn't been built.
        """
        tab = (
            editor_tabs_state.find_tab_by_id(self._script_tab_id)
            if self._script_tab_id
            else None
        )
        if tab is not None:
            tab.output_log.append(line)
        if tab is not None and tab.id == editor_tabs_state.active_tab_id:
            if ui_client is not None:
                with ui_client:
                    log_panel.push(line)
            else:
                log_panel.push(line)

    # ---- Public lifecycle ----

    async def toggle(self) -> None:
        """Toggle start/stop based on current state."""
        if simulation_state.script_running:
            await self.stop()
        else:
            await self.start()

    async def start(self) -> None:
        """Start the current editor content as a Python subprocess."""
        if simulation_state.script_running:
            ui.notify("Script already running", color="warning")
            return

        try:
            filename_input = editor_tabs_state.active_filename_input
            filename = (
                filename_input.value.strip() if filename_input else ""
            ) or "program.py"
            if not filename.endswith(".py"):
                filename += ".py"

            textarea = editor_tabs_state.active_textarea
            content = textarea.value if textarea else ""
            assert self._program_dir is not None, "program_dir not set"
            runtime_dir = self._program_dir / ".runtime"
            script_path = runtime_dir / filename
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(content, encoding="utf-8")

            if filename_input:
                filename_input.value = filename

            # Remember the launching tab so output is appended to its log
            # even after the user switches tabs while the script runs.
            launching_tab = editor_tabs_state.get_active_tab()
            self._script_tab_id = launching_tab.id if launching_tab else None
            if launching_tab is not None:
                launching_tab.output_log.clear()
            log_panel.clear()

            script_config = create_default_config(str(script_path), str(REPO_ROOT))

            ui_client = self._ui_client or context.client

            def on_stdout(line: str) -> None:
                self._record_line(line, ui_client)

            def on_stderr(line: str) -> None:
                self._record_line(f"[ERR] {line}", ui_client)

            self._step_session_id = uuid.uuid4().hex[:8]
            self._step_controller = GUIStepController(self._step_session_id)
            self._step_controller.initialize()

            self.script_handle = await run_script(
                script_config, on_stdout, on_stderr, session_id=self._step_session_id
            )

            # Subprocess is live — flip script_running and emit one notification
            # so playback's listener sees the script-start edge and reacts.
            simulation_state.script_running = True
            simulation_state.is_playing = True
            simulation_state.executing_step_index = -1
            simulation_state.executing_step_at_end = False
            self._step_controller.signal_play()
            simulation_state.notify_changed()

            log_panel.expand()

            self._event_watcher_task = asyncio.create_task(
                self._watch_script_events(ui_client)
            )

            handle = self.script_handle
            asyncio.create_task(
                self._monitor_script_completion(handle, filename, ui_client)
            )

            ui.notify(f"Started script: {filename}", color="positive")
            logger.info("Started script: %s", filename)

        except Exception as e:
            ui.notify(f"Failed to start script: {e}", color="negative")
            logger.error("Failed to start script: %s", e)
            # Reap the subprocess if run_script succeeded before the exception
            # — otherwise the process group outlives the failed start.
            leaked_handle = self.script_handle
            if leaked_handle is not None:
                try:
                    await stop_script(leaked_handle)
                except Exception as stop_err:
                    logger.error(
                        "Failed to stop leaked subprocess after start error: %s",
                        stop_err,
                    )
            self._reset_state()

    async def stop(self) -> None:
        """Stop the running script process."""
        if not simulation_state.script_running or not self.script_handle:
            ui.notify("No script running", color="warning")
            return

        try:
            handle = self.script_handle
            self.script_handle = None
            simulation_state.script_running = False
            simulation_state.is_playing = False
            simulation_state.notify_changed()
            self.cleanup_stepping()
            if handle:
                await stop_script(handle)
            ui.notify("Script stopped", color="warning")
            logger.info("Script stopped by user")
        except Exception as e:
            ui.notify(f"Error stopping script: {e}", color="negative")
            logger.error("Error stopping script: %s", e)

    # ---- Public step-controller actions (called from playback UI handlers) ----

    def signal_play(self) -> None:
        """Resume a paused script subprocess (no-op if no script is stepping)."""
        if self._step_controller:
            self._step_controller.signal_play()

    def signal_pause(self) -> None:
        """Pause a running script subprocess (no-op if no script is stepping)."""
        if self._step_controller:
            self._step_controller.signal_pause()

    def signal_step(self) -> None:
        """Step a paused script forward by one command (no-op if not stepping)."""
        if self._step_controller:
            self._step_controller.signal_step()

    # ---- Internals ----

    async def _watch_script_events(self, ui_client: Client) -> None:
        """Poll for script events and publish step transitions to simulation_state."""
        watcher_crashed = False
        try:
            while simulation_state.script_running and self._step_controller:
                events = self._step_controller.poll_events()
                for event in events:
                    event_type = event.get("event")
                    method = event.get("method", "")
                    step = event.get("step", 0)
                    if event_type == "start":
                        with ui_client:
                            simulation_state.executing_step_index = step
                            simulation_state.executing_step_at_end = False
                            simulation_state.current_step_index = step
                            simulation_state.notify_step_changed()
                    elif event_type == "complete":
                        with ui_client:
                            simulation_state.executing_step_index = step
                            simulation_state.executing_step_at_end = True
                            simulation_state.current_step_index = step
                            simulation_state.notify_step_changed()
                        logger.debug(
                            "Script event: %s completed (step %d)", method, step
                        )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.debug("Event watcher task cancelled")
            raise
        except Exception as e:
            logger.error("Error in event watcher: %s", e)
            watcher_crashed = True
        finally:
            # If the watcher died unexpectedly while the script is still flagged
            # as running, fire a stop edge so playback unstalls instead of waiting
            # for the subprocess-completion monitor to notice.
            if watcher_crashed and simulation_state.script_running:
                with ui_client:
                    simulation_state.script_running = False
                    simulation_state.is_playing = False
                    simulation_state.notify_changed()

    async def _monitor_script_completion(
        self,
        handle: ScriptProcessHandle,
        filename: str,
        ui_client: Client,
    ) -> None:
        """Monitor script subprocess completion and reset state when it finishes."""
        try:
            rc = await handle["proc"].wait()
            for t in (handle["stdout_task"], handle["stderr_task"]):
                with contextlib.suppress(Exception):
                    await t
            if self.script_handle is handle:
                with ui_client:
                    self._reset_state()
                    logger.info("Script %s finished with code %s", filename, rc)
        except Exception as e:
            logger.error("Error monitoring script process: %s", e)
            with ui_client:
                if self.script_handle is handle:
                    self._reset_state()

    def _reset_state(self) -> None:
        """Reset all script-related state after a script finishes or errors."""
        self.script_handle = None
        simulation_state.script_running = False
        simulation_state.is_playing = False
        simulation_state.sim_pose_override = False
        simulation_state.notify_changed()
        self.cleanup_stepping()

    def _cancel_watcher(self) -> None:
        """Cancel the event watcher task without touching step IPC state.
        Used by per-page cleanup so the subprocess can keep stepping while
        no page is connected."""
        if self._event_watcher_task and not self._event_watcher_task.done():
            self._event_watcher_task.cancel()
        self._event_watcher_task = None

    def cleanup_stepping(self) -> None:
        """Full stepping teardown — cancel watcher, deinit step controller,
        delete IPC files. Used on script completion or stop."""
        self._cancel_watcher()
        if self._step_controller:
            self._step_controller.cleanup()
            self._step_controller = None
        self._step_session_id = None


script_exec: ScriptExecutionController = ScriptExecutionController()
