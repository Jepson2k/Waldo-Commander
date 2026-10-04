"""The status chip's Waldo wears the robot's state.

Driven through the real paths on the fake-serial controller — the E-STOP
and Reset buttons, jog and home buttons, the editor's play and record
buttons, the settings switches, an MCP session taking control — and read
back off the Waldo element the page renders. How the browser draws each mood
and reaction is covered in ``test_waldo_screen.py``.
"""

import asyncio
import time
from collections.abc import Callable

import pytest
import waldoctl
from fastmcp import Client
from fastmcp.exceptions import ToolError
from nicegui import app as ng_app
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    simulate_click,
    teleport_to_jog_pose,
    wait_for_app_ready,
)
from waldo_commander.components.waldo import (
    CALM_STORAGE_KEY,
    Agent,
    Light,
    Mood,
    Reaction,
    Waldo,
)
from waldo_commander.components.script_execution import script_exec
from waldo_commander.constants import HOME_LONG_PRESS_S
from waldo_commander.mcp.server import get_mcp
from waldo_commander.services.control_lease import control_lease
from waldo_commander.state import robot_events, robot_state, ui_state


def _waldo(user: User, marker: str) -> Waldo:
    element = next(iter(user.find(marker=marker).elements))
    assert isinstance(element, Waldo)
    return element


def _record_reactions(waldo: Waldo) -> list[Reaction]:
    """Every reaction *waldo* is sent from here on, not only the latest: one
    can follow another before a check looks."""
    reactions: list[Reaction] = []
    react = waldo.react

    def recording(reaction: Reaction) -> None:
        reactions.append(reaction)
        react(reaction)

    waldo.react = recording
    return reactions


async def _wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.integration
async def test_chip_waldo_follows_estop_and_watches_the_arm_move(user: User) -> None:
    """Neutral in the simulator, alarmed for as long as an E-STOP is latched
    (with its own alarmed Waldo in the dialog), focused on the arm while it
    moves, and looking the way each jog goes while it is pressed: along the
    arm for J1, a head tilt for the wrist's roll. A new tool and its gripper
    each get a reaction."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    chip = _waldo(user, "readout-waldo")
    assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL)
    assert await _wait_for(lambda: not chip.busy), "idle arm, idle Waldo"

    looks: list[tuple[float, float, float] | None] = []
    set_look = chip.set_look

    def recording(look: tuple[float, float, float] | None) -> None:
        looks.append(look)
        set_look(look)

    chip.set_look = recording

    waldoctl.commander.settings.jog.joint_step_deg = 10.0
    await simulate_click(user, "btn-j1-plus")
    assert await _wait_for(lambda: chip.busy, timeout=5.0), (
        "Waldo should focus on the arm while the jog moves it"
    )
    assert await _wait_for(lambda: not chip.busy, timeout=10.0), (
        "Waldo should relax once the arm has settled"
    )
    assert looks == [(1.0, 0.0, 0.0), None], looks
    assert chip.look is None

    looks.clear()
    await simulate_click(user, "btn-j4-minus")
    assert await _wait_for(lambda: len(looks) == 2), looks
    assert looks == [(0.0, 0.0, -10.0), None], looks

    user.find(marker="btn-estop").click()
    assert await _wait_for(lambda: chip.mood == Mood.ALARMED)
    assert _waldo(user, "estop-waldo").mood == Mood.ALARMED

    user.find(marker="btn-estop-resume").click()
    assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL), (
        "Reset clears the E-STOP, so Waldo calms back down"
    )

    # A new tool gets a spin; its gripper closing and opening get their own.
    reactions = _record_reactions(chip)
    client = waldoctl.commander.client
    try:
        assert await client.wait_command(await client.select_tool("PNEUMATIC"), 5)
        assert await _wait_for(lambda: Reaction.TOOL in reactions), reactions
        # From open, whichever way the tool came up.
        assert await client.wait_command(await client.tool.open(), 5)
        assert await _wait_for(lambda: waldoctl.commander.status.tool.position < 0.5)
        assert await client.wait_command(await client.tool.close(), 5)
        assert await _wait_for(lambda: Reaction.GRIP_CLOSE in reactions), reactions
        assert await client.wait_command(await client.tool.open(), 5)
        assert await _wait_for(lambda: Reaction.GRIP_OPEN in reactions), reactions
    finally:
        assert await client.wait_command(await client.select_tool("NONE"), 5)


@pytest.mark.integration
async def test_chip_waldo_reacts_to_how_programs_end_and_to_new_warnings(
    user: User,
) -> None:
    """A run starts with Waldo settling in; a clean exit is celebrated and a
    crash winced at, in the chip and by the run bar's Waldo peeking over the
    bar. Waldo works along while the program runs, and a new warning or error
    gets the reaction of its severity."""
    await user.open("/")
    await wait_for_app_ready()

    chip = _waldo(user, "readout-waldo")
    peek = _waldo(user, "run-bar-waldo")
    reactions = _record_reactions(chip)
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    program = waldoctl.commander.programs.active
    assert program is not None

    async def run(source: str) -> int:
        ui_state.active_textarea.value = source
        program.source = source
        script_exec.last_exit_code = None
        user.find(marker="editor-play-btn").click()
        assert await _wait_for(lambda: script_exec.last_exit_code is not None, 15.0)
        assert script_exec.last_exit_code is not None
        return script_exec.last_exit_code

    busy_seen = False

    async def watch_busy() -> None:
        nonlocal busy_seen
        while not busy_seen:
            busy_seen = chip.busy
            await asyncio.sleep(0.02)

    watcher = asyncio.create_task(watch_busy())
    assert await run("import time\ntime.sleep(1.0)\n") == 0
    watcher.cancel()
    assert busy_seen, "Waldo should be busy while a program runs"
    assert await _wait_for(lambda: chip.last_reaction == Reaction.CELEBRATE)
    assert reactions.index(Reaction.START) < reactions.index(Reaction.CELEBRATE)
    assert await _wait_for(
        lambda: peek.last_reaction == Reaction.CELEBRATE and peek.peeked
    ), "the run bar's Waldo should peek up to cheer a clean run"
    assert await _wait_for(lambda: not chip.busy)

    assert await run("raise RuntimeError('boom')\n") != 0
    assert await _wait_for(lambda: chip.last_reaction == Reaction.OOPS)
    assert await _wait_for(lambda: peek.last_reaction == Reaction.OOPS and peek.peeked)

    # The fake-serial backend reports no warnings of its own; add one the
    # way the status consumer does when a new condition arrives. The log is
    # process-global, and an entry repeating the last one is not news.
    robot_events.clear()
    robot_events.add(code=70, title="Control loop degraded", cause="p99 over band")
    assert await _wait_for(lambda: chip.last_reaction == Reaction.WARNING)
    robot_events.add(code=12, title="Bus off", cause="CAN error", severity="error")
    assert await _wait_for(lambda: chip.last_reaction == Reaction.ERROR)


@pytest.mark.integration
async def test_chip_waldo_shrugs_at_a_joint_limit_and_rolls_its_eyes_when_homed(
    user: User,
) -> None:
    """Jogging a joint into its limit gets a shrug; a calibration homing that
    completes gets the homing reaction."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    chip = _waldo(user, "readout-waldo")
    client = ui_state.control_panel.client
    reactions = _record_reactions(chip)

    try:
        # Park J1 a step short of its upper limit, then hold J1+ into it.
        hi = float(ui_state.active_robot.joints.limits.position.deg[0, 1])
        step = abs(float(waldoctl.commander.settings.jog.joint_step_deg))
        pose = [float(a) for a in waldoctl.commander.status.joints.angles.deg]
        pose[0] = hi - step - 1.0
        assert await client.teleport(pose) == 1
        user.find(marker="btn-j1-plus").trigger("mousedown")
        assert await _wait_for(lambda: ui_state.joint_jog_timer.active, timeout=3.0), (
            "the hold never started jogging"
        )
        assert await _wait_for(
            lambda: not ui_state.joint_jog_timer.active, timeout=10.0
        ), "the panel never stopped the jog at J1's limit"
        assert await _wait_for(lambda: Reaction.SHRUG in reactions), reactions
    finally:
        user.find(marker="btn-j1-plus").trigger("mouseup")
        await teleport_to_jog_pose(client)

    assert await _wait_for(lambda: robot_state.homed, timeout=15.0)
    btn = user.find(marker="btn-home")
    reactions.clear()
    btn.trigger("pointerdown")
    deadline = time.monotonic() + HOME_LONG_PRESS_S + 3.0
    while robot_state.homed and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not robot_state.homed, "calibration never dropped the homed flag"
    btn.trigger("pointerup")
    btn.trigger("click")
    assert await _wait_for(lambda: robot_state.homed, timeout=30.0)
    assert await _wait_for(lambda: Reaction.HOME in reactions), reactions


@pytest.mark.integration
async def test_chip_waldo_lights_up_for_recording_and_follows_an_ai_session(
    user: User,
) -> None:
    """A REC light while the motion recorder runs. An MCP session lights the
    antenna tips and, holding control, drives them; a request it makes waits
    with a question on both the chip and the approval card, and the human's
    answer gets a nod or a head shake; a new control mode flashes the tips."""
    await user.open("/")
    await wait_for_app_ready()
    chip = _waldo(user, "readout-waldo")
    assert chip.light is None
    reactions = _record_reactions(chip)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    user.find(marker="editor-record-btn").click()
    assert await _wait_for(lambda: chip.light == Light.RECORDING)
    user.find(marker="editor-record-btn").click()
    assert await _wait_for(lambda: chip.light is None)

    try:
        async with Client(get_mcp()) as client:
            await client.call_tool("control.take_control")
            assert await _wait_for(lambda: chip.agent == Agent.DRIVING)
            assert chip.light is None, "the agent shows on the tips, not the bulbs"

            jog = {"joint": 0, "speed": 0.1, "duration": 0.01}
            for answer, nod in (
                ("btn-consent-allow", Reaction.NOD),
                ("btn-consent-deny", Reaction.HEADSHAKE),
            ):
                with pytest.raises(ToolError):
                    await client.call_tool("motion.jog_j", jog)
                assert await _wait_for(lambda: chip.asking), (
                    "the chip should ask while the move waits for approval"
                )
                await user.should_see(marker="approval-waldo")
                assert _waldo(user, "approval-waldo").asking
                user.find(marker=answer).click()
                assert await _wait_for(lambda: not chip.asking)
                assert await _wait_for(lambda nod=nod: chip.last_reaction == nod)
                if nod == Reaction.NOD:
                    # The retry spends the one-shot grant.
                    await client.call_tool("motion.jog_j", jog)

            user.find(marker="footer-ai-mode").click()
            assert await _wait_for(lambda: Reaction.AI_MODE in reactions), reactions

            await client.call_tool("control.release_control")
            assert await _wait_for(lambda: chip.agent == Agent.PRESENT), (
                "a connected agent that hands control back stays on the tips"
            )
    finally:
        control_lease.reset()


@pytest.mark.integration
async def test_chip_waldo_dozes_only_in_the_simulator_and_calms_on_request(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the simulator's Waldo may fall asleep — on a hardware connection a
    sleeping robot would read as "offline". The Calm Waldo setting stills it
    and every Waldo built afterwards."""
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    chip = _waldo(user, "readout-waldo")
    assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL)
    assert chip.sleep_after_s > 0

    # Leaving the simulator makes the controller open a serial port this box
    # does not have, so the backend flip is stubbed; the GUI side is real.
    async def _fake_simulator(enabled: bool) -> int:
        return 1

    monkeypatch.setattr(ui_state.control_panel.client, "simulator", _fake_simulator)
    try:
        user.find(marker="btn-robot-toggle").click()
        assert await _wait_for(lambda: chip.mood == Mood.SAD)
        assert chip.sleep_after_s == 0
        user.find(marker="btn-robot-toggle").click()
        assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL)
        assert chip.sleep_after_s > 0
    finally:
        waldoctl.commander.status.simulator_active = True
        ng_app.storage.general.pop("startup_mode", None)

    try:
        assert not chip.calm
        user.find(marker="tab-settings").click()
        await asyncio.sleep(0)
        user.find(marker="switch-calm-waldo").click()
        assert await _wait_for(lambda: chip.calm)

        # A Waldo built after the switch flips (the E-STOP dialog's) is calm too.
        user.find(marker="btn-estop").click()
        await user.should_see(marker="estop-waldo")
        assert _waldo(user, "estop-waldo").calm
        user.find(marker="btn-estop-resume").click()
        assert await _wait_for(lambda: chip.mood == Mood.NEUTRAL)
    finally:
        ng_app.storage.general.pop(CALM_STORAGE_KEY, None)
