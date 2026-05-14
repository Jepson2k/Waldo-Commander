"""Script execution controller: subprocess lifecycle + GUI step controller.

Owns the python-subprocess script handle, the GUI step controller, and the
event-watcher / completion-monitor tasks. Sub-controller call sites in the
editor delegate to this singleton instead of holding the state themselves.

Cross-controller side effects are kept narrow: the singleton mutates
``simulation_state.script_running`` / ``is_playing`` and notifies, then
external listeners (decorations, log_panel) react. Playback transitions
(on_script_start / on_script_step_start / on_script_step_complete /
on_script_stop) reach into ``ui_state.editor_panel.playback`` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path

from nicegui import Client, context, ui

from waldo_commander.components.editor_decorations import decorations
from waldo_commander.components.log_panel import log_panel
from waldo_commander.services.script_runner import (
    ScriptProcessHandle,
    create_default_config,
    run_script,
    stop_script,
)
from waldo_commander.services.stepping_client import GUIStepController
from waldo_commander.state import editor_tabs_state, simulation_state, ui_state

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
        ep = getattr(ui_state, "_editor_panel", None)
        playback = ep.playback if ep is not None else None
        if playback:
            playback.stop_playback()

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
            from waldo_commander.constants import REPO_ROOT

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
            simulation_state.script_running = True

            simulation_state.is_playing = True
            self._step_controller.signal_play()
            if playback:
                playback.on_script_start()

            log_panel.expand()

            ui_client = context.client
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
            simulation_state.script_running = False
            self._step_session_id = None
            if self._step_controller:
                self._step_controller.cleanup()
                self._step_controller = None
            simulation_state.is_playing = False
            if playback:
                playback.update_play_button()

    async def stop(self) -> None:
        """Stop the running script process."""
        if not simulation_state.script_running or not self.script_handle:
            ui.notify("No script running", color="warning")
            return

        ep = getattr(ui_state, "_editor_panel", None)
        playback = ep.playback if ep is not None else None
        try:
            handle = self.script_handle
            self.script_handle = None
            simulation_state.script_running = False
            simulation_state.is_playing = False
            if playback:
                playback.update_play_button()
            self._cleanup_stepping()
            if handle:
                await stop_script(handle)
            ui.notify("Script stopped", color="warning")
            logger.info("Script stopped by user")
        except Exception as e:
            ui.notify(f"Error stopping script: {e}", color="negative")
            logger.error("Error stopping script: %s", e)

    # ---- Internals ----

    async def _watch_script_events(self, ui_client: Client) -> None:
        """Poll for script events and update visualization."""
        try:
            while simulation_state.script_running and self._step_controller:
                events = self._step_controller.poll_events()
                ep = getattr(ui_state, "_editor_panel", None)
                playback = ep.playback if ep is not None else None
                for event in events:
                    event_type = event.get("event")
                    method = event.get("method", "")
                    step = event.get("step", 0)
                    if event_type == "start" and playback:
                        playback.on_script_step_start(step, ui_client)
                    elif event_type == "complete" and playback:
                        playback.on_script_step_complete(step, ui_client)
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
                    self._reset_state(handle, ui_client)
                    logger.info("Script %s finished with code %s", filename, rc)
        except Exception as e:
            logger.error("Error monitoring script process: %s", e)
            with ui_client:
                if self.script_handle is handle:
                    self._reset_state(handle, ui_client)

    def _reset_state(self, handle: ScriptProcessHandle, ui_client: Client) -> None:
        """Reset all script-related state after a script finishes or errors."""
        self.script_handle = None
        simulation_state.script_running = False
        simulation_state.is_playing = False
        simulation_state.sim_pose_override = False
        ep = getattr(ui_state, "_editor_panel", None)
        playback = ep.playback if ep is not None else None
        if playback:
            playback.on_script_stop(ui_client)
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

        decorations.clear_executing_line_highlight()


script_exec: ScriptExecutionController = ScriptExecutionController()
