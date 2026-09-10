"""Installed skills retain native motion semantics through generated Python."""

import ast
import asyncio
import textwrap
from dataclasses import asdict
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
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
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
    entries, diagnostics = library(client.skill_capabilities)
    assert not diagnostics
    approach(client, target=start, clearance_mm=2, speed=0.5)
    assert len(client.segment_collector) == 2
    native_transform = np.empty((4, 4))
    se3_from_rpy(*start.values[:3], *np.radians(start.values[3:]), native_transform)
    assert np.asarray(
        client.segment_collector[0]["points"][-1]
    ) * 1000 == pytest.approx(
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
    count = len(client.segment_collector)
    assert align_tool_axis(client, direction=tuple(aligned[:3, 2])) is None
    assert len(client.segment_collector) == count
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
    assert all(segment["is_valid"] for segment in client.segment_collector)


@pytest.mark.integration
async def test_skill_panel_inserts_fixed_calls_records_once_and_runs_via_mcp(
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
    user.find(marker="tab-skills").click()
    element("skill-choice").set_value("waldo.approach")
    await asyncio.sleep(0)
    element("skill-arg-clearance_mm").set_value(2)
    user.find(marker="skill-insert").click()
    await user.should_see(content="Inserted Python skill call")
    ast.parse(original.source)
    assert "_skill_waldo_approach(rbt, target=Pose(" in original.source
    assert "clearance_mm=2.0" in original.source
    result = await run.cpu_bound(
        _run_simulation_isolated,
        original.source,
        np.radians(START),
        dry_run_client_cls=DryRunRobotClient,
        setup_directory=str(tmp_path),
    )
    assert result["error"] is None, result["error"]
    assert len(result["segments"]) == 2
    assert np.asarray(result["segments"][-1]["points"][-1]) * 1000 == pytest.approx(
        setup.resolve("pick").values[:3], abs=0.1
    )

    element("skill-choice").set_value("waldo.retract")
    await asyncio.sleep(0)
    element("skill-arg-distance_mm").set_value(2)
    motion_recorder.toggle_recording()
    before_source = original.source
    before_pose = await client.pose()
    assert before_pose is not None
    # Two queued clicks must produce one native run and one recorded call.
    user.find(marker="skill-run").click()
    user.find(marker="skill-run").click()
    try:
        await user.should_see(content="Skill completed", retries=300)
        assert waldoctl.commander.programs.active is original
        actual = await client.pose()
        assert actual is not None
        assert np.linalg.norm(np.array(actual[:3]) - before_pose[:3]) == pytest.approx(
            2, abs=0.15
        )
        recorded = original.source[len(before_source) :]
        assert recorded.count("_skill_waldo_retract(rbt,") == 1
        assert "rbt.move_l(" not in recorded
        ast.parse(original.source)
        before_cancel = original.source
        element("skill-arg-distance_mm").set_value(20)
        element("skill-arg-speed").set_value(0.001)
        user.find(marker="skill-run").click()
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
        await user.should_see(
            content="Skill did not complete; see the program log", retries=50
        )
        assert original.source == before_cancel, (
            "a cancelled run is not recorded as success"
        )
    finally:
        if is_any_program_running():
            await script_exec.stop()
        motion_recorder.toggle_recording()

    entries, _ = library(client.skill_capabilities)
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
    user.find(marker="tab-skills").click()
    element("skill-choice").set_value("waldo.gripper_open")
    await asyncio.sleep(0)
    previous = waldoctl.commander.programs.active
    user.find(marker="skill-run").click()
    async with asyncio.timeout(30):
        while (
            waldoctl.commander.programs.active is previous or is_any_program_running()
        ):
            await asyncio.sleep(0.05)
    launched = waldoctl.commander.programs.active
    assert launched is not None
    assert script_exec.last_exit_code == 0, "\n".join(
        entry.text for entry in launched.log.entries
    )
    await user.should_see(content="Skill completed")
    after_run_once = await client.io()
    assert after_run_once is not None and after_run_once[2] == opened[2]
