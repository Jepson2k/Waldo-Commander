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

from tests.helpers.stepping import _drain


# ============================================================================
# Unit Tests - StepIO (Script-side IPC)
# ============================================================================


class TestStepIO:
    """The program side of the stepping link.

    A program constructs StepIO from WALDO_STEP_SESSION and connects to the
    GUI's listener for that session. Without a session it is never held;
    with one, it never runs on without its GUI.
    """

    def test_from_env_connects_or_refuses_to_run_unmanaged(self, monkeypatch):
        from waldo_commander.services.stepping_client import GUIStepController, StepIO

        monkeypatch.delenv("WALDO_STEP_SESSION", raising=False)
        assert StepIO.from_env() is None
        # A session nobody listens on: the program was launched managed, and
        # running it unmanaged would ignore every Pause and Step.
        monkeypatch.setenv("WALDO_STEP_SESSION", "nobody-listens")
        with pytest.raises(ConnectionError, match="nobody-listens"):
            StepIO.from_env()
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

    def test_every_event_reaches_the_gui_in_order_until_cleanup(self):
        """No window, no loss: a burst far larger than any file window arrives
        complete and ordered, each event with its command. Cleanup closes the
        link: nothing of the session outlives it, and the program is held to
        account at its next gate rather than let run on."""
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            SteppingLinkLost,
            StepIO,
        )

        controller = GUIStepController("burst")
        controller.initialize()
        try:
            io = StepIO(controller.session_id)
            for i in range(2000):
                io.emit_event("complete", "delay", command=io.issue(), index=i)
            io.emit_event("start", "move_j", command=io.issue(), extra_data="test")
            deadline = time.monotonic() + 5.0
            events = []
            while len(events) < 2001 and time.monotonic() < deadline:
                events.extend(controller.poll_events())
                time.sleep(0.01)
            assert len(events) == 2001
            assert [e["index"] for e in events[:-1]] == list(range(2000))
            assert [e["command"] for e in events] == list(range(2001))
            assert (
                events[-1]["method"] == "move_j" and events[-1]["extra_data"] == "test"
            )
            assert controller.poll_events() == []
        finally:
            controller.cleanup()
        assert not any(
            t.name == f"step-gui-{controller.session_id}" and t.is_alive()
            for t in threading.enumerate()
        ), "the accept thread outlived its session"
        deadline = time.monotonic() + 1.0
        while io._connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not io._connected
        io.emit_event("start", "move_l")
        assert controller.poll_events() == []
        with pytest.raises(SteppingLinkLost):
            io.check_should_pause()

    async def test_wait_for_step_blocks_paused_and_releases(self):
        """A paused session blocks until a step is granted; once the GUI has
        closed the link the wait fails rather than block or run on. The async
        wait mirrors the sync one."""
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            SteppingLinkLost,
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
        with pytest.raises(SteppingLinkLost):
            step_io.wait_for_step_or_play(poll_interval=0.01)
        assert time.monotonic() - start < 1.0

        async_controller = GUIStepController("test_async_wait")
        async_controller.initialize()
        async_io = StepIO("test_async_wait")
        task = asyncio.ensure_future(
            async_io.wait_for_step_or_play_async(poll_interval=0.01)
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "paused session must keep blocking"
        async_controller.signal_step()
        await asyncio.wait_for(task, timeout=1.0)

        async_controller.cleanup()
        with pytest.raises(SteppingLinkLost):
            await asyncio.wait_for(
                async_io.wait_for_step_or_play_async(poll_interval=0.01), timeout=1.0
            )

    def test_a_lost_gui_stops_the_program_instead_of_releasing_it(
        self, session_controller
    ):
        """A GUI that goes away mid-run takes nothing with it: the program's
        next command raises instead of running on unmanaged, and the arm is
        stopped, queued motion and all."""
        from parol6 import RobotClient

        from tests.conftest import _get_test_ports
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            SteppingClientWrapper,
            SteppingLinkLost,
            StepIO,
        )

        controller = GUIStepController("lost-gui")
        controller.initialize()
        controller.signal_play()
        port, _ = _get_test_ports()
        with RobotClient(host="127.0.0.1", port=port, timeout=5.0) as client:
            client.simulator(True)
            client.reset()
            assert client.home(wait=True, timeout=10.0) >= 0
            step_io = StepIO("lost-gui")
            wrapper = SteppingClientWrapper(client, step_io)
            home = client.angles()
            assert home is not None
            away = [a + 5.0 for a in home]
            try:
                # A blend group is queued and not waited on: only a Stop ends it.
                assert wrapper.move_j(away, duration=4.0, r=15, wait=False) >= 0
                controller.cleanup()
                deadline = time.monotonic() + 1.0
                while step_io._connected and time.monotonic() < deadline:
                    time.sleep(0.01)
                with pytest.raises(SteppingLinkLost):
                    wrapper.move_j(home, duration=0.5)
                deadline = time.monotonic() + 2.0
                while not (client.queue() == [] and client.is_robot_stopped()):
                    assert time.monotonic() < deadline, "the arm was left moving"
                    time.sleep(0.05)
            finally:
                controller.cleanup()
                client.stop()


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


# ============================================================================
# Unit Tests - SteppingClientWrapper
# ============================================================================


class TestSteppingClientWrapper:
    """Unit tests for SteppingClientWrapper.

    Wraps a robot client to intercept motion commands, adding wait_command
    after each motion so the script pauses until the robot completes the move.
    """

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
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        monkeypatch.setattr(completion_budget, "PLAN_GRACE_S", 0.3)
        controller = GUIStepController("test_grace")
        controller.initialize()
        controller.signal_play()
        client = MagicMock()
        client.wait_command = MagicMock(return_value=False)
        client.wait_status = MagicMock(return_value=False)
        client.error = MagicMock(return_value=None)
        client.status = MagicMock(
            return_value=SimpleNamespace(
                queued_duration=0.0, completed_index=-1, executing_index=-1
            )
        )
        try:
            wrapper = SteppingClientWrapper(client, StepIO("test_grace"))
            started = time.monotonic()
            assert wrapper.wait_command(5) is False
            assert time.monotonic() - started < 3.0
        finally:
            controller.cleanup()

    def test_stop_reaches_controller_while_a_blend_group_is_pending(
        self, tmp_path, monkeypatch, session_controller
    ):
        """stop()/estop() are not gated on the blend barrier: a pending group
        is discarded, the controller is told at once, and the next motion
        command runs normally. Group members share one start event, and a
        read is not a step."""
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

            # A stop whose ack never arrives leaves the group possibly still
            # moving: it stays tracked, and the program is told.
            acked_stop = client.stop
            monkeypatch.setattr(client, "stop", lambda: 0)
            with pytest.raises(RuntimeError, match="not acknowledged"):
                wrapper.stop()
            assert wrapper._in_blend is True
            opened = _drain(controller, 2, timeout=0.5)
            assert [(e["event"], e["method"]) for e in opened] == [
                ("start", "move_j")
            ], "an unconfirmed stop must not close the group"
            assert opened[0]["blend"] is True
            monkeypatch.setattr(client, "stop", acked_stop)

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
            assert len(wrapper.angles()) == 6
            index = wrapper.move_j(home, duration=0.5)
            assert index >= 0 and client.wait_command(index, timeout=5.0)

        events = _drain(controller, 3)
        assert [(e["event"], e["method"]) for e in events] == [
            ("complete", "blend_group"),
            ("start", "move_j"),
            ("complete", "move_j"),
        ]
        controller.cleanup()

    def test_a_paused_jog_is_held_until_play_or_a_step(
        self, tmp_path, monkeypatch, session_controller
    ):
        """Streamed motion obeys a managed Pause: a jog issued while paused
        never reaches the controller until Play releases it. Streamed motion
        (jog, servo) is also a step like queued motion: a paused program holds
        after it until the operator steps, in the sync and the async wrapper
        alike."""
        import threading
        import time

        from parol6 import AsyncRobotClient, RobotClient

        from tests.conftest import _get_test_ports
        from waldo_commander.services.stepping_client import (
            AsyncSteppingClientWrapper,
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        controller = GUIStepController("test_jog_hold")
        controller.initialize()
        controller.signal_play()
        controller.signal_pause()
        try:
            port, _ = _get_test_ports()
            with RobotClient(host="127.0.0.1", port=port, timeout=5.0) as client:
                client.simulator(True)
                client.reset()
                assert client.home(wait=True, timeout=10.0) >= 0
                start = client.angles()
                assert start is not None
                wrapper = SteppingClientWrapper(client, StepIO("test_jog_hold"))
                errors: list[BaseException] = []

                def jog() -> None:
                    try:
                        wrapper.jog_j(0, speed=0.5, duration=0.5)
                    except BaseException as exc:
                        errors.append(exc)

                jogger = threading.Thread(target=jog, daemon=True)
                jogger.start()
                # Being held is an absence of motion, so it is watched over a window.
                window_end = time.monotonic() + 1.0
                while time.monotonic() < window_end:
                    assert jogger.is_alive(), "a jog issued while paused was dispatched"
                    angles = client.angles()
                    assert angles is not None and abs(angles[0] - start[0]) < 0.05, (
                        "the arm moved while the program was paused"
                    )
                    time.sleep(0.05)

                controller.signal_play()
                jogger.join(timeout=5.0)
                assert not jogger.is_alive(), "Play did not release the held jog"
                assert errors == []
                deadline = time.monotonic() + 3.0
                while True:
                    angles = client.angles()
                    if angles is not None and abs(angles[0] - start[0]) > 0.5:
                        break
                    assert time.monotonic() < deadline, (
                        "the released jog never moved J1"
                    )
                    time.sleep(0.05)
        finally:
            controller.cleanup()

        # One session per program, as Commander runs them: a step granted to
        # one is not a grant to the next.
        controller = GUIStepController("test_jog")
        controller.initialize()
        async_controller = GUIStepController("test_jog_async")
        async_controller.initialize()
        try:
            port, _ = _get_test_ports()
            with RobotClient(host="127.0.0.1", port=port, timeout=5.0) as client:
                client.simulator(True)
                client.reset()
                wrapper = SteppingClientWrapper(client, StepIO("test_jog"))
                jog = threading.Thread(
                    target=lambda: wrapper.jog_j(0, 0.2, 0.1), daemon=True
                )
                jog.start()
                jog.join(timeout=0.5)
                assert jog.is_alive(), "a paused program must hold after a jog"
                controller.signal_step()
                jog.join(timeout=2.0)
                assert not jog.is_alive(), "a granted step must release the jog"

            async def async_jog() -> None:
                async with AsyncRobotClient(
                    host="127.0.0.1", port=port, timeout=5.0
                ) as client:
                    wrapper = AsyncSteppingClientWrapper(
                        client, StepIO("test_jog_async")
                    )
                    task = asyncio.ensure_future(wrapper.jog_j(0, -0.2, 0.1))
                    await asyncio.sleep(0.5)
                    assert not task.done(), "a paused program must hold after a jog"
                    async_controller.signal_step()
                    await asyncio.wait_for(task, timeout=2.0)

            asyncio.run(async_jog())

            for stepped in (controller, async_controller):
                assert [(e["event"], e["method"]) for e in _drain(stepped, 2)] == [
                    ("start", "jog_j"),
                    ("complete", "jog_j"),
                ]
        finally:
            controller.cleanup()
            async_controller.cleanup()

    def test_an_earlier_blend_member_keeps_its_own_deadline(
        self, tmp_path, monkeypatch, session_controller
    ):
        """Closing a blend group enforces every member's timeout=, not only
        the last one's: an earlier member's overrun raises and stops the arm."""
        import time

        from parol6 import RobotClient

        from tests.conftest import _get_test_ports
        from waldo_commander.services.stepping_client import (
            GUIStepController,
            StepIO,
            SteppingClientWrapper,
        )

        controller = GUIStepController("test_blend_deadline")
        controller.initialize()
        controller.signal_play()
        try:
            port, _ = _get_test_ports()
            with RobotClient(host="127.0.0.1", port=port, timeout=5.0) as client:
                client.simulator(True)
                client.reset()
                assert client.home(wait=True, timeout=10.0) >= 0
                home = client.angles()
                assert home is not None
                away = [a + 10.0 for a in home]
                wrapper = SteppingClientWrapper(client, StepIO("test_blend_deadline"))
                started = time.monotonic()
                assert wrapper.move_j(away, duration=4.0, r=15, timeout=0.5) >= 0
                assert wrapper.move_j(home, duration=0.5, r=15) >= 0
                with pytest.raises(TimeoutError):
                    wrapper.move_j(home, duration=0.5)
                assert time.monotonic() - started < 2.5, (
                    "the first member's deadline was not enforced"
                )
                deadline = time.monotonic() + 3.0
                while not (client.queue() == [] and client.is_robot_stopped()):
                    assert time.monotonic() < deadline, "controller did not stop"
                    time.sleep(0.05)
        finally:
            controller.cleanup()
