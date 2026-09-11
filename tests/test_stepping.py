"""Tests for stepping functionality - GUI-controlled script execution.

The stepping system allows users to execute robot scripts step-by-step:
- StepIO: File-based IPC for script subprocess to communicate with GUI
- GUIStepController: GUI-side controller for sending play/pause/step signals
- SteppingClientWrapper: Wraps robot client to intercept motion commands

These are unit tests for the IPC components.
"""

import json
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


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

    def test_event_publication_recovers_after_transient_file_lock(
        self, tmp_path, monkeypatch
    ):
        from waldo_commander.services import stepping_client

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = stepping_client.GUIStepController("locked-event-file")
        controller.initialize()
        publisher = stepping_client.StepIO(controller.session_id)
        publisher.emit_event("command_started", "move_j")
        assert [e["event"] for e in controller.poll_events()] == ["command_started"]

        with monkeypatch.context() as fault:

            def locked_file(source, destination):
                raise PermissionError("event file temporarily held by a reader")

            fault.setattr(stepping_client.os, "replace", locked_file)
            publisher.emit_event("command_completed", "move_j")
        assert controller.poll_events() == []
        publisher.emit_event("command_started", "delay")
        events = controller.poll_events()
        assert [(e["event"], e["method"]) for e in events] == [
            ("command_completed", "move_j"),
            ("command_started", "delay"),
        ]
        assert [e["sequence"] for e in events] == [2, 3]
        assert controller.poll_events() == []
        controller.cleanup()

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

    async def test_wait_for_step_async_blocks_and_releases(self, tmp_path, monkeypatch):
        """The async wait mirrors the sync semantics: blocks while paused,
        releases on a granted step, returns immediately with no control file."""
        import asyncio

        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_async_wait")
        controller.initialize()
        step_io = StepIO("test_async_wait")

        task = asyncio.ensure_future(
            step_io.wait_for_step_or_play_async(poll_interval=0.01)
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "paused session must keep blocking"
        controller.signal_step()
        await asyncio.wait_for(task, timeout=1.0)

        controller.cleanup()
        await asyncio.wait_for(
            step_io.wait_for_step_or_play_async(poll_interval=0.01), timeout=1.0
        )


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

        from waldo_commander.services.stepping_client import StepIO

        step_io = StepIO(controller.session_id)
        event_file = tmp_path / ".parol_events_test_poll"
        step_io.emit_event("start", "move_j")
        step_io.emit_event("complete", "move_j")

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

        mock_client.move_j.assert_called_once_with([0, 0, 0, 0, 0, 0], wait=False)
        mock_client.wait_command.assert_called_once_with(42, timeout=0.1)
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

    def test_paused_wrapper_strips_blend_radius(self, tmp_path, monkeypatch):
        """While stepping (paused), each r>0 group member is dispatched as an
        exact-stop move — one per step grant — while events keep the blend
        group's granularity (start once, complete at group close)."""
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_strip")
        controller.initialize()

        mock_client = MagicMock()
        mock_client.move_j = MagicMock(return_value=7)
        mock_client.wait_command = MagicMock()

        step_io = StepIO("test_strip")
        wrapper = SteppingClientWrapper(mock_client, step_io)

        controller.signal_step()  # pre-grant so the post-member pause releases
        result = wrapper.move_j([0, 0, 0, 0, 0, 0], r=15, wait=False)
        assert result == 7
        assert mock_client.move_j.call_args.kwargs["r"] == 0.0
        mock_client.wait_command.assert_called_with(7, timeout=0.1)

        controller.signal_step()
        wrapper.move_j([1, 1, 1, 1, 1, 1], r=15, wait=False)
        assert mock_client.move_j.call_args.kwargs["r"] == 0.0

        events_file = tmp_path / ".parol_events_test_strip"
        events = json.loads(events_file.read_text())["events"]
        assert [e["event"] for e in events] == ["start"], (
            "group members must not emit per-member events"
        )
        assert events[0]["blend"] is True

        # A non-blended call closes the group: one blend_group complete, then
        # the normal per-command events.
        controller.signal_play()
        wrapper.move_j([2, 2, 2, 2, 2, 2])
        events = json.loads(events_file.read_text())["events"]
        assert [(e["event"], e["method"]) for e in events] == [
            ("start", "move_j"),
            ("complete", "blend_group"),
            ("start", "move_j"),
            ("complete", "move_j"),
        ]
        assert wrapper._in_blend is False

    @pytest.mark.timeout(30)
    def test_unbounded_wait_still_ends_when_the_plan_is_empty(
        self, tmp_path, monkeypatch
    ):
        """Without an explicit deadline the wait is sized by the controller's
        queued motion; a command it reports nothing left to play for, and
        never completes, times out after the grace period rather than never."""
        import time

        from waldo_commander.services import completion_budget
        from waldo_commander.services.stepping_client import (
            StepIO,
            SteppingClientWrapper,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(completion_budget, "PLAN_GRACE_S", 0.3)
        control_file = tmp_path / ".parol_control_test_grace"
        control_file.write_text(json.dumps({"paused": False, "step_signal": 0}))
        client = MagicMock()
        client.wait_command = MagicMock(return_value=False)
        client.wait_status = MagicMock(return_value=False)
        client.error = MagicMock(return_value=None)
        client.status = MagicMock(
            return_value=SimpleNamespace(
                queued_duration=0.0, completed_index=-1, executing_index=-1
            )
        )
        wrapper = SteppingClientWrapper(client, StepIO("test_grace"))
        started = time.monotonic()
        assert wrapper.wait_command(5) is False
        assert time.monotonic() - started < 3.0

    def test_stop_reaches_controller_while_a_blend_group_is_pending(
        self, tmp_path, monkeypatch, session_controller
    ):
        """stop()/estop() are not gated on the blend barrier: a pending group
        is discarded, the controller is told at once, and the next motion
        command runs normally."""
        import time

        from parol6 import RobotClient

        from tests.conftest import _get_test_ports
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        controller = GUIStepController("test_stop")
        controller.initialize()
        controller.signal_play()
        port, _ = _get_test_ports()
        with RobotClient(host="127.0.0.1", port=port, timeout=5.0) as client:
            client.simulator(True)
            client.reset()
            assert client.home(wait=True, timeout=10.0) >= 0
            wrapper = SteppingClientWrapper(client, StepIO("test_stop"))
            home = client.angles()
            assert home is not None
            away = [a + 5.0 for a in home]
            assert wrapper.move_j(away, duration=8.0, r=15, wait=False) >= 0
            assert wrapper.move_j(home, duration=8.0, r=15, wait=False) >= 0
            assert wrapper._in_blend is True

            started = time.monotonic()
            assert wrapper.stop() == 1
            assert time.monotonic() - started < 1.0, (
                "stop must not wait for the blend group it cancels"
            )
            assert wrapper._in_blend is False
            deadline = time.monotonic() + 3.0
            while not (client.queue() == [] and client.is_robot_stopped()):
                assert time.monotonic() < deadline, "controller did not stop"
                time.sleep(0.05)
            index = wrapper.move_j(home, duration=0.5)
            assert index >= 0 and client.wait_command(index, timeout=5.0)

        events = json.loads((tmp_path / ".parol_events_test_stop").read_text())
        assert [(e["event"], e["method"]) for e in events["events"]] == [
            ("start", "move_j"),
            ("complete", "blend_group"),
            ("start", "move_j"),
            ("complete", "move_j"),
        ]

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
