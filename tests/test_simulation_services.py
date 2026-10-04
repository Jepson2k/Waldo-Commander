"""Functional tests for simulation services.

These tests verify actual behavior rather than just checking if buttons exist.
"""

import asyncio
import contextlib
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import waldoctl
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl import TickIndex, following_error

from tests.helpers.programs import set_active_recording
from waldo_commander.profiles import get_robot
from waldo_commander.services.motion_recorder import MotionRecorder
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.path_visualizer import PathVisualizer
from waldo_commander.services.preview_segments import segments_from_record
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.services.urdf_scene.envelope_renderer import WorkspaceEnvelope
from waldo_commander.state import (
    playback_coordination,
    robot_state,
    simulation_state,
    ui_state,
)

# ============================================================================
# Dry Run Client Tests
# ============================================================================


class TestDryRunClient:
    """The preview client over parol6's dry-run client: every command goes
    through the real planning pipeline and lands on the plan record."""

    @staticmethod
    def _planned(client):
        client.close()
        record = client.plan()
        return record, segments_from_record(record, client.notes)

    def test_move_joints_records_a_block_the_scene_draws_as_a_segment(self):
        client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
        target = [85, -85, 135, 10, 45, 170]
        index = client.move_j(target, speed=1.0)
        record, segments = self._planned(client)

        assert index == 0 and client.wait_command(index)
        block = record.blocks[0]
        assert block.move_type == "joints" and block.rows >= 2
        assert record.joints_rad.shape == (record.rows, 6)
        assert np.degrees(record.joints_rad[block.start_row + block.rows - 1]) == (
            pytest.approx(target, abs=0.5)
        )
        assert len(segments) == 1
        segment = segments[0]
        assert segment.is_valid and segment.move_type == "joints"
        assert segment.command == 0 and segment.rows == block.rows
        assert segment.estimated_duration == pytest.approx(block.rows * record.row_dt_s)
        assert not client.target_collector, "no literal source line, no target"

    def test_cartesian_moves_record_a_block_or_are_refused_on_the_record(self):
        """A reachable move_l is a cartesian block; a target the planner
        cannot reach is a block with an error and rows the scene draws red —
        not a crash, and not a silent gap."""
        client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
        assert client.move_l([150, 100, 250, 0, 0, 0], speed=1.0) == 0
        record, segments = self._planned(client)
        assert record.blocks[0].move_type == "cartesian"
        assert segments[0].move_type == "cartesian" and segments[0].is_valid

        client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
        index = client.move_l([9999, 9999, 9999, 0, 0, 0], speed=1.0)
        record, segments = self._planned(client)

        assert index < 0 or not client.wait_command(index)
        assert record.blocks[0].error is not None
        assert any(not s.is_valid for s in segments)


# ============================================================================
# Motion Recorder Tests
# ============================================================================


def set_robot_pose(x, y, z, rx=0.0, ry=0.0, rz=0.0):
    """Set the commander.status.pose scalars and the robot_state pose matrix."""
    waldoctl.commander.status.pose.x = x
    waldoctl.commander.status.pose.y = y
    waldoctl.commander.status.pose.z = z
    waldoctl.commander.status.pose.rx = rx
    waldoctl.commander.status.pose.ry = ry
    waldoctl.commander.status.pose.rz = rz
    robot_state.pose = np.array(
        [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1],
        dtype=np.float64,
    )


@pytest.fixture
def mock_textarea():
    """Set up a mocked active textarea + editor_panel + program for motion recorder tests.

    Yields the textarea mock — tests assert on its ``.value`` to verify what
    motion_recorder inserted. ``ui_state.editor_panel`` is wired to a separate
    MagicMock so production code that does presence checks still works. An
    active ``Program`` is ensured so ``motion_recorder._start_recording`` has
    a target to write recording state into.
    """
    from tests.helpers.programs import ensure_active_program

    mock_textarea = MagicMock()
    mock_textarea.value = "# Initial code\n"
    ui_state.active_textarea = mock_textarea
    ui_state.editor_panel = MagicMock()
    old_robot = ui_state.robot
    ui_state.robot = get_robot()
    # An app test earlier in the session may have left the program tab
    # selected; with no page here, an inserted line has no editor to flash in.
    panel_visible = ui_state.program_panel_visible
    ui_state.program_panel_visible = False
    ensure_active_program()
    yield mock_textarea
    ui_state.program_panel_visible = panel_visible
    ui_state.editor_panel = None
    ui_state.active_textarea = None
    ui_state.robot = old_robot


class TestMotionRecorder:
    """Tests for motion recording functionality (code-insertion API)."""

    def test_capture_current_pose_inserts_move_l_or_move_j(self, mock_textarea):
        """capture_current_pose inserts the TCP pose as move_l, or the joint
        angles as move_j in joints mode."""
        set_robot_pose(150.0, 250.0, 350.0)

        recorder = MotionRecorder()
        recorder.capture_current_pose()

        inserted_code = mock_textarea.value
        assert "rbt.move_l([150.000, 250.000, 350.000" in inserted_code
        assert "speed=" in inserted_code
        assert "accel=" in inserted_code

        waldoctl.commander.status.joints.angles.set_deg(
            np.array([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
        )
        recorder.capture_current_pose(move_type="joints")
        assert "rbt.move_j([10.00, 20.00, 30.00, 40.00, 50.00, 60.00" in (
            mock_textarea.value
        )

    def test_toggle_recording_lifecycle(self, mock_textarea):
        """Jogs and actions are ignored until toggle_recording starts a
        session, and the next toggle ends it."""
        recorder = MotionRecorder()
        assert not is_any_program_recording()

        recorder.on_jog_start("joint", "J1+")
        assert recorder._active_jog is None
        recorder.on_jog_end()
        set_active_recording(False)
        recorder.record_action("home")
        assert mock_textarea.value == "# Initial code\n"

        recorder.toggle_recording()
        assert is_any_program_recording()

        recorder.toggle_recording()
        assert not is_any_program_recording()

    def test_record_action_generates_code(self, mock_textarea):
        """record_action turns home, I/O and gripper actions into code."""
        recorder = MotionRecorder()
        set_active_recording(True)

        recorder.record_action("home")
        assert "rbt.home()" in mock_textarea.value

        mock_textarea.value = ""
        recorder.record_action("io", port=1, state=1)
        assert "rbt.write_io(1, 1)" in mock_textarea.value

        # Part 1: Calibrate command
        mock_textarea.value = ""
        recorder.record_action("gripper", calibrate=True)
        inserted_code = mock_textarea.value
        assert "rbt.tool.calibrate()" in inserted_code

        # Part 2: Move command with params (partial position → set_position)
        mock_textarea.value = ""
        recorder.record_action("gripper", position=0.5, speed=0.5, current=0.3)
        inserted_code = mock_textarea.value
        assert "rbt.tool.set_position(0.5, speed=0.5, current=0.3)" in inserted_code

        # Part 3: Full open (position=0.0) — always uses set_position
        mock_textarea.value = ""
        recorder.record_action("gripper", position=0.0)
        inserted_code = mock_textarea.value
        assert "rbt.tool.set_position(0.0)" in inserted_code

        # Part 4: Full close (position=1.0) — always uses set_position
        mock_textarea.value = ""
        recorder.record_action("gripper", position=1.0)
        inserted_code = mock_textarea.value
        assert "rbt.tool.set_position(1.0)" in inserted_code

    def test_record_set_shapes_prepends_import_unless_truly_imported(
        self, mock_textarea
    ):
        """Import detection parses import statements: an incidental mention of
        the class name (comment, attribute access, alias) must not suppress
        the needed ``from waldoctl import`` — the recorded program must stay
        runnable — while a genuine import must not be duplicated."""
        from waldoctl import Box

        recorder = MotionRecorder()
        set_active_recording(True)
        box = Box(name="cage", x=0.1, y=0.1, z=0.1)

        # A comment mention is not an import.
        mock_textarea.value = "# put a Box near the origin\n"
        recorder.record_action("set_shapes", shapes=[box])
        assert "from waldoctl import Box" in mock_textarea.value
        assert "rbt.set_shapes(" in mock_textarea.value

        # Attribute-style usage binds no bare name.
        mock_textarea.value = (
            "import waldoctl\nw = waldoctl.Box(name='b', x=1, y=1, z=1)\n"
        )
        recorder.record_action("set_shapes", shapes=[box])
        assert "from waldoctl import Box" in mock_textarea.value

        # An aliased import doesn't bind the bare name either.
        mock_textarea.value = "from waldoctl import Box as KeepOut\n"
        recorder.record_action("set_shapes", shapes=[box])
        assert "from waldoctl import Box\n" in mock_textarea.value

        # A genuine import must not be duplicated.
        mock_textarea.value = "from waldoctl import Box\n"
        recorder.record_action("set_shapes", shapes=[box])
        assert mock_textarea.value.count("from waldoctl import Box") == 1

        # A body's code says `physics=Physical(...)`, so it needs that import
        # too — the class name alone leaves the program un-runnable.
        from waldoctl import Physical

        block = Box(name="block", x=0.04, y=0.04, z=0.06, physics=Physical(mass=0.05))
        mock_textarea.value = ""
        recorder.record_action("set_shapes", shapes=[block])
        assert "from waldoctl import Box, Physical" in mock_textarea.value
        assert "physics=Physical(" in mock_textarea.value

    def test_multiple_jogs_insert_multiple_code_lines(self, mock_textarea):
        """Each cartesian jog start/end cycle while recording inserts a move_l."""
        set_robot_pose(100.0, 100.0, 100.0)
        waldoctl.commander.status.joints.angles.set_deg(np.zeros(6))

        recorder = MotionRecorder()
        recorder.toggle_recording()  # Start

        # First jog
        recorder.on_jog_start("cartesian", "X+")
        time.sleep(0.15)  # Need time > 0.1s
        set_robot_pose(150.0, 100.0, 100.0)
        recorder.on_jog_end()

        # Second jog
        recorder.on_jog_start("cartesian", "Y+")
        time.sleep(0.15)
        set_robot_pose(150.0, 200.0, 100.0)
        recorder.on_jog_end()

        recorder.toggle_recording()  # Stop

        assert mock_textarea.value.count("rbt.move_l(") >= 2, mock_textarea.value

    def test_stop_recording_ends_active_jog(self, mock_textarea):
        """Stopping recording should end any active jog."""
        set_robot_pose(100.0, 100.0, 100.0)
        waldoctl.commander.status.joints.angles.set_deg(np.zeros(6))

        recorder = MotionRecorder()
        recorder.toggle_recording()  # Start

        # Start jog but don't end it
        recorder.on_jog_start("cartesian", "X+")
        time.sleep(0.15)
        set_robot_pose(150.0, 100.0, 100.0)

        # Stop recording should capture the active jog
        recorder.toggle_recording()  # Stop

        # Check that code was inserted
        inserted_code = mock_textarea.value
        assert "rbt.move_l(" in inserted_code


class TestMotionRecorderWaitTimeGaps:
    """Tests for recorder inserting delays after non-blocking moves."""

    def test_a_wait_between_actions_is_recorded_but_not_before_a_motion(
        self, mock_textarea
    ):
        """Wall time between recorded actions becomes a time.sleep(), except
        before a motion command."""
        recorder = MotionRecorder()
        recorder.toggle_recording()

        recorder.record_action("gripper", position=0.5)
        time.sleep(0.2)
        recorder.record_action("gripper", position=1.0)
        assert "time.sleep(" in mock_textarea.value, (
            "Expected time.sleep() to be inserted for gap between actions"
        )

        time.sleep(0.2)
        set_robot_pose(100, 200, 300)
        recorder.record_action(
            "move_j",
            angles=[0, 0, 0, 0, 0, 0],
            speed=0.5,
            accel=0.5,
        )
        lines = mock_textarea.value.strip().split("\n")
        moves = [i for i, line in enumerate(lines) if "rbt.move_j" in line and i > 0]
        assert moves, mock_textarea.value
        for i in moves:
            assert "time.sleep" not in lines[i - 1], (
                "No delay should be inserted before a motion command"
            )

        recorder.toggle_recording()

    def test_queued_tool_is_recorded_after_the_blocking_move(self, mock_textarea):
        """Queued tools follow the recorded move without artificial overlap."""
        recorder = MotionRecorder()
        recorder.toggle_recording()

        set_robot_pose(100, 200, 300)
        recorder.on_jog_start("cartesian", "X+")

        # Queue a pending action during the jog
        time.sleep(0.1)
        recorder.record_action("gripper", position=0.5)
        assert len(recorder._pending_actions) == 1

        # End the jog — flushes pending actions
        set_robot_pose(200, 200, 300)
        time.sleep(0.1)
        recorder.on_jog_end()

        code = mock_textarea.value
        lines = [line.strip() for line in code.splitlines()]
        move = next(i for i, line in enumerate(lines) if "rbt.move_l(" in line)
        assert lines[move + 1].startswith("rbt.tool.set_position(0.5")
        assert "wait=False" not in lines[move]
        assert "time.sleep" not in code

        recorder.toggle_recording()


# ============================================================================
# Workspace Envelope Tests
# ============================================================================


class TestWorkspaceEnvelope:
    """Tests for workspace envelope generation.

    The WorkspaceEnvelope now uses a lightweight max_reach approach instead
    of generating a full point cloud. It calculates the maximum reach radius
    and visualizes it as a wireframe sphere.
    """

    @pytest.fixture
    def envelope(self):
        """Create fresh envelope instance for each test."""
        old_robot = ui_state.robot
        ui_state.robot = get_robot()
        env = WorkspaceEnvelope()
        yield env
        env.reset()
        ui_state.robot = old_robot

    def test_generate_sync_finds_the_reach_and_its_tool_offset_radius(self, envelope):
        """generate_sync computes the arm's reach in-process; once generated,
        generate() does not start again, and a tool offset of either sign
        extends the radius by its length."""
        assert envelope.generate_sync(samples=64) is True  # 2 samples per joint
        assert 0.3 < envelope.max_reach < 1.0
        assert envelope.generate(samples=10) is True

        reach = envelope.max_reach
        assert envelope.get_radius_with_tool_offset(0.05) == pytest.approx(reach + 0.05)
        assert envelope.get_radius_with_tool_offset(-0.05) == pytest.approx(
            reach + 0.05
        )
        assert envelope.get_radius_with_tool_offset(0.0) == reach


class TestSimulationCaching:
    """Tests for per-tab simulation caching and optimization.

    These tests verify:
    - Default script optimization skips simulation and uses cached home position
    - Non-default scripts trigger actual simulation
    - Results are stored in the originating tab, not the active tab
    - Anchor check uses cached final_joints_rad (instant, no blocking)
    """

    @pytest.fixture(autouse=True)
    def _set_robot(self):
        old_robot = ui_state.robot
        ui_state.robot = get_robot()
        yield
        ui_state.robot = old_robot

    def test_default_script_detected(self):
        """is_default_script returns True for default content, skipping simulation."""
        from waldo_commander.components.simulation_engine import (
            default_python_snippet,
            is_default_script,
        )

        default_content = default_python_snippet()
        assert is_default_script(default_content) is True

        # Whitespace variations should still match
        assert is_default_script(default_content + "\n\n  \n") is True

        # Non-default content should not match
        assert is_default_script("rbt.move_j([0,0,0,0,0,0])") is False

    @pytest.mark.asyncio
    async def test_results_stored_in_originating_tab(self):
        """Simulation results go to tab_id, not active tab (for tab switch during sim)."""
        import waldoctl
        from waldoctl import Program

        from waldo_commander.services.path_visualizer import PathVisualizer
        from waldo_commander.state import simulation_state

        # Create two tabs
        tab1 = Program(
            id="tab1", filename="a.py", file_path=None, source="", _saved_source=""
        )
        tab2 = Program(
            id="tab2", filename="b.py", file_path=None, source="", _saved_source=""
        )
        waldoctl.commander.programs.items = [tab1, tab2]
        waldoctl.commander.programs.active_id = "tab2"  # Active is tab2

        # The simulation itself runs in a pool worker, so stub the process
        # boundary rather than the function inside it: this test is about
        # which tab the result lands in, not about simulating anything.
        # notify_changed avoids a slot stack error.
        async def _fake_cpu_bound(_func, _args):
            return {
                "segments": [],
                "targets": [],
                "truncated": False,
                "error": None,
                "total_steps": 0,
                "final_joints_rad": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            }

        with (
            patch(
                "waldo_commander.services.path_visualizer.run.cpu_bound",
                _fake_cpu_bound,
            ),
            patch.object(simulation_state, "notify_changed"),
        ):
            visualizer = PathVisualizer()
            # Run simulation for tab1 (not active)
            await visualizer.update_path_visualization("print('hi')", tab_id="tab1")

            # Results should be in tab1, not tab2
            assert tab1.dry_run.final_joints_rad == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
            assert tab2.dry_run.final_joints_rad is None


def test_every_client_a_program_builds_plans_into_one_record():
    """A program that opens a second client, sync then async, gets one
    dry run: the second move starts where the first ended and both are on
    the record, not just the last client's."""
    from waldo_commander.services.path_visualizer import _run_simulation_isolated

    program = """import asyncio
from parol6 import AsyncRobotClient, RobotClient

rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)

async def main():
    async with AsyncRobotClient() as other:
        await other.move_j([90, -90, 180, 0, 0, 180], speed=1.0)

asyncio.run(main())
"""
    result = _run_simulation_isolated(program)
    assert result["error"] is None, result["error"]
    commanded = result["commanded"]
    notes = result["notes"]
    moves = [b for b in commanded.blocks if notes[b.command].method == "move_j"]
    assert [b.line_number for b in moves] == [5, 9]
    assert all(b.rows > 0 for b in moves)
    # The editor caches where the program ends.
    assert np.degrees(result["final_joints_rad"]) == pytest.approx(
        [90, -90, 180, 0, 0, 180], abs=0.5
    )


class TestPathVisualizerIntegration:
    """Integration tests for PathVisualizer with dry run client.

    These tests run in a subprocess via NiceGUI's cpu_bound(), so mocking
    PAROL6_ROBOT doesn't work (mocks don't transfer across process boundaries).
    The tests use the real robot kinematics module which should be available.
    """

    @staticmethod
    def _active_dry_run():
        """Return the active program's dry_run (test convenience accessor)."""
        import waldoctl

        active = waldoctl.commander.programs.active
        assert active is not None, "test setup did not create an active program"
        return active.dry_run

    @pytest.fixture(autouse=True)
    def setup_test_tab(self):
        """Create a test tab so path visualizer can store results.

        State reset is handled by conftest.reset_state fixture.
        This fixture only sets up the test tab needed for these tests.
        """
        import waldoctl
        from waldoctl import Program

        old_robot = ui_state.robot
        ui_state.robot = get_robot()

        # Clear change listeners to prevent UI rendering attempts without context
        simulation_state._change_listeners.clear()

        # Create a test tab so path visualizer can store results
        test_tab = Program(
            id="test-tab",
            filename="test.py",
            file_path=None,
            source="",
            _saved_source="",
        )
        waldoctl.commander.programs.items = [test_tab]
        waldoctl.commander.programs.active_id = "test-tab"

        yield

        simulation_state._change_listeners.clear()
        ui_state.robot = old_robot

    @pytest.mark.asyncio
    async def test_predicted_pass_skipped_after_first_probe_on_parol6(self):
        """parol6 plans and does not simulate, so its first predicted pass
        comes back as the plan: the same record, carrying nothing a plant
        would add. That is the last predicted pass the session runs for it
        — the next plan is not answered at all, and until a pass lands the
        predicted record is the commanded one.

        A program that draws a random target commands something else on the
        predicted pass's run, under the same revision, so that prediction
        answers commands the plan never had: it is dropped, and it settles
        nothing about the backend."""
        from waldo_commander.components.playback import layers_available

        visualizer = PathVisualizer()
        dry_run = self._active_dry_run()
        randomized = (
            "import random\n"
            "from parol6 import RobotClient\n"
            "rbt = RobotClient()\n"
            "rbt.move_j([85 + random.uniform(-5, 5), -85, 175, 5, 5, 175], speed=1.0)\n"
        )
        assert (
            await visualizer.update_path_visualization(randomized, revision=1) is None
        )
        assert dry_run.commanded is not None and dry_run.commanded_revision == 1
        assert await visualizer.update_physics_simulation("test-tab") is None
        assert dry_run.predicted is None
        assert "parol6" not in visualizer._predicted_diverges

        program = """
from parol6 import RobotClient
with RobotClient() as rbt:
    rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
"""
        assert await visualizer.update_path_visualization(program, revision=2) is None
        assert dry_run.commanded is not None and dry_run.commanded_revision == 2
        assert dry_run.predicted_current is None
        assert not any(layers_available(dry_run).values())

        assert await visualizer.update_physics_simulation("test-tab") is None
        assert visualizer._predicted_diverges["parol6"] is False
        predicted = dry_run.predicted_current
        assert predicted is not None and predicted.digest == dry_run.commanded.digest
        assert not any(layers_available(dry_run).values())

        again = program.replace("175]", "180]")
        assert await visualizer.update_path_visualization(again, revision=3) is None
        assert dry_run.predicted_current is None, "a new plan retires the old answer"
        assert await visualizer.update_physics_simulation("test-tab") is None
        assert dry_run.predicted is None, "the probe is not repeated this session"
        assert not visualizer.physics_in_flight("test-tab")

    @pytest.mark.asyncio
    async def test_sys_exit_entry_point_previews(self):
        """A script ending in ``sys.exit(main())`` previews its motion: exit
        status 0 is a normal finish, a failure status is the preview's error,
        and neither exit reaches the app. An exit part-way through keeps the
        motion planned before it and none after."""
        visualizer = PathVisualizer()
        program = """
import asyncio
import sys

import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
    return STATUS

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
"""

        error = await visualizer.update_path_visualization(
            program.replace("STATUS", "0")
        )
        assert error is None
        assert len(self._active_dry_run().path_segments) >= 1

        error = await visualizer.update_path_visualization(
            program.replace("STATUS", "3")
        )
        assert error is not None and "status 3" in error
        assert len(self._active_dry_run().path_segments) >= 1

        # An exit part-way through ends the preview where the real run ends.
        exits_early = """
import asyncio
import sys

import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
        sys.exit(0)
        await rbt.move_j([100, -100, 190, -10, -10, 190], speed=1.0)

asyncio.run(main())
"""
        assert await visualizer.update_path_visualization(exits_early) is None
        assert len(self._active_dry_run().path_segments) == 1

    @pytest.mark.asyncio
    async def test_moves_draw_in_metres_and_mark_literal_and_refused_targets(self):
        """Every planned move draws a segment in metres. A move written with
        literal coordinates gets a drag-to-edit target; one built from
        variables draws but has none. A move the backend refuses still marks
        where it was headed, red, in metres and radians — from its literal or
        computed pose, but not from joint angles, which name no TCP pose."""
        import math

        visualizer = PathVisualizer()
        program = """
import parol6

async def main():
    joints_b = [95, -95, 185, -5, -5, 185]
    somewhere = [100, 200, 300, 0, 0, 0]
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
        await rbt.move_j(joints_b, speed=1.0)
        await rbt.move_j([80, -80, 170, 10, 10, 170], speed=1.0)
        await rbt.move_l([100, 200, 300, 90, 0, 0], speed=50)
        await rbt.move_j(pose=[100, 200, 300, 0, 0, 0], speed=50)
        await rbt.move_j([0, 0, 0, 0, 0, 0], speed=50)
        await rbt.move_l(somewhere, speed=50)

import asyncio
asyncio.run(main())
"""

        def line(fragment: str) -> int:
            return next(
                n for n, text in enumerate(program.splitlines(), 1) if fragment in text
            )

        error = await visualizer.update_path_visualization(program)
        dry_run = self._active_dry_run()

        segments = dry_run.path_segments
        assert {line("[85, -85"), line("joints_b,"), line("[80, -80")} <= {
            s.line_number for s in segments
        }, [s.line_number for s in segments]
        coords = [c for s in segments for point in s.points for c in point[:3]]
        assert coords and all(abs(c) < 1.0 for c in coords), "segments are in metres"

        targets = {t.line_number: t for t in dry_run.targets}
        for literal in (line("[85, -85"), line("[80, -80")):
            assert targets[literal].id == f"auto_{literal}"
            assert targets[literal].is_valid
        assert line("joints_b,") not in targets, "a variable pose is not editable"

        refused = targets[line("90, 0, 0], speed=50")]
        assert not refused.is_valid
        assert refused.pose == pytest.approx([0.1, 0.2, 0.3, math.pi / 2, 0.0, 0.0])
        assert not targets[line("pose=[100")].is_valid
        assert not targets[line("somewhere, speed")].is_valid
        assert line("[0, 0, 0, 0, 0, 0], speed=50") not in targets
        assert error is not None and f"Line {line('90, 0, 0], speed=50')}" in error

    @pytest.mark.asyncio
    async def test_infeasible_duration_marks_segment_not_timing_feasible(self):
        """A move with an unrealistically short duration produces a path segment
        with timing_feasible=False so the editor can surface a warning diagnostic.
        """
        visualizer = PathVisualizer()

        program = """
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], duration=0.01)

import asyncio
asyncio.run(main())
"""

        await visualizer.update_path_visualization(program)

        assert len(self._active_dry_run().path_segments) >= 1, (
            f"Expected at least 1 segment, got {len(self._active_dry_run().path_segments)}"
        )

        infeasible = [
            s for s in self._active_dry_run().path_segments if not s.timing_feasible
        ]
        assert len(infeasible) >= 1, (
            "Expected at least one segment with timing_feasible=False; "
            f"got {[s.timing_feasible for s in self._active_dry_run().path_segments]}"
        )
        seg = infeasible[0]
        assert seg.estimated_duration is not None and seg.estimated_duration > 0.01, (
            f"Expected estimated_duration > requested 0.01s, got {seg.estimated_duration}"
        )

        # Blended moves plan as one segment, which takes the time of every
        # move in the blend, not only the first one's.
        def blended(first_s: float, second_s: float) -> str:
            return f"""
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], duration={first_s}, r=5, wait=False)
        await rbt.move_j([95, -95, 185, -5, -5, 185], duration={second_s})

import asyncio
asyncio.run(main())
"""

        await visualizer.update_path_visualization(blended(1.0, 1.0))
        segments = self._active_dry_run().path_segments
        assert segments and all(s.timing_feasible for s in segments), [
            (s.line_number, s.estimated_duration, s.requested_duration)
            for s in segments
        ]
        await visualizer.update_path_visualization(blended(0.01, 0.01))
        assert not all(
            s.timing_feasible for s in self._active_dry_run().path_segments
        ), "a blend too short for its moves is still flagged"


# ============================================================================
# Home and Checkpoint Tests
# ============================================================================


class TestHomeAndCheckpoints:
    """home() and checkpoint() on the plan record."""

    @staticmethod
    def _planned(client):
        client.close()
        record = client.plan()
        return record, segments_from_record(record, client.notes)

    def test_home_moves_a_referenced_arm_and_snaps_an_unreferenced_one(self):
        """A referenced arm homes along a planned move that takes time on the
        timeline, and the next move starts from home; an unreferenced arm
        references itself without a move."""
        from parol6.config import HOME_ANGLES_DEG

        from waldo_commander.services.timeline import Timeline

        client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        client.home()
        record, segments = self._planned(client)
        assert [s.checkpoint for s in segments] == [None, "home"]
        home = segments[1]
        assert home.rows == record.blocks[1].rows > 0
        assert home.estimated_duration > 0.0
        assert np.degrees(record.joints_rad[-1]) == pytest.approx(
            HOME_ANGLES_DEG, abs=0.6
        )
        assert client.angles() == pytest.approx(HOME_ANGLES_DEG, abs=0.6)
        tl = Timeline.from_record(record, segments)
        assert tl.segment_durations[1] > 0.0
        assert np.degrees(tl.sample(tl.total_duration).joints) == pytest.approx(
            HOME_ANGLES_DEG, abs=0.6
        )

        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient, initial_homed=False
        )
        client.home()
        record, segments = self._planned(client)
        assert record.rows <= 1, "referencing is a snap, not a move"
        assert [(s.checkpoint, s.rows) for s in segments] == [("home", record.rows)]
        assert segments[0].estimated_duration <= record.row_dt_s
        assert Timeline.from_record(record, segments).total_duration <= record.row_dt_s
        assert client.angles() == pytest.approx(HOME_ANGLES_DEG, abs=0.6)

        client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        client.home()
        client.move_j([90, -90, 140, 15, 50, 175], speed=1.0)
        record, _ = self._planned(client)
        third = record.blocks[2]
        assert third.rows >= 2
        assert np.degrees(record.joints_rad[third.start_row]) == pytest.approx(
            HOME_ANGLES_DEG, abs=1.0
        )

    def test_checkpoint_is_a_zero_width_marker(self):
        client = PathPreviewClient(dry_run_client_cls=DryRunRobotClient)
        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        client.checkpoint("pick_done")
        record, segments = self._planned(client)
        assert len(segments) == 2
        marker = segments[1]
        assert marker.checkpoint == "pick_done" and marker.move_type == "checkpoint"
        assert marker.rows == 0 and marker.points == []
        assert marker.estimated_duration == 0.0
        assert marker.start_row == record.rows, (
            "the marker sits where the program stood"
        )


# ============================================================================
# Tool Action Tracking Tests
# ============================================================================


class TestToolActionTracking:
    """Tests for tool action start_positions tracking across calls."""

    def test_tool_start_positions_across_calls(self):
        """close() then open() records correct start_positions for each action."""
        from waldo_commander.services.path_visualizer import _tool_metadata

        tool_actions: list = []
        tool_meta = _tool_metadata(get_robot("parol6"))

        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            tool_action_collector=tool_actions,
            tool_meta_registry=tool_meta,
            initial_gripper_calibrated=True,
        )

        client.select_tool("SSG-48", "pinch")
        client.tool.close()
        client.tool.open()

        assert len(tool_actions) == 2

        # First action: close — starts open (0.0), targets closed (1.0)
        assert tool_actions[0].start_positions == (0.0,)
        assert tool_actions[0].target_positions == (1.0,)

        # Second action: open — starts closed (1.0), targets open (0.0)
        assert tool_actions[1].start_positions == (1.0,)
        assert tool_actions[1].target_positions == (0.0,)


# ============================================================================
# Sim Pose Override Auto-Clear Tests
# ============================================================================


class TestSimPoseOverrideAutoClear:
    """Tests for the timestamp-based auto-clear of sim_pose_override.

    These drive the real ``_maybe_clear_sim_pose_override`` from ``main`` (the
    same function the status loop calls each tick) instead of re-deriving its
    condition, so a flipped comparison in production fails the test. The
    condition reads ``commander.programs.active.dry_run.playback.is_active`` to
    decide whether the user is still scrubbing; each test seeds an active
    program so the lookup resolves.
    """

    @staticmethod
    def _seed_program(is_active: bool):
        from tests.helpers.programs import ensure_active_program

        program = ensure_active_program()
        assert program is not None
        program.dry_run.playback.is_active = is_active
        return program

    def test_override_clears_only_once_scrubbing_has_stopped(self):
        """The override clears 100 ms after the last teleport — not while
        teleports are recent, not during playback, and not when no teleport
        was ever sent."""
        from waldo_commander.main import _maybe_clear_sim_pose_override

        for playing, seconds_ago, clears in (
            (False, 0.2, True),
            (False, 0.0, False),
            (True, 0.2, False),
            (False, None, False),
        ):
            self._seed_program(is_active=playing)
            playback_coordination.sim_pose_override = True
            playback_coordination.last_teleport_ts = (
                0.0 if seconds_ago is None else time.monotonic() - seconds_ago
            )

            _maybe_clear_sim_pose_override()

            case = (playing, seconds_ago)
            assert playback_coordination.sim_pose_override is not clears, case
            if clears:
                assert playback_coordination.last_teleport_ts == 0.0, case


# ============================================================================
# ScriptExecutionController Lifecycle Tests
# ============================================================================


class TestScriptExecutionLifecycle:
    """Tests for ScriptExecutionController subprocess lifecycle."""

    @pytest.mark.integration
    async def test_start_runs_a_subdir_program_and_reaps_one_whose_ui_fails(
        self, user, tmp_path, monkeypatch, caplog
    ):
        """A program loaded from a subdirectory runs to completion; a UI
        failure after launch reaps the child and clears run state."""
        from tests.helpers.wait import wait_for_app_ready
        from waldo_commander.components import script_execution as se

        await user.open("/")
        await wait_for_app_ready()
        active_program = waldoctl.commander.programs.active
        assert active_program is not None
        assert ui_state.active_textarea is not None
        assert ui_state.active_filename_input is not None
        se.script_exec.set_program_dir(tmp_path)

        content = 'print("subdirectory program finished")\n'
        ui_state.active_textarea.value = content
        ui_state.active_filename_input.value = "sub/regression.py"
        try:
            await se.script_exec.start()
            await user.should_see("subdirectory program finished", retries=100)
            # The exit code lands while the program's links are still closing;
            # the run ends after them.
            deadline = time.monotonic() + 10
            while (
                se.script_exec.last_exit_code is None or is_any_program_running()
            ) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert se.script_exec.last_exit_code == 0
            # The exit code lands before the run lets go of the program.
            deadline = time.monotonic() + 5
            while is_any_program_running() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert not is_any_program_running()
            written = tmp_path / ".runtime" / "sub" / "regression.py"
            assert written.read_text(encoding="utf-8") == content
        finally:
            if se.script_exec.script_handle is not None:
                await se.script_exec.stop()

        ui_state.active_textarea.value = (
            "import time\nwhile True:\n    time.sleep(0.1)\n"
        )
        ui_state.active_filename_input.value = "long_running.py"
        captured = {}
        run_script = se.run_script
        expand = se.log_panel.expand

        async def capture(*args, **kwargs):
            handle = await run_script(*args, **kwargs)
            captured["handle"] = handle
            monkeypatch.setattr(se.log_panel, "expand", fail_expand)
            return handle

        def fail_expand():
            monkeypatch.setattr(se.log_panel, "expand", expand)
            raise RuntimeError("test: UI failed after subprocess started")

        monkeypatch.setattr(se, "run_script", capture)
        try:
            await se.script_exec.start()
            assert "handle" in captured, "subprocess never started"
            assert captured["handle"]["proc"].returncode is not None
            assert se.script_exec.script_handle is None
            assert not active_program.execution.is_running
            assert not active_program.dry_run.playback.is_playing
            expected = (
                "Failed to start script: test: UI failed after subprocess started"
            )
            records = caplog.get_records("call")
            assert any(r.getMessage() == expected for r in records)
            records[:] = [r for r in records if r.getMessage() != expected]
        finally:
            handle = captured.get("handle")
            if handle and handle["proc"].returncode is None:
                from waldo_commander.services.script_runner import stop_script

                await stop_script(handle)

    @pytest.mark.asyncio
    async def test_cleanup_preserves_stepping_ipc_across_page_reload(
        self, tmp_path, monkeypatch
    ):
        """Per-page ``cleanup()`` must NOT close the stepping link or end the
        run's event watcher.

        Regression: pre-fix, ``cleanup()`` called ``cleanup_stepping()``,
        which tore the session down under a still-running program — the
        program then ran unmanaged (free-run) for the rest of its life.

        The step controller, session id, link and watcher belong to the run:
        the program keeps stepping and recording with no page, and the next
        page's ``set_ui_client`` only says where to show it.
        """
        from waldo_commander.components import script_execution as se
        from waldo_commander.services.stepping_client import GUIStepController

        # Simulate "script is running mid-stepping" — initialize a real
        # step controller (which opens the link), then flag the
        # simulation_state so the watcher-restart logic sees it.
        session_id = "test_cross_reload_ipc"
        step_controller = GUIStepController(session_id)
        step_controller.initialize()
        assert step_controller._listener is not None

        active_program = waldoctl.commander.programs.active
        if active_program is None:
            active_program = waldoctl.commander.programs.new(filename="ipc_test.py")
        old_running = active_program.execution.is_running
        old_session = se.script_exec._step_session_id
        old_controller = se.script_exec._step_controller
        old_watcher = se.script_exec._event_watcher_task
        old_client = se.script_exec._ui_client
        try:
            active_program.execution.is_running = True
            se.script_exec._step_session_id = session_id
            se.script_exec._step_controller = step_controller

            # Mock the watcher task — a never-completing coroutine that we
            # can verify was cancelled.
            watcher_started = asyncio.Event()
            watcher_cancelled = asyncio.Event()

            async def fake_watcher():
                watcher_started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    watcher_cancelled.set()
                    raise

            se.script_exec._event_watcher_task = asyncio.create_task(fake_watcher())
            await watcher_started.wait()

            watcher = se.script_exec._event_watcher_task
            # Per-page disconnect cleanup
            se.script_exec.cleanup()

            await asyncio.sleep(0)
            assert not watcher_cancelled.is_set(), "the run's watcher was cancelled"
            assert se.script_exec._event_watcher_task is watcher
            # Step controller + session preserved across the disconnect.
            assert se.script_exec._step_controller is step_controller
            assert se.script_exec._step_session_id == session_id
            # The link survived.
            assert step_controller._listener is not None

            # A new page connecting is where the same watcher shows the run.
            fake_client = MagicMock()
            se.script_exec.set_ui_client(fake_client)
            assert se.script_exec._event_watcher_task is watcher
            assert se.script_exec._ui_client is fake_client
        finally:
            # Drop the watcher before the link it polls is closed.
            if (
                se.script_exec._event_watcher_task is not None
                and not se.script_exec._event_watcher_task.done()
            ):
                se.script_exec._event_watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await se.script_exec._event_watcher_task
            active_program.execution.is_running = old_running
            se.script_exec._step_session_id = old_session
            se.script_exec._step_controller = old_controller
            se.script_exec._event_watcher_task = old_watcher
            se.script_exec._ui_client = old_client
            step_controller.cleanup()

    def test_import_order_script_execution_first(self):
        """script_execution must be importable before playback (no module cycle).

        Regression guard: pre-fix, ``playback.py`` did
        ``from waldo_commander.components.script_execution import script_exec``
        at module level, and ``script_execution.py`` reached for ``playback``
        via a module-alias import. Importing ``script_execution`` first raised
        ``ImportError: cannot import name 'script_exec' from partially
        initialized module``. Other imports happened to load ``playback`` first
        in normal runs, masking the bug. Run this in a fresh subprocess so the
        ambient ``sys.modules`` cache can't paper over a re-introduced cycle.
        """
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from waldo_commander.components.script_execution import script_exec; "
                "assert script_exec is not None",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Importing script_execution first failed:\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )


# ============================================================================
# Singleton listener-leak regression
# ============================================================================


def test_editor_panel_cleanup_removes_playback_listener():
    """Production regression guard: ``EditorPanel.cleanup()`` (now called from
    ``_on_disconnect``) must remove the per-page playback listener so a user
    reloading the browser tab doesn't leak one listener per reload. It also
    runs again from ``_on_shutdown``, so a second call must be harmless.
    """
    from waldo_commander.components.editor import EditorPanel
    from waldo_commander.components.playback import playback

    baseline = len(simulation_state._change_listeners)
    simulation_state.add_change_listener(playback._on_state_change)
    assert len(simulation_state._change_listeners) == baseline + 1

    # Build a panel so cleanup() has something to delegate to. We only need
    # cleanup() to fire, not build() — the playback singleton is module-level.
    panel = EditorPanel()
    panel.cleanup()

    assert len(simulation_state._change_listeners) == baseline, (
        f"editor_panel.cleanup() leaked: baseline={baseline}, "
        f"now={len(simulation_state._change_listeners)}"
    )
    panel.cleanup()
    assert len(simulation_state._change_listeners) == baseline


# ============================================================================
# Per-tab log routing
# ============================================================================


def test_script_output_appends_to_launching_tab_only():
    """Per-tab log isolation: a script's stdout/stderr must land in the
    output_log of the tab that started the script, regardless of which
    tab the user is currently viewing.
    """
    import waldoctl
    from waldoctl import Program

    from waldo_commander.components.script_execution import script_exec

    # Set up two tabs with empty logs.
    tab_a = Program(
        id="tab-a", filename="a.py", file_path=None, source="", _saved_source=""
    )
    tab_b = Program(
        id="tab-b", filename="b.py", file_path=None, source="", _saved_source=""
    )
    waldoctl.commander.programs.items = [tab_a, tab_b]
    waldoctl.commander.programs.active_id = "tab-a"

    # Script launched from tab A; record lines while user switches to B.
    script_exec._script_tab_id = "tab-a"
    script_exec._record_line("line1 while on A")
    waldoctl.commander.programs.active_id = "tab-b"
    script_exec._record_line("line2 after switching to B")
    script_exec._record_line("line3 still on B")
    waldoctl.commander.programs.active_id = "tab-a"
    script_exec._record_line("line4 back on A")

    assert [e.text for e in tab_a.log.entries] == [
        "line1 while on A",
        "line2 after switching to B",
        "line3 still on B",
        "line4 back on A",
    ], "All script output must accumulate in the launching tab's log"
    assert len(tab_b.log.entries) == 0, "Tab B owns its own (empty) log"

    # Once the run is over, post-completion scrubbing falls back to the
    # active tab instead of staying pinned to the (often hidden) launcher.
    assert script_exec.launching_tab_id == "tab-a"
    script_exec._reset_state()
    assert script_exec.launching_tab_id is None


# Note: the legacy ``output_log`` cap test (1000-entry FIFO) was a WC-specific
# implementation detail tied to ``ui.log(max_lines=1000)``. ``Program.log.entries``
# is unbounded by design; host applications cap visibly via the widget. The cap
# may be reintroduced as a host-level concern but isn't part of the public surface.


def test_notify_step_changed_only_fires_step_listeners():
    """Regression: step events route through the dedicated step channel so
    urdf_scene's ``_update_simulation_view`` (a change-channel listener)
    doesn't re-walk segment fingerprints on every ~20Hz step event."""
    change_count = [0]
    step_count = [0]

    def on_change():
        change_count[0] += 1

    def on_step():
        step_count[0] += 1

    simulation_state.add_change_listener(on_change)
    simulation_state.add_step_listener(on_step)
    try:
        simulation_state.notify_step_changed()
        assert step_count[0] == 1
        assert change_count[0] == 0, "Step channel must not fire change listeners"

        simulation_state.notify_changed()
        assert change_count[0] == 1
        assert step_count[0] == 1, "Change channel must not fire step listeners"
    finally:
        simulation_state.remove_change_listener(on_change)
        simulation_state.remove_step_listener(on_step)


class TestSimulatedRunPlumbing:
    """The predicted record reaching the display: what the overlay draws
    from a commanded/predicted pair and when it leaves the scene alone."""

    @staticmethod
    def _records(rows: int = 6, joints: int = 6) -> tuple[TickIndex, TickIndex]:
        """A pair the way a backend reports them: the predicted arm lags
        its command, and the lag grows along the run."""
        t = np.linspace(0.0, 1.0, rows, dtype=np.float32)
        commanded = np.tile(t[:, None], (1, joints))
        predicted = commanded - np.tile(t[:, None], (1, joints)) * 0.01
        tcp = np.zeros((rows, 6), dtype=np.float32)
        tcp[:, 0] = 0.3 + t * 0.1
        blocks = (waldoctl.TickBlock(command=0, start_row=0, rows=rows, line_number=3),)

        def record(q: np.ndarray, digest: bytes) -> TickIndex:
            return waldoctl.TickIndex(
                row_dt_s=0.02,
                joints_rad=q.astype(np.float32),
                tcp=tcp,
                tool_closed=np.zeros(rows, dtype=np.float32),
                tool_gripping=np.zeros(rows, dtype=np.bool_),
                blocks=blocks,
                digest=digest,
            )

        return record(commanded, b"commanded"), record(predicted, b"predicted")

    def test_a_record_reports_its_geometry_and_a_pair_its_following_error(self):
        commanded, predicted = self._records(rows=6)

        assert predicted.rows == 6
        assert predicted.duration_s == pytest.approx(0.12)
        # Time maps to a row, and rows past the end clamp rather than
        # raising: a scrub bar dragged to the far right must land
        # somewhere real.
        assert predicted.row_at(0.0) == 0
        assert predicted.row_at(0.05) == 2
        assert predicted.row_at(99.0) == 5
        assert predicted.block_at(3) is predicted.blocks[0]
        assert predicted.block_at(99) is None

        err = following_error(commanded, predicted)
        assert err.shape == (6,)
        assert err[0] == pytest.approx(0.0)
        assert err[-1] > err[1], "the following error must grow along the run"
        assert not following_error(commanded, commanded).any()

    def test_following_error_colors_run_from_on_track_to_diverged(self):
        from waldo_commander.services.urdf_scene.physics_overlay import (
            FULL_FOLLOWING_ERROR_RAD,
            following_error_colors,
        )

        colors = following_error_colors(
            np.array([0.0, FULL_FOLLOWING_ERROR_RAD / 2, FULL_FOLLOWING_ERROR_RAD * 3])
        )
        assert len(colors) == 3
        on_track, half, diverged = colors
        # Green where the arm is doing what it was told, red where it is
        # not, and saturating past the scale rather than running off it.
        assert on_track[1] > on_track[0], "on-track reads green"
        assert diverged[0] > diverged[1], "diverged reads red"
        assert on_track[1] > half[1] > diverged[1]
        assert (
            diverged == following_error_colors(np.array([FULL_FOLLOWING_ERROR_RAD]))[0]
        )

    def test_an_unchanged_pair_is_not_redrawn_and_a_prediction_that_is_the_plan_draws_nothing(
        self,
    ):
        """The flash guard: an identical pair repaints nothing.

        The backend guarantees a bit-identical record for the same
        program, so equal digests mean an equal picture — and the overlay
        must take that as permission to leave the scene alone. A
        predicted record equal to the commanded one shows nothing the
        commanded path does not already, so nothing is built for it.
        """
        from waldo_commander.services.urdf_scene.physics_overlay import PhysicsOverlay

        commanded, predicted = self._records()
        overlay = PhysicsOverlay(MagicMock(scene=None))
        # No live scene: the build is a no-op and nothing is cached, so a
        # later call with a real scene still builds.
        overlay.render(commanded, predicted, show_predicted=True)
        assert not overlay.is_built

        overlay._digest = (commanded.digest, predicted.digest)
        overlay._group = object()
        overlay.render(commanded, predicted, show_predicted=True)
        assert overlay.is_built, "an identical pair must not tear the group down"

        overlay.render(commanded, commanded, show_predicted=True)
        assert not overlay.is_built, "a prediction that is the plan draws nothing"

        overlay._group = object()
        overlay.render(commanded, None, show_predicted=True)
        assert not overlay.is_built, "no predicted record, no overlay"

    def test_a_long_run_is_decimated_without_losing_a_following_error_spike(self):
        """A ten-minute record is 30,000 rows; every one would cross as a
        point triple and a colour triple in one scene command built on the
        event loop. Positions are sampled, but the error is taken as the
        max over each collapsed span — a spike lasting three rows is the
        whole reason the overlay exists.
        """
        from waldo_commander.services.urdf_scene.physics_overlay import (
            MAX_PREDICTED_POINTS,
            decimate,
        )

        rows = 30_000
        tcp = np.zeros((rows, 6), dtype=np.float32)
        tcp[:, 0] = np.linspace(0.0, 1.0, rows)
        error = np.full(rows, 1e-4, dtype=np.float32)
        error[17_000:17_003] = 0.05  # a three-row spike

        points, worst = decimate(tcp, error)
        assert len(points) == len(worst) <= MAX_PREDICTED_POINTS
        assert points[0][0] == pytest.approx(tcp[0][0])
        assert worst.max() == pytest.approx(0.05), (
            "sampling the error would drop a spike shorter than the stride"
        )
        # Short runs are left alone.
        assert decimate(tcp[:10], error[:10])[0].shape[0] == 10


class TestWorldStateRepair:
    """One way the shape layers could be left in a state no edit escapes, and
    one way the scene lied about the ground. (Readback adopting a drafted name
    is driven through the real client in test_collision_viz.py.)"""

    def test_a_refused_withdrawal_keeps_the_proposal(self):
        """Withdrawing moves both layers at once.

        Discarding first and re-adding after destroys the drafted
        geometry whenever the re-add is refused — and the caller has
        nothing left to put back.
        """
        from waldoctl.shapes import Box

        from waldo_commander.services.urdf_scene.scene_handle import WcSceneHandle

        handle = WcSceneHandle()
        handle._installation_draft = (Box(name="wall", x=1, y=1, z=1),)
        handle._shapes = []

        with patch.object(WcSceneHandle, "_assign", side_effect=ValueError("refused")):
            with pytest.raises(ValueError):
                handle.withdraw_proposal("wall")

        assert [s.name for s in handle.installation_draft] == ["wall"], (
            "a refusal must leave the proposal where it was"
        )

    def test_only_actual_ground_replaces_the_placeholder_disc(self):
        """A table is not a floor.

        The disc stands in for a backend that describes no ground; hiding
        it for any non-empty installation leaves a table floating in the
        void, and the table-only layout is the example the backend's own
        docs give.
        """
        from waldoctl.shapes import Box, Cylinder

        from waldo_commander.services.urdf_scene.urdf_scene import _is_ground

        floor = Box(name="floor", x=6, y=6, z=0.2, pose=(0, 0, -0.1, 0, 0, 0))
        table = Box(name="table", x=1, y=1, z=0.7, pose=(0.6, 0, 0.35, 0, 0, 0))
        # A cylinder spanning the origin at floor level counts too — the
        # rule is about where a solid sits, not what kind it is.
        disc = Cylinder(name="pad", radius=3.0, length=0.1, pose=(0, 0, -0.05, 0, 0, 0))
        assert _is_ground(floor)
        assert _is_ground(disc)
        assert not _is_ground(table), "a table beside the robot is not the floor"
        # A slab that spans the origin but rises above the base is furniture.
        assert not _is_ground(
            Box(name="plinth", x=6, y=6, z=1.0, pose=(0, 0, 0.5, 0, 0, 0))
        )
