"""
Stepping client wrapper for GUI-controlled script execution.

Provides a wrapper around RobotClient that:
1. Emits events for each motion command (start/complete)
2. Optionally pauses after each command for stepping through scripts
3. Communicates with GUI via file-based IPC

Cross-platform compatible (Windows, macOS, Linux).
"""

import asyncio
import inspect
import json
import os
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from collections.abc import Awaitable, Coroutine
from typing import TypeVar, cast

from waldoctl.client import RobotClient

from .path_preview_client import MOTION_METHODS
from .completion_budget import CompletionBudget, current_budget
from .command_records import recorded_method

R = TypeVar("R")

# Methods that trigger wait_command and stepping: all motion methods plus
# non-motion commands that queue on the controller.
STEPPABLE_METHODS = frozenset(MOTION_METHODS) | frozenset(
    {"home", "tool_action", "delay"}
)

_EXECUTION_CONTROLS = frozenset(
    {"pause", "resume", "stop", "estop", "execution_speed", "set_execution_speed"}
)


def _nonblocking(method: Callable, kwargs: dict) -> tuple[dict, float | None]:
    parameters = inspect.signature(method).parameters
    timeout = kwargs.get("timeout")
    if timeout is None and "timeout" in parameters:
        default = parameters["timeout"].default
        if isinstance(default, (int, float)):
            timeout = default
    if "wait" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    ):
        kwargs = {**kwargs, "wait": False}
    return kwargs, timeout


def _atomic_write(path: Path, data: dict) -> None:
    # Event payloads can contain opted-in recording values. mkstemp makes
    # them private from creation, before either process sees the new file.
    descriptor, name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_control(control_file: Path) -> dict:
    """Read control file, return defaults if not exists or parse error."""
    try:
        return json.loads(control_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"paused": True, "step_signal": 0, "step_acked": 0}


def _is_blended(kwargs: dict) -> bool:
    """Check if motion kwargs specify a blend radius."""
    return float(kwargs.get("r", 0)) > 0


class StepIO:
    """
    File-based IPC for stepping control between script subprocess and GUI.

    Uses two files:
    - Control file (GUI -> Script): Contains paused flag and step signals
    - Event file (Script -> GUI): Contains command start/complete events
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._temp_dir = Path(tempfile.gettempdir())
        self._control_file = self._temp_dir / f".parol_control_{session_id}"
        self._event_file = self._temp_dir / f".parol_events_{session_id}"
        self._ack_file = self._temp_dir / f".parol_ack_{session_id}"
        self._step_count = 0
        self._last_step_acked = 0
        self.capture_values = os.environ.get("WALDO_RECORD_VALUES") == "1"
        self._event_lock = threading.Lock()
        self._events = self._read_events()

    def active_time(self) -> float:
        control = _read_control(self._control_file)
        now = time.monotonic()
        paused = control.get("pause_elapsed", 0.0)
        started = control.get("pause_started")
        if started is not None:
            paused += max(0.0, now - started)
        return now - paused

    def hold_requested(self) -> bool:
        return _read_control(self._control_file).get("pause_started") is not None

    def wait_until_resumed(self, check: Callable[[], None]) -> None:
        while self.hold_requested():
            check()
            time.sleep(0.05)

    async def wait_until_resumed_async(
        self, check: Callable[[], Awaitable[None]]
    ) -> None:
        while self.hold_requested():
            await check()
            await asyncio.sleep(0.05)

    @classmethod
    def from_env(cls) -> "StepIO | None":
        """
        Create StepIO from environment variables.
        Returns None if WALDO_STEP_SESSION is not set.
        """
        session_id = os.environ.get("WALDO_STEP_SESSION")
        if not session_id:
            return None
        return cls(session_id)

    def _read_events(self) -> list[dict]:
        """Read events from event file."""
        try:
            data = json.loads(self._event_file.read_text())
            return data.get("events", [])
        except (json.JSONDecodeError, OSError):
            return []

    def emit_event(self, event_type: str, method: str, **extra: Any) -> None:
        """
        Emit an event to the event file.

        Args:
            event_type: "start" or "complete"
            method: Name of the motion method
            **extra: Additional event data
        """
        with self._event_lock:
            events = self._events
            sequence = events[-1].get("sequence", len(events)) + 1 if events else 1
            events.append(
                {
                    "event": event_type,
                    "method": method,
                    "step": self._step_count,
                    "ts": time.time(),
                    "mono_ns": time.monotonic_ns(),
                    "active_s": self.active_time(),
                    "sequence": sequence,
                    **extra,
                }
            )
            # Keep unpublished events for the next flush if a reader or virus
            # scanner temporarily prevents replacing the file on Windows.
            self._events = events[-256:]
            try:
                _atomic_write(self._event_file, {"events": self._events})
            except OSError:
                # Diagnostics cannot change whether a command executes.
                logging.getLogger(__name__).exception("Could not record command event")

    def check_should_pause(self) -> bool:
        """Check if the script should pause (paused flag is true)."""
        control = _read_control(self._control_file)
        return control.get("paused", True)

    def _step_released(self) -> bool:
        """One poll of the control file. True when the script may proceed:
        play mode, a granted step, or the control file is gone (the session
        is no longer GUI-controlled, so blocking would hang the script)."""
        if not self._control_file.exists():
            return True
        control = _read_control(self._control_file)
        if not control.get("paused", True):
            return True
        step_signal = control.get("step_signal", 0)
        if step_signal > _read_control(self._ack_file).get("step_acked", 0):
            self._ack_step(control, step_signal)
            return True
        return False

    def wait_for_step_or_play(
        self, poll_interval: float = 0.05, *, check: Callable[[], None] | None = None
    ) -> None:
        """Block until the GUI signals step or play. No timeout: paused
        means paused until the operator says otherwise."""
        self._set_waiting(True)
        try:
            while not self._step_released():
                if check is not None:
                    check()
                time.sleep(poll_interval)
        finally:
            self._set_waiting(False)

    async def wait_for_step_or_play_async(
        self,
        poll_interval: float = 0.05,
        *,
        check: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Async twin of ``wait_for_step_or_play`` for the async wrapper."""
        self._set_waiting(True)
        try:
            while not self._step_released():
                if check is not None:
                    await check()
                await asyncio.sleep(poll_interval)
        finally:
            self._set_waiting(False)

    def _ack_step(self, control: dict, step_signal: int) -> None:
        """Only the GUI writes control; a child acknowledgement cannot undo Pause."""
        self._last_step_acked = step_signal
        self._set_waiting(False)

    def _set_waiting(self, waiting: bool) -> None:
        if not self._control_file.exists():
            return
        _atomic_write(
            self._ack_file, {"step_acked": self._last_step_acked, "waiting": waiting}
        )

    def increment_step_count(self) -> None:
        """Increment the internal step counter."""
        self._step_count += 1


_STEPPABLE_TOOL_METHODS = frozenset({"set_position", "open", "close", "calibrate"})


class _SteppingToolProxy:
    """Proxy that wraps a sync tool's action methods with stepping behavior."""

    def __init__(self, sync_tool: Any, owner: "SteppingClientWrapper") -> None:
        self._tool = sync_tool
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._tool, name)
        if not callable(attr) or name not in _STEPPABLE_TOOL_METHODS:
            return attr

        return self._owner._wrap_motion_method(
            "tool_action", attr, record_name=f"tool.{name}"
        )


class SteppingClientWrapper:
    """
    Wrapper around RobotClient that adds stepping behavior.

    - Intercepts motion methods
    - Emits start/complete events for GUI visualization
    - Calls wait_command() after each motion command
    - Optionally pauses for stepping based on control file
    - Blended commands (r > 0) are grouped as a single step in play mode;
      while stepping, each group member executes as an exact stop, one per
      step grant, with events still emitted at group granularity
    """

    def __init__(self, wrapped_client: Any, step_io: StepIO) -> None:
        """
        Initialize the wrapper.

        Args:
            wrapped_client: The RobotClient instance to wrap
            step_io: StepIO instance for IPC
        """
        self._wrapped = wrapped_client
        self._step_io = step_io
        self._in_blend = False
        self._last_blend_index: int = -1
        self._blend_waits: list[tuple[int, CompletionBudget]] = []

    def run_skill(self, invoke: Callable[[RobotClient], Coroutine[Any, Any, R]]) -> R:
        """Keep native motion stepping when a sync program calls an async skill."""
        self._flush_blend()

        async def execute(client: RobotClient) -> R:
            wrapped = AsyncSteppingClientWrapper(client, self._step_io)
            result = await invoke(cast(RobotClient, wrapped))
            await wrapped.finalize()
            return result

        return self._wrapped.run_skill(execute)

    def _wait_completed(self, index: int) -> None:
        try:
            if not self.wait_command(index, timeout=None):
                raise TimeoutError(f"Command {index} completion was not confirmed")
        except Exception:
            if self._wrapped.stop() <= 0:
                raise RuntimeError(
                    "Command failed and controller stop was not confirmed"
                )
            raise

    def _check_health(self) -> None:
        if self._wrapped.status() is None:
            raise ConnectionError("Controller unavailable during managed pause")
        if self._wrapped.wait_status(lambda status: not status.enabled, timeout=0.01):
            raise RuntimeError("Controller was disabled during managed pause")
        error = self._wrapped.error()
        if error is not None:
            if isinstance(error, BaseException):
                raise error
            raise RuntimeError(f"Controller fault during managed pause: {error}")

    def wait_command(self, command_index: int, timeout: float | None = 10.0) -> bool:
        try:
            result = self._wait_command_active(command_index, timeout)
        except BaseException as error:
            if self._step_io.capture_values:
                self._step_io.emit_event(
                    "command_wait_failed",
                    "wait_command",
                    index=command_index,
                    error_type=type(error).__name__,
                    message=str(error)[:512],
                )
            raise
        if self._step_io.capture_values:
            self._step_io.emit_event(
                "command_completed" if result else "command_unconfirmed",
                "wait_command",
                index=command_index,
            )
        return result

    def _wait_command_active(
        self, command_index: int, timeout: float | None = 10.0
    ) -> bool:
        budget = current_budget.get() or CompletionBudget(timeout)
        budget.bind(self._wrapped, self._step_io.active_time)
        while budget.remaining > 0:
            if self._wrapped.wait_command(
                command_index, timeout=min(0.1, budget.remaining)
            ):
                budget.confirmed_index = command_index
                return True
            self._check_health()
        return False

    def finalize(self) -> None:
        """Barrier for a pending blend group: wait it out, emit completion,
        clear. No pause gate — callers add one where stepping applies."""
        if not self._in_blend:
            return
        try:
            for index, budget in self._blend_waits:
                token = current_budget.set(budget)
                try:
                    self._wait_completed(index)
                finally:
                    current_budget.reset(token)
        finally:
            self._in_blend = False
            self._last_blend_index = -1
            self._blend_waits.clear()
        self._step_io.emit_event("complete", "blend_group")
        self._step_io.increment_step_count()

    def _flush_blend(self) -> None:
        """Flush any pending blend group, emit events, and pause if stepping."""
        if not self._in_blend:
            return
        self.finalize()
        if self._step_io.check_should_pause():
            self._step_io.wait_for_step_or_play(check=self._check_health)

    @property
    def tool(self):
        """Return the sync tool with stepping behavior on action methods."""
        self._flush_blend()
        return _SteppingToolProxy(self._wrapped.tool, self)

    def __enter__(self) -> "SteppingClientWrapper":
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args: Any) -> bool | None:
        if args[0] is None:
            self.finalize()
        else:
            self._in_blend = False
            self._last_blend_index = -1
            self._blend_waits.clear()
        return self._wrapped.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        """
        Delegate attribute access to wrapped client.
        Intercept motion methods to add stepping behavior.
        """
        attr = getattr(self._wrapped, name)

        if name in STEPPABLE_METHODS and callable(attr):
            return self._wrap_motion_method(name, attr)

        if name not in _EXECUTION_CONTROLS:
            self._flush_blend()
        return (
            recorded_method(self._step_io, name, attr)
            if callable(attr) and not name.startswith("_")
            else attr
        )

    def _wrap_motion_method(
        self, name: str, method: Callable, *, record_name: str | None = None
    ) -> Callable:
        """Create a wrapper function for a motion method."""
        method = recorded_method(self._step_io, record_name or name, method)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            kwargs, timeout = _nonblocking(method, kwargs)
            if name == "tool_action" and timeout is None:
                timeout = 10.0
            budget = current_budget.get() or CompletionBudget(timeout)
            budget.bind(self._wrapped, self._step_io.active_time)
            token = current_budget.set(budget)
            try:
                self._step_io.wait_until_resumed(self._check_health)
                return execute(*args, **kwargs)
            finally:
                current_budget.reset(token)

        def execute(*args: Any, **kwargs: Any) -> Any:
            is_blended = _is_blended(kwargs)

            if is_blended and self._step_io.check_should_pause():
                # Stepping: run this group member as an exact stop so one
                # press moves exactly one command (industrial step-mode
                # semantics). Events stay grouped — the preview timeline
                # renders the blend as a single segment, so per-member
                # events would desync the executing-step highlight.
                if not self._in_blend:
                    self._step_io.emit_event("start", name, blend=True)
                    self._in_blend = True
                result = method(*args, **{**kwargs, "r": 0.0})
                if isinstance(result, int) and result >= 0:
                    self._wait_completed(result)
                if self._step_io.check_should_pause():
                    self._step_io.wait_for_step_or_play(check=self._check_health)
                return result

            if is_blended:
                # Blended command — emit start event on first blend command,
                # then execute without waiting or stepping
                if not self._in_blend:
                    self._step_io.emit_event("start", name, blend=True)
                    self._in_blend = True
                result = method(*args, **kwargs)
                if isinstance(result, int) and result >= 0:
                    self._last_blend_index = result
                    budget = current_budget.get()
                    assert budget is not None
                    self._blend_waits.append((result, budget))
                return result

            # Non-blended command — flush any pending blend group first
            self.finalize()

            self._step_io.emit_event("start", name)

            result = method(*args, **kwargs)

            if isinstance(result, int) and result >= 0:
                self._wait_completed(result)

            self._step_io.emit_event("complete", name)
            self._step_io.increment_step_count()

            if self._step_io.check_should_pause():
                self._step_io.wait_for_step_or_play(check=self._check_health)

            return result

        return wrapper


class _AsyncSteppingToolProxy:
    """Async twin of ``_SteppingToolProxy``. Holds the owner wrapper so the
    first awaited action can flush a pending blend group (a property can't)."""

    def __init__(self, async_tool: Any, owner: "AsyncSteppingClientWrapper") -> None:
        self._tool = async_tool
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._tool, name)
        if not callable(attr) or name not in _STEPPABLE_TOOL_METHODS:
            return attr

        return self._owner._wrap_motion_method(
            "tool_action", attr, record_name=f"tool.{name}"
        )


class AsyncSteppingClientWrapper:
    """Async twin of ``SteppingClientWrapper`` for AsyncRobotClient scripts.

    Same stepping semantics; the wrapper is the completion barrier (the async
    client's motion methods default to ``wait=False``), and pause polling
    yields to the script's event loop instead of blocking it.
    """

    def __init__(self, wrapped_client: Any, step_io: StepIO) -> None:
        self._wrapped = wrapped_client
        self._step_io = step_io
        self._in_blend = False
        self._last_blend_index: int = -1
        self._blend_waits: list[tuple[int, CompletionBudget]] = []

    async def _wait_completed(self, index: int) -> None:
        try:
            if not await self.wait_command(index, timeout=None):
                raise TimeoutError(f"Command {index} completion was not confirmed")
        except Exception:
            async with asyncio.timeout(3.0):
                if await self._wrapped.stop() <= 0:
                    raise RuntimeError(
                        "Command failed and controller stop was not confirmed"
                    )
            raise

    async def _check_health(self) -> None:
        if await self._wrapped.status() is None:
            raise ConnectionError("Controller unavailable during managed pause")
        if await self._wrapped.wait_status(
            lambda status: not status.enabled, timeout=0.01
        ):
            raise RuntimeError("Controller was disabled during managed pause")
        error = await self._wrapped.error()
        if error is not None:
            if isinstance(error, BaseException):
                raise error
            raise RuntimeError(f"Controller fault during managed pause: {error}")

    async def wait_command(
        self, command_index: int, timeout: float | None = 10.0
    ) -> bool:
        try:
            result = await self._wait_command_active(command_index, timeout)
        except BaseException as error:
            if self._step_io.capture_values:
                self._step_io.emit_event(
                    "command_wait_failed",
                    "wait_command",
                    index=command_index,
                    error_type=type(error).__name__,
                    message=str(error)[:512],
                )
            raise
        if self._step_io.capture_values:
            self._step_io.emit_event(
                "command_completed" if result else "command_unconfirmed",
                "wait_command",
                index=command_index,
            )
        return result

    async def _wait_command_active(
        self, command_index: int, timeout: float | None = 10.0
    ) -> bool:
        budget = current_budget.get() or CompletionBudget(timeout)
        budget.bind(self._wrapped, self._step_io.active_time)
        while budget.remaining > 0:
            if await self._wrapped.wait_command(
                command_index, timeout=min(0.1, budget.remaining)
            ):
                budget.confirmed_index = command_index
                return True
            await self._check_health()
        return False

    async def finalize(self) -> None:
        """Barrier for a pending blend group: wait it out, emit completion,
        clear. No pause gate — callers add one where stepping applies."""
        if not self._in_blend:
            return
        try:
            for index, budget in self._blend_waits:
                token = current_budget.set(budget)
                try:
                    await self._wait_completed(index)
                finally:
                    current_budget.reset(token)
        finally:
            self._in_blend = False
            self._last_blend_index = -1
            self._blend_waits.clear()
        self._step_io.emit_event("complete", "blend_group")
        self._step_io.increment_step_count()

    async def _flush_blend(self) -> None:
        if not self._in_blend:
            return
        await self.finalize()
        if self._step_io.check_should_pause():
            await self._step_io.wait_for_step_or_play_async(check=self._check_health)

    @property
    def tool(self):
        """Return the async tool with stepping behavior on action methods."""
        return _AsyncSteppingToolProxy(self._wrapped.tool, self)

    async def __aenter__(self) -> "AsyncSteppingClientWrapper":
        await self._wrapped.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            await self.finalize()
        else:
            self._in_blend = False
            self._last_blend_index = -1
            self._blend_waits.clear()
        return await self._wrapped.__aexit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._wrapped, name)

        if name in STEPPABLE_METHODS and callable(attr):
            return self._wrap_motion_method(name, attr)

        if asyncio.iscoroutinefunction(attr):

            async def passthrough(*args: Any, **kwargs: Any) -> Any:
                if name not in _EXECUTION_CONTROLS:
                    await self._flush_blend()
                return await recorded_method(self._step_io, name, attr)(*args, **kwargs)

            return passthrough

        return attr

    def _wrap_motion_method(
        self, name: str, method: Callable, *, record_name: str | None = None
    ) -> Callable:
        method = recorded_method(self._step_io, record_name or name, method)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            kwargs, timeout = _nonblocking(method, kwargs)
            if name == "tool_action" and timeout is None:
                timeout = 10.0
            budget = current_budget.get() or CompletionBudget(timeout)
            budget.bind(self._wrapped, self._step_io.active_time)
            token = current_budget.set(budget)
            try:
                await self._step_io.wait_until_resumed_async(self._check_health)
                return await execute(*args, **kwargs)
            finally:
                current_budget.reset(token)

        async def execute(*args: Any, **kwargs: Any) -> Any:
            is_blended = _is_blended(kwargs)

            if is_blended and self._step_io.check_should_pause():
                # Stepping: exact-stop group members, one per step grant,
                # with events at group granularity (see the sync wrapper).
                if not self._in_blend:
                    self._step_io.emit_event("start", name, blend=True)
                    self._in_blend = True
                result = await method(*args, **{**kwargs, "r": 0.0})
                if isinstance(result, int) and result >= 0:
                    await self._wait_completed(result)
                if self._step_io.check_should_pause():
                    await self._step_io.wait_for_step_or_play_async(
                        check=self._check_health
                    )
                return result

            if is_blended:
                if not self._in_blend:
                    self._step_io.emit_event("start", name, blend=True)
                    self._in_blend = True
                result = await method(*args, **kwargs)
                if isinstance(result, int) and result >= 0:
                    self._last_blend_index = result
                    budget = current_budget.get()
                    assert budget is not None
                    self._blend_waits.append((result, budget))
                return result

            await self.finalize()

            self._step_io.emit_event("start", name)
            result = await method(*args, **kwargs)
            if isinstance(result, int) and result >= 0:
                await self._wait_completed(result)
            self._step_io.emit_event("complete", name)
            self._step_io.increment_step_count()

            if self._step_io.check_should_pause():
                await self._step_io.wait_for_step_or_play_async(
                    check=self._check_health
                )

            return result

        return wrapper


class GUIStepController:
    """
    GUI-side controller for stepping.
    Used by the GUI to control script execution via IPC files.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._temp_dir = Path(tempfile.gettempdir())
        self._control_file = self._temp_dir / f".parol_control_{session_id}"
        self._event_file = self._temp_dir / f".parol_events_{session_id}"
        self._ack_file = self._temp_dir / f".parol_ack_{session_id}"
        self._last_event_count = 0

    def initialize(self) -> None:
        """Initialize control file with default state (paused=True)."""
        _atomic_write(
            self._control_file,
            {
                "paused": True,
                "step_signal": 0,
                "step_acked": 0,
                "pause_elapsed": 0.0,
                "pause_started": None,
            },
        )
        _atomic_write(self._event_file, {"events": []})
        _atomic_write(self._ack_file, {"step_acked": 0, "waiting": False})

    def waiting_for_step(self) -> bool:
        control = _read_control(self._control_file)
        ack = _read_control(self._ack_file)
        return bool(
            control.get("paused", True)
            and control.get("pause_started") is None
            and ack.get("waiting", False)
            and control.get("step_signal", 0) <= ack.get("step_acked", 0)
        )

    def signal_step(self) -> None:
        """Signal the script to execute one command then pause."""
        control = _read_control(self._control_file)
        self._resume_clock(control)
        control["paused"] = True
        control["step_signal"] = control.get("step_signal", 0) + 1
        _atomic_write(self._control_file, control)

    def signal_play(self) -> None:
        """Signal the script to continue without pausing (play mode)."""
        control = _read_control(self._control_file)
        self._resume_clock(control)
        control["paused"] = False
        _atomic_write(self._control_file, control)

    def signal_pause(self) -> None:
        """Hold instrumented calls and suspend their completion budgets."""
        control = _read_control(self._control_file)
        control["paused"] = True
        if control.get("pause_started") is None:
            control["pause_started"] = time.monotonic()
        _atomic_write(self._control_file, control)

    @staticmethod
    def _resume_clock(control: dict) -> None:
        started = control.get("pause_started")
        if started is not None:
            control["pause_elapsed"] = control.get("pause_elapsed", 0.0) + max(
                0.0, time.monotonic() - started
            )
            control["pause_started"] = None

    def poll_events(self) -> list[dict]:
        """
        Poll for new events from the script.
        Returns list of new events since last poll.
        """
        try:
            data = json.loads(self._event_file.read_text())
            events = data.get("events", [])
            new_events = [
                e for e in events if e.get("sequence", 0) > self._last_event_count
            ]
            if new_events:
                missed = new_events[0]["sequence"] - self._last_event_count - 1
                self._last_event_count = new_events[-1]["sequence"]
                if missed:
                    new_events.insert(0, {"event": "events_lost", "count": missed})
            return new_events
        except (json.JSONDecodeError, OSError):
            return []

    def get_step_count(self) -> int:
        """Get the current step count from events."""
        try:
            data = json.loads(self._event_file.read_text())
            events = data.get("events", [])
            return max(
                (e.get("step", 0) + int(e.get("event") == "complete") for e in events),
                default=0,
            )
        except (json.JSONDecodeError, OSError):
            return 0

    def cleanup(self) -> None:
        """Remove IPC files."""
        try:
            if self._control_file.exists():
                self._control_file.unlink()
        except OSError:
            pass
        try:
            if self._event_file.exists():
                self._event_file.unlink()
        except OSError:
            pass
        self._ack_file.unlink(missing_ok=True)
