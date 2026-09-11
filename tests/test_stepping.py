"""Tests for stepping functionality - GUI-controlled script execution.

The stepping system allows users to execute robot scripts step-by-step:
- StepIO: the program side of the stepping link (a duplex pipe to the GUI)
- GUIStepController: GUI-side controller for sending play/pause/step signals
- SteppingClientWrapper: Wraps robot client to intercept motion commands
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from waldo_commander.services.stepping_client import GUIStepController


def _drain(controller, count, timeout=2.0):
    """Events the program published, waiting for `count` of them."""
    deadline = time.monotonic() + timeout
    events = []
    while len(events) < count and time.monotonic() < deadline:
        events.extend(controller.poll_events())
        time.sleep(0.01)
    return events


# ============================================================================
# Unit Tests - StepIO (Script-side IPC)
# ============================================================================


class TestStepIO:
    """The program side of the stepping link.

    A program constructs StepIO from WALDO_STEP_SESSION and connects to the
    GUI's listener for that session; without a GUI it is never held.
    """

    def test_from_env_connects_or_runs_unmanaged(self, monkeypatch):
        from waldo_commander.services.stepping_client import GUIStepController, StepIO

        monkeypatch.delenv("WALDO_STEP_SESSION", raising=False)
        assert StepIO.from_env() is None
        # A session nobody listens on: unmanaged, so nothing pauses or holds.
        monkeypatch.setenv("WALDO_STEP_SESSION", "nobody-listens")
        orphan = StepIO.from_env()
        assert isinstance(orphan, StepIO) and orphan.session_id == "nobody-listens"
        assert orphan.check_should_pause() is False
        assert orphan.hold_requested() is False
        orphan.wait_for_step_or_play(poll_interval=0.01)
        controller = GUIStepController("from-env")
        controller.initialize()
        try:
            monkeypatch.setenv("WALDO_STEP_SESSION", "from-env")
            linked = StepIO.from_env()
            assert linked.check_should_pause() is True, "a fresh session starts paused"
            controller.signal_play()
            linked.wait_for_step_or_play(poll_interval=0.01)
            assert linked.check_should_pause() is False
        finally:
            controller.cleanup()

    def test_every_event_reaches_the_gui_in_order(self):
        """No window, no loss: a burst far larger than any file window arrives
        complete and ordered, and the step counter rides along."""
        from waldo_commander.services.stepping_client import GUIStepController, StepIO

        controller = GUIStepController("burst")
        controller.initialize()
        try:
            io = StepIO(controller.session_id)
            for i in range(2000):
                io.emit_event("complete", "delay", index=i)
                io.increment_step_count()
            io.emit_event("start", "move_j", extra_data="test")
            deadline = time.monotonic() + 5.0
            events = []
            while len(events) < 2001 and time.monotonic() < deadline:
                events.extend(controller.poll_events())
                time.sleep(0.01)
            assert len(events) == 2001
            assert [e["index"] for e in events[:-1]] == list(range(2000))
            assert (
                events[-1]["method"] == "move_j" and events[-1]["extra_data"] == "test"
            )
            assert events[-1]["step"] == 2000
            assert controller.poll_events() == []
        finally:
            controller.cleanup()

    def test_wait_for_step_blocks_paused_and_releases(self):
        """A paused session blocks until a step is granted; once the GUI has
        closed the link the session is no longer managed and must not block."""
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
        )

        controller = GUIStepController("test_wait")
        controller.initialize()
        step_io = StepIO("test_wait")
        assert step_io.check_should_pause() is True

        waiter = threading.Thread(
            target=lambda: step_io.wait_for_step_or_play(poll_interval=0.01)
        )
        waiter.start()
        time.sleep(0.3)
        assert waiter.is_alive(), "paused session must keep blocking"
        deadline = time.monotonic() + 1.0
        while not controller.waiting_for_step() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert controller.waiting_for_step(), "the GUI sees the program waiting"

        controller.signal_step()
        waiter.join(timeout=1.0)
        assert not waiter.is_alive(), "a granted step must release the wait"
        deadline = time.monotonic() + 1.0
        while controller.waiting_for_step() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not controller.waiting_for_step()

        controller.cleanup()
        start = time.monotonic()
        step_io.wait_for_step_or_play(poll_interval=0.01)
        assert time.monotonic() - start < 1.0

    async def test_wait_for_step_async_blocks_and_releases(self):
        """The async wait mirrors the sync semantics: blocks while paused,
        releases on a granted step, returns immediately once the GUI is gone."""
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
        )

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
    """The GUI side of the stepping link: play/pause/step reach the program,
    the program's events reach the GUI, cleanup closes the link."""

    def test_control_signals_reach_the_program(self):
        from waldo_commander.services.stepping_client import GUIStepController, StepIO

        controller = GUIStepController("test_init")
        controller.initialize()
        try:
            io = StepIO(controller.session_id)
            assert io.check_should_pause() is True and io.hold_requested() is False
            controller.signal_play()
            io.wait_for_step_or_play(poll_interval=0.01)
            assert io.check_should_pause() is False
            controller.signal_pause()
            deadline = time.monotonic() + 1.0
            while not io.hold_requested() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert io.hold_requested() and io.check_should_pause()
            # One grant releases one wait; the next wait blocks until the
            # GUI acts again.
            controller.signal_step()
            io.wait_for_step_or_play(poll_interval=0.01)
            waiter = threading.Thread(
                target=lambda: io.wait_for_step_or_play(poll_interval=0.01)
            )
            waiter.start()
            time.sleep(0.2)
            assert waiter.is_alive()
            controller.signal_play()
            waiter.join(timeout=1.0)
            assert not waiter.is_alive()
        finally:
            controller.cleanup()

    def test_poll_events_and_cleanup(self):
        from waldo_commander.services.stepping_client import GUIStepController, StepIO

        controller = GUIStepController("test_poll")
        controller.initialize()
        step_io = StepIO(controller.session_id)
        step_io.emit_event("start", "move_j")
        step_io.emit_event("complete", "move_j")
        deadline = time.monotonic() + 2.0
        events = []
        while len(events) < 2 and time.monotonic() < deadline:
            events.extend(controller.poll_events())
            time.sleep(0.01)
        assert [e["event"] for e in events] == ["start", "complete"]
        assert controller.poll_events() == []

        controller.cleanup()
        # The link is closed: the program runs unmanaged and its events go nowhere.
        deadline = time.monotonic() + 1.0
        while step_io._connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not step_io._connected
        step_io.emit_event("start", "move_l")
        assert controller.poll_events() == []


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
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        mock_client = MagicMock()
        mock_client.move_j = MagicMock(return_value=42)
        mock_client.wait_command = MagicMock()

        controller = GUIStepController("test_wrapper")
        controller.initialize()
        controller.signal_play()
        step_io = StepIO("test_wrapper")

        wrapper = SteppingClientWrapper(mock_client, step_io)

        result = wrapper.move_j([0, 0, 0, 0, 0, 0])

        mock_client.move_j.assert_called_once_with([0, 0, 0, 0, 0, 0], wait=False)
        mock_client.wait_command.assert_called_once_with(42, timeout=0.1)
        assert result == 42

        events = _drain(controller, 2)
        assert [e["event"] for e in events] == ["start", "complete"]
        controller.cleanup()

    def test_passes_through_non_motion_methods(self, tmp_path, monkeypatch):
        """Non-motion methods are passed through without wrapping."""
        from waldo_commander.services.stepping_client import (
            StepIO,
            SteppingClientWrapper,
        )

        mock_client = MagicMock()
        mock_client.status = MagicMock(return_value="status")

        controller = GUIStepController("test_passthrough")
        controller.initialize()
        controller.signal_play()
        step_io = StepIO("test_passthrough")
        wrapper = SteppingClientWrapper(mock_client, step_io)

        result = wrapper.status()

        mock_client.status.assert_called_once()
        mock_client.wait_command.assert_not_called()
        assert result == "status"

        # A read is not a step: nothing is published for it.
        time.sleep(0.1)
        assert controller.poll_events() == []
        controller.cleanup()

    def test_paused_wrapper_strips_blend_radius(self, tmp_path, monkeypatch):
        """While stepping (paused), each r>0 group member is dispatched as an
        exact-stop move — one per step grant — while events keep the blend
        group's granularity (start once, complete at group close)."""
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        controller = GUIStepController("test_strip")
        controller.initialize()

        mock_client = MagicMock()
        mock_client.move_j = MagicMock(return_value=7)
        mock_client.wait_command = MagicMock()
        # The health check runs while a grant is still in flight on the link.
        mock_client.wait_status = MagicMock(return_value=False)
        mock_client.error = MagicMock(return_value=None)

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

        events = _drain(controller, 1)
        assert [e["event"] for e in events] == ["start"], (
            "group members must not emit per-member events"
        )
        assert events[0]["blend"] is True

        # A non-blended call closes the group: one blend_group complete, then
        # the normal per-command events.
        controller.signal_play()
        wrapper.move_j([2, 2, 2, 2, 2, 2])
        events += _drain(controller, 3)
        assert [(e["event"], e["method"]) for e in events] == [
            ("start", "move_j"),
            ("complete", "blend_group"),
            ("start", "move_j"),
            ("complete", "move_j"),
        ]
        assert wrapper._in_blend is False
        controller.cleanup()

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

        monkeypatch.setattr(completion_budget, "PLAN_GRACE_S", 0.3)
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

        events = _drain(controller, 4)
        assert [(e["event"], e["method"]) for e in events] == [
            ("start", "move_j"),
            ("complete", "blend_group"),
            ("start", "move_j"),
            ("complete", "move_j"),
        ]
        controller.cleanup()

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
