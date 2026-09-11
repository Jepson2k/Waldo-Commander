"""
Stepping client wrapper for GUI-controlled script execution.

Provides a wrapper around RobotClient that:
1. Emits events for each motion command (start/complete)
2. Optionally pauses after each command for stepping through scripts
3. Talks to the GUI over one duplex pipe (`multiprocessing.connection`:
   a named pipe on Windows, a Unix socket file elsewhere)

Cross-platform compatible (Windows, macOS, Linux).
"""

import asyncio
import inspect
import logging
import os
import queue
import socket
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any, Callable
from collections.abc import Awaitable, Coroutine
from typing import TypeVar, cast

from waldoctl.client import RobotClient

from .path_preview_client import MOTION_METHODS
from .command_records import recorded_method
from .completion_budget import CompletionBudget, PlanWatchdog, current_budget

logger = logging.getLogger(__name__)

R = TypeVar("R")

# Methods that trigger wait_command and stepping: all motion methods plus
# non-motion commands that queue on the controller.
STEPPABLE_METHODS = frozenset(MOTION_METHODS) | frozenset(
    {"home", "tool_action", "delay"}
)

# Controls that must reach the controller at once: they cancel whatever a
# pending blend group was waiting for, so they never wait on it first.
_IMMEDIATE_CONTROLS = frozenset({"stop", "estop"})

_EXECUTION_CONTROLS = frozenset(
    {"pause", "resume", "stop", "estop", "execution_speed", "set_execution_speed"}
)


def _nonblocking(method: Callable, kwargs: dict) -> tuple[dict, float | None]:
    """Dispatch without the client's own wait; the wrapper is the barrier.

    Only an explicit ``timeout=`` becomes a hard deadline. The client's
    default is sized for a standalone call, not for a managed program whose
    moves run as long as the operator planned them.
    """
    parameters = inspect.signature(method).parameters
    timeout = kwargs.get("timeout")
    if "wait" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    ):
        kwargs = {**kwargs, "wait": False}
    return kwargs, timeout


def _is_blended(kwargs: dict) -> bool:
    """Check if motion kwargs specify a blend radius."""
    return float(kwargs.get("r", 0)) > 0


def _step_address(session_id: str) -> str:
    """The listener address for one stepping session: a named pipe on Windows,
    an owner-only socket file in the temp directory elsewhere."""
    if sys.platform == "win32":
        return rf"\\.\pipe\waldo-step-{session_id}"
    return str(Path(tempfile.gettempdir()) / f".waldo-step-{session_id}")


def _step_authkey(session_id: str) -> bytes:
    return f"waldo-step-{session_id}".encode()


_CONTROL_DEFAULT: dict[str, Any] = {
    "paused": True,
    "step_signal": 0,
    "pause_elapsed": 0.0,
    "pause_started": None,
}


class StepIO:
    """The program side of the stepping link.

    One duplex pipe to the GUI (`multiprocessing.connection`; a named pipe
    on Windows, a Unix socket file elsewhere). Down come control states —
    paused, granted steps, the pause clock; up go command/skill events and
    the program's waiting state. Nothing is polled from disk: a control
    change wakes a waiting program, and an event is in the GUI's queue the
    moment it is sent. A program without a GUI (`from_env` → None, or the
    GUI gone) is never held.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._step_count = 0
        self._last_step_acked = 0
        self.capture_values = os.environ.get("WALDO_RECORD_VALUES") == "1"
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._control: dict[str, Any] = dict(_CONTROL_DEFAULT)
        self._connected = False
        self._conn: Connection | None = None
        self._first_control = threading.Event()
        try:
            self._conn = Client(
                _step_address(session_id), authkey=_step_authkey(session_id)
            )
            self._connected = True
        except (OSError, EOFError) as error:
            logging.getLogger(__name__).warning(
                "Stepping link %s unavailable (%s); running unmanaged",
                session_id,
                error,
            )
            return
        threading.Thread(
            target=self._receive, name=f"step-io-{session_id}", daemon=True
        ).start()
        # The GUI's current control state arrives first; a program must not
        # judge pause/play from the default before it has.
        self._first_control.wait(2.0)

    def _receive(self) -> None:
        assert self._conn is not None
        try:
            while True:
                message = self._conn.recv()
                if isinstance(message, dict) and message.get("type") == "control":
                    with self._cond:
                        self._control = {
                            k: v for k, v in message.items() if k != "type"
                        }
                        self._cond.notify_all()
                    self._first_control.set()
        except (EOFError, OSError):
            pass
        finally:
            with self._cond:
                self._connected = False
                self._cond.notify_all()
            self._first_control.set()

    def _send(self, message: dict) -> None:
        if not self._connected or self._conn is None:
            return
        try:
            with self._send_lock:
                self._conn.send(message)
        except (OSError, ValueError):
            # Diagnostics cannot change whether a command executes.
            logging.getLogger(__name__).exception("Could not reach the stepping GUI")
            with self._cond:
                self._connected = False
                self._cond.notify_all()

    def _snapshot(self) -> dict[str, Any]:
        with self._cond:
            return dict(self._control)

    def active_time(self) -> float:
        control = self._snapshot()
        now = time.monotonic()
        paused = control.get("pause_elapsed", 0.0)
        started = control.get("pause_started")
        if started is not None:
            paused += max(0.0, now - started)
        return now - paused

    def hold_requested(self) -> bool:
        return self._connected and self._snapshot().get("pause_started") is not None

    def wait_until_resumed(self, check: Callable[[], None]) -> None:
        while self.hold_requested():
            check()
            with self._cond:
                self._cond.wait(0.05)

    async def wait_until_resumed_async(
        self, check: Callable[[], Awaitable[None]]
    ) -> None:
        while self.hold_requested():
            await check()
            await asyncio.sleep(0.05)

    @classmethod
    def from_env(cls) -> "StepIO | None":
        """The link named by WALDO_STEP_SESSION, or None outside a managed run."""
        session_id = os.environ.get("WALDO_STEP_SESSION")
        if not session_id:
            return None
        return cls(session_id)

    def emit_event(self, event_type: str, method: str, **extra: Any) -> None:
        """Publish one command/skill event to the GUI (start, complete, …)."""
        self._send(
            {
                "type": "event",
                "event": event_type,
                "method": method,
                "step": self._step_count,
                "ts": time.time(),
                "mono_ns": time.monotonic_ns(),
                "active_s": self.active_time(),
                **extra,
            }
        )

    def check_should_pause(self) -> bool:
        """Whether the GUI holds the program between commands (step mode)."""
        return self._connected and self._snapshot().get("paused", True)

    def _step_released(self) -> bool:
        """True when the program may proceed: play mode, a granted step, or no
        GUI holding the link (blocking then would hang the program)."""
        if not self._connected:
            return True
        control = self._snapshot()
        if not control.get("paused", True):
            return True
        step_signal = control.get("step_signal", 0)
        if step_signal > self._last_step_acked:
            self._last_step_acked = step_signal
            self._set_waiting(False)
            return True
        return False

    def wait_for_step_or_play(
        self, poll_interval: float = 0.05, *, check: Callable[[], None] | None = None
    ) -> None:
        """Block until the GUI signals step or play. No timeout: paused means
        paused until the operator says otherwise; `check` runs every interval."""
        self._set_waiting(True)
        try:
            while not self._step_released():
                if check is not None:
                    check()
                with self._cond:
                    self._cond.wait(poll_interval)
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

    def _set_waiting(self, waiting: bool) -> None:
        self._send(
            {"type": "waiting", "waiting": waiting, "step_acked": self._last_step_acked}
        )

    def increment_step_count(self) -> None:
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

    def _check_health(self) -> Any:
        status = self._wrapped.status()
        if status is None:
            raise ConnectionError("Controller unavailable during managed pause")
        if self._wrapped.wait_status(lambda status: not status.enabled, timeout=0.01):
            raise RuntimeError("Controller was disabled during managed pause")
        error = self._wrapped.error()
        if error is not None:
            if isinstance(error, BaseException):
                raise error
            raise RuntimeError(f"Controller fault during managed pause: {error}")
        return status

    def wait_command(self, command_index: int, timeout: float | None = None) -> bool:
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
        self, command_index: int, timeout: float | None = None
    ) -> bool:
        budget = current_budget.get() or CompletionBudget(timeout)
        budget.bind(self._wrapped, self._step_io.active_time)
        watchdog = PlanWatchdog(self._step_io.active_time)
        while budget.remaining > 0:
            if self._wrapped.wait_command(
                command_index, timeout=min(0.1, budget.remaining)
            ):
                budget.confirmed_index = command_index
                return True
            if budget.timeout is None and not watchdog.check(self._check_health()):
                return False
            elif budget.timeout is not None:
                self._check_health()
        return False

    def finalize(self) -> None:
        """Barrier for a pending blend group: wait it out, emit completion,
        clear. No pause gate — callers add one where stepping applies."""
        if not self._in_blend:
            return
        try:
            # Indices complete in order: the group's last member covers the
            # rest, and its budget is what the whole group was dispatched with.
            if self._blend_waits:
                index, budget = self._blend_waits[-1]
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

    def _discard_blend(self) -> None:
        """Close a pending blend group without waiting: the controller has
        just been told to drop it, so its indices will never complete."""
        if not self._in_blend:
            return
        self._in_blend = False
        self._last_blend_index = -1
        self._step_io.emit_event("complete", "blend_group")
        self._step_io.increment_step_count()

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

        if name in _IMMEDIATE_CONTROLS:

            def immediate(*args: Any, **kwargs: Any) -> Any:
                result = attr(*args, **kwargs)
                self._discard_blend()
                return result

            return immediate

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

    async def _check_health(self) -> Any:
        status = await self._wrapped.status()
        if status is None:
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
        return status

    async def wait_command(
        self, command_index: int, timeout: float | None = None
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
        self, command_index: int, timeout: float | None = None
    ) -> bool:
        budget = current_budget.get() or CompletionBudget(timeout)
        budget.bind(self._wrapped, self._step_io.active_time)
        watchdog = PlanWatchdog(self._step_io.active_time)
        while budget.remaining > 0:
            if await self._wrapped.wait_command(
                command_index, timeout=min(0.1, budget.remaining)
            ):
                budget.confirmed_index = command_index
                return True
            status = await self._check_health()
            if budget.timeout is None and not watchdog.check(status):
                return False
        return False

    async def finalize(self) -> None:
        """Barrier for a pending blend group: wait it out, emit completion,
        clear. No pause gate — callers add one where stepping applies."""
        if not self._in_blend:
            return
        try:
            if self._blend_waits:
                index, budget = self._blend_waits[-1]
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

    def _discard_blend(self) -> None:
        if not self._in_blend:
            return
        self._in_blend = False
        self._last_blend_index = -1
        self._step_io.emit_event("complete", "blend_group")
        self._step_io.increment_step_count()

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
            if name in _IMMEDIATE_CONTROLS:

                async def immediate(*args: Any, **kwargs: Any) -> Any:
                    result = await attr(*args, **kwargs)
                    self._discard_blend()
                    return result

                return immediate

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


def _shutdown(conn: Connection) -> None:
    """Close a link so a reader blocked in `recv` on either side wakes with
    EOF: on Unix the fd is a socket and needs `shutdown`, a bare `close`
    leaves the peer waiting for as long as our own reader is blocked."""
    if sys.platform != "win32":
        try:
            probe = socket.socket(fileno=conn.fileno())
            try:
                probe.shutdown(socket.SHUT_RDWR)
            finally:
                probe.detach()
        except OSError:
            pass
    try:
        conn.close()
    except OSError:
        pass  # the reader that woke on EOF closed it first


class GUIStepController:
    """The GUI side of the stepping link: listens for the program (every
    client it constructs — a program may hold a sync and an async client),
    sends control states to all of them, queues the events they publish."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._listener: Listener | None = None
        self._conns: list[Connection] = []
        self._lock = threading.Lock()
        self._control: dict[str, Any] = dict(_CONTROL_DEFAULT)
        self._events: queue.SimpleQueue[dict] = queue.SimpleQueue()
        self._waiting: dict[int, bool] = {}
        self._step_acked = 0
        self._closed = False

    def initialize(self) -> None:
        """Open the listener; a program connects when it constructs StepIO."""
        address = _step_address(self.session_id)
        if sys.platform != "win32":
            Path(address).unlink(missing_ok=True)
        self._listener = Listener(address, authkey=_step_authkey(self.session_id))
        if sys.platform != "win32":
            os.chmod(address, 0o600)
        threading.Thread(
            target=self._accept, name=f"step-gui-{self.session_id}", daemon=True
        ).start()

    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        while True:
            try:
                conn = listener.accept()
            except (OSError, EOFError):
                return
            with self._lock:
                if self._closed:
                    conn.close()
                    return
                self._conns.append(conn)
                control = {"type": "control", **self._control}
            try:
                conn.send(control)
            except (OSError, ValueError):
                self._drop(conn)
                continue
            threading.Thread(
                target=self._serve,
                args=(conn,),
                name=f"step-gui-{self.session_id}-rx",
                daemon=True,
            ).start()

    def _serve(self, conn: Connection) -> None:
        key = id(conn)
        try:
            while True:
                message = conn.recv()
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "event":
                    self._events.put({k: v for k, v in message.items() if k != "type"})
                elif message.get("type") == "waiting":
                    with self._lock:
                        self._waiting[key] = bool(message.get("waiting", False))
                        self._step_acked = max(
                            self._step_acked, int(message.get("step_acked", 0))
                        )
        except (EOFError, OSError):
            pass
        finally:
            self._drop(conn)

    def _drop(self, conn: Connection) -> None:
        with self._lock:
            self._waiting.pop(id(conn), None)
            if conn in self._conns:
                self._conns.remove(conn)
        try:
            conn.close()
        except OSError:
            pass  # cleanup() already shut it down under the reader

    def _publish(self) -> None:
        for conn in list(self._conns):
            try:
                conn.send({"type": "control", **self._control})
            except (OSError, ValueError):
                logger.debug(
                    "Stepping link %s: a program client went away", self.session_id
                )

    def waiting_for_step(self) -> bool:
        """A program client sits at a step boundary with nothing granted."""
        with self._lock:
            return bool(
                self._control["paused"]
                and self._control["pause_started"] is None
                and any(self._waiting.values())
                and self._control["step_signal"] <= self._step_acked
            )

    def signal_step(self) -> None:
        """Let the program execute one command, then hold again."""
        with self._lock:
            self._resume_clock(self._control)
            self._control["paused"] = True
            self._control["step_signal"] += 1
            self._publish()

    def signal_play(self) -> None:
        """Let the program run without holding between commands."""
        with self._lock:
            self._resume_clock(self._control)
            self._control["paused"] = False
            self._publish()

    def signal_pause(self) -> None:
        """Hold instrumented calls and suspend their completion budgets."""
        with self._lock:
            self._control["paused"] = True
            if self._control["pause_started"] is None:
                self._control["pause_started"] = time.monotonic()
            self._publish()

    @staticmethod
    def _resume_clock(control: dict) -> None:
        started = control.get("pause_started")
        if started is not None:
            control["pause_elapsed"] = control.get("pause_elapsed", 0.0) + max(
                0.0, time.monotonic() - started
            )
            control["pause_started"] = None

    def poll_events(self) -> list[dict]:
        """Every event published since the last call, in order, none lost."""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def cleanup(self) -> None:
        """Close the link; a program still running proceeds unmanaged."""
        with self._lock:
            self._closed = True
            conns, self._conns = list(self._conns), []
            self._waiting.clear()
        for conn in conns:
            _shutdown(conn)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if sys.platform != "win32":
            Path(_step_address(self.session_id)).unlink(missing_ok=True)
