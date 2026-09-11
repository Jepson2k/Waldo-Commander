"""A saved fixture is shared by teaching, ordinary Python and native preview."""

import asyncio
import json
from typing import cast

import numpy as np
import pytest
import waldoctl
from nicegui import run, ui
from nicegui.testing import User
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl.setup import Frame, Pose, PoseValues, SetupSnapshot

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
)
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.setup import SetupStore, export_snapshot, load_setup
from waldo_commander.state import ui_state


def test_named_storage_and_export_remain_independent_snapshots(tmp_path):
    store = SetupStore(tmp_path)
    original = SetupSnapshot(
        frames={"fixture": Frame((10, 20, 30, 0, 0, 90))},
        poses={"pick": Pose((5, 0, 0, 0, 0, 0), "fixture")},
    )
    store.save("bench", original)
    loaded = load_setup("bench", directory=tmp_path)
    namespace = {}
    exec(export_snapshot(loaded), namespace)
    store.save("bench", original.with_frame("fixture", Frame((40, 20, 30, 0, 0, 90))))
    assert SetupStore(tmp_path).load("bench").resolve("pick").values[
        :3
    ] == pytest.approx((40, 25, 30))
    assert loaded.resolve("pick").values[:3] == pytest.approx((10, 25, 30))
    assert namespace["setup"].resolve("pick").values[:3] == pytest.approx((10, 25, 30))
    assert store.names() == ["bench"]
    for bad_name in ("../outside", "", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="Names"):
            store.save(bad_name, original)
    corrupt = original.to_dict()
    corrupt["frames"]["fixture"]["parent"] = "missing"
    (tmp_path / "broken.json").write_text(json.dumps(corrupt))
    with pytest.raises(ValueError, match="Unknown frame"):
        store.load("broken")


@pytest.mark.integration
async def test_teach_saved_fixture_preview_and_execute_same_named_pose(
    user: User, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    start_angles = [85, -85, 135, 10, 45, 170]
    index = await client.move_j(start_angles, speed=1.0)
    assert index >= 0 and await client.wait_command(index, timeout=20)
    current = await client.pose()
    assert current is not None
    native = await client.status()
    assert native is not None
    assert Pose(cast(PoseValues, tuple(current))).matrix() == pytest.approx(
        np.asarray(native.pose).reshape(4, 4), abs=0.01
    ), "setup poses must use the native robot's orientation convention"

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    async def message(text):
        await user.should_see(content=text)

    user.find(marker="tab-setup").click()
    user.find(marker="setup-teach-frame").click()
    await message("Captured TCP. Save setup to keep it.")
    user.find(marker="setup-set-frame").click()
    await message("Frame fixture updated. Save setup to keep it.")
    user.find(kind=ui.tab, content="Poses").click()
    element("setup-pose-frame").set_value("fixture")
    user.find(marker="setup-teach-pose").click()
    await message("Captured TCP. Save setup to keep it.")
    for axis in ("x", "y", "z", "rx", "ry", "rz"):
        assert element(f"setup-pose-{axis}").value == pytest.approx(0, abs=0.1)
    element("setup-pose-z").set_value(2.0)
    user.find(marker="setup-set-pose").click()
    await message("Pose pick updated. Save setup to keep it.")
    user.find(kind=ui.tab, content="Parameters").click()
    user.find(marker="setup-set-parameter").click()
    await message("Parameter clearance updated. Save setup to keep it.")
    user.find(marker="setup-save").click()
    await message("Saved bench")
    before = load_setup("bench")
    assert before.parameters["clearance"].value == 30
    assert before.relative_pose(before.resolve("pick"), "fixture").values[
        2
    ] == pytest.approx(2)

    # Move one frame; every pose that refers to it follows on the next load.
    user.find(kind=ui.tab, content="Frames").click()
    element("setup-frame-x").set_value(element("setup-frame-x").value + 1.0)
    # Save includes the edited frame without requiring Keep frame first.
    user.find(kind=ui.tab, content="Poses").click()
    user.find(marker="setup-save").click()
    await message("Saved bench")
    after = load_setup("bench")
    target = after.resolve("pick")
    assert np.array(target.values[:3]) - before.resolve("pick").values[
        :3
    ] == pytest.approx((1, 0, 0))
    user.find(marker="setup-load").click()
    await message("Loaded bench")

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    textarea = ui_state.active_textarea
    assert textarea is not None
    program = waldoctl.commander.programs.active
    assert program is not None
    textarea.value = '"""Fixture program."""\nfrom __future__ import annotations\nfrom parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.move_l(setup.resolve("pick").as_list(), speed=0.3)\n'
    await asyncio.sleep(0)
    original_source = program.source
    user.find(marker="tab-setup").click()
    user.find(marker="setup-save").click()
    await message("Saved bench")
    assert program.source == original_source, (
        "saving setup must not silently edit Python"
    )
    user.find(marker="setup-insert-load").click()
    await message("Inserted setup load at the start of the active program")
    assert "setup = load_setup('bench')" in program.source
    assert textarea.value == program.source

    initial = await client.angles()
    assert initial is not None
    result = await run.cpu_bound(
        _run_simulation_isolated,
        program.source,
        np.radians(initial),
        dry_run_client_cls=DryRunRobotClient,
        setup_directory=str(tmp_path),
    )
    assert result["error"] is None, result["error"]
    assert len(result["segments"]) == 1 and result["segments"][0]["is_valid"]
    assert np.asarray(result["segments"][0]["points"][-1]) * 1000 == pytest.approx(
        target.values[:3], abs=0.1
    )

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    from waldo_commander.components.simulation_engine import simulation

    await simulation.run_simulation()
    assert program.dry_run.path_segments
    user.find(marker="editor-step-program").click()
    try:
        async with asyncio.timeout(30):
            while not program.dry_run.playback.executing_step_at_end:
                await asyncio.sleep(0.05)
        actual = await client.pose()
        assert actual is not None
        assert Pose(cast(PoseValues, tuple(actual))).matrix() == pytest.approx(
            target.matrix(), abs=0.2
        )
        user.find(marker="editor-play-btn").click()
        async with asyncio.timeout(15):
            while is_any_program_running():
                await asyncio.sleep(0.05)
    finally:
        if is_any_program_running():
            user.find(marker="editor-stop-btn").click()
            async with asyncio.timeout(10):
                while is_any_program_running():
                    await asyncio.sleep(0.05)


async def test_pending_frame_edits_are_honoured_by_teach_and_block_frame_removal(
    user: User, tmp_path, monkeypatch
):
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = waldoctl.commander.client
    index = await client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
    assert index >= 0 and await client.wait_command(index, timeout=20)
    current = await client.pose()
    assert current is not None

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    async def message(text):
        await user.should_see(content=text)

    user.find(marker="tab-setup").click()
    user.find(marker="setup-teach-frame").click()
    await message("Captured TCP. Save setup to keep it.")
    user.find(marker="setup-set-frame").click()
    await message("Frame fixture updated. Save setup to keep it.")
    # An uncommitted frame edit is saved together with the pose taught in it,
    # so teaching must resolve against the edited frame.
    element("setup-frame-x").set_value(element("setup-frame-x").value + 10.0)
    user.find(kind=ui.tab, content="Poses").click()
    element("setup-pose-frame").set_value("fixture")
    user.find(marker="setup-teach-pose").click()
    await message("Captured TCP. Save setup to keep it.")
    taught = [element(f"setup-pose-{axis}").value for axis in ("x", "y", "z")]
    assert np.linalg.norm(taught) == pytest.approx(10, abs=0.1), (
        "the pose is taught relative to the edited frame, 10 mm away"
    )
    user.find(marker="setup-save").click()
    await message("Saved bench")
    saved = load_setup("bench")
    assert np.array(saved.resolve("pick").values[:3]) == pytest.approx(
        current[:3], abs=0.1
    ), "the taught pose resolves to the TCP it was taught at"

    # A pending pose in a frame pins that frame: removing it would otherwise
    # silently rebind the pending pose to WRF with frame-local numbers.
    element("setup-pose-z").set_value(element("setup-pose-z").value + 5.0)
    user.find(kind=ui.tab, content="Frames").click()
    user.find(marker="setup-remove-frame").click()
    await message(
        "Keep or discard the pending pose in fixture before removing the frame"
    )
    assert element("setup-pose-frame").value == "fixture"
    user.find(marker="setup-save").click()
    await message("Saved bench")
    after = load_setup("bench")
    assert "fixture" in after.frames
    assert after.poses["pick"].frame == "fixture"
    assert after.relative_pose(after.resolve("pick"), "fixture").values[
        2
    ] == pytest.approx(
        saved.relative_pose(saved.resolve("pick"), "fixture").values[2] + 5.0, abs=0.01
    )
