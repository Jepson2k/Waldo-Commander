"""Managed completion waits use active time; backend transport stays live."""

import asyncio
import time
from typing import cast
from uuid import uuid4

import pytest
import waldoctl
from nicegui.testing import User
from waldoctl.skills import SkillError

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.services.completion_budget import (
    CompletionBudget,
    current_budget,
)
from waldo_commander.services.stepping_client import (
    AsyncSteppingClientWrapper,
    GUIStepController,
    StepIO,
)
from waldo_commander.skills._motion import completed


def test_step_ack_cannot_erase_a_newer_pause():
    """A step granted and a pause requested right behind it: the program
    takes the step, and the pause is still standing when it looks again —
    control states travel in order and only the GUI writes them."""
    controller = GUIStepController(uuid4().hex)
    controller.initialize()
    step_io = StepIO(controller.session_id)
    try:
        controller.signal_step()
        controller.signal_pause()
        step_io.wait_for_step_or_play(poll_interval=0.01)
        deadline = time.monotonic() + 1.0
        while not step_io.hold_requested() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert step_io.hold_requested(), "step acknowledgement overwrote the GUI pause"
    finally:
        controller.cleanup()


@pytest.mark.integration
async def test_editor_pause_holds_native_motion_and_managed_program(
    user: User, monkeypatch
):
    """Pause holds the controller and the program, the speed menu shows the
    live run's selection, and a Pause the controller does not confirm still
    redraws the button: pressing it again would resume a held program."""
    from waldo_commander.components.playback import playback
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.components.simulation_engine import simulation
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    start = await client.angles()
    assert start is not None
    target = list(start)
    target[0] += 8
    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = (
        "from parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        f"    rbt.move_j({target!r}, duration=2, timeout=15)\n"
        "    print('FIRST', flush=True)\n"
        "    rbt.delay(0.1)\n"
        "    print('FINISHED', flush=True)\n"
    )
    program = waldoctl.commander.programs.active
    assert program is not None
    program.source = ui_state.active_textarea.value
    await simulation.run_simulation()
    assert program.dry_run.path_segments
    assert playback._ensure_timeline() is not None
    slider = next(iter(user.find(marker="editor-scrub-slider").elements))
    try:
        user.find(marker="editor-speed-double").click()
        await asyncio.sleep(0)
        assert program.dry_run.playback.playback_speed == 2
        assert (await client.execution_speed()).resume_scale == 1
        await script_exec.start()
        assert await client.wait_status(
            lambda s: s.angles[0] > start[0] + 0.5, timeout=10
        )
        user.find(marker="editor-play-btn").click()
        async with asyncio.timeout(3):
            while program.dry_run.playback.is_playing:
                await asyncio.sleep(0.02)
        assert (await client.execution_speed()).target_scale == 0, (
            "editor Pause did not reach the controller"
        )
        user.find(marker="editor-speed-half").click()
        async with asyncio.timeout(3):
            while (await client.execution_speed()).resume_scale != 0.5:
                await asyncio.sleep(0.02)
        assert (await client.execution_speed()).target_scale == 0
        assert program.dry_run.playback.playback_speed == 2
        async with asyncio.timeout(3):
            while not (await client.execution_speed()).paused:
                await asyncio.sleep(0.02)
        await playback._refresh_execution_speed()
        assert playback._speed_2x is not None and not playback._speed_2x.visible, (
            "2x is offered for a live program"
        )
        assert playback._speed_tooltip is not None
        tooltip = playback._speed_tooltip.text
        assert "50% selected" in tooltip and "Paused" in tooltip, tooltip
        held_progress = slider.value
        await asyncio.sleep(1.0)
        assert slider.value == pytest.approx(held_progress, abs=0.03), (
            "editor progress continued through a paused motion"
        )
        assert is_any_program_running()
        assert not any(entry.text == "FIRST" for entry in program.log.entries)

        play_btn = playback.play_btn
        assert play_btn is not None
        user.find(marker="editor-play-btn").click()
        assert await wait_until(lambda: play_btn.props["icon"] == "pause", 3)
        real_pause = client.pause

        async def unconfirmed_pause(*, timeout: float = 3.0) -> int:
            return 0

        monkeypatch.setattr(client, "pause", unconfirmed_pause)
        with pytest.raises(TimeoutError):
            await playback.toggle_play()
        assert play_btn.props["icon"] == "play_arrow", (
            "a held program still shows Pause after an unconfirmed pause"
        )
        monkeypatch.setattr(client, "pause", real_pause)
        user.find(marker="editor-play-btn").click()
        async with asyncio.timeout(20):
            while is_any_program_running():
                await asyncio.sleep(0.05)
        log = [entry.text for entry in program.log.entries]
        assert script_exec.last_exit_code == 0, "\n".join(log)
        assert "FINISHED" in log
    finally:
        if is_any_program_running():
            await script_exec.stop()
        await client.resume()
        await client.set_execution_speed(1)


@pytest.mark.integration
async def test_managed_waits_are_sized_by_the_plan_not_a_default_deadline(
    user: User,
):
    """A managed move or blend group runs as long as it was planned: the
    client's standalone 10 s default is not a deadline under the wrapper.
    Each case plans just over 10 s; r=1 keeps blending from shortening the
    group below it."""
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    start = await client.angles()
    assert start is not None
    start = list(start)
    target = list(start)
    target[0] += 8
    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = (
        "from parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        f"    rbt.move_j({target!r}, duration=10.5)\n"
        "    for i in range(5):\n"
        f"        rbt.move_j({target!r} if i % 2 else {start!r}, duration=2.3, r=1, wait=False)\n"
        f"    rbt.move_j({start!r}, duration=1)\n"
        "    print('FINISHED', flush=True)\n"
    )
    program = waldoctl.commander.programs.active
    assert program is not None
    program.source = ui_state.active_textarea.value
    try:
        await script_exec.start()
        async with asyncio.timeout(90):
            while is_any_program_running():
                await asyncio.sleep(0.1)
        log = [entry.text for entry in program.log.entries]
        assert script_exec.last_exit_code == 0, "\n".join(log)
        assert "FINISHED" in log
    finally:
        if is_any_program_running():
            await script_exec.stop()


async def test_managed_completion_preserves_remaining_budget_during_pause(
    session_controller,
):
    client = session_controller.create_async_client()
    controller = GUIStepController(uuid4().hex)
    controller.initialize()
    controller.signal_play()
    managed = cast(
        waldoctl.RobotClient,
        AsyncSteppingClientWrapper(client, StepIO(controller.session_id)),
    )
    task = None
    try:
        await client.simulator(True)
        await client.reset()
        assert await client.resume() == 1
        assert await client.set_execution_speed(1) == 1
        assert await client.home(wait=True, timeout=10.0) >= 0

        # An explicit command deadline must cover the wrapper's completion
        # barrier, including when the native call uses wait=False.
        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 4
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            await managed.move_j(target, duration=2, wait=False, timeout=0.2)
        assert time.monotonic() - began < 1.5
        assert await client.queue() == []
        assert await client.wait_status(lambda s: not s.action_current, timeout=3)

        # A blend's final barrier must retain each queued command's budget.
        wrapper = cast(AsyncSteppingClientWrapper, managed)
        began = time.monotonic()
        await managed.move_j(target, duration=2, r=1, timeout=0.2)
        with pytest.raises(TimeoutError):
            await wrapper.finalize()
        assert time.monotonic() - began < 1.5
        assert await client.queue() == []
        assert await client.wait_status(lambda s: not s.action_current, timeout=3)

        from parol6 import RobotClient

        from tests.conftest import _get_test_ports
        from waldo_commander.services.stepping_client import SteppingClientWrapper

        def sync_timeout():
            port, _ = _get_test_ports()
            with RobotClient(host="127.0.0.1", port=port) as sync_client:
                wrapped = SteppingClientWrapper(
                    sync_client, StepIO(controller.session_id)
                )
                with pytest.raises(TimeoutError):
                    wrapped.move_j(target, duration=2, timeout=0.2)

        await asyncio.to_thread(sync_timeout)
        assert await client.queue() == []
        assert await client.wait_status(lambda s: not s.action_current, timeout=3)

        # A skill invoked while already paused must bind its budget before
        # waiting at the dispatch gate. Dwell avoids profile-transition timing.
        controller.signal_pause()
        assert await client.pause() == 1
        task = asyncio.create_task(completed(managed, managed.delay(0.2), 1.0, "Dwell"))
        await asyncio.sleep(1.2)  # longer than the declared completion budget
        assert not task.done(), "intentional pause consumed the managed timeout"
        assert await client.ping() is not None
        assert await client.resume() == 1
        controller.signal_play()
        assert await asyncio.wait_for(task, 5) >= 0

        # A command the controller has already finished is complete however
        # late the wrapper checks it: asking is what decides, not the clock.
        # Reporting it as a timeout here would stop the arm mid-program.
        index = await client.delay(0.05)
        assert index >= 0 and await client.wait_command(index, timeout=3)
        spent = CompletionBudget(0.2)
        spent.bind(client, time.monotonic)
        await asyncio.sleep(0.3)  # the budget runs out before the wrapper looks
        assert spent.remaining == 0
        token = current_budget.set(spent)
        try:
            assert await managed.wait_command(index, timeout=0.2), (
                "a finished command was reported as a timeout because the "
                "budget had run out before the wrapper looked"
            )
        finally:
            current_budget.reset(token)

        # The skill budget includes dispatch through the stepping wrapper.
        began = time.monotonic()
        with pytest.raises(SkillError, match="completion"):
            await completed(managed, managed.delay(2), 0.2, "Dwell")
        assert time.monotonic() - began < 1.5

        # A paused completion wait must still notice a controller fault.
        task = asyncio.create_task(managed.delay(10))
        assert await client.wait_status(lambda s: bool(s.action_current), timeout=3)
        controller.signal_pause()
        assert await client.pause() == 1
        await client.estop()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, 3)
    finally:
        controller.signal_play()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.stop()
        await client.reset()
        await client.resume()
        await client.close()
        controller.cleanup()


@pytest.mark.integration
async def test_watcher_or_launch_failure_leaves_no_motion_running(
    user: User, monkeypatch, caplog
):
    """A step watcher that can no longer read the program stops it and its
    native queue. Starting a program resumes the controller: it refuses while
    a native pause holds a move the run did not queue, and a launch that
    fails after resuming stops what it released."""
    from waldo_commander.components import script_execution
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.services.script_runner import stop_script
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = (
        "from parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        "    rbt.delay(20)\n"
        "    print('UNEXPECTED CONTINUATION', flush=True)\n"
    )
    handles = []
    try:
        await script_exec.start()
        handle = script_exec.script_handle
        assert handle is not None
        handles.append(handle)
        assert await client.wait_status(lambda s: bool(s.action_current), timeout=10)

        def unreadable_events(_client):
            raise OSError("test: event channel became unreadable")

        with monkeypatch.context() as patched:
            patched.setattr(script_exec, "_consume_script_events", unreadable_events)
            async with asyncio.timeout(5):
                while is_any_program_running():
                    await asyncio.sleep(0.02)
        assert handle["proc"].returncode is not None, "watcher left the program running"
        assert await client.queue() == []
        assert await client.wait_status(lambda s: not s.action_current, timeout=3)
        expected = "Error in event watcher: test: event channel became unreadable"
        records = caplog.get_records("call")
        assert any(r.getMessage() == expected for r in records)
        records[:] = [r for r in records if r.getMessage() != expected]

        start = await client.angles()
        assert start is not None
        target = list(start)
        target[0] += 8
        ui_state.active_textarea.value = (
            "from parol6 import RobotClient\n"
            "with RobotClient() as rbt:\n"
            f"    rbt.move_j({target!r}, duration=4, timeout=15)\n"
        )
        real_run_script = script_execution.run_script

        async def run_script_then_fail(*args, **kwargs):
            handle = await real_run_script(*args, **kwargs)
            handles.append(handle)
            assert await client.wait_status(
                lambda s: bool(s.action_current), timeout=10
            )
            raise RuntimeError("test: launch failed after the program moved")

        assert await client.pause() == 1
        assert await client.move_j(target, duration=1) >= 0
        assert await client.wait_status(lambda s: s.queued_duration > 0, timeout=3)
        await script_exec.start()
        assert not is_any_program_running()
        assert (await client.execution_speed()).target_scale == 0, (
            "starting a program released a move it did not queue"
        )
        assert await client.wait_status(lambda s: s.queued_duration > 0, timeout=1)
        await client.stop()
        await client.resume()
        assert await client.wait_status(lambda s: not s.action_current, timeout=3)

        monkeypatch.setattr(script_execution, "run_script", run_script_then_fail)
        await script_exec.start()
        assert len(handles) == 2, "the program was never spawned"
        assert not is_any_program_running()
        assert await client.wait_status(
            lambda s: s.queued_duration < 1e-3 and not s.action_current, timeout=1
        ), "a failed launch left its motion running"
        expected = [
            r
            for r in caplog.get_records("call")
            if r.getMessage().startswith("Failed to start script")
        ]
        assert len(expected) == 2
        records = caplog.get_records("call")
        records[:] = [r for r in records if r not in expected]
    finally:
        for handle in handles:
            if handle["proc"].returncode is None:
                await stop_script(handle)
        if is_any_program_running():
            await script_exec.stop()
        await client.stop()
        await client.resume()
        script_exec._reset_state()


@pytest.mark.integration
async def test_failed_program_keeps_stop_available_until_controller_confirms(
    user: User, tmp_path, monkeypatch, caplog
):
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    start = await client.angles()
    assert start is not None
    target = list(start)
    target[0] += 8
    release = tmp_path / "fail-program"
    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = (
        "import os, time\nfrom pathlib import Path\nfrom parol6 import RobotClient\n"
        "with RobotClient() as rbt:\n"
        f"    rbt.move_j({target!r}, duration=6, r=1, timeout=15)\n"
        "    if os.environ.get('WALDO_STEP_SESSION'):\n"
        f"        while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "        raise RuntimeError('test program failed with queued motion')\n"
    )
    real_stop = client.stop

    async def unavailable_stop():
        raise ConnectionError("test stop acknowledgement unavailable")

    handle = None
    try:
        # A Run pressed while the controller still holds a queued command
        # waits for it to drain instead of refusing.
        assert await client.delay(0.5) >= 0
        await script_exec.start()
        handle = script_exec.script_handle
        assert handle is not None
        assert await client.wait_status(
            lambda s: s.angles[0] > start[0] + 0.2, timeout=10
        )
        monkeypatch.setattr(client, "stop", unavailable_stop)
        release.touch()
        async with asyncio.timeout(5):
            while script_exec.last_exit_code is None:
                await asyncio.sleep(0.01)
        assert is_any_program_running(), "failed stop discarded execution tracking"
        await user.should_see("Controller stop is unconfirmed", retries=50)
        assert script_exec.script_handle is handle
        monkeypatch.setattr(client, "stop", real_stop)
        await script_exec.stop()
        assert not is_any_program_running()
        # The stop discarded the program's 6 s move; the page may still be
        # fitting its tool through the queue, which drains at once.
        async with asyncio.timeout(2):
            while await client.queue():
                await asyncio.sleep(0.05)
        assert await client.wait_status(lambda s: not s.action_current, timeout=3)
        records = caplog.get_records("call")
        # The controller's failure is logged where the stop was sent; the run
        # reports the stop it could not confirm.
        assert any(
            "test stop acknowledgement unavailable" in r.getMessage() for r in records
        )
        expected = [
            r for r in records if "Controller stop is unconfirmed" in r.getMessage()
        ]
        assert expected
        records[:] = [r for r in records if r not in expected]
    finally:
        release.touch()
        monkeypatch.setattr(client, "stop", real_stop)
        if is_any_program_running():
            await script_exec.stop()
        await client.stop()
