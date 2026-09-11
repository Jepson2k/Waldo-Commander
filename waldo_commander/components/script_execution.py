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
import time
import uuid
from pathlib import Path
from dataclasses import asdict

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
from waldo_commander.services.run_records import RunRecord
from waldo_commander.services.supervised_restart import (
    RestartState,
    discover_entries,
    fresh_state,
    source_digest,
)
from waldo_commander.services.programs import is_any_program_running
import waldoctl
from waldoctl import LogEntry

from waldo_commander.state import playback_coordination, simulation_state, ui_state

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
        # ``Program.log`` so switching tabs preserves the originating tab's log.
        self._script_tab_id: str | None = None
        # Exit code of the most recently finished run (None while running or
        # before any run) — lets execution.wait_active report success/crash.
        self.last_exit_code: int | None = None
        self._execution_control_lock = asyncio.Lock()
        self.record_runs = False
        self.active_record: RunRecord | None = None
        self.last_record: Path | None = None
        self.last_run_source_digest: str | None = None
        self.last_outcome: str | None = None
        self._launch_task: asyncio.Task | None = None
        self._cancel_launch_from_stop = False
        self._stop_unconfirmed = False

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
            and is_any_program_running()
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

    def _launching_program(self):
        """The ``Program`` whose content launched the current script, or None."""
        if not self._script_tab_id:
            return None
        return waldoctl.commander.programs.get(self._script_tab_id)

    def _record_line(
        self, line: str, ui_client: Client | None = None, stream: str = "stdout"
    ) -> None:
        """Append a log line to the launching tab's log; push to the
        visible log_panel only when the launching tab is currently active.

        ``ui_client`` is the page client captured at ``start()`` so the
        push runs in the right NiceGUI context when called from a script
        subprocess callback. Tests may pass ``None`` — ``log_panel.push``
        no-ops when the log widget hasn't been built.
        """
        tab = self._launching_program()
        if tab is not None:
            tab.log.append(LogEntry(timestamp=time.time(), stream=stream, text=line))
        if tab is not None and tab.id == waldoctl.commander.programs.active_id:
            if ui_client is not None:
                with ui_client:
                    log_panel.push(line)
            else:
                log_panel.push(line)

    # ---- Public lifecycle ----

    async def toggle(self) -> None:
        """Toggle start/stop based on current state."""
        if is_any_program_running():
            await self.stop()
        else:
            await self.start()

    async def start(
        self,
        paused: bool = False,
        *,
        restart_entry: str | None = None,
        restart_reference: RestartState | None = None,
        reviewed_source_digest: str | None = None,
    ) -> bool:
        """Start the current editor content as a Python subprocess.

        With ``paused``, the stepping control file is left in its initial
        paused state: the subprocess executes exactly the first steppable
        command, then blocks awaiting step/play signals — a single-step
        launch from idle.
        """
        if is_any_program_running():
            ui.notify("Script already running", color="warning")
            return False

        self._launch_task = asyncio.current_task()
        self._cancel_launch_from_stop = False
        restart_state = None
        try:
            filename_input = ui_state.active_filename_input
            filename = (
                filename_input.value.strip() if filename_input else ""
            ) or "program.py"
            if not filename.endswith(".py"):
                filename += ".py"

            textarea = ui_state.active_textarea
            content = textarea.value if textarea else ""
            if restart_entry is not None:
                if (
                    restart_reference is None
                    or source_digest(content) != reviewed_source_digest
                ):
                    raise ValueError(
                        "Review this program and the physical setup before restarting"
                    )
                if restart_entry not in {
                    entry.name for entry in discover_entries(content)
                }:
                    raise ValueError("The selected restart entry is no longer declared")
                # Refuse before anything of the interrupted run is wiped: its
                # log, outcome and record are what the operator reviews next.
                restart_state = await fresh_state(waldoctl.commander.client)
                restart_state.require_ready()
                restart_state.require_same_setup(restart_reference)
            self.last_exit_code = None
            assert self._program_dir is not None, "program_dir not set"
            runtime_dir = self._program_dir / ".runtime"
            script_path = runtime_dir / filename
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(content, encoding="utf-8")

            if filename_input:
                filename_input.value = filename

            # Remember the launching tab so output is appended to its log
            # even after the user switches tabs while the script runs.
            launching_tab = waldoctl.commander.programs.active
            self._script_tab_id = launching_tab.id if launching_tab else None
            if launching_tab is not None:
                launching_tab.log.clear()
                launching_tab.execution.is_running = True
            log_panel.clear()

            script_config = create_default_config(str(script_path), str(REPO_ROOT))
            from waldo_commander.project import find_project

            project = find_project(launching_tab.file_path if launching_tab else None)
            script_config["env"]["WALDO_PROJECT_ROOT"] = str(project) if project else ""
            script_config["env"]["WALDO_PROGRAM_ORIGIN"] = (
                str(Path(launching_tab.file_path).resolve())
                if project and launching_tab and launching_tab.file_path
                else ""
            )
            if project:
                script_config["cwd"] = str(project)
                script_config["env"]["WALDO_SETUP_DIR"] = str(project / "setups")
            script_config["env"]["WALDO_RECORD_VALUES"] = "0"
            script_config["env"]["WALDO_RESTART_ENTRY"] = restart_entry or ""
            self.last_run_source_digest = source_digest(content)
            self.last_outcome = "running"
            if self.record_runs:
                try:
                    self.active_record = RunRecord(
                        content, ui_state.active_robot.backend_package
                    )
                    self.last_record = self.active_record.path
                    script_config["env"]["WALDO_RECORD_VALUES"] = "1"
                    self.active_record.start_status(waldoctl.commander.client)
                    await self.active_record.capture_context(waldoctl.commander.client)
                except OSError as error:
                    ui.notify(f"Run recording unavailable: {error}", color="warning")

            ui_client = self._ui_client or context.client

            def on_stdout(line: str) -> None:
                self._record_line(line, ui_client)

            def on_stderr(line: str) -> None:
                self._record_line(f"[ERR] {line}", ui_client, stream="stderr")

            self._step_session_id = uuid.uuid4().hex[:8]
            self._step_controller = GUIStepController(self._step_session_id)
            self._step_controller.initialize()

            if launching_tab is not None:
                launching_tab.execution.is_running = True
            if restart_entry is not None and self.active_record:
                assert restart_state is not None
                self.active_record.append(
                    {
                        "event": "restart_selected",
                        "entry": restart_entry,
                        "snapshot": asdict(restart_state),
                    }
                )
            if "execution.speed" in waldoctl.commander.client.skill_capabilities:
                if await waldoctl.commander.client.resume(timeout=3.0) <= 0:
                    raise TimeoutError("Controller resume was not confirmed")

            self.script_handle = await run_script(
                script_config, on_stdout, on_stderr, session_id=self._step_session_id
            )

            # Subprocess is live — flip execution.is_running on the launching
            # program and emit one notification so playback's listener sees
            # the script-start edge and reacts.
            if launching_tab is not None:
                launching_tab.execution.is_running = True
                launching_tab.dry_run.playback.executing_step_index = -1
                launching_tab.dry_run.playback.executing_step_at_end = False
                launching_tab.dry_run.playback.is_playing = not paused
                launching_tab.dry_run.playback.notify_step_changed()
            if not paused:
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
            return True

        except asyncio.CancelledError:
            if self.script_handle is not None:
                await stop_script(self.script_handle)
                self.script_handle = None
            if self._cancel_launch_from_stop:
                return False
            self._finish_record("interrupted")
            self._reset_state()
            raise
        except Exception as e:
            ui.notify(f"Failed to start script: {e}", color="negative")
            if restart_entry is not None and isinstance(e, ValueError):
                logger.warning("Restart refused: %s", e)
            else:
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
            self._finish_record("start_failed")
            self._reset_state()
            return False
        finally:
            self._launch_task = None

    async def stop(self) -> None:
        """Terminate the program, then cancel its native motion and queue."""
        if not is_any_program_running() or (
            self.script_handle is None
            and self._launch_task is None
            and not self._stop_unconfirmed
        ):
            ui.notify("No script running", color="warning")
            return

        handle = self.script_handle
        try:
            if self._launch_task is not None:
                self._cancel_launch_from_stop = True
                self._launch_task.cancel()
                await asyncio.gather(self._launch_task, return_exceptions=True)
            handle = self.script_handle
            self.script_handle = None
            self._cancel_watcher()
            if handle:
                await stop_script(handle)
            # The process can no longer enqueue commands. Keep the run marked
            # active until its previously queued motion has been cancelled.
            await self._confirm_controller_stop()
            try:
                self._consume_script_events(self._ui_client or context.client)
            except Exception:
                logger.warning(
                    "Terminal events unavailable after controller stop", exc_info=True
                )
                if self.active_record:
                    self.active_record.append(
                        {"event": "events_lost", "reason": "terminal read failed"}
                    )
            ui.notify("Script stopped", color="warning")
            logger.info("Script stopped by user")
        except Exception as error:
            self.script_handle = handle
            self._report_unconfirmed_stop(error)
            raise
        else:
            self._finish_record("stopped")
            self._reset_state()
        finally:
            self._cancel_launch_from_stop = False

    async def _confirm_controller_stop(self) -> None:
        async with asyncio.timeout(3.0):
            if await waldoctl.commander.client.stop() <= 0:
                raise TimeoutError("Controller did not acknowledge Stop")

    def _report_unconfirmed_stop(self, error: Exception) -> None:
        self._stop_unconfirmed = True
        ui.notify(
            "Controller stop is unconfirmed. Retry Program Stop before starting another run.",
            color="negative",
        )
        logger.error("Controller stop is unconfirmed: %s", error)
        if self.active_record:
            self.active_record.append({"event": "stop_unconfirmed"})

    # ---- Public step-controller actions (called from playback UI handlers) ----

    async def signal_play(self) -> None:
        """Resume a paused script subprocess (no-op if no script is stepping)."""
        async with self._execution_control_lock:
            if self._step_controller:
                if "execution.speed" in waldoctl.commander.client.skill_capabilities:
                    if await waldoctl.commander.client.resume(timeout=3.0) <= 0:
                        raise TimeoutError("Controller resume was not confirmed")
                self._step_controller.signal_play()
                if self.active_record:
                    self.active_record.append({"event": "resume"})

    async def signal_pause(self) -> None:
        """Pause a running script subprocess (no-op if no script is stepping)."""
        async with self._execution_control_lock:
            if self._step_controller:
                self._step_controller.signal_pause()
                if "execution.speed" in waldoctl.commander.client.skill_capabilities:
                    if await waldoctl.commander.client.pause(timeout=3.0) <= 0:
                        raise TimeoutError(
                            "Script held, but controller pause was not confirmed"
                        )
                if self.active_record:
                    self.active_record.append({"event": "pause"})

    async def signal_step(self) -> None:
        """Step a paused script forward by one command (no-op if not stepping)."""
        async with self._execution_control_lock:
            if self._step_controller:
                if "execution.speed" in waldoctl.commander.client.skill_capabilities:
                    if await waldoctl.commander.client.resume(timeout=3.0) <= 0:
                        raise TimeoutError("Controller resume was not confirmed")
                self._step_controller.signal_step()
                if self.active_record:
                    self.active_record.append({"event": "step"})

    # ---- Internals ----

    def _consume_script_events(self, ui_client: Client) -> None:
        if self._step_controller is None:
            return
        events = self._step_controller.poll_events()
        for event in events:
            if self.active_record:
                self.active_record.append(event)
            event_type = event.get("event")
            method = event.get("method", "")
            step = event.get("step", 0)
            running_tab = self._launching_program()
            if isinstance(event_type, str) and event_type.startswith("skill_"):
                phase = event_type.removeprefix("skill_")
                message = event.get("message", "")
                fraction = event.get("fraction")
                progress = f" ({fraction:.0%})" if fraction is not None else ""
                detail = f": {message}" if message else ""
                self._record_line(f"{method} {phase}{progress}{detail}", ui_client)
            elif event_type == "start":
                with ui_client:
                    if running_tab is not None:
                        running_tab.dry_run.playback.executing_step_index = step
                        running_tab.dry_run.playback.executing_step_at_end = False
                        running_tab.dry_run.playback.current_step = step
                        running_tab.dry_run.playback.notify_step_changed()
                    simulation_state.notify_step_changed()
            elif event_type == "complete":
                with ui_client:
                    if running_tab is not None:
                        running_tab.dry_run.playback.executing_step_index = step
                        running_tab.dry_run.playback.executing_step_at_end = True
                        running_tab.dry_run.playback.current_step = step
                        running_tab.dry_run.playback.notify_step_changed()
                    simulation_state.notify_step_changed()
                logger.debug("Script event: %s completed (step %d)", method, step)

    async def _watch_script_events(self, ui_client: Client) -> None:
        """Poll for script events and publish step transitions to simulation_state."""
        watcher_crashed = False
        try:
            while is_any_program_running() and self._step_controller:
                self._consume_script_events(ui_client)
                async with self._execution_control_lock:
                    if (
                        self._step_controller
                        and self._step_controller.waiting_for_step()
                    ):
                        self._step_controller.signal_pause()
                        if (
                            "execution.speed"
                            in waldoctl.commander.client.skill_capabilities
                        ):
                            if await waldoctl.commander.client.pause(timeout=3.0) <= 0:
                                raise TimeoutError(
                                    "Step completed, but controller pause was not confirmed"
                                )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.debug("Event watcher task cancelled")
            raise
        except Exception as e:
            logger.error("Error in event watcher: %s", e)
            watcher_crashed = True
        finally:
            if watcher_crashed and is_any_program_running():
                with ui_client:
                    # Losing managed control cannot leave an untracked process
                    # feeding the motion queue. stop() reaps it before clearing state.
                    try:
                        await self.stop()
                    except Exception:
                        logger.exception(
                            "Controller stop after event watcher failure was unconfirmed"
                        )

    async def _monitor_script_completion(
        self,
        handle: ScriptProcessHandle,
        filename: str,
        ui_client: Client,
    ) -> None:
        """Keep failed runs tracked until their controller queue is cancelled."""
        rc = None
        monitor_failed = False
        try:
            rc = await handle["proc"].wait()
            for task in (handle["stdout_task"], handle["stderr_task"]):
                with contextlib.suppress(Exception):
                    await task
            if self.script_handle is not handle:
                return
            self.last_exit_code = rc
            self._consume_script_events(ui_client)
        except Exception as error:
            monitor_failed = True
            logger.error("Error monitoring script process: %s", error)
        if self.script_handle is not handle:
            return
        with ui_client:
            self._cancel_watcher()
            if rc != 0 or monitor_failed:
                try:
                    await self._confirm_controller_stop()
                except Exception as error:
                    if self.script_handle is handle:
                        self._report_unconfirmed_stop(error)
                    return
            if self.script_handle is handle:
                self._finish_record(
                    "completed" if rc == 0 and not monitor_failed else "failed", rc
                )
                self._reset_state()
                logger.info("Script %s finished with code %s", filename, rc)

    def _finish_record(self, outcome: str, exit_code: int | None = None) -> None:
        if self.last_outcome == "running":
            self.last_outcome = outcome
        if self.active_record is not None:
            self.active_record.finish(outcome, exit_code)
            self.active_record = None

    def _reset_state(self) -> None:
        """Reset all script-related state after a script finishes or errors."""
        self.script_handle = None
        self._stop_unconfirmed = False
        self._finish_record("interrupted")
        running_tab = self._launching_program()
        if running_tab is not None:
            running_tab.execution.is_running = False
            running_tab.dry_run.playback.is_playing = False
        self._script_tab_id = None
        playback_coordination.sim_pose_override = False
        simulation_state.notify_changed()
        self.cleanup_stepping()

    def _cancel_watcher(self) -> None:
        """Cancel the event watcher task without touching step IPC state.
        Used by per-page cleanup so the subprocess can keep stepping while
        no page is connected."""
        if (
            self._event_watcher_task
            and not self._event_watcher_task.done()
            and self._event_watcher_task is not asyncio.current_task()
        ):
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
