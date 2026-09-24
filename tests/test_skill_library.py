"""Installed skills retain native motion semantics through generated Python."""

import ast
import asyncio
import re
import tempfile
import textwrap
from dataclasses import asdict
from pathlib import Path
from typing import cast

import numpy as np
import pytest
import waldoctl
from fastmcp import Client
from nicegui import run
from nicegui.testing import User
from parol6 import Robot
from parol6.client.dry_run_client import DryRunRobotClient
from pinokin import se3_from_rpy
from waldoctl.setup import Frame, Pose, PoseValues, SetupSnapshot
from waldoctl.skills import MissingCapability

from tests.helpers.mcp import payload
from tests.helpers.preview import block_end_tcp, motion_blocks
from tests.test_editor_integration import _set_selection
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.skill_library import call_source, library
from waldo_commander.setup import SetupStore
from waldo_commander.skills import (
    align_tool_axis,
    approach,
    gripper_close,
    gripper_open,
)
from waldo_commander.state import ui_state

START = [85, -85, 135, 10, 45, 170]


def pose_of(client) -> Pose:
    return Pose(cast(PoseValues, tuple(client.pose())))


def test_starter_skills_plan_fixed_setup_alignment_and_gripper_actions():
    tool = Robot().tools["PNEUMATIC"]
    client = PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient,
        initial_joints=np.radians(START),
        tool_meta_registry={
            "PNEUMATIC": {
                "motions": [{"type": "linear", **asdict(m)} for m in tool.motions],
                "activation_type": tool.activation_type.value,
            }
        },
    )
    start = pose_of(client)
    entries, diagnostics = library(client.robot)
    assert not diagnostics
    approach(client, target=start, clearance_mm=2, speed=0.5)
    moves = motion_blocks(client)
    assert len(moves) == 2
    native_transform = np.empty((4, 4))
    se3_from_rpy(*start.values[:3], *np.radians(start.values[3:]), native_transform)
    assert np.asarray(block_end_tcp(client, moves[0])[:3]) * 1000 == pytest.approx(
        native_transform[:3, 3] + 2 * native_transform[:3, 2], abs=0.1
    ), "approach clearance must follow native tool Z, including mixed rotations"
    assert pose_of(client).matrix() == pytest.approx(start.matrix(), abs=0.1)

    setup = SetupSnapshot(
        frames={"fixture": Frame(start.values)},
        poses={"park": Pose((0, 0, 2, 0, 0, 0), "fixture")},
    )
    snippet = call_source(entries["waldo.park"], {"setup": setup, "speed": 0.5})
    exec(snippet, {"rbt": client})
    assert pose_of(client).matrix() == pytest.approx(
        setup.resolve("park").matrix(), abs=0.1
    )
    before = pose_of(client).matrix()
    desired = before[:3, 2] + 0.015 * before[:3, 0]
    assert align_tool_axis(client, direction=tuple(desired), speed=0.5) is not None
    aligned = pose_of(client).matrix()
    assert aligned[:3, 3] == pytest.approx(before[:3, 3], abs=0.1)
    assert aligned[:3, 2] == pytest.approx(desired / np.linalg.norm(desired), abs=0.002)
    count = len(motion_blocks(client))
    assert align_tool_axis(client, direction=tuple(aligned[:3, 2])) is None
    assert len(motion_blocks(client)) == count
    for invalid in ((0, 0, 0), (float("nan"), 0, 1), (0, 1), (float("inf"), 0, 1)):
        with pytest.raises(ValueError, match="direction"):
            align_tool_axis(client, direction=invalid)
    with pytest.raises(ValueError, match="Resolve"):
        approach(client, target=Pose((0, 0, 0, 0, 0, 0), "fixture"))
    with pytest.raises(MissingCapability, match="Select a supported gripper"):
        gripper_open(client)

    selection = client.select_tool("PNEUMATIC")
    assert selection >= 0 and client.wait_command(selection)
    assert gripper_open(client) >= 0
    assert gripper_close(client) >= 0
    assert len(client.tool_action_collector) == 2
    assert all(block.error is None for block in motion_blocks(client))


@pytest.mark.integration
async def test_skill_form_inserts_fixed_calls_and_the_selection_runs_live(
    user: User, tmp_path, monkeypatch
):
    from waldo_commander.components.script_execution import script_exec
    from waldo_commander.mcp.server import get_mcp
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.services.motion_recorder import motion_recorder
    from waldo_commander.services.programs import is_any_program_running

    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    # Build the actual installed panel against an isolated saved setup.
    initial_preview = PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient, initial_joints=np.radians(START)
    )
    setup = SetupSnapshot(poses={"pick": pose_of(initial_preview)})
    SetupStore(tmp_path).save("bench", setup)
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    index = await client.move_j(START, speed=1)
    assert index >= 0 and await client.wait_command(index, timeout=20)

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    assert textarea is not None
    textarea.value = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    )
    original = waldoctl.commander.programs.active
    assert original is not None

    def open_skill(key: str) -> None:
        user.find(marker="editor-commands-btn").click()
        user.find(marker=f"editor-skill-{key}").click()

    def line_of(text: str) -> int:
        return next(
            number
            for number, line in enumerate(str(textarea.value).split("\n"), start=1)
            if text in line
        )

    async def run_selected(first: int, last: int) -> None:
        _set_selection(textarea, first, last)
        await asyncio.sleep(0)
        user.find(marker="editor-run-selection").click()

    async def run_finished() -> None:
        async with asyncio.timeout(30):
            while editor._running_selection or is_any_program_running():
                await asyncio.sleep(0.05)

    editor = ui_state.editor_panel
    assert editor is not None
    open_skill("waldo.approach")
    await asyncio.sleep(0)
    # A clearance far outside the workspace is refused by the planner, and the
    # form says so before the call is inserted, rather than drawing nothing and
    # leaving the refusal for the robot to deliver.
    element("skill-arg-clearance_mm").set_value(5000)
    await user.should_see(content="Cannot plan this from the current pose", retries=100)
    element("skill-arg-clearance_mm").set_value(2)
    # The configured call is drawn in the scene before it is inserted anywhere.
    scene = ui_state.urdf_scene
    assert scene is not None
    assert await wait_until(lambda: bool(scene._skill_preview_objects), timeout_s=10)
    await user.should_not_see(content="Cannot plan this from the current pose")
    user.find(marker="skill-insert").click()
    await asyncio.sleep(0)
    assert not scene._skill_preview_objects, "inserting closes the form and its path"
    await user.should_not_see(marker="skill-dialog")
    ast.parse(original.source)
    assert "_skill_waldo_approach(rbt, target=Pose(" in original.source
    assert "clearance_mm=2.0" in original.source
    result = await run.cpu_bound(
        _run_simulation_isolated,
        original.source,
        np.radians(START),
        setup_directory=str(tmp_path),
    )
    assert result["error"] is None, result["error"]
    record = result["commanded"]
    moves = [b for b in record.blocks if b.move_type is not None]
    assert len(moves) == 2
    last = moves[-1]
    assert record.tcp[last.start_row + last.rows - 1][:3] * 1000 == pytest.approx(
        setup.resolve("pick").values[:3], abs=0.1
    )

    # Inserting a call is an action in the recording: the delay before the next
    # recorded action measures from the insert, not from whatever the operator
    # last did before opening the form and composing the call.
    motion_recorder.toggle_recording()
    motion_recorder.record_action("io", port=0, state=1)
    await asyncio.sleep(0.8)
    mark = len(original.source)
    open_skill("waldo.approach")
    await asyncio.sleep(0)
    element("skill-arg-clearance_mm").set_value(2)
    user.find(marker="skill-insert").click()
    await asyncio.sleep(0.1)
    motion_recorder.record_action("io", port=0, state=0)
    composed = original.source[mark:]
    delays = [
        float(v)
        for v in re.findall(r"(?:rbt\.delay|time\.sleep)\(([0-9.]+)\)", composed)
    ]
    assert delays and max(delays) < 0.5, (
        f"the program waits out the time spent composing the call: {composed}"
    )
    motion_recorder.toggle_recording()

    # Closing the form takes its path away.
    open_skill("waldo.approach")
    await asyncio.sleep(0)
    element("skill-arg-clearance_mm").set_value(3)
    assert await wait_until(lambda: bool(scene._skill_preview_objects), timeout_s=10)
    user.find(marker="skill-close").click()
    await asyncio.sleep(0)
    assert not scene._skill_preview_objects, "closing the form takes its path away"

    # Live use of a skill is running its line: insert it while recording, then
    # run the selection. The program holds the call once, and the run's own
    # motion is not recorded a second time.
    motion_recorder.toggle_recording()
    open_skill("waldo.retract")
    await asyncio.sleep(0)
    element("skill-arg-distance_mm").set_value(2)
    user.find(marker="skill-insert").click()
    await asyncio.sleep(0)
    before_source = original.source
    before_pose = await client.pose()
    assert before_pose is not None
    call = line_of("_skill_waldo_retract(rbt,")
    try:
        # Two quick clicks are one run.
        await run_selected(call, call)
        user.find(marker="editor-run-selection").click()
        await asyncio.sleep(0.1)
        await run_finished()
        assert waldoctl.commander.programs.active is original
        assert script_exec.last_exit_code == 0
        actual = await client.pose()
        assert actual is not None
        assert np.linalg.norm(np.array(actual[:3]) - before_pose[:3]) == pytest.approx(
            2, abs=0.15
        )
        assert original.source == before_source, "a run adds nothing to the program"
        assert original.source.count("_skill_waldo_retract(rbt,") == 1
        ast.parse(original.source)

        open_skill("waldo.retract")
        await asyncio.sleep(0)
        element("skill-arg-distance_mm").set_value(20)
        element("skill-arg-speed").set_value(0.001)
        user.find(marker="skill-insert").click()
        await asyncio.sleep(0)
        slow = line_of("distance_mm=20.0")
        await run_selected(slow, slow)
        async with asyncio.timeout(10):
            while not is_any_program_running():
                await asyncio.sleep(0.05)
        assert await client.wait_status(
            lambda s: (
                s.executing_index > 0
                and s.action_state == waldoctl.ActionState.EXECUTING
            ),
            timeout=15,
        ), "the cancellation case must reach actual motion"
        await script_exec.stop()
        assert await client.wait_status(
            lambda s: s.action_state == waldoctl.ActionState.IDLE,
            timeout=2,
        ), "stopping a program must cancel its active native motion"
        await run_finished()
        assert waldoctl.commander.programs.active is original, (
            "the recording program is active again after a failed run"
        )
        before_move = original.source
        motion_recorder.record_action("move_j", angles=list(START))
        await asyncio.sleep(0)
        assert "rbt.move_j(" in original.source[len(before_move) :], (
            "recorded actions land in the recording program, not the run's"
        )
    finally:
        if is_any_program_running():
            await script_exec.stop()
        motion_recorder.toggle_recording()

    entries, _ = library(client.robot)
    snippet = call_source(
        entries["waldo.retract"], {"distance_mm": 2.0}, async_call=True
    )
    source = (
        "import asyncio\nfrom parol6 import AsyncRobotClient\n"
        "async def main():\n    async with AsyncRobotClient() as rbt:\n"
        + textwrap.indent(snippet, "        ")
        + "\nasyncio.run(main())\n"
    )
    before = await client.pose()
    assert before is not None
    set_control_mode(ControlMode.AUTOPILOT)
    try:
        async with Client(get_mcp()) as mcp:
            await mcp.call_tool("control.take_control")
            await mcp.call_tool(
                "programs.new", {"filename": "mcp_skill.py", "source": source}
            )
            await mcp.call_tool("execution.run_active")
            result = payload(
                await mcp.call_tool("execution.wait_active", {"timeout": 30})
            )
            log = payload(await mcp.call_tool("programs.get_log"))
        assert result["finished"] and result["exit_ok"], (result, log)
        actual = await client.pose()
        assert actual is not None
        assert np.linalg.norm(np.array(actual[:3]) - before[:3]) == pytest.approx(
            2, abs=0.15
        )
        assert any(
            "waldo.retract" in entry["text"] and "completed" in entry["text"]
            for entry in log
        )
    finally:
        if is_any_program_running():
            await script_exec.stop()
        control_lease.reset()

    assert await client.select_tool("PNEUMATIC") >= 0
    assert await client.wait_status(
        lambda s: s.tool_status is not None and s.tool_status.key == "PNEUMATIC"
    )
    await gripper_open.async_call(client)
    opened = await client.io()
    await gripper_close.async_call(client)
    closed = await client.io()
    assert opened is not None and closed is not None
    assert opened[2] != closed[2], "gripper skills must actuate the simulated valve"
    # A run selects the tool the arm carries, so a line that reads rbt.tool
    # runs on its own.
    waldoctl.commander.programs.switch(original.id)
    await asyncio.sleep(0)
    textarea.value = (
        "from parol6 import RobotClient\n"
        "from waldo_commander.skills import gripper_open\n"
        "with RobotClient() as rbt:\n"
        "    gripper_open(rbt)\n"
    )
    await asyncio.sleep(0)
    await run_selected(4, 4)
    await asyncio.sleep(0.1)
    await run_finished()
    assert script_exec.last_exit_code == 0, "\n".join(
        entry.text for entry in waldoctl.commander.programs.active.log.entries
    )
    after_run = await client.io()
    assert after_run is not None and after_run[2] == opened[2]


def test_a_skill_without_a_saved_setup_or_a_pose_says_what_to_do():
    """The panel surfaces these as its status text, so they have to name the
    missing thing: with no setup saved there is no name to load, and the
    store's name-format complaint says nothing about saving a setup."""
    from waldo_commander.components.skill_library import _loaded, _pose, _skill_labels

    store = SetupStore(Path(tempfile.mkdtemp()))
    with pytest.raises(ValueError, match="Save a setup"):
        _loaded(store, None)
    with pytest.raises(ValueError, match="Save a setup"):
        _loaded(store, "")
    store.save("bench", SetupSnapshot(frames={"fixture": Frame()}))
    snapshot = _loaded(store, "bench")
    with pytest.raises(ValueError, match="no poses"):
        _pose(snapshot, None)

    # Two plugins can each provide a `retract`; unqualified they are two
    # identical entries and the user cannot tell which is about to be inserted.
    labels = _skill_labels(["waldo.retract", "acme.retract", "waldo.approach"])
    assert labels["waldo.approach"] == "Approach"
    assert labels["waldo.retract"] != labels["acme.retract"]
    assert "waldo" in labels["waldo.retract"] and "acme" in labels["acme.retract"]
