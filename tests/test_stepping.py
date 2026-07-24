"""Tests for stepping functionality - GUI-controlled script execution.

The stepping system allows users to execute robot scripts step-by-step:
- StepIO: File-based IPC for script subprocess to communicate with GUI
- GUIStepController: GUI-side controller for sending play/pause/step signals
- SteppingClientWrapper: Wraps robot client to intercept motion commands

These are unit tests for the IPC components.
"""

import json
import tempfile
from unittest.mock import MagicMock


# ============================================================================
# Unit Tests - StepIO (Script-side IPC)
# ============================================================================


class TestStepIO:
    """Unit tests for StepIO file-based IPC.

    StepIO is used by the script subprocess to:
    - Emit events (start/complete) to the GUI
    - Check if execution should pause
    - Wait for step/play signals from GUI

    The WALDO_STEP_SESSION env var is set by script_runner.py when launching
    a script subprocess. It contains the session ID for IPC file naming.
    """

    def test_from_env_returns_step_io_when_session_set(self, monkeypatch):
        """StepIO.from_env returns StepIO when WALDO_STEP_SESSION is set."""
        from waldo_commander.services.stepping_client import StepIO

        monkeypatch.setenv("WALDO_STEP_SESSION", "test123")
        result = StepIO.from_env()
        assert isinstance(result, StepIO)
        assert result.session_id == "test123"

    def test_from_env_returns_none_when_session_not_set(self, monkeypatch):
        """StepIO.from_env returns None when env var is not set."""
        from waldo_commander.services.stepping_client import StepIO

        monkeypatch.delenv("WALDO_STEP_SESSION", raising=False)
        result = StepIO.from_env()
        assert result is None

    def test_emit_event_writes_to_file(self, tmp_path, monkeypatch):
        """emit_event writes events to the event file."""
        from waldo_commander.services.stepping_client import StepIO

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        step_io = StepIO("test_emit")
        step_io.emit_event("start", "move_j", extra_data="test")

        event_file = tmp_path / ".parol_events_test_emit"
        assert event_file.exists()

        data = json.loads(event_file.read_text())
        assert "events" in data
        assert len(data["events"]) == 1
        assert data["events"][0]["event"] == "start"
        assert data["events"][0]["method"] == "move_j"
        assert data["events"][0]["extra_data"] == "test"

    def test_check_should_pause_behavior(self, tmp_path, monkeypatch):
        """check_should_pause returns True by default, False when control file says so."""
        from waldo_commander.services.stepping_client import StepIO

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        step_io = StepIO("test_pause")

        # No control file exists - should default to paused=True
        assert step_io.check_should_pause() is True

        # Create control file with paused=False
        control_file = tmp_path / ".parol_control_test_pause"
        control_file.write_text(json.dumps({"paused": False}))

        assert step_io.check_should_pause() is False

    def test_wait_for_step_blocks_paused_and_releases(self, tmp_path, monkeypatch):
        """A paused session blocks until a step is granted; a missing control
        file means the session is no longer GUI-controlled and must not block."""
        import threading
        import time

        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_wait")
        controller.initialize()
        step_io = StepIO("test_wait")

        waiter = threading.Thread(
            target=lambda: step_io.wait_for_step_or_play(poll_interval=0.01)
        )
        waiter.start()
        time.sleep(0.3)
        assert waiter.is_alive(), "paused session must keep blocking"

        controller.signal_step()
        waiter.join(timeout=1.0)
        assert not waiter.is_alive(), "a granted step must release the wait"

        # Control file deleted mid-session: not GUI-controlled anymore, so the
        # wait returns immediately instead of polling the paused default.
        controller.cleanup()
        start = time.monotonic()
        step_io.wait_for_step_or_play(poll_interval=0.01)
        assert time.monotonic() - start < 1.0


# ============================================================================
# Unit Tests - GUIStepController (GUI-side IPC)
# ============================================================================


class TestGUIStepController:
    """Unit tests for GUIStepController.

    GUIStepController is used by the GUI to:
    - Initialize IPC files for a stepping session
    - Send play/pause/step signals to the script
    - Poll events from the script
    - Clean up IPC files
    """

    def test_initialize_and_control_signals(self, tmp_path, monkeypatch):
        """Controller creates files and play/pause signals work correctly."""
        from waldo_commander.services.stepping_client import GUIStepController

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_init")
        controller.initialize()

        control_file = tmp_path / ".parol_control_test_init"
        assert control_file.exists()

        # Initial state: paused
        data = json.loads(control_file.read_text())
        assert data["paused"] is True
        assert data["step_signal"] == 0

        # Signal play
        controller.signal_play()
        data = json.loads(control_file.read_text())
        assert data["paused"] is False

        # Signal pause
        controller.signal_pause()
        data = json.loads(control_file.read_text())
        assert data["paused"] is True

    def test_signal_step_increments_counter(self, tmp_path, monkeypatch):
        """signal_step increments step_signal counter."""
        from waldo_commander.services.stepping_client import GUIStepController

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_step")
        controller.initialize()

        controller.signal_step()
        control_file = tmp_path / ".parol_control_test_step"
        data = json.loads(control_file.read_text())
        assert data["step_signal"] == 1

        controller.signal_step()
        data = json.loads(control_file.read_text())
        assert data["step_signal"] == 2

    def test_poll_events_and_cleanup(self, tmp_path, monkeypatch):
        """poll_events returns new events; cleanup removes IPC files."""
        from waldo_commander.services.stepping_client import GUIStepController

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_poll")
        controller.initialize()

        # Write some events to event file
        event_file = tmp_path / ".parol_events_test_poll"
        event_file.write_text(
            json.dumps(
                {
                    "events": [
                        {"event": "start", "method": "move_j", "step": 0},
                        {"event": "complete", "method": "move_j", "step": 0},
                    ]
                }
            )
        )

        events = controller.poll_events()
        assert len(events) == 2
        assert events[0]["event"] == "start"
        assert events[1]["event"] == "complete"

        # Second poll should return empty (already read)
        events = controller.poll_events()
        assert len(events) == 0

        # Cleanup removes files
        control_file = tmp_path / ".parol_control_test_poll"
        assert control_file.exists()
        assert event_file.exists()

        controller.cleanup()
        assert not control_file.exists()
        assert not event_file.exists()


# ============================================================================
# Unit Tests - SteppingClientWrapper
# ============================================================================


class TestSteppingClientWrapper:
    """Unit tests for SteppingClientWrapper.

    Wraps a robot client to intercept motion commands, adding wait_command
    after each motion so the script pauses until the robot completes the move.
    """

    def test_wraps_motion_methods(self, tmp_path, monkeypatch):
        """Wrapper intercepts motion methods and waits for completion."""
        from waldo_commander.services.stepping_client import (
            StepIO,
            SteppingClientWrapper,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        mock_client = MagicMock()
        mock_client.move_j = MagicMock(return_value=42)
        mock_client.wait_command = MagicMock()

        step_io = StepIO("test_wrapper")
        # Set up control file so we don't pause (paused=False)
        control_file = tmp_path / ".parol_control_test_wrapper"
        control_file.write_text(json.dumps({"paused": False, "step_signal": 0}))

        wrapper = SteppingClientWrapper(mock_client, step_io)

        result = wrapper.move_j([0, 0, 0, 0, 0, 0])

        mock_client.move_j.assert_called_once_with([0, 0, 0, 0, 0, 0])
        mock_client.wait_command.assert_called_once_with(42)
        assert result == 42

        # Verify events were emitted
        event_file = tmp_path / ".parol_events_test_wrapper"
        assert event_file.exists()
        events = json.loads(event_file.read_text())["events"]
        assert len(events) == 2
        assert events[0]["event"] == "start"
        assert events[1]["event"] == "complete"

    def test_passes_through_non_motion_methods(self, tmp_path, monkeypatch):
        """Non-motion methods are passed through without wrapping."""
        from waldo_commander.services.stepping_client import (
            StepIO,
            SteppingClientWrapper,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        mock_client = MagicMock()
        mock_client.status = MagicMock(return_value="status")

        step_io = StepIO("test_passthrough")
        wrapper = SteppingClientWrapper(mock_client, step_io)

        result = wrapper.status()

        mock_client.status.assert_called_once()
        mock_client.wait_command.assert_not_called()
        assert result == "status"

        # No events should be emitted for non-motion methods
        event_file = tmp_path / ".parol_events_test_passthrough"
        assert not event_file.exists()

    def test_motion_methods_list_is_correct(self):
        """STEPPABLE_METHODS contains expected robot motion commands."""
        from waldo_commander.services.stepping_client import STEPPABLE_METHODS

        expected = {
            "home",
            "move_j",
            "move_l",
            "jog_j",
            "jog_l",
            "tool_action",
            "delay",
        }
        assert expected.issubset(STEPPABLE_METHODS)
