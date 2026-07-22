"""Tests for opt-in I/O automation: cycle-start on Input 1 and at-home Output 2.

Digital inputs cannot be driven through the fake-serial controller (the mock
transport only echoes outputs), so the tests pulse Input 1 on the published
``commander.status.io`` surface — exactly what the watcher reads. The status
loop consumes the pulse on the next tick and then re-publishes the wire value
(low), which gives each injection the shape of a real one-tick input pulse.
The home-output path is exercised fully end-to-end: the watcher's ``write_io``
goes through the controller and is observed back on ``status.io.outputs``.
"""

import asyncio
import contextlib
import time

import pytest
import waldoctl
from nicegui import app as ng_app, ui
from nicegui.testing import User

from tests.helpers.wait import enable_sim, wait_for_app_ready
from waldo_commander.components.script_execution import script_exec
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.state import automation_state, ui_state


@pytest.fixture(autouse=True)
def _clean_automation_storage():
    """Drop persisted automation keys so they can't hydrate into later tests."""
    yield
    for key in (
        "automation/cycle_start",
        "automation/home_output",
        "automation/home_tolerance_deg",
    ):
        ng_app.storage.general.pop(key, None)


def _pulse_input_1() -> None:
    """One-tick high pulse on Input 1 (consumed by the next status tick)."""
    waldoctl.commander.status.io.inputs[0] = 1


async def _settle(seconds: float = 0.5) -> None:
    """Window for negative assertions — long enough for several status ticks."""
    await asyncio.sleep(seconds)


async def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.integration
async def test_cycle_start_input_runs_active_program(user: User) -> None:
    """Rising edge on Input 1 runs the active program; guards and the re-arm
    debounce block it."""
    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    tab = waldoctl.commander.programs.active
    assert tab is not None
    script = "print('cycle')\n"
    ui_state.active_textarea.value = script
    tab.source = script

    # Disabled (the default): a pulse must not start anything.
    _pulse_input_1()
    await _settle()
    assert not is_any_program_running()
    assert script_exec.last_exit_code is None

    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)
    user.find(marker="switch-cycle-start").click()
    await asyncio.sleep(0)
    assert automation_state.cycle_start_enabled

    # Guard: editing mode blocks the trigger (edge is consumed, not queued).
    waldoctl.commander.status.editing_mode = True
    _pulse_input_1()
    await _settle()
    assert not is_any_program_running()
    assert script_exec.last_exit_code is None
    waldoctl.commander.status.editing_mode = False

    # Guard: e-stop not clear blocks the trigger. The injected estop=0 is
    # consumed with the pulse on the same tick, then re-published clear.
    waldoctl.commander.status.io.estop = 0
    _pulse_input_1()
    await _settle()
    assert not is_any_program_running()
    assert script_exec.last_exit_code is None

    # All guards pass: the pulse starts the program and it runs to completion.
    _pulse_input_1()
    assert await _wait_for(lambda: script_exec.last_exit_code == 0, timeout=15.0), (
        "rising edge on Input 1 should run the active program to completion"
    )

    # Debounce: a fresh pulse within the re-arm window must not re-fire.
    automation_state._cycle_last_fire = time.monotonic() + 5.0
    script_exec.last_exit_code = None
    _pulse_input_1()
    await _settle()
    assert not is_any_program_running()
    assert script_exec.last_exit_code is None

    # Once the window has passed, the input is re-armed and fires again.
    automation_state._cycle_last_fire = time.monotonic() - 2.0
    _pulse_input_1()
    assert await _wait_for(lambda: script_exec.last_exit_code == 0, timeout=15.0)

    # Guard: a pulse while a program is running neither starts nor queues one.
    slow_script = "import time\ntime.sleep(1.5)\n"
    ui_state.active_textarea.value = slow_script
    tab.source = slow_script
    automation_state._cycle_last_fire = time.monotonic() - 2.0
    _pulse_input_1()
    assert await _wait_for(is_any_program_running, timeout=15.0)
    automation_state._cycle_last_fire = time.monotonic() - 2.0
    _pulse_input_1()
    assert await _wait_for(lambda: not is_any_program_running(), timeout=15.0)
    await _settle()
    assert not is_any_program_running(), "consumed mid-run pulse must not queue a run"


@pytest.mark.integration
async def test_home_output_tracks_home_pose(user: User) -> None:
    """Output 2 turns on within tolerance of home, holds through the
    hysteresis band, drops when clearly away, and writes only on transitions."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    client = ui_state.control_panel.client
    home = ui_state.active_robot.joints.home.deg
    # The io/joints sub-objects are stable (mutate-in-place invariant), but
    # io.outputs is reassigned wholesale on change — always read through io.
    io = waldoctl.commander.status.io
    angles = waldoctl.commander.status.joints.angles

    # Earlier suite tests leave the arm at arbitrary poses, and STATUS frames
    # re-publish the controller's real pose over the fixture's seeded angles —
    # the first ON transition needs the robot actually at home.
    await client.teleport(home.tolist())
    assert await _wait_for(lambda: abs(angles.deg[0] - home[0]) < 0.1)

    writes: list[tuple[int, int]] = []
    orig_write = client.write_io

    async def counting_write(index: int, value: int) -> int:
        writes.append((index, value))
        return await orig_write(index, value)

    client.write_io = counting_write  # type: ignore[method-assign]
    try:
        # Robot sits at the home pose (per-test reset homes it): enabling the
        # switch is the first ON transition, observed via the controller echo.
        settings_tab = user.find(kind=ui.tab, content="Settings")
        settings_tab.click()
        await asyncio.sleep(0)
        user.find(marker="switch-home-output").click()
        await asyncio.sleep(0)
        assert automation_state.home_output_enabled
        assert await _wait_for(lambda: io.outputs[1] == 1), (
            "Output 2 should turn on at the home pose"
        )

        # Inside the hysteresis band (tol < dist <= tol + 0.5): stays on, no
        # extra write.
        tol = automation_state.home_tolerance_deg
        near = home.tolist()
        near[0] += tol + 0.3
        await client.teleport(near)
        assert await _wait_for(lambda: abs(angles.deg[0] - near[0]) < 0.1)
        await _settle()
        assert io.outputs[1] == 1, "output should hold inside the hysteresis band"
        assert writes == [(1, 1)]

        # Clearly away: exactly one OFF transition.
        away = home.tolist()
        away[0] += tol + 5.0
        await client.teleport(away)
        assert await _wait_for(lambda: io.outputs[1] == 0), (
            "Output 2 should turn off away from home"
        )
        await _settle()
        assert writes == [(1, 1), (1, 0)], "no writes while sitting away from home"

        # Back at home: on again.
        await client.teleport(home.tolist())
        assert await _wait_for(lambda: io.outputs[1] == 1)
        assert writes == [(1, 1), (1, 0), (1, 1)]

        # Disabling the setting clears a latched-on output.
        user.find(marker="switch-home-output").click()
        await asyncio.sleep(0)
        assert await _wait_for(lambda: io.outputs[1] == 0)
        assert writes == [(1, 1), (1, 0), (1, 1), (1, 0)]
    finally:
        automation_state.home_output_enabled = False
        automation_state._home_out_on = False
        with contextlib.suppress(AttributeError):
            del client.write_io


@pytest.mark.integration
async def test_automation_settings_round_trip_storage(user: User) -> None:
    """Automation rows dual-write WC state and app.storage.general."""
    await user.open("/")
    await wait_for_app_ready()

    settings_tab = user.find(kind=ui.tab, content="Settings")
    settings_tab.click()
    await asyncio.sleep(0)

    cycle_switch = user.find(marker="switch-cycle-start")
    cycle_switch.click()
    await asyncio.sleep(0)
    assert automation_state.cycle_start_enabled is True
    assert ng_app.storage.general.get("automation/cycle_start") is True
    cycle_switch.click()
    await asyncio.sleep(0)
    assert automation_state.cycle_start_enabled is False
    assert ng_app.storage.general.get("automation/cycle_start") is False

    home_switch = user.find(marker="switch-home-output")
    home_switch.click()
    await asyncio.sleep(0)
    assert automation_state.home_output_enabled is True
    assert ng_app.storage.general.get("automation/home_output") is True
    home_switch.click()
    await asyncio.sleep(0)
    assert automation_state.home_output_enabled is False
    assert ng_app.storage.general.get("automation/home_output") is False

    tol_input = next(iter(user.find(marker="input-home-tolerance").elements))
    tol_input.set_value(5.0)
    await asyncio.sleep(0)
    assert automation_state.home_tolerance_deg == 5.0
    assert ng_app.storage.general.get("automation/home_tolerance_deg") == 5.0

    # Out-of-range and empty commits are rejected, keeping the last valid value.
    for bad in (0.05, 50.0, None):
        tol_input.set_value(bad)
        await asyncio.sleep(0)
        assert automation_state.home_tolerance_deg == 5.0
        assert ng_app.storage.general.get("automation/home_tolerance_deg") == 5.0
