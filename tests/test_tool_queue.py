"""Manual and scripted tools obey the same ordered command queue."""

import asyncio

import numpy as np
import pytest
import waldoctl
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction
from parol6 import AsyncRobotClient, RobotClient
from parol6.client.dry_run_client import DryRunRobotClient

from tests.conftest import _get_test_ports
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_for_tool_key,
    wait_until,
)
from tests.test_editor_integration import _fire_editor_event
from tests.test_stepping import _drain
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.motion_guard import motion_guard
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.preview_segments import segments_from_record
from waldo_commander.services.stepping_client import (
    AsyncSteppingClientWrapper,
    GUIStepController,
    StepIO,
    SteppingClientWrapper,
)
from waldo_commander.services.timeline import Timeline
from waldo_commander.state import ui_state


async def _gripper(user):
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    client = ui_state.control_panel.client
    selected = await client.select_tool("SSG-48")
    assert selected >= 0 and await client.wait_command(selected, timeout=5)
    await wait_for_tool_key("SSG-48")
    calibrated = await client.tool.calibrate()
    assert calibrated >= 0 and await client.wait_command(calibrated, timeout=10)
    waldoctl.commander.settings.gripper.current = 50
    return client


@pytest.mark.integration
async def test_slider_keeps_the_final_target_and_records_it_once(user: User):
    client = await _gripper(user)
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    ui_state.active_textarea.value = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    )
    motion_recorder.toggle_recording()
    user.find(marker="tab-gripper").click()
    await asyncio.sleep(0)
    slider = ui_state.gripper_page._pos_slider
    interaction = UserInteraction(user, {slider}, None)

    def value(v):
        _fire_editor_event(slider, "update:model-value", v)

    try:
        value(10)
        interaction.trigger("pan", "start")
        for v in (20, 35, 50, 65):
            value(v)
            await asyncio.sleep(0.015)
        interaction.trigger("pan", "end")
        interaction.trigger("change", 85)
        await asyncio.sleep(0.04)
        value(85)
        assert await wait_until(
            lambda: abs(waldoctl.commander.status.tool.position - 0.85) < 0.025, 12
        )
        assert await wait_until(
            lambda: "rbt.tool.set_position(0.85" in str(ui_state.active_textarea.value),
            12,
        )
        code = str(ui_state.active_textarea.value)
        assert code.count("rbt.tool.set_position(") == 1, code
        for target in (15, 70):
            interaction.trigger("pointerdown")
            value(target)
            interaction.trigger("pan", "start")
            interaction.trigger("pan", "end")
            interaction.trigger("change", target)
            await asyncio.sleep(0.08)
        # A key at a limit may produce no model update or change event.
        interaction.trigger("keydown")
        assert await wait_until(
            lambda: str(ui_state.active_textarea.value).count("rbt.tool.set_position(")
            == 3,
            15,
        )
        assert await wait_until(lambda: ui_state.gripper_page._slider_task is None, 5)
        code = str(ui_state.active_textarea.value)
        assert code.index("rbt.tool.set_position(0.15") < code.index(
            "rbt.tool.set_position(0.7"
        )
        motion_recorder.toggle_recording()
        # A stop invalidates the gesture, including a delayed Quasar change.
        value(40)
        interaction.trigger("pan", "start")
        await asyncio.sleep(0.08)
        assert await motion_guard.stop_robot(client, "slider regression")
        await asyncio.sleep(0.2)
        stopped = (await client.tool.status()).position
        interaction.trigger("pan", "end")
        interaction.trigger("change", 5)
        await asyncio.sleep(0.5)
        assert (await client.tool.status()).position == pytest.approx(stopped, abs=0.01)
        value(85)
        interaction.trigger("pan", "start")
        interaction.trigger("pan", "end")
        interaction.trigger("change", 85)
        assert await wait_until(
            lambda: abs(waldoctl.commander.status.tool.position - 0.85) < 0.025, 12
        )
        program = waldoctl.commander.programs.active
        program.execution.is_running = True
        try:
            value(20)
            interaction.trigger("pan", "start")
            value(5)
            interaction.trigger("pan", "end")
            interaction.trigger("change", 5)
            await asyncio.sleep(1.2)
            assert (await client.tool.status()).position == pytest.approx(
                0.85, abs=0.025
            )
        finally:
            program.execution.is_running = False
    finally:
        if waldoctl.commander.programs.active.recording.is_recording:
            motion_recorder.toggle_recording()
        await client.select_tool("NONE")


@pytest.mark.integration
async def test_two_quick_tool_toggles_return_to_the_open_target(user: User):
    client = await _gripper(user)
    waldoctl.commander.settings.jog.speed = 20
    waldoctl.commander.settings.gripper.target_position = 0.0
    try:
        user.find(marker="btn-tool-action-l").click()
        await asyncio.sleep(0.1)
        user.find(marker="btn-tool-action-l").click()
        assert await wait_until(
            lambda: waldoctl.commander.settings.gripper.target_position == 0.0, 2
        )
        assert await client.wait_motion(timeout=15, settle_window=0.1)
        assert (await client.tool.status()).position == pytest.approx(0, abs=5 / 255)
    finally:
        await client.select_tool("NONE")


def test_queued_tool_preview_follows_motion_and_splits_blends():
    from waldo_commander.profiles import get_robot
    from waldo_commander.services.path_visualizer import _tool_metadata

    for blended in (False, True):
        preview = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            initial_joints=np.radians([85, -85, 135, 10, 45, 170]),
            tool_meta_registry=_tool_metadata(get_robot("parol6")),
        )
        preview.select_tool("SSG-48", "pinch")
        preview.tool.calibrate()
        preview.move_j(
            [90, -85, 135, 10, 45, 170], speed=0.5, r=5 if blended else 0, wait=False
        )
        if not blended:
            preview.record_sleep(0.3)
        preview.tool.close()
        preview.move_j([85, -85, 135, 10, 45, 170], speed=0.5, r=5 if blended else 0)
        preview.flush()
        record = preview.plan()
        segments = segments_from_record(record, preview.notes)
        moves = [b for b in record.blocks if b.move_type == "joints"]
        action = preview.tool_action_collector[-1]
        assert action.sleep_offset == 0
        tool = next(b for b in record.blocks if b.command == action.command)
        assert len(moves) == 2
        assert moves[0].start_row + moves[0].rows <= tool.start_row
        assert tool.start_row + tool.rows <= moves[1].start_row
        timeline = Timeline.from_record(record, segments)
        assert (
            timeline.tool_spans[-1].start
            >= (moves[0].start_row + moves[0].rows - 1) * record.row_dt_s
        )
        assert timeline.tool_spans[-1].blocking


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stepped_tool_waits_for_the_jaws_and_release_has_events(
    session_controller, asynchronous
):
    port, _ = _get_test_ports()
    name = f"queued-tool-{asynchronous}"
    controller = GUIStepController(name)
    controller.initialize()
    controller.signal_play()
    try:
        if asynchronous:

            async def run():
                async with AsyncRobotClient(
                    host="127.0.0.1", port=port, timeout=5
                ) as client:
                    await client.simulator(True)
                    await client.reset()
                    selected = await client.select_tool("SSG-48")
                    assert await client.wait_command(selected, timeout=5)
                    wrapper = AsyncSteppingClientWrapper(client, StepIO(name))
                    try:
                        await wrapper.tool.calibrate()
                        await wrapper.tool.set_position(1.0, speed=0.1)
                        assert (await client.tool.status()).position == pytest.approx(
                            1, abs=5 / 255
                        )
                        await wrapper.tool.release()
                    finally:
                        await client.select_tool("NONE")

            asyncio.run(run())
        else:
            with RobotClient(host="127.0.0.1", port=port, timeout=5) as client:
                client.simulator(True)
                client.reset()
                selected = client.select_tool("SSG-48")
                assert client.wait_command(selected, timeout=5)
                wrapper = SteppingClientWrapper(client, StepIO(name))
                try:
                    wrapper.tool.calibrate()
                    wrapper.tool.set_position(1.0, speed=0.1)
                    assert client.tool.status().position == pytest.approx(
                        1, abs=5 / 255
                    )
                    wrapper.tool.release()
                finally:
                    client.select_tool("NONE")
        events = _drain(controller, 6)
        assert [(e["event"], e["method"]) for e in events] == [
            (event, "tool_action") for _ in range(3) for event in ("start", "complete")
        ]
    finally:
        controller.cleanup()


@pytest.mark.integration
async def test_panel_calibration_seeds_the_program_preview(user: User):
    from waldo_commander.services.path_visualizer import path_visualizer
    from waldo_commander.state import robot_state

    client = await _gripper(user)
    robot_state.gripper_calibrated = False
    user.find(marker="btn-tool-action-r").click()
    assert await wait_until(lambda: robot_state.gripper_calibrated, 10)
    error = await path_visualizer.update_path_visualization(
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.tool.close()\n    rbt.delay(0.1)\n"
    )
    assert error is None, error
    record = waldoctl.commander.programs.active.dry_run.commanded
    assert record is not None
    assert record.tool_closed[-1] == pytest.approx(1, abs=0.02)
    waldoctl.commander.status.simulator_active = False
    with user.client:
        await ui_state.control_panel.on_toggle_sim()
    assert await wait_until(lambda: not robot_state.gripper_calibrated, 5)
    await client.select_tool("NONE")


@pytest.mark.integration
async def test_scrubbing_after_tool_removal_still_teleports_the_arm(user: User):
    from waldo_commander.components.playback import playback
    from waldo_commander.services.preview_segments import index_boundaries

    client = await _gripper(user)
    source = PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient,
        initial_joints=np.radians([85, -85, 135, 10, 45, 170]),
    )
    source.select_tool("SSG-48")
    source.tool.calibrate()
    source.tool.close()
    source.select_tool("NONE")
    source.move_j([90, -85, 135, 10, 45, 170], speed=0.5)
    source.flush()
    record = source.plan()
    segments = segments_from_record(record, source.notes)
    index_boundaries(segments, source.tool_selection_collector)
    timeline = Timeline.from_record(
        record, segments, tool_selections=source.tool_selection_collector
    )
    playback._timeline = timeline
    playback._apply_time(timeline.total_duration)
    assert await wait_until(lambda: waldoctl.commander.status.tool.key == "NONE", 5)
    await asyncio.sleep(0.2)
    assert np.array(await client.angles()) == pytest.approx(
        [90, -85, 135, 10, 45, 170], abs=0.1
    )


def test_tool_stop_does_not_complete_a_pending_arm_blend(session_controller):
    port, _ = _get_test_ports()
    controller = GUIStepController("tool-stop-blend")
    controller.initialize()
    controller.signal_play()
    try:
        with RobotClient(host="127.0.0.1", port=port, timeout=5) as client:
            client.simulator(True)
            client.reset()
            client.home(wait=True, timeout=30)
            selected = client.select_tool("SSG-48")
            assert client.wait_command(selected, timeout=5)
            client.tool.calibrate(wait=True)
            wrapper = SteppingClientWrapper(client, StepIO("tool-stop-blend"))
            wrapper.move_j([85, -85, 135, 10, 45, 170], r=10, speed=0.5, wait=False)
            assert wrapper.tool.stop() > 0
            events = _drain(controller, 2)
            assert not any(
                e["event"] == "complete" and e["method"] == "blend_group"
                for e in events
            )
            client.stop()
            client.select_tool("NONE")
    finally:
        controller.cleanup()
