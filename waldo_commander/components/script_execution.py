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
import atexit
import contextlib
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from dataclasses import asdict

from nicegui import Client, background_tasks, context, ui

from waldo_commander.components.log_panel import log_panel
from waldo_commander.constants import REPO_ROOT
from waldo_commander.services.script_runner import (
    ScriptProcessHandle,
    create_default_config,
    run_script,
    stop_script,
)
from waldo_commander.services.control_lease import control_lease
from waldo_commander.services.motion_guard import (
    PROGRAM,
    MotionBusy,
    Reservation,
    motion_guard,
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
from waldoctl import CommandNote, LogEntry
from waldoctl.commands import command_table

from waldo_commander.state import playback_coordination, simulation_state, ui_state

logger = logging.getLogger(__name__)

_COMMANDS = command_table()
# How long a launch lets already-queued commands drain before refusing.
_QUEUE_DRAIN_S = 2.0


def _lessee() -> tuple[str, str] | None:
    holder = control_lease.holder()
    return None if holder is None else (holder.channel, holder.id)


def program_command(notes: Sequence[CommandNote], ordinal: int) -> int:
    """The program index of the *ordinal*-th queued command a running script
    issued.

    The dry run notes every command in program order, and the stepping
    wrapper counts only the ones the command table says mint a queue index,
    so the ordinal walks the notes that do. Without notes — no plan for the
    program — the ordinal is the best index there is.
    """
    seen = -1
    for index, note in enumerate(notes):
        spec = _COMMANDS.get(note.method)
        if spec is not None and spec.mints_index:
            seen += 1
            if seen == ordinal:
                return index
    return ordinal


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
        # Held from start() until the run is over, so nothing else drives
        # the robot while a program does.
        self._reservation: Reservation | None = None
        # Bumped by every start(), so a Stop resets only the run it stopped.
        self._run = 0
        self._restart_run = False

    @property
    def active(self) -> bool:
        """A program is launching, running, or waiting on a confirmed Stop:
        it has to be stopped before the robot changes mode."""
        return (
            self._launch_task is not None
            or self._stop_unconfirmed
            or is_any_program_running()
        )

    @property
    def program_dir(self) -> Path | None:
        """The program library a launched program imports its neighbours from."""
        return self._program_dir

    def cleanup(self) -> None:
        """Per-page cleanup. The run outlives the page: its subprocess,
        stepping link and event watcher are untouched (``_on_shutdown`` reaps
        them), and only UI output stops until the next ``set_ui_client``."""
        self._ui_client = None

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
        if self.active:
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
        # A restart reads the controller's state before anything is marked
        # running; the launch is reserved here so nothing else starts, and
        # Stop can cancel it, in the meantime.
        if self._launch_task is not None:
            ui.notify("Script already starting", color="warning")
            return False
        if self._stop_unconfirmed:
            ui.notify(
                "Controller stop is unconfirmed. Retry Program Stop first.",
                color="negative",
            )
            return False
        try:
            self._reservation = motion_guard.reserve(PROGRAM)
        except MotionBusy as e:
            ui.notify(str(e), color="warning")
            return False

        self._launch_task = asyncio.current_task()
        self._cancel_launch_from_stop = False
        self._run += 1
        self._restart_run = restart_entry is not None
        lessee = _lessee()
        # Stop is offered from here: the launch may read the controller
        # for seconds before anything counts as running.
        simulation_state.notify_changed()
        resumed = False
        try:
            filename_input = ui_state.active_filename_input
            filename = (
                filename_input.value.strip() if filename_input else ""
            ) or "program.py"
            if not filename.endswith(".py"):
                filename += ".py"

            textarea = ui_state.active_textarea
            content = textarea.value if textarea else ""
            # Taken with the content: a tab switch while the controller is
            # read must not attach this run to another program.
            launching_tab = waldoctl.commander.programs.active
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
                    raise ValueError(
                        "The selected function can no longer be started on its own"
                    )
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
            self._script_tab_id = launching_tab.id if launching_tab else None
            if launching_tab is not None:
                launching_tab.log.clear()
                launching_tab.execution.is_running = True
                # Stop has to show for the whole launch, not from the spawn.
                simulation_state.notify_changed()
            log_panel.clear()

            script_config = create_default_config(str(script_path), str(REPO_ROOT))
            script_config["env"]["WALDO_RECORD_VALUES"] = "0"
            script_config["env"]["WALDO_RESTART_ENTRY"] = restart_entry or ""
            script_config["env"]["WALDO_PROGRAM_DIR"] = str(self._program_dir)
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
                self._record_line(line, self._ui_client or ui_client)

            def on_stderr(line: str) -> None:
                self._record_line(
                    f"[ERR] {line}", self._ui_client or ui_client, stream="stderr"
                )

            self._step_session_id = uuid.uuid4().hex[:8]
            self._step_controller = GUIStepController(self._step_session_id)
            self._step_controller.initialize()

            # Resuming releases whatever a native pause holds, which must be
            # nothing but this run's own motion.
            client = waldoctl.commander.client
            # The page's own preview fits its tool through the queue, and a
            # Run pressed right after an edit lands while that is in flight.
            drain_by = time.monotonic() + _QUEUE_DRAIN_S
            while (queued := await client.queue()) and time.monotonic() < drain_by:
                await asyncio.sleep(0.05)
            if queued is None:
                raise ConnectionError("Controller queue is unavailable")
            if queued or not await client.wait_status(
                lambda status: status.queued_duration < 1e-3, timeout=0.5
            ):
                raise RuntimeError("the controller still has queued motion")
            if launching_tab is not None:
                launching_tab.execution.is_running = True
            if restart_entry is not None:
                assert restart_reference is not None
                # Read again: the review above is seconds old by now.
                restart_state = await fresh_state(waldoctl.commander.client)
                restart_state.require_ready()
                restart_state.require_same_setup(restart_reference)
                if self.active_record:
                    self.active_record.append(
                        {
                            "event": "restart_selected",
                            # The same key the bootstrap's entry_started uses,
                            # so the export keeps the entry name instead of
                            # dropping it as an unknown field.
                            "method": restart_entry,
                            "snapshot": asdict(restart_state),
                        }
                    )
            if _lessee() != lessee:
                raise PermissionError(
                    "Control changed hands while the program was starting"
                )
            resumed = True
            if await client.resume(timeout=3.0) <= 0:
                raise TimeoutError("Controller resume was not confirmed")

            self.script_handle = await run_script(
                script_config, on_stdout, on_stderr, session_id=self._step_session_id
            )

            # Subprocess is live — flip execution.is_running on the launching
            # program and emit one notification so playback's listener sees
            # the script-start edge and reacts.
            if launching_tab is not None:
                launching_tab.execution.is_running = True
                launching_tab.dry_run.playback.executing_command = -1
                launching_tab.dry_run.playback.executing_step_at_end = False
                launching_tab.dry_run.playback.is_playing = not paused
                launching_tab.dry_run.playback.notify_step_changed()
            if not paused:
                self._step_controller.signal_play()
            simulation_state.notify_changed()

            log_panel.expand()

            self._event_watcher_task = asyncio.create_task(
                self._watch_script_events(ui_client, self._run)
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
            if resumed:
                self.script_handle = None
                try:
                    await self._confirm_controller_stop()
                except Exception as stop_error:
                    self._report_unconfirmed_stop(stop_error)
                    return False
            self._finish_record("start_failed")
            self._reset_state()
            return False
        finally:
            self._launch_task = None
            simulation_state.notify_changed()

    async def stop(self) -> None:
        """Terminate the program, then cancel its native motion and queue.

        Allowed while a previous Stop is unconfirmed, which it retries; it
        resets only the run it stopped."""
        if not (
            self._launch_task is not None
            or self._stop_unconfirmed
            or (is_any_program_running() and self.script_handle is not None)
        ):
            ui.notify("No script running", color="warning")
            return

        motion_guard.note_stop("program stop")
        run = self._run
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
            if self._run == run:
                self.script_handle = handle
                self._report_unconfirmed_stop(error)
            raise
        else:
            if self._run == run:
                self._finish_record("stopped")
                self._reset_state()
                self._refresh_tcp()
        finally:
            self._cancel_launch_from_stop = False

    async def _confirm_controller_stop(self) -> None:
        if not await motion_guard.stop_robot(waldoctl.commander.client, "program stop"):
            raise TimeoutError("Controller did not acknowledge Stop")

    def _report_unconfirmed_stop(self, error: Exception) -> None:
        self._stop_unconfirmed = True
        simulation_state.notify_changed()
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
            controller = self._step_controller
            if controller is None:
                return
            if await waldoctl.commander.client.resume(timeout=3.0) <= 0:
                raise TimeoutError("Controller resume was not confirmed")
            # A run that ended during the resume must not release its successor.
            if controller is self._step_controller:
                controller.signal_play()
                if self.active_record:
                    self.active_record.append({"event": "resume"})

    async def signal_pause(self) -> None:
        """Pause a running script subprocess (no-op if no script is stepping)."""
        async with self._execution_control_lock:
            if self._step_controller:
                # The subprocess is held first, so no further commands are
                # issued while the controller's pause is in flight. The caller
                # is told about an unconfirmed pause only after the hold is
                # reported, or the play button keeps showing a program that is
                # in fact held.
                self._step_controller.signal_pause()
                if await waldoctl.commander.client.pause(timeout=3.0) <= 0:
                    raise TimeoutError(
                        "Script held, but controller pause was not confirmed"
                    )
                if self.active_record:
                    self.active_record.append({"event": "pause"})

    async def signal_step(self) -> None:
        """Step a paused script forward by one command (no-op if not stepping)."""
        async with self._execution_control_lock:
            controller = self._step_controller
            if controller is None:
                return
            if await waldoctl.commander.client.resume(timeout=3.0) <= 0:
                raise TimeoutError("Controller resume was not confirmed")
            if controller is self._step_controller:
                controller.signal_step()
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
            ordinal = int(event.get("command", -1))
            running_tab = self._launching_program()
            command = (
                program_command(running_tab.dry_run.commands, ordinal)
                if running_tab is not None
                else ordinal
            )
            if isinstance(event_type, str) and event_type.startswith("skill_"):
                phase = event_type.removeprefix("skill_")
                message = event.get("message", "")
                fraction = event.get("fraction")
                progress = f" ({fraction:.0%})" if fraction is not None else ""
                detail = f": {message}" if message else ""
                self._record_line(f"{method} {phase}{progress}{detail}", ui_client)
            # A restarted entry's ordinals are not the full program's plan,
            # so they would highlight the wrong lines.
            elif event_type in ("start", "complete") and not self._restart_run:
                with ui_client:
                    if running_tab is not None:
                        pb = running_tab.dry_run.playback
                        pb.executing_command = command
                        pb.executing_step_at_end = event_type == "complete"
                        pb.notify_step_changed()
                    simulation_state.notify_step_changed()
                if event_type == "complete":
                    logger.debug(
                        "Script event: %s completed (command %d)", method, command
                    )

    async def _watch_script_events(self, ui_client: Client, run: int) -> None:
        """Poll for script events and publish step transitions to simulation_state.

        Owned by the run, not the page: it records events and holds step
        mode while no page is connected, showing them on whichever page is.
        """
        watcher_crashed = False
        try:
            while is_any_program_running() and self._step_controller:
                self._consume_script_events(self._ui_client or ui_client)
                async with self._execution_control_lock:
                    if (
                        self._step_controller
                        and self._step_controller.waiting_for_step()
                    ):
                        self._step_controller.signal_pause()
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
            if watcher_crashed and is_any_program_running() and self._run == run:
                with self._ui_client or ui_client:
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
            # The program's last events may still be on the link's reader
            # threads; they are queued once its links reach their end.
            controller = self._step_controller
            if controller is not None and not await asyncio.to_thread(
                controller.join_links, 1.0
            ):
                logger.warning("Stepping link still open after the program exited")
                if self.active_record:
                    self.active_record.append(
                        {"event": "events_lost", "reason": "link open after exit"}
                    )
            self._consume_script_events(self._ui_client or ui_client)
        except Exception as error:
            monitor_failed = True
            logger.error("Error monitoring script process: %s", error)
        if self.script_handle is not handle:
            return
        with self._ui_client or ui_client:
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
                self._refresh_tcp()

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
        self._release_reservation()
        simulation_state.notify_changed()
        self.cleanup_stepping()

    @staticmethod
    def _refresh_tcp() -> None:
        # A run can change the fitted tool's TCP transform, which no status
        # field carries.
        from waldo_commander.components.settings import refresh_applied_tcp

        background_tasks.create(
            refresh_applied_tcp(waldoctl.commander.client), name="tcp-refresh"
        )

    def _release_reservation(self) -> None:
        if self._reservation is not None:
            self._reservation.release()
            self._reservation = None

    def _cancel_watcher(self) -> None:
        """Cancel the event watcher task without touching step IPC state."""
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

    def kill_orphaned_script(self) -> None:
        """Synchronously kill any running script subprocess.

        Registered with atexit as a last-resort cleanup.
        """
        try:
            if self.script_handle:
                proc = self.script_handle.get("proc")
                if proc and proc.returncode is None:
                    logger.info("Killing orphaned script process (PID: %s)", proc.pid)
                    try:
                        # On Unix, try to kill the entire process group
                        if sys.platform != "win32" and proc.pid:
                            try:
                                pgid = os.getpgid(proc.pid)
                                os.killpg(pgid, signal.SIGKILL)
                                logger.debug("Killed process group %s", pgid)
                            except (ProcessLookupError, OSError):
                                proc.kill()
                        else:
                            proc.kill()
                    except ProcessLookupError:
                        pass
                    except Exception as e:
                        logger.debug("Error killing script process: %s", e)
        except Exception as e:
            logger.debug("Error in script cleanup: %s", e)


script_exec: ScriptExecutionController = ScriptExecutionController()
atexit.register(script_exec.kill_orphaned_script)
