"""Calibrate a tip held at one simulated pivot and use the saved transform."""

import asyncio
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
    wait_until,
)
from waldo_commander.services.control_lease import BROWSER, MCP, control_lease
from waldo_commander.services.path_visualizer import _run_simulation_isolated
from waldo_commander.setup import SetupStore
from waldo_commander.state import ui_state


@pytest.mark.integration
async def test_pivot_orientation_saved_setup_and_confirmed_application(
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

    def element(marker):
        return next(iter(user.find(marker=marker).elements))

    async def completed(index):
        assert index >= 0 and await client.wait_command(index, timeout=20)

    try:
        await completed(await client.select_tool("NONE"))
        await completed(await client.set_tcp_transform())
        await completed(await client.move_j([85, -85, 135, 10, 45, 170], speed=1.0))
        current = await client.pose()
        assert current is not None
        base = Pose(cast(PoseValues, tuple(current))).matrix()
        tip = np.array([0.0, 0.0, 25.0])
        pivot = base[:3, 3] + base[:3, :3] @ tip
        axes = base.copy()
        axes[:3, 3] = 0
        SetupStore(tmp_path).save(
            "bench",
            SetupSnapshot(frames={"axes": Frame(Pose.from_matrix(axes).values)}),
        )
        old = Pose((4, -2, 12, 10, -15, 20))
        await completed(await client.set_tcp_transform(*old.values))

        user.find(marker="tab-setup").click()
        user.find(marker="setup-load").click()
        await user.should_see("Loaded bench")
        user.find(kind=ui.tab, content="TCP").click()
        next(iter(user.find(marker="tcp-measure-details").elements)).set_value(True)
        user.find(marker="tcp-calibration-solve").click()
        await user.should_see("Pivot calibration requires at least four tool poses")
        program = waldoctl.commander.programs.active
        assert program is not None
        source_before = program.source

        for i, (rx, ry) in enumerate([(-8, -6), (8, -6), (-8, 6), (8, 6)]):
            nominal = base.copy()
            nominal[:3, :3] = base[:3, :3] @ Pose((0, 0, 0, rx, ry, 0)).matrix()[:3, :3]
            nominal[:3, 3] = pivot - nominal[:3, :3] @ tip
            target = Pose.from_matrix(nominal @ old.matrix())
            await completed(await client.move_j(pose=target.as_list(), speed=0.7))
            user.find(marker="tcp-calibration-capture").click()
            await user.should_see(f"{i + 1} samples", retries=50)
        user.find(marker="tcp-calibration-solve").click()
        await user.should_see("Orientation is unchanged.")
        for key, wanted in zip(("x", "y", "z"), tip):
            assert element(f"tcp-calibration-{key}").value == pytest.approx(
                wanted, abs=0.5
            )
        element("tcp-calibration-reference").set_value("axes")
        user.find(marker="tcp-calibration-orientation").click()
        await user.should_see(
            "Orientation taught against axes; position is unchanged.", retries=50
        )
        user.find(marker="tcp-calibration-set").click()
        await user.should_see("Calibration kept. Save setup to persist it.")
        user.find(marker="setup-save").click()
        await user.should_see("Saved bench")
        saved = SetupStore(tmp_path).load("bench").tcp_calibrations["tip"]
        assert saved.position_samples == 4 and saved.position_rms_mm < 1.0
        assert saved.orientation_reference == "axes"
        assert program.source == source_before, (
            "saving calibration edited the Python program"
        )
        assert await client.tcp_transform() == pytest.approx(old.values), (
            "saving applied robot configuration"
        )

        control_lease.seize(MCP, "tcp-review", "Review MCP")
        user.find(marker="tcp-calibration-apply").click()
        await user.should_see(
            "Controller confirmed the displayed TCP transform.", retries=50
        )
        assert control_lease.held_by(BROWSER, ui_state.active_client_id)
        await user.should_see("You've taken control from the AI")
        assert await client.tcp_transform() == pytest.approx(saved.values)
        actual = await client.pose()
        assert actual is not None
        matrix = Pose(cast(PoseValues, tuple(actual))).matrix()
        assert matrix[:3, 3] == pytest.approx(pivot, abs=0.5)
        assert matrix[:3, :3] == pytest.approx(axes[:3, :3], abs=0.003)
        angles = await client.angles()
        assert angles is not None
        fk = ui_state.active_robot.fk(np.radians(angles), np.empty(6))
        local_matrix = Pose(
            cast(PoseValues, tuple([*(fk[:3] * 1000), *np.degrees(fk[3:])]))
        ).matrix()
        assert local_matrix == pytest.approx(matrix, abs=0.05)

        source = """from waldo_commander.setup import load_setup
from parol6 import RobotClient
with RobotClient() as rbt:
    calibration = load_setup('bench').tcp_calibrations['tip']
    rbt.set_tcp_transform(*calibration.values)
    rbt.move_l([0, 0, 2, 0, 0, 0], frame='TRF', rel=True, speed=0.3)
"""
        result = await run.cpu_bound(
            _run_simulation_isolated,
            source,
            np.radians(angles),
            dry_run_client_cls=DryRunRobotClient,
            setup_directory=str(tmp_path),
        )
        assert result["error"] is None, result["error"]
        assert len(result["segments"]) == 1
        assert np.asarray(result["segments"][0]["points"][-1]) * 1000 == pytest.approx(
            matrix[:3, 3] + 2 * matrix[:3, 2], abs=0.2
        )

        ui_state.active_client_id = None
        await user.open("/")
        await wait_for_app_ready()
        await user.should_not_see("Connecting to controller...", retries=200)
        angles = await client.angles()
        actual = await client.pose()
        assert angles is not None and actual is not None
        fk = ui_state.active_robot.fk(np.radians(angles), np.empty(6))
        after_reload = Pose(
            cast(PoseValues, tuple([*(fk[:3] * 1000), *np.degrees(fk[3:])]))
        ).matrix()
        assert after_reload == pytest.approx(
            Pose(cast(PoseValues, tuple(actual))).matrix(), abs=0.05
        ), "reloading the scene discarded the applied TCP transform"
        user.find(marker="tab-setup").click()
        user.find(marker="setup-load").click()
        await user.should_see("Loaded bench")
        user.find(kind=ui.tab, content="TCP").click()
        next(iter(user.find(marker="tcp-measure-details").elements)).set_value(True)
        element("tcp-calibration-existing").set_value("tip")
        await completed(await client.select_tool("PNEUMATIC", variant_key="horizontal"))
        assert await wait_until(
            lambda: waldoctl.commander.status.tool.variant_key == "horizontal"
        ), "public tool status lost the controller variant"
        user.find(marker="tcp-calibration-apply").click()
        await user.should_see(
            "This calibration belongs to a different tool or variant", retries=50
        )
        assert await client.tcp_transform() == pytest.approx([0] * 6)
        # Capturing under the fitted tool identifies that tool for new samples
        # but must not rebind the loaded values to it on the next save.
        user.find(marker="tcp-calibration-capture").click()
        await user.should_see(
            "Tool or variant changed; samples cleared. Capture again.", retries=50
        )
        user.find(marker="setup-save").click()
        await user.should_see("Saved bench")
        kept = SetupStore(tmp_path).load("bench").tcp_calibrations["tip"]
        assert (kept.tool_key, kept.variant_key) == ("NONE", "")
        assert kept.values == saved.values
    finally:
        await client.stop()
        await completed(await client.select_tool("NONE"))
        await completed(await client.set_tcp_transform())
        await asyncio.sleep(0)
