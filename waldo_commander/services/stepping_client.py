"""
Stepping client wrapper for GUI-controlled script execution.

Provides a wrapper around RobotClient that:
1. Emits events for each motion command (start/complete)
2. Optionally pauses after each command for stepping through scripts
3. Communicates with GUI via file-based IPC

Cross-platform compatible (Windows, macOS, Linux).
"""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .path_preview_client import MOTION_METHODS

# Methods that trigger wait_command and stepping: all motion methods plus
# non-motion commands that queue on the controller.
STEPPABLE_METHODS = frozenset(MOTION_METHODS) | frozenset(
    {"home", "tool_action", "delay"}
)


def _atomic_write(path: Path, data: dict) -> None:
    """Write data to file atomically using temp file + move."""
    temp_path = path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(data, indent=2))
        shutil.move(str(temp_path), str(path))
    except Exception:
        # Remove the temp file so a failed move leaves no partial artifact.
        if temp_path.exists():
            temp_path.unlink()
        raise


def _read_control(control_file: Path) -> dict:
    """Read control file, return defaults if not exists or parse error."""
    try:
        return json.loads(control_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"paused": True, "step_signal": 0, "step_acked": 0}


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
        self._step_count = 0
        self._last_step_acked = 0

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
        events = self._read_events()
        events.append(
            {
                "event": event_type,
                "method": method,
                "step": self._step_count,
                "ts": time.time(),
                **extra,
            }
        )
        _atomic_write(self._event_file, {"events": events})

    def check_should_pause(self) -> bool:
        """Check if the script should pause (paused flag is true)."""
        control = _read_control(self._control_file)
        return control.get("paused", True)

    def wait_for_step_or_play(
        self, timeout: float = 300.0, poll_interval: float = 0.05
    ) -> bool:
        """
        Wait until either:
        - step_signal > step_acked (step forward requested)
        - paused becomes False (play mode activated)

        Returns True if should continue, False on timeout.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            control = _read_control(self._control_file)

            # paused False means play mode - continue immediately
            if not control.get("paused", True):
                return True

            step_signal = control.get("step_signal", 0)
            step_acked = control.get("step_acked", 0)

            if step_signal > step_acked:
                self._ack_step(control, step_signal)
                return True

            time.sleep(poll_interval)

        return False

    def _ack_step(self, control: dict, step_signal: int) -> None:
        """Acknowledge a step by incrementing step_acked."""
        control["step_acked"] = step_signal
        _atomic_write(self._control_file, control)

    def increment_step_count(self) -> None:
        """Increment the internal step counter."""
        self._step_count += 1


_STEPPABLE_TOOL_METHODS = frozenset({"set_position", "open", "close", "calibrate"})


class _SteppingToolProxy:
    """Proxy that wraps a sync tool's action methods with stepping behavior."""

    def __init__(self, sync_tool: Any, step_io: StepIO) -> None:
        self._tool = sync_tool
        self._step_io = step_io

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._tool, name)
        if not callable(attr) or name not in _STEPPABLE_TOOL_METHODS:
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._step_io.emit_event("start", "tool_action")
            result = attr(*args, **kwargs)
            self._step_io.emit_event("complete", "tool_action")
            self._step_io.increment_step_count()
            if self._step_io.check_should_pause():
                self._step_io.wait_for_step_or_play()
            return result

        return wrapper


class SteppingClientWrapper:
    """
    Wrapper around RobotClient that adds stepping behavior.

    - Intercepts motion methods
    - Emits start/complete events for GUI visualization
    - Calls wait_command() after each motion command
    - Optionally pauses for stepping based on control file
    - Blended commands (r > 0) are grouped as a single step
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

    def _flush_blend(self) -> None:
        """Flush any pending blend group, emit events, and pause if stepping."""
        if not self._in_blend:
            return
        if self._last_blend_index >= 0:
            self._wrapped.wait_command(self._last_blend_index)
        self._in_blend = False
        self._last_blend_index = -1
        self._step_io.emit_event("complete", "blend_group")
        self._step_io.increment_step_count()
        if self._step_io.check_should_pause():
            self._step_io.wait_for_step_or_play()

    @property
    def tool(self):
        """Return the sync tool with stepping behavior on action methods."""
        self._flush_blend()
        return _SteppingToolProxy(self._wrapped.tool, self._step_io)

    def __enter__(self) -> "SteppingClientWrapper":
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args: Any) -> bool | None:
        if self._in_blend:
            # Only wait/complete if not exiting due to an exception
            if args[0] is None:
                if self._last_blend_index >= 0:
                    self._wrapped.wait_command(self._last_blend_index)
                self._step_io.emit_event("complete", "blend_group")
                self._step_io.increment_step_count()
            self._in_blend = False
            self._last_blend_index = -1
        return self._wrapped.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        """
        Delegate attribute access to wrapped client.
        Intercept motion methods to add stepping behavior.
        """
        attr = getattr(self._wrapped, name)

        if name in STEPPABLE_METHODS and callable(attr):
            return self._wrap_motion_method(name, attr)

        self._flush_blend()
        return attr

    @staticmethod
    def _is_blended(kwargs: dict) -> bool:
        """Check if motion kwargs specify a blend radius."""
        return float(kwargs.get("r", 0)) > 0

    def _wrap_motion_method(self, name: str, method: Callable) -> Callable:
        """Create a wrapper function for a motion method."""

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            is_blended = self._is_blended(kwargs)

            if is_blended:
                # Blended command — emit start event on first blend command,
                # then execute without waiting or stepping
                if not self._in_blend:
                    self._step_io.emit_event("start", name, blend=True)
                    self._in_blend = True
                result = method(*args, **kwargs)
                if isinstance(result, int) and result >= 0:
                    self._last_blend_index = result
                return result

            # Non-blended command — flush any pending blend group first
            if self._in_blend:
                if self._last_blend_index >= 0:
                    self._wrapped.wait_command(self._last_blend_index)
                self._in_blend = False
                self._last_blend_index = -1
                self._step_io.emit_event("complete", "blend_group")
                self._step_io.increment_step_count()

            self._step_io.emit_event("start", name)

            result = method(*args, **kwargs)

            if isinstance(result, int) and result >= 0:
                self._wrapped.wait_command(result)

            self._step_io.emit_event("complete", name)
            self._step_io.increment_step_count()

            if self._step_io.check_should_pause():
                self._step_io.wait_for_step_or_play()

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
        self._last_event_count = 0

    def initialize(self) -> None:
        """Initialize control file with default state (paused=True)."""
        _atomic_write(
            self._control_file,
            {
                "paused": True,
                "step_signal": 0,
                "step_acked": 0,
            },
        )
        _atomic_write(self._event_file, {"events": []})

    def signal_step(self) -> None:
        """Signal the script to execute one command then pause."""
        control = _read_control(self._control_file)
        control["paused"] = True
        control["step_signal"] = control.get("step_signal", 0) + 1
        _atomic_write(self._control_file, control)

    def signal_play(self) -> None:
        """Signal the script to continue without pausing (play mode)."""
        control = _read_control(self._control_file)
        control["paused"] = False
        _atomic_write(self._control_file, control)

    def signal_pause(self) -> None:
        """Signal the script to pause after the current command."""
        control = _read_control(self._control_file)
        control["paused"] = True
        _atomic_write(self._control_file, control)

    def poll_events(self) -> list[dict]:
        """
        Poll for new events from the script.
        Returns list of new events since last poll.
        """
        try:
            data = json.loads(self._event_file.read_text())
            events = data.get("events", [])
            new_events = events[self._last_event_count :]
            self._last_event_count = len(events)
            return new_events
        except (json.JSONDecodeError, OSError):
            return []

    def get_step_count(self) -> int:
        """Get the current step count from events."""
        try:
            data = json.loads(self._event_file.read_text())
            events = data.get("events", [])
            return sum(1 for e in events if e.get("event") == "complete")
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
