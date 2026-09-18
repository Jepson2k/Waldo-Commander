"""Calibrate a tip held at one simulated pivot and use the saved transform."""

import asyncio
from typing import cast

import numpy as np
import pytest
import waldoctl
from nicegui import run, ui
from nicegui.testing import User
from waldoctl.setup import Frame, Pose, PoseValues, SetupSnapshot

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.services.control_lease import BROWSER, MCP, control_lease
from waldo_commander.services.tcp_calibration import (
    calibrate_tcp_position,
    teach_tcp_orientation,
)
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
            setup_directory=str(tmp_path),
        )
        assert result["error"] is None, result["error"]
        record = result["commanded"]
        moves = [b for b in record.blocks if b.move_type is not None]
        assert len(moves) == 1 and moves[0].error is None and moves[0].rows > 0
        end = record.tcp[moves[0].start_row + moves[0].rows - 1]
        assert np.asarray(end[:3]) * 1000 == pytest.approx(
            matrix[:3, 3] + 2 * matrix[:3, 2], abs=0.2
        )

        # User.open leaves the previous simulated page alive. Close it first
        # so its heartbeat cannot schedule a competing reload of the same user.
        previous_page = user.client
        assert previous_page is not None
        for handler in previous_page.disconnect_handlers:
            previous_page.safe_invoke(handler)
        previous_page.delete()
        reloaded_page = await user.open("/")
        assert ui_state.active_client_id == reloaded_page.id
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


def _pivot_samples():
    tip = np.array([11.0, -7.0, 115.0])
    pivot = np.array([340.0, 80.0, 210.0])
    rotations = [
        np.eye(3),
        np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]]),
        np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]]),
        np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]]),
    ]
    samples = []
    for rotation in rotations:
        tool = np.eye(4)
        tool[:3, :3] = rotation
        tool[:3, 3] = pivot - rotation @ tip
        samples.append(Pose.from_matrix(tool))
    return samples, tip, pivot


def test_pivot_solve_and_orientation_teaching_recover_the_tip():
    samples, tip, pivot = _pivot_samples()
    result = calibrate_tcp_position(samples, max_error_mm=0.1)
    assert result.offset_mm == pytest.approx(tip)
    assert result.pivot_wrf_mm == pytest.approx(pivot)
    assert result.max_error_mm < 1e-10

    # Re-observing the same pivot far from the origin changes its world
    # coordinates, not the calibrated tip in the registered tool frame.
    shifted = []
    for sample in samples:
        transform = sample.matrix()
        transform[:3, 3] += [1e6, -2e6, 3e6]
        shifted.append(Pose.from_matrix(transform))
    assert calibrate_tcp_position(shifted).offset_mm == pytest.approx(tip)

    axes = Pose((0, 0, 0, 0, 90, 0))
    orientation = teach_tcp_orientation(samples[1], axes)
    calibrated = Pose((*result.offset_mm, *orientation)).matrix()
    achieved = samples[1].matrix() @ calibrated
    assert achieved[:3, 3] == pytest.approx(pivot)
    assert achieved[:3, :3] == pytest.approx(
        np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]]), abs=1e-12
    )

    noisy = samples.copy()
    matrix = noisy[-1].matrix()
    matrix[:3, 3] += [0.05, -0.02, 0.03]
    noisy[-1] = Pose.from_matrix(matrix)
    observed = calibrate_tcp_position(noisy, max_error_mm=0.1)
    assert 0 < observed.rms_error_mm <= observed.max_error_mm < 0.1
    assert observed.offset_mm == pytest.approx(tip, abs=0.1)
    with pytest.raises(ValueError, match="disagree"):
        calibrate_tcp_position(noisy, max_error_mm=0.001)


def test_pivot_refuses_insufficient_degenerate_or_invalid_observations():
    samples, _, _ = _pivot_samples()
    for count in range(4):
        with pytest.raises(ValueError, match="at least four"):
            calibrate_tcp_position(samples[:count])
    with pytest.raises(ValueError, match="degenerate"):
        calibrate_tcp_position([samples[0]] * 4)
    with pytest.raises(ValueError, match="degenerate"):
        calibrate_tcp_position([Pose((0, 0, 0, 0, 0, yaw)) for yaw in (0, 30, 60, 90)])
    small = [
        Pose((0, 0, 0, r, p, y))
        for r, p, y in ((0, 0, 0), (0.01, 0, 0), (0, 0.01, 0), (0, 0, 0.01))
    ]
    with pytest.raises(ValueError, match="poorly conditioned"):
        calibrate_tcp_position(small)
    for error in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="tolerance"):
            calibrate_tcp_position(samples, max_error_mm=error)
    for condition in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="condition"):
            calibrate_tcp_position(samples, max_condition=condition)
    local = Pose(samples[0].values, frame="fixture")
    with pytest.raises(ValueError, match="WRF"):
        calibrate_tcp_position([local, *samples[1:]])
    for tool, axes in ((local, samples[0]), (samples[0], local)):
        with pytest.raises(ValueError, match="WRF"):
            teach_tcp_orientation(tool, axes)
