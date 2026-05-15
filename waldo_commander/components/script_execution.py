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

    ``__init__`` takes a program directory; callers reach the singleton via
    ``script_exec`` (constructed after EditorPanel chooses the program dir).
    """

    def __init__(self, program_dir: Path | None = None) -> None:
        self._program_dir: Path | None = program_dir
        self.script_handle: ScriptProcessHandle | None = None
        self._step_session_id: str | None = None
        self._step_controller: GUIStepController | None = None
        self._event_watcher_task: asyncio.Task | None = None

    def cleanup(self) -> None:
        """Per-page cleanup — cancel the event watcher and step controller
        bound to this page. Does NOT touch ``script_handle``: the user's
        subprocess outlives the page (``_on_shutdown`` reaps it)."""
        self._cleanup_stepping()

    def reset_for_test(self) -> None:
        """Restore field defaults by replaying ``__init__`` on this instance.
        Nulls ``_program_dir``; ``EditorPanel.__init__`` re-sets it on next
        page build via ``set_program_dir()``."""
        self.cleanup()
        type(self).__init__(self)

    def set_program_dir(self, program_dir: Path) -> None:
        self._program_dir = program_dir

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
            runtime_dir.mkdir(parents=True, exist_ok=True)
            script_path = runtime_dir / filename
            script_path.write_text(content, encoding="utf-8")

            if filename_input:
                filename_input.value = filename

            log_panel.clear()

            script_config = create_default_config(str(script_path), str(REPO_ROOT))

            ui_client = context.client

            def on_stdout(line: str) -> None:
                with ui_client:
                    log_panel.push(line)

            def on_stderr(line: str) -> None:
                with ui_client:
                    log_panel.push(f"[ERR] {line}")

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
            self.script_handle = None
            if leaked_handle is not None:
                try:
                    await stop_script(leaked_handle)
                except Exception as stop_err:
                    logger.error(
                        "Failed to stop leaked subprocess after start error: %s",
                        stop_err,
                    )
            simulation_state.script_running = False
            self._step_session_id = None
            if self._step_controller:
                self._step_controller.cleanup()
                self._step_controller = None
            simulation_state.is_playing = False
            simulation_state.notify_changed()

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
            self._cleanup_stepping()
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
                            simulation_state.notify_changed()
                    elif event_type == "complete":
                        with ui_client:
                            simulation_state.executing_step_index = step
                            simulation_state.executing_step_at_end = True
                            simulation_state.current_step_index = step
                            simulation_state.notify_changed()
                        logger.debug(
                            "Script event: %s completed (step %d)", method, step
                        )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.debug("Event watcher task cancelled")
        except Exception as e:
            logger.error("Error in event watcher: %s", e)

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
        self._cleanup_stepping()

    def _cleanup_stepping(self) -> None:
        """Clean up stepping controller and event watcher."""
        if self._event_watcher_task and not self._event_watcher_task.done():
            self._event_watcher_task.cancel()
        self._event_watcher_task = None

        if self._step_controller:
            self._step_controller.cleanup()
            self._step_controller = None
        self._step_session_id = None


script_exec: ScriptExecutionController = ScriptExecutionController()
