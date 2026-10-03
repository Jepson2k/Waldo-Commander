"""Installed skills retain native motion semantics through generated Python,
and go into a program as a call whose arguments are fields."""

import ast
import asyncio
import re
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
from waldoctl.setup import Frame, Parameter, Pose, PoseValues, SetupSnapshot
from waldoctl.signals import DigitalSignal
from waldoctl.skills import MissingCapability, skill

from tests.helpers.mcp import payload
from tests.helpers.preview import block_end_tcp, motion_blocks
from tests.test_editor_integration import (
    _fire_editor_event,
    _set_cursor_line,
    _set_selection,
)
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.components.skill_library import _skill_labels
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.python_source import (
    in_async_scope,
    missing_statements,
    preamble_statements,
    program_setup,
)
from waldo_commander.services.skill_library import (
    SETUP_IMPORT,
    SkillEntry,
    arguments_from_call,
    call_source,
    call_template,
    field_at,
    library,
    parse_skill_call,
    replace_argument,
)
from waldo_commander.setup import SetupStore
from waldo_commander.skills import (
    align_tool_axis,
    approach,
    gripper_close,
    gripper_open,
    retract,
    transfer,
    transfer_with_signal,
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
async def test_skill_fields_teach_preview_record_and_run_live(
    user: User, tmp_path, monkeypatch
):
    from waldo_commander.components.editor_decorations import decorations
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
    initial_preview = PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient, initial_joints=np.radians(START)
    )
    pick = pose_of(initial_preview)
    place = Pose(
        cast(PoseValues, (*pick.values[:2], pick.values[2] + 2, *pick.values[3:]))
    )
    SetupStore(tmp_path).save(
        "bench", SetupSnapshot(poses={"pick": pick, "place": place})
    )
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

    user.find(marker="tab-setup").click()
    element("setup-saved").set_value("bench")
    await user.should_see(content="Loaded bench")
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    # What the side tabs report when the program column opens: flashes land
    # on the lines rather than on the tab.
    ui_state.program_panel_visible = True
    textarea = ui_state.active_textarea
    assert textarea is not None
    base = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    textarea.value = base
    original = waldoctl.commander.programs.active
    assert original is not None
    editor = ui_state.editor_panel
    assert editor is not None
    scene = ui_state.urdf_scene
    assert scene is not None

    def lines() -> list[str]:
        return str(ui_state.active_textarea.value).split("\n")

    def line_of(text: str) -> int:
        return next(
            number for number, line in enumerate(lines(), start=1) if text in line
        )

    def place_cursor(line: int, after: str) -> None:
        """Click just after *after* on *line*."""
        column = lines()[line - 1].index(after) + len(after) + 1
        area = ui_state.active_textarea
        _fire_editor_event(area, "focus-change", {"focused": True})
        _fire_editor_event(
            area,
            "selection-change",
            {
                "line": line,
                "column": column,
                "from_line": line,
                "to_line": line,
                "empty": True,
            },
        )

    def edit_line(line: int, old: str, new: str) -> None:
        rows = lines()
        assert old in rows[line - 1], rows[line - 1]
        rows[line - 1] = rows[line - 1].replace(old, new)
        ui_state.active_textarea.value = "\n".join(rows)

    def open_skill(key: str) -> None:
        user.find(marker="editor-commands-btn").click()
        user.find(marker=f"editor-skill-{key}").click()

    def strip_line() -> int:
        strip = editor.skill_strip(waldoctl.commander.programs.active_id)
        assert strip is not None and strip.call is not None
        return strip.line

    async def run_selected(first: int, last: int) -> None:
        _set_selection(ui_state.active_textarea, first, last)
        await asyncio.sleep(0)
        user.find(marker="editor-run-selection").click()

    async def run_finished() -> None:
        async with asyncio.timeout(30):
            while editor._running_selection or is_any_program_running():
                await asyncio.sleep(0.05)

    # The skill goes in as its call, every argument a field and the setup's
    # poses by name; the imports and the setup load go at the top.
    _set_cursor_line(textarea, 3)
    open_skill("waldo.approach")
    await asyncio.sleep(0)
    call = line_of("_skill_waldo_approach(rbt,")
    assert lines()[:4] == [
        "from waldo_commander.skills.motion import approach as _skill_waldo_approach",
        "from waldo_commander.setup import load_setup",
        'setup = load_setup("bench")',
        "",
    ]
    assert lines()[call - 1] == (
        '    _skill_waldo_approach(rbt, target=setup.resolve("pick"), '
        "clearance_mm=30.0, speed=0.2, timeout=30.0)"
    ), "the call goes in below the cursor, inside the block"
    assert any(call in flashed for _, flashed in decorations._active_flashes), (
        "the call line flashes like any other insert"
    )
    assert element("skill-strip-title").text == "Approach"
    assert element("skill-strip-field").text == "Target"
    assert element("skill-strip-names").options == ["pick", "place"]
    assert element("skill-strip-names").value == "pick"

    # The call is planned from its text: a clearance far outside the
    # workspace is refused and the strip says so; a reachable one is drawn.
    edit_line(call, "clearance_mm=30.0", "clearance_mm=5000")
    await user.should_see(content="Cannot plan this from the current pose", retries=100)
    edit_line(call, "clearance_mm=5000", "clearance_mm=2.0")
    assert await wait_until(lambda: bool(scene._skill_preview_objects), timeout_s=10)
    await user.should_not_see(content="Cannot plan this from the current pose")
    # A temporarily incomplete call keeps its fields but loses its old path.
    edit_line(call, "timeout=30.0)", "timeout=30.0")
    await asyncio.sleep(0)
    assert not scene._skill_preview_objects
    await asyncio.sleep(0.4)
    assert not scene._skill_preview_objects
    edit_line(call, "timeout=30.0", "timeout=30.0)")
    assert await wait_until(lambda: bool(scene._skill_preview_objects), timeout_s=10)
    user.find(marker="program-panel-close").click()
    assert not scene._skill_preview_objects
    user.find(marker="tab-program").click()
    ui_state.program_panel_visible = True
    place_cursor(call, "clearance_mm=")
    assert element("skill-strip-field").text == "Clearance"

    # Choosing another of the setup's poses writes its reference.
    place_cursor(call, "target=")
    element("skill-strip-names").set_value("place")
    assert 'target=setup.resolve("place"), clearance_mm=2.0' in lines()[call - 1]
    ast.parse(original.source)
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
        place.values[:3], abs=0.1
    )

    # Teach now saves where the arm is to the program's setup, writes its
    # reference into the field, and the editor completes the new name.
    element("skill-strip-teach-name").set_value("drop")
    user.find(marker="skill-strip-teach").click()
    assert await wait_until(
        lambda: 'target=setup.resolve("drop")' in lines()[call - 1], timeout_s=5
    ), lines()[call - 1]
    here = await client.pose()
    assert here is not None
    taught = SetupStore(tmp_path).load("bench")
    assert taught.resolve("drop").values[:3] == pytest.approx(here[:3], abs=0.1)
    assert "place" in taught.poses, "teaching keeps the setup's other poses"
    assert any(
        item["label"] == 'setup.resolve("drop")' for item in textarea.completions
    )
    assert element("skill-strip-names").value == "drop"

    # The already loaded panel follows the strip's save; saving its fields
    # must retain the pose just taught by the strip.
    user.find(marker="tab-setup").click()
    user.find(marker="setup-save").click()
    await user.should_see(content="Saved bench")
    assert "drop" in SetupStore(tmp_path).read_literal("bench").poses
    user.find(marker="tab-program").click()
    ui_state.program_panel_visible = True
    place_cursor(call, "target=")

    # A new frame is saved where the arm is, and a pose taught in it is
    # stored relative to it.
    element("skill-strip-new-frame-name").set_value("tray")
    user.find(marker="skill-strip-new-frame").click()
    assert await wait_until(
        lambda: "tray" in SetupStore(tmp_path).load("bench").frames, timeout_s=5
    )
    assert element("skill-strip-teach-frame").value == "tray"
    element("skill-strip-teach-name").set_value("slot")
    user.find(marker="skill-strip-teach").click()
    assert await wait_until(
        lambda: 'target=setup.resolve("slot")' in lines()[call - 1], timeout_s=5
    )
    slot = SetupStore(tmp_path).load("bench").poses["slot"]
    assert slot.frame == "tray"
    assert slot.values == pytest.approx((0, 0, 0, 0, 0, 0), abs=0.1)

    # A selection that refers to the setup runs with the program's setup load.
    try:
        await run_selected(call, call)
        await asyncio.sleep(0.1)
        await run_finished()
        assert script_exec.last_exit_code == 0, "\n".join(
            entry.text for entry in waldoctl.commander.programs.active.log.entries
        )
        assert waldoctl.commander.programs.active is original
        arrived = await client.pose()
        assert arrived is not None
        assert np.asarray(arrived[:3]) == pytest.approx(
            SetupStore(tmp_path).load("bench").resolve("slot").values[:3], abs=0.2
        )
    finally:
        if is_any_program_running():
            await script_exec.stop()

    # More skills add only what the program lacks.
    place_cursor(call, "timeout=")
    open_skill("waldo.approach")
    await asyncio.sleep(0)
    open_skill("waldo.retract")
    await asyncio.sleep(0)
    source = str(textarea.value)
    ast.parse(source)
    assert source.count("import approach as _skill_waldo_approach") == 1
    assert source.count("import retract as _skill_waldo_retract") == 1
    assert source.count(SETUP_IMPORT) == 1
    assert source.count("load_setup(") == 1
    assert source.count("_skill_waldo_approach(rbt,") == 2
    assert "setup.resolve" not in lines()[strip_line() - 1], (
        "retract takes nothing from the setup"
    )
    call = line_of('target=setup.resolve("slot")')

    # While recording, the prelude is staged with the call, and Undo takes
    # both out again.
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0)
    scratch = ui_state.active_textarea
    assert scratch is not None and scratch is not textarea
    scratch.value = base
    _set_cursor_line(scratch, 3)
    motion_recorder.toggle_recording()
    try:
        open_skill("waldo.approach")
        await asyncio.sleep(0)
        written = str(scratch.value)
        assert 'setup = load_setup("bench")' in written
        assert "_skill_waldo_approach(rbt," in written
        session = motion_recorder.session
        assert session is not None and any(b.first_line == 1 for b in session.blocks)
        user.find(marker="staged-undo").click()
        assert await wait_until(lambda: str(scratch.value) == base, timeout_s=2), (
            scratch.value
        )
    finally:
        if motion_recorder.session is not None:
            motion_recorder.undo()
    editor._switch_to_tab(original.id)
    await asyncio.sleep(0)
    assert ui_state.active_textarea is textarea

    # Inserting a call is an action in the recording and filling in its
    # fields is composing it: neither is time the program then waits.
    place_cursor(call, "timeout=")
    motion_recorder.toggle_recording()
    motion_recorder.record_action("io", port=0, state=1)
    await asyncio.sleep(0.8)
    open_skill("waldo.retract")
    await asyncio.sleep(0)
    composed = strip_line()
    await asyncio.sleep(0.6)
    edit_line(composed, "distance_mm=30.0", "distance_mm=2.0")
    await asyncio.sleep(0.1)
    motion_recorder.record_action("io", port=0, state=0)
    delays = [
        float(v)
        for v in re.findall(
            r"(?:rbt\.delay|time\.sleep)\(([0-9.]+)\)", str(textarea.value)
        )
    ]
    assert delays and max(delays) < 0.5, (
        f"the program waits out the time spent composing the call: {textarea.value}"
    )
    motion_recorder.toggle_recording()

    # The path goes with the cursor off the call.
    assert await wait_until(lambda: bool(scene._skill_preview_objects), timeout_s=10)
    place_cursor(line_of("with RobotClient() as rbt:"), "with")
    assert not scene._skill_preview_objects
    await user.should_not_see(marker="skill-strip-title")

    # Live use of a skill is running its line: insert it while recording,
    # then run the selection. The run's own motion is not recorded again.
    motion_recorder.toggle_recording()
    open_skill("waldo.retract")
    await asyncio.sleep(0)
    live = strip_line()
    edit_line(live, "distance_mm=30.0", "distance_mm=2.0")
    before_source = original.source
    before_pose = await client.pose()
    assert before_pose is not None
    try:
        # Two quick clicks are one run.
        await run_selected(live, live)
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
        ast.parse(original.source)

        open_skill("waldo.retract")
        await asyncio.sleep(0)
        slow = strip_line()
        edit_line(slow, "distance_mm=30.0, speed=0.2", "distance_mm=20.0, speed=0.001")
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
        assert (
            original.source.count("rbt.move_j(") == before_move.count("rbt.move_j(") + 1
        ), "recorded actions land in the recording program, not the run's"
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


@skill(id="test.labelled", version="1.0.0")
async def labelled(
    rbt, *, label: str = "{x}", prefix: str = "a{", target: Pose
) -> None:
    """A skill whose defaults hold snippet braces."""


def test_skill_calls_are_written_as_fields_and_read_back_from_their_line():
    entries = {
        candidate.spec.id: SkillEntry(candidate)
        for candidate in (transfer, transfer_with_signal, retract, labelled)
    }
    # With no setup to read, a setup field names its argument, so the call
    # says what to teach; the fields are numbered in signature order.
    template, plain, fields = call_template(entries["waldo.transfer"], None, False)
    assert plain == (
        '_skill_waldo_transfer(rbt, pick=setup.resolve("pick"), '
        'place=setup.resolve("place"), clearance_mm=30.0, speed=0.2, timeout=30.0)'
    )
    assert fields == ["pick", "place", "clearance_mm", "speed", "timeout"]
    assert template == (
        '_skill_waldo_transfer(rbt, pick=${1:setup.resolve("pick")}, '
        'place=${2:setup.resolve("place")}, clearance_mm=${3:30.0}, '
        "speed=${4:0.2}, timeout=${5:30.0})"
    )
    # A setup without a pose of the argument's name starts on its first pose,
    # under the name the program loads it as; inside async code it is awaited.
    tray = SetupSnapshot(poses={"tray": Pose((1, 2, 3, 0, 0, 0))})
    _, plain, _ = call_template(
        entries["waldo.transfer"], tray, True, setup_variable="cell"
    )
    assert plain.startswith(
        "await _skill_waldo_transfer.async_call(rbt, "
        'pick=cell.resolve("tray"), place=cell.resolve("tray"),'
    )
    # A closing brace cannot sit inside a snippet field, so that argument stays
    # plain text; literal braces are escaped either way.
    template, plain, fields = call_template(entries["test.labelled"], tray, False)
    assert fields == ["prefix", "target"]
    assert template == (
        "_skill_test_labelled(rbt, label='\\{x\\}', prefix=${1:'a\\{'}, "
        'target=${2:setup.resolve("tray")})'
    )
    assert plain == (
        "_skill_test_labelled(rbt, label='{x}', prefix='a{', "
        'target=setup.resolve("tray"))'
    )

    # Argument spans are str indices, past characters UTF-8 spells in 4 bytes.
    line = '    _skill_test_labelled(rbt, label="🙂🙂", prefix="x", target=setup.resolve("tray"))'
    call = parse_skill_call(line, entries)
    assert call is not None and call.key == "test.labelled"
    assert {name: line[a:b] for name, (a, b) in call.arguments.items()} == {
        "label": '"🙂🙂"',
        "prefix": '"x"',
        "target": 'setup.resolve("tray")',
    }
    assert field_at(call, line.index("prefix=") + len("prefix=")) == "prefix"
    assert field_at(call, line.index("rbt")) is None
    assert replace_argument(line, call, "prefix", '"y"') == line.replace(
        'prefix="x"', 'prefix="y"'
    )
    bare = "_skill_waldo_retract(rbt)"
    bare_call = parse_skill_call(bare, entries)
    assert bare_call is not None
    assert replace_argument(bare, bare_call, "distance_mm", "2.0") == (
        "_skill_waldo_retract(rbt, distance_mm=2.0)"
    )
    assert parse_skill_call("retract(rbt, distance_mm=2.0)", entries) is None

    # The preview reads the call's values without running any of it.
    setup = SetupSnapshot(
        frames={"fixture": Frame((10, 0, 0, 0, 0, 0))},
        poses={"pick": Pose((0, 0, 5, 0, 0, 0), "fixture")},
        parameters={"clearance": Parameter(2.0, "mm")},
        signals={"grip": DigitalSignal("parol6", "output", 0, 2, 2)},
    )
    written = (
        '_skill_waldo_transfer_with_signal(rbt, pick=setup.resolve("pick"), '
        'place=Pose((1, 2, 3, 0, 0, 0)), grip=setup.signals["grip"], '
        'clearance_mm=setup.parameters["clearance"].value, speed=-0.5)'
    )
    parsed = parse_skill_call(written, entries)
    assert parsed is not None
    assert arguments_from_call(parsed, setup) == {
        "pick": setup.resolve("pick"),
        "place": Pose((1, 2, 3, 0, 0, 0)),
        "grip": setup.signals["grip"],
        "clearance_mm": 2.0,
        "speed": -0.5,
    }
    for text, message in (
        ('pick=open("pwned")', "fixed values"),
        ('pick=setup.resolve("drop")', "drop"),
        ('grip=setup.signals["valve"]', "valve"),
    ):
        refused = parse_skill_call(
            f"_skill_waldo_transfer_with_signal(rbt, {text})", entries
        )
        assert refused is not None
        with pytest.raises(ValueError, match=message):
            arguments_from_call(refused, setup)
    for text in ("*poses", "**options", "pick=Pose(*coordinates)"):
        expanded = parse_skill_call(
            f"_skill_waldo_transfer_with_signal(rbt, {text})", entries
        )
        assert expanded is not None
        with pytest.raises(ValueError, match="fixed values"):
            arguments_from_call(expanded, setup)
    with pytest.raises(ValueError, match="loads no setup"):
        arguments_from_call(parsed, None)

    # A missing statement brings along an import it uses even when the
    # program holds that import further down, below where the prelude goes.
    program = "from parol6 import RobotClient\n" + SETUP_IMPORT + "\n"
    load = 'setup = load_setup("bench")'
    assert missing_statements(program, [SETUP_IMPORT, load]) == [SETUP_IMPORT, load]
    assert (
        missing_statements(
            program + "setup = load_setup('bench')\n", [SETUP_IMPORT, load]
        )
        == []
    )
    # Inside an async def, its header included, a call is awaited; in a sync
    # def nested in it, and at module level, it is not.
    source = (
        "import asyncio\nasync def main():\n    async with Client() as rbt:\n"
        "        pass\n    def helper():\n        pass\nasyncio.run(main())\n"
    )
    assert [in_async_scope(source, line) for line in range(1, 8)] == [
        False,
        True,
        True,
        True,
        False,
        False,
        False,
    ]
    # Lines lifted out of a program keep its imports and setup load.
    program = (
        "from parol6 import RobotClient\n" + SETUP_IMPORT + "\n"
        "with RobotClient() as rbt:\n    cell = load_setup('bench')\n    pass\n"
    )
    assert program_setup(program) == ("cell", "bench", None)
    assert preamble_statements(program) == [
        "from parol6 import RobotClient",
        SETUP_IMPORT,
    ]

    # Two plugins can each provide a `retract`; unqualified they are two
    # identical entries and the user cannot tell which is about to be inserted.
    labels = _skill_labels(["waldo.retract", "acme.retract", "waldo.approach"])
    assert labels["waldo.approach"] == "Approach"
    assert labels["waldo.retract"] != labels["acme.retract"]
    assert "waldo" in labels["waldo.retract"] and "acme" in labels["acme.retract"]


@pytest.mark.integration
async def test_a_skill_preview_leaves_the_app_collision_world_alone(
    user: User, monkeypatch: pytest.MonkeyPatch
):
    """A skill is planned in the preview worker, never in the app: a skill that
    edits the world must not leave its shapes in the app's own collision
    checker, which mirrors the controller's world."""
    import importlib.metadata

    import parol6.PAROL6_ROBOT as PAROL6_ROBOT
    import waldoctl.skills

    real_entry_points = waldoctl.skills.entry_points
    fence = importlib.metadata.EntryPoint(
        name="fence_then_retract",
        value="tests.helpers.preview_skills:fence_then_retract",
        group="waldoctl.skills",
    )

    def entry_points(*, group: str):
        found = list(real_entry_points(group=group))
        return [*found, fence] if group == "waldoctl.skills" else found

    monkeypatch.setattr(waldoctl.skills, "entry_points", entry_points)
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    index = await client.move_j(START, speed=1)
    assert index >= 0 and await client.wait_command(index, timeout=20)
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    # User does not execute the panel visibility report sent by browser JS.
    ui_state.program_panel_visible = True
    scene = ui_state.urdf_scene
    assert scene is not None
    world = PAROL6_ROBOT.program_shapes()
    try:
        user.find(marker="editor-commands-btn").click()
        user.find(marker="editor-skill-test.fence_then_retract").click()
        assert await wait_until(
            lambda: bool(scene._skill_preview_objects), timeout_s=30
        ), "the skill was never planned"
        assert [shape.name for shape in PAROL6_ROBOT.program_shapes()] == [
            shape.name for shape in world
        ]
        user.find(marker="program-panel-close").click()
    finally:
        PAROL6_ROBOT.apply_shapes(world)


@pytest.mark.integration
async def test_teaching_uses_the_setup_in_scope_and_its_directory(
    user: User, tmp_path, monkeypatch
):
    local_dir, remote_dir = tmp_path / "local", tmp_path / "remote"
    monkeypatch.setenv("WALDO_SETUP_DIR", str(local_dir))
    local = SetupStore(local_dir)
    remote = SetupStore(remote_dir)
    local.save("bench", SetupSnapshot(poses={"local_pick": Pose((1, 2, 3, 0, 0, 0))}))
    remote.save("bench", SetupSnapshot(poses={"remote_pick": Pose((4, 5, 6, 0, 0, 0))}))
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    lines = [
        "from waldo_commander.setup import load_setup",
        "from waldo_commander.skills.motion import approach as _skill_waldo_approach",
        "from parol6 import RobotClient",
        'cell = load_setup("bench")',
        f'cell = load_setup("bench", directory={str(remote_dir)!r})',
        "with RobotClient() as rbt:",
        '    _skill_waldo_approach(rbt, target=cell.resolve("remote_pick"))',
    ]
    textarea.value = "\n".join(lines)
    _fire_editor_event(textarea, "focus-change", {"focused": True})
    _fire_editor_event(
        textarea,
        "selection-change",
        {
            "line": 7,
            "column": lines[-1].index("target=") + len("target=") + 1,
            "from_line": 7,
            "to_line": 7,
            "empty": True,
        },
    )
    await asyncio.sleep(0)

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    assert element("skill-strip-names").options == ["remote_pick"]
    element("skill-strip-teach-name").set_value("taught")
    user.find(marker="skill-strip-teach").click()
    assert await wait_until(lambda: "taught" in remote.read_literal("bench").poses, 5)
    assert "taught" not in local.read_literal("bench").poses
    assert 'cell.resolve("taught")' in str(textarea.value)
