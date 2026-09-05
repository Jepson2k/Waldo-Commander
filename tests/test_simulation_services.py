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

from waldo_commander.profiles import get_robot
from waldo_commander.state import (
    playback_coordination,
    simulation_state,
    robot_state,
    ui_state,
)
from parol6.client.dry_run_client import DryRunRobotClient
from tests.helpers.programs import set_active_recording
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.services.path_preview_client import PathPreviewClient
from waldo_commander.services.motion_recorder import MotionRecorder
from waldo_commander.services.path_visualizer import PathVisualizer
from waldo_commander.services.urdf_scene.envelope_renderer import WorkspaceEnvelope


# ============================================================================
# Dry Run Client Tests
# ============================================================================


class TestDryRunClient:
    """Tests for dry run simulation client (PathPreviewClient).

    The client delegates to parol6's PathPreviewClient which runs commands
    through the real command pipeline. No mocking of PAROL6_ROBOT needed.
    """

    @pytest.mark.asyncio
    async def test_move_joints_creates_path_segment(self):
        """move_j should create a path segment with joint data."""
        segments: list[dict] = []
        targets: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
            target_collector=targets,
        )

        # Use angles away from singularities (J5 != 0 avoids gimbal lock)
        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)

        # Verify path segment created in collector
        assert len(segments) == 1
        segment = segments[0]
        assert segment["is_valid"] is True
        assert segment["joints"] is not None
        assert len(segment["joints"]) == 6
        assert segment["move_type"] == "joints"
        # Verify full joint trajectory is present for smooth playback
        assert segment["joint_trajectory"] is not None
        assert len(segment["joint_trajectory"]) >= 2  # At least start and end
        assert len(segment["joint_trajectory"][0]) == 6  # 6 joints per waypoint

    @pytest.mark.asyncio
    async def test_move_cartesian_creates_path_segment(self):
        """move_l should create a path segment with cartesian data."""
        segments: list[dict] = []
        targets: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
            target_collector=targets,
        )

        client.move_l([150, 100, 250, 0, 0, 0], speed=1.0)

        # Verify segment created
        assert len(segments) == 1
        segment = segments[0]
        assert segment["is_valid"] is True
        assert segment["move_type"] == "cartesian"

    @pytest.mark.asyncio
    async def test_move_creates_segment_but_not_target_without_literal_args(self):
        """Moves without literal list arguments should create segment but not target.

        ProgramTarget objects are only created when the source line has
        literal list arguments (not variables). When move_j is called
        directly (not via code parsing), there's no source line to
        inspect, so no target is created.
        """
        segments: list[dict] = []
        targets: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
            target_collector=targets,
        )

        # Use angles away from singularities (J5 != 0 avoids gimbal lock)
        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)

        # Verify path segment was created (always created for visualization)
        assert len(segments) == 1
        segment = segments[0]
        assert segment["move_type"] == "joints"
        assert segment["joints"] is not None

        # Verify NO target was created (no TARGET marker in source)
        assert len(targets) == 0

    @pytest.mark.asyncio
    async def test_unreachable_cartesian_creates_error_result(self):
        """Unreachable cartesian target produces per-pose valid/invalid segments."""
        segments: list[dict] = []
        targets: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
            target_collector=targets,
        )

        # Extremely far target — mostly unreachable
        client.move_l([9999, 9999, 9999, 0, 0, 0], speed=1.0)

        # Per-pose IK diagnostic produces green (valid) + red (invalid) segments
        assert len(segments) >= 1
        has_invalid = any(not s["is_valid"] for s in segments)
        assert has_invalid, (
            "Expected at least one invalid segment for unreachable target"
        )


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
    ensure_active_program()
    yield mock_textarea
    ui_state.editor_panel = None
    ui_state.active_textarea = None
    ui_state.robot = old_robot


class TestMotionRecorder:
    """Tests for motion recording functionality (code-insertion API)."""

    def test_capture_current_pose_inserts_code(self, mock_textarea):
        """capture_current_pose should insert move_l code into editor."""
        set_robot_pose(150.0, 250.0, 350.0)

        recorder = MotionRecorder()
        recorder.capture_current_pose()

        inserted_code = mock_textarea.value
        assert "rbt.move_l([150.000, 250.000, 350.000" in inserted_code
        assert "speed=" in inserted_code
        assert "accel=" in inserted_code

    def test_capture_current_pose_joints_mode(self, mock_textarea):
        """capture_current_pose with joints mode should insert move_j code."""
        waldoctl.commander.status.joints.angles.set_deg(
            np.array([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
        )

        recorder = MotionRecorder()
        recorder.capture_current_pose(move_type="joints")

        inserted_code = mock_textarea.value
        assert "rbt.move_j([10.00, 20.00, 30.00, 40.00, 50.00, 60.00" in inserted_code

    def test_toggle_recording_lifecycle(self, mock_textarea):
        """toggle_recording should toggle recording state on/off."""
        recorder = MotionRecorder()

        # Initially not recording
        assert not is_any_program_recording()

        # First toggle starts recording
        recorder.toggle_recording()
        assert is_any_program_recording()

        # Second toggle stops recording
        recorder.toggle_recording()
        assert not is_any_program_recording()

    def test_jog_recording_lifecycle(self, mock_textarea):
        """Test complete jog recording cycle: start sets state, end inserts code."""
        set_robot_pose(100.0, 200.0, 300.0)
        waldoctl.commander.status.joints.angles.set_deg(np.zeros(6))

        recorder = MotionRecorder()
        recorder.toggle_recording()  # Start recording

        # --- Part 1: on_jog_start should set active jog ---
        recorder.on_jog_start("cartesian", "X+")

        assert recorder._active_jog is not None
        assert recorder._active_jog.move_type == "cartesian"
        assert recorder._active_jog.axis_info == "X+"

        # --- Part 2: on_jog_end should insert code ---
        # Simulate robot movement during jog (need time to pass > 0.1s)
        time.sleep(0.15)
        set_robot_pose(150.0, 250.0, 350.0)

        recorder.on_jog_end()

        # Check that code was inserted
        inserted_code = mock_textarea.value
        assert "rbt.move_l(" in inserted_code

    def test_jog_events_ignored_when_not_recording(self):
        """Jog start and end events should be ignored when not recording."""

        recorder = MotionRecorder()
        mock_textarea = MagicMock()
        mock_textarea.value = ""
        ui_state.active_textarea = mock_textarea

        # Not recording - jog start should be ignored
        recorder.on_jog_start("joint", "J1+")
        assert recorder._active_jog is None

        # Not recording - jog end should also be ignored
        recorder.on_jog_end()
        assert mock_textarea.value == ""

        ui_state.active_textarea = None

    def test_record_action_home_generates_code(self, mock_textarea):
        """record_action for home should generate home code."""
        recorder = MotionRecorder()
        set_active_recording(True)

        recorder.record_action("home")

        inserted_code = mock_textarea.value
        assert "rbt.home()" in inserted_code

    def test_record_action_gripper_commands(self, mock_textarea):
        """record_action for gripper should generate tool access + method calls."""
        recorder = MotionRecorder()
        set_active_recording(True)

        # Part 1: Calibrate command
        recorder.record_action("gripper", calibrate=True)
        inserted_code = mock_textarea.value
        assert "rbt.tool.calibrate()" in inserted_code

        # Part 2: Move command with params (partial position → set_position)
        mock_textarea.value = ""
        recorder.record_action("gripper", position=0.5, speed=50, current=200)
        inserted_code = mock_textarea.value
        assert "rbt.tool.set_position(0.5, speed=50, current=200)" in inserted_code

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

    def test_record_action_io(self, mock_textarea):
        """record_action for io should generate write_io code."""
        recorder = MotionRecorder()
        set_active_recording(True)

        recorder.record_action("io", port=1, state=1)

        inserted_code = mock_textarea.value
        assert "rbt.write_io(1, 1)" in inserted_code

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

    def test_record_action_ignored_when_not_recording(self, mock_textarea):
        """record_action should be ignored when not recording."""
        recorder = MotionRecorder()
        set_active_recording(False)

        recorder.record_action("home")

        # Code should not have been inserted (still just initial code)
        assert mock_textarea.value == "# Initial code\n"

    def test_multiple_jogs_insert_multiple_code_lines(self, mock_textarea):
        """Multiple jog start/end cycles should insert multiple code lines."""
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

        # Should have inserted code for both moves
        inserted_code = mock_textarea.value
        # Count occurrences of move commands
        assert inserted_code.count("rbt.move") >= 2

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

    def test_wall_time_initialized_on_recording_start(self, mock_textarea):
        """_last_action_wall_time resets to 0 when recording starts."""
        recorder = MotionRecorder()
        recorder._last_action_wall_time = 99.0
        recorder.toggle_recording()
        assert recorder._last_action_wall_time == 0.0
        recorder.toggle_recording()

    def test_wall_time_updated_after_record_action(self, mock_textarea):
        """_last_action_wall_time is stamped after each recorded action."""
        recorder = MotionRecorder()
        recorder.toggle_recording()
        assert recorder._last_action_wall_time == 0.0

        set_robot_pose(100, 200, 300)
        recorder.capture_current_pose()
        assert recorder._last_action_wall_time > 0

        recorder.toggle_recording()

    def test_gap_inserted_between_non_jog_actions(self, mock_textarea):
        """A time.sleep() is inserted when wall-clock time elapses between actions."""
        recorder = MotionRecorder()
        recorder.toggle_recording()

        # Record first action
        recorder.record_action("gripper", position=0.5)
        first_wall = recorder._last_action_wall_time
        assert first_wall > 0

        # Simulate elapsed time
        time.sleep(0.2)

        # Record second action — should insert a delay
        recorder.record_action("gripper", position=1.0)

        inserted_code = mock_textarea.value
        assert "time.sleep(" in inserted_code, (
            "Expected time.sleep() to be inserted for gap between actions"
        )

        recorder.toggle_recording()

    def test_no_gap_for_motion_actions(self, mock_textarea):
        """Motion actions (move_j/move_l) don't get delay inserted before them."""
        recorder = MotionRecorder()
        recorder.toggle_recording()

        # Record a gripper action first
        recorder.record_action("gripper", position=0.5)
        time.sleep(0.2)

        # Record a motion — should NOT get a delay
        set_robot_pose(100, 200, 300)
        recorder.record_action(
            "move_j",
            angles=[0, 0, 0, 0, 0, 0],
            speed=0.5,
            accel=0.5,
        )

        inserted_code = mock_textarea.value
        # time.sleep should NOT appear between gripper and move_j
        lines = inserted_code.strip().split("\n")
        # Find the move_j line and check the line before it
        for i, line in enumerate(lines):
            if "rbt.move_j" in line and i > 0:
                assert "time.sleep" not in lines[i - 1], (
                    "No delay should be inserted before a motion command"
                )

        recorder.toggle_recording()

    def test_flush_sets_wall_time_to_last_pending(self, mock_textarea):
        """After flushing pending actions, wall time = last pending action time."""
        recorder = MotionRecorder()
        recorder.toggle_recording()

        set_robot_pose(100, 200, 300)
        recorder.on_jog_start("cartesian", "X+")

        # Queue a pending action during the jog
        time.sleep(0.1)
        recorder.record_action("gripper", position=0.5)
        assert len(recorder._pending_actions) == 1
        queued_time = recorder._pending_actions[0][2]

        # End the jog — flushes pending actions
        set_robot_pose(200, 200, 300)
        time.sleep(0.1)
        recorder.on_jog_end()

        # Wall time should be set to the queued action's timestamp
        assert recorder._last_action_wall_time == pytest.approx(queued_time, abs=0.01)

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

    def test_reset_clears_data(self, envelope):
        """reset should clear all generated data."""
        envelope.max_reach = 0.65
        envelope._generated = True

        envelope.reset()

        assert envelope.max_reach == 0.0
        assert envelope._generated is False

    def test_generate_sync_creates_max_reach_with_valid_robot(self, envelope):
        """generate_sync should calculate max_reach when robot is available.

        This test uses the real PAROL6_ROBOT module since _generate_envelope_cpu_bound
        imports it directly and mocks don't transfer to separate processes.
        """
        # Use generate_sync which runs in-process
        result = envelope.generate_sync(samples=64)  # 64 = 2^6 for grid sampling

        # With real robot module, should calculate max_reach
        if result:
            assert envelope._generated is True
            assert envelope.max_reach > 0
            # PAROL6 robot typically has reach around 0.6-0.7 meters
            assert 0.3 < envelope.max_reach < 1.0
        else:
            # Robot module may not be available in test environment
            assert envelope._generated is False

    def test_generate_skips_if_already_generated(self, envelope):
        """generate should return True immediately if already generated."""
        envelope._generated = True

        result = envelope.generate(samples=10)

        assert result is True

    def test_generate_sync_handles_exceptions_gracefully(self, envelope):
        """generate_sync should catch exceptions without crashing.

        The _generate_envelope_cpu_bound function handles exceptions internally
        and returns None on failure. generate_sync should handle this gracefully.
        """
        # generate_sync uses _generate_envelope_cpu_bound which handles exceptions
        # If robot module has issues, it should return False without crashing
        _result = envelope.generate_sync(samples=64)

        # Whether it succeeds depends on robot module availability
        # The key is it doesn't crash and _generating flag is reset
        assert envelope._generating is False

    def test_concurrent_generation_prevented(self, envelope):
        """generate should return True when already generating (indicates in-progress).

        The async generate() returns True when generation is already in progress
        since starting/in-progress are both valid states for non-blocking generation.
        """
        envelope._generating = True

        result = envelope.generate(samples=10)

        # Returns True because generation is in progress (valid state)
        assert result is True

    @pytest.mark.parametrize(
        "offset,expected",
        [
            (0.05, 0.65),  # Positive offset extends reach
            (-0.05, 0.65),  # Negative offset uses abs()
            (0.0, 0.6),  # Zero offset returns base reach
        ],
    )
    def test_get_radius_with_tool_offset(self, envelope, offset, expected):
        """get_radius_with_tool_offset should add abs(offset) to max_reach."""
        envelope.max_reach = 0.6  # 600mm base reach

        effective_radius = envelope.get_radius_with_tool_offset(offset)

        assert effective_radius == expected, (
            f"With offset={offset}, expected {expected}, got {effective_radius}"
        )


# ============================================================================
# Editor Auto-Simulation Tests
# ============================================================================


class TestEditorAutoSimulation:
    """Tests for editor auto-simulation on code change."""

    def test_debounce_defaults(self):
        """SimulationEngine should have correct debounce defaults."""
        from waldo_commander.components.simulation_engine import simulation

        assert simulation._debounce_delay == 1.0

    def testschedule_debounced_simulation_creates_timer(self):
        """schedule_debounced_simulation should create a timer."""
        from waldo_commander.components.simulation_engine import simulation
        import waldoctl

        with patch("waldo_commander.components.simulation_engine.ui") as mock_ui:
            mock_timer = MagicMock()
            mock_ui.timer.return_value = mock_timer

            waldoctl.commander.programs.active_id = "test-tab"
            simulation._simulation_debounce_timer = None

            simulation.schedule_debounced_simulation()

            mock_ui.timer.assert_called_once()
            call_args = mock_ui.timer.call_args
            assert call_args[0][0] == 1.0
            assert call_args[1]["once"] is True

    def testschedule_debounced_simulation_cancels_previous_timer(self):
        """Calling schedule_debounced_simulation again should cancel previous timer."""
        from waldo_commander.components.simulation_engine import simulation
        import waldoctl

        with patch("waldo_commander.components.simulation_engine.ui") as mock_ui:
            mock_timer1 = MagicMock()
            mock_timer2 = MagicMock()
            mock_ui.timer.side_effect = [mock_timer1, mock_timer2]

            waldoctl.commander.programs.active_id = "test-tab"
            simulation._simulation_debounce_timer = None

            simulation.schedule_debounced_simulation()
            assert simulation._simulation_debounce_timer == mock_timer1

            simulation.schedule_debounced_simulation()
            mock_timer1.cancel.assert_called_once_with(with_current_invocation=True)
            assert simulation._simulation_debounce_timer == mock_timer2

    @pytest.mark.asyncio
    async def test_run_simulation_calls_path_visualizer(self):
        """run_simulation should call path_visualizer.update_path_visualization."""
        from waldo_commander.components.simulation_engine import simulation
        import waldoctl

        with patch("waldo_commander.components.simulation_engine.ui"):
            with patch(
                "waldo_commander.components.simulation_engine.path_visualizer"
            ) as mock_visualizer:
                update_called = False
                update_content = None

                async def mock_update(content, tab_id=None):
                    nonlocal update_called, update_content
                    update_called = True
                    update_content = content

                mock_visualizer.update_path_visualization = mock_update

                mock_textarea = MagicMock()
                mock_textarea.value = "rbt.move_j([0,0,0,0,0,0])"
                ui_state.active_textarea = mock_textarea
                waldoctl.commander.programs.active_id = "tab_under_test"
                ui_state.textareas_by_tab["tab_under_test"] = mock_textarea

                try:
                    await simulation.run_simulation()
                finally:
                    ui_state.textareas_by_tab.pop("tab_under_test", None)
                    waldoctl.commander.programs.active_id = None
                    ui_state.active_textarea = None

                assert update_called is True
                assert update_content == "rbt.move_j([0,0,0,0,0,0])"

    @pytest.mark.asyncio
    async def test_run_simulation_empty_content_skips_visualization(self):
        """run_simulation should skip visualization when content is empty."""
        from waldo_commander.components.simulation_engine import simulation
        import waldoctl

        with patch("waldo_commander.components.simulation_engine.ui"):
            with patch(
                "waldo_commander.components.simulation_engine.path_visualizer"
            ) as mock_visualizer:
                update_called = False

                async def mock_update(content, tab_id=None):
                    nonlocal update_called
                    update_called = True

                mock_visualizer.update_path_visualization = mock_update

                mock_textarea = MagicMock()
                mock_textarea.value = ""
                ui_state.active_textarea = mock_textarea
                waldoctl.commander.programs.active_id = "tab_under_test"
                ui_state.textareas_by_tab["tab_under_test"] = mock_textarea

                try:
                    await simulation.run_simulation()
                finally:
                    ui_state.textareas_by_tab.pop("tab_under_test", None)
                    waldoctl.commander.programs.active_id = None
                    ui_state.active_textarea = None

                assert update_called is False


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
        from waldo_commander.state import simulation_state
        import waldoctl
        from waldoctl import Program
        from waldo_commander.services.path_visualizer import PathVisualizer

        # Create two tabs
        tab1 = Program(
            id="tab1", filename="a.py", file_path=None, source="", _saved_source=""
        )
        tab2 = Program(
            id="tab2", filename="b.py", file_path=None, source="", _saved_source=""
        )
        waldoctl.commander.programs.items = [tab1, tab2]
        waldoctl.commander.programs.active_id = "tab2"  # Active is tab2

        # Under pytest the visualizer always takes the in-process path, so mock
        # the sim entry point; notify_changed avoids a slot stack error.
        with (
            patch(
                "waldo_commander.services.path_visualizer._run_simulation_isolated",
                return_value={
                    "segments": [],
                    "targets": [],
                    "truncated": False,
                    "error": None,
                    "total_steps": 0,
                    "final_joints_rad": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
                },
            ),
            patch.object(simulation_state, "notify_changed"),
        ):
            visualizer = PathVisualizer()
            # Run simulation for tab1 (not active)
            await visualizer.update_path_visualization("print('hi')", tab_id="tab1")

            # Results should be in tab1, not tab2
            assert tab1.dry_run.final_joints_rad == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
            assert tab2.dry_run.final_joints_rad is None

    def test_simulation_returns_final_joints_rad(self):
        """Simulation result includes final_joints_rad for caching."""
        from waldo_commander.services.path_visualizer import _run_simulation_isolated

        program = """
from parol6 import RobotClient
rbt = RobotClient()
rbt.home()
"""
        result = _run_simulation_isolated(
            program,
            dry_run_client_cls=DryRunRobotClient,
        )

        assert "final_joints_rad" in result
        if result["final_joints_rad"] is not None:
            assert len(result["final_joints_rad"]) == 6


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
    async def test_visualizer_executes_simple_program(self):
        """PathVisualizer should execute program and create path segments.

        Uses real PAROL6_ROBOT module in subprocess - no mocking needed.
        Joint targets must be within PAROL6 limits:
        J1: [-123, 123], J2: [-145, -3.4], J3: [108, 288],
        J4: [-105, 105], J5: [-90, 90], J6: [0, 360]
        """
        visualizer = PathVisualizer()

        # Valid joint targets within PAROL6 limits
        program = """
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
"""

        await visualizer.update_path_visualization(program)

        # Should have created at least one segment
        assert len(self._active_dry_run().path_segments) >= 1, (
            f"Expected at least 1 segment, got {len(self._active_dry_run().path_segments)}"
        )

    @pytest.mark.asyncio
    async def test_visualizer_updates_total_steps(self):
        """PathVisualizer should update total_steps after simulation."""
        visualizer = PathVisualizer()

        # Valid joint targets within PAROL6 limits
        program = """
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([80, -80, 170, 10, 10, 170], speed=1.0)
        await rbt.move_j([100, -100, 190, -10, -10, 190], speed=1.0)
"""

        await visualizer.update_path_visualization(program)

        # Should have 2 segments and total_steps should match
        assert self._active_dry_run().total_steps == len(
            self._active_dry_run().path_segments
        )

    @pytest.mark.asyncio
    async def test_visualizer_joint_coordinates_in_meters(self):
        """Path segment coordinates should be in meters (not mm).

        Joint moves produce TCP poses via FK. The segment points should be
        converted from mm to meters for the 3D scene which uses SI units.
        """
        visualizer = PathVisualizer()

        # Valid joint move within PAROL6 limits
        program = """
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
"""

        await visualizer.update_path_visualization(program)

        # Should have created a segment
        assert len(self._active_dry_run().path_segments) >= 1, (
            f"Expected at least 1 segment, got {len(self._active_dry_run().path_segments)}"
        )

        # Check that all points are in meters (not mm)
        # PAROL6 workspace is ~600mm reach, so all coords should be < 1.0m
        segment = self._active_dry_run().path_segments[-1]
        end_point = segment.points[-1]  # [x, y, z]

        assert abs(end_point[0]) < 1.0, (
            f"X coordinate {end_point[0]} appears to be in mm, expected meters"
        )
        assert abs(end_point[1]) < 1.0, (
            f"Y coordinate {end_point[1]} appears to be in mm, expected meters"
        )
        assert abs(end_point[2]) < 1.0, (
            f"Z coordinate {end_point[2]} appears to be in mm, expected meters"
        )

    @pytest.mark.asyncio
    async def test_literal_moves_create_targets(self):
        """Moves with literal coordinates create auto-generated targets for 3D editing."""
        visualizer = PathVisualizer()

        program = """
import parol6

async def main():
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
        await rbt.move_j([95, -95, 185, -5, -5, 185], speed=1.0)
"""

        await visualizer.update_path_visualization(program)

        assert len(self._active_dry_run().path_segments) >= 2, (
            f"Expected at least 2 segments, got {len(self._active_dry_run().path_segments)}"
        )

        assert len(self._active_dry_run().targets) == 2, (
            f"Expected 2 targets (one per literal move), got {len(self._active_dry_run().targets)}. "
            f"Bug: compile() may not be using 'simulation_script.py' filename for frame inspection."
        )

        target_ids = [t.id for t in self._active_dry_run().targets]
        assert all(tid.startswith("auto_") for tid in target_ids), (
            f"Expected auto-generated target IDs, got {target_ids}"
        )

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

    @pytest.mark.asyncio
    async def test_move_with_variables_no_target_created(self):
        """Moves with variable arguments should visualize but NOT create targets.

        When move commands use variables instead of literal values, the path
        should still be visualized, but no ProgramTarget is created since
        the coordinates aren't statically determinable.
        """
        visualizer = PathVisualizer()

        # Valid joint targets using variables (not literals)
        program = """
import parol6

async def main():
    joints_a = [85, -85, 175, 5, 5, 175]
    joints_b = [95, -95, 185, -5, -5, 185]
    async with parol6.AsyncRobotClient() as rbt:
        await rbt.move_j(joints_a, speed=1.0)
        await rbt.move_j(joints_b, speed=1.0)
"""

        await visualizer.update_path_visualization(program)

        # Should have created path segments (visualization still works)
        assert len(self._active_dry_run().path_segments) >= 2, (
            f"Expected at least 2 segments, got {len(self._active_dry_run().path_segments)}"
        )

        # Should NOT have created any targets (variables not inspectable)
        assert len(self._active_dry_run().targets) == 0, (
            f"Expected 0 targets (moves use variables), got {len(self._active_dry_run().targets)}"
        )


# ============================================================================
# Home and Checkpoint Tests
# ============================================================================


class TestHomeAndCheckpoints:
    """Tests for home teleport and checkpoint marker creation."""

    def test_home_segment_mirrors_referencing_state(self):
        """home() is tagged checkpoint='home' and ends at the home joints —
        a planned return move (real duration) when the preview is seeded
        homed, an instant referencing snap when seeded unhomed."""
        from parol6.config import HOME_ANGLES_DEG

        home_rad = np.radians(HOME_ANGLES_DEG)

        segments: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
        )
        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        client.home()

        assert len(segments) == 2
        home_seg = segments[1]
        assert home_seg["checkpoint"] == "home"
        assert home_seg["estimated_duration"] > 0.0
        assert home_seg["joints"] is not None
        assert np.allclose(home_seg["joints"], home_rad, atol=0.01)

        segments.clear()
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
            initial_homed=False,
        )
        client.home()

        assert len(segments) == 1
        snap_seg = segments[0]
        assert snap_seg["checkpoint"] == "home"
        assert snap_seg["estimated_duration"] == pytest.approx(0.0)
        assert snap_seg["joints"] is not None
        assert np.allclose(snap_seg["joints"], home_rad, atol=0.01)

    def test_home_updates_planner_for_subsequent_moves(self):
        """After home(), subsequent move_j starts from home position."""
        segments: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
        )

        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        client.home()
        client.move_j([90, -90, 140, 15, 50, 175], speed=1.0)

        assert len(segments) == 3
        # Third segment should have a trajectory starting near home
        third = segments[2]
        assert third["joint_trajectory"] is not None
        assert len(third["joint_trajectory"]) >= 2

    def test_checkpoint_creates_zero_width_marker(self):
        """checkpoint() creates a zero-width segment with the correct label."""
        segments: list[dict] = []
        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
        )

        client.move_j([85, -85, 135, 10, 45, 170], speed=1.0)
        client.checkpoint("pick_done")

        assert len(segments) == 2
        cp_seg = segments[1]
        assert cp_seg["checkpoint"] == "pick_done"
        assert cp_seg["estimated_duration"] == pytest.approx(0.0)
        assert cp_seg["points"] == []
        assert cp_seg["move_type"] == "checkpoint"


# ============================================================================
# Tool Action Tracking Tests
# ============================================================================


class TestToolActionTracking:
    """Tests for tool action start_positions tracking across calls."""

    def test_tool_start_positions_across_calls(self):
        """close() then open() records correct start_positions for each action."""
        from dataclasses import asdict
        from waldoctl.tools import LinearMotion

        segments: list[dict] = []
        tool_actions: list = []
        robot = get_robot("parol6")

        # Build tool metadata registry (same logic as path_visualizer)
        def _serialize_motions(motion_list):
            return [
                {"type": "linear", **asdict(m)}
                if isinstance(m, LinearMotion)
                else {"type": "rotary", **asdict(m)}
                for m in motion_list
            ]

        tool_meta: dict[str, dict] = {}
        for spec in robot.tools.available:
            if spec.key == "NONE":
                continue
            base = _serialize_motions(spec.motions) if spec.motions else []
            variants = {}
            for v in spec.variants:
                if v.motions:
                    variants[v.key] = {"motions": _serialize_motions(v.motions)}
            if base or variants:
                tool_meta[spec.key] = {
                    "motions": base,
                    "variants": variants,
                    "activation_type": spec.activation_type.value,
                }

        client = PathPreviewClient(
            dry_run_client_cls=DryRunRobotClient,
            segment_collector=segments,
            tool_action_collector=tool_actions,
            tool_meta_registry=tool_meta,
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
# Teleport Command Tests
# ============================================================================


class TestTeleportCommand:
    """Tests for TeleportCommand as a streamable motion command."""

    def test_teleport_is_streamable_motion_command(self):
        from parol6.commands.basic_commands import TeleportCommand
        from parol6.commands.base import MotionCommand

        assert issubclass(TeleportCommand, MotionCommand)
        assert TeleportCommand.streamable is True

    def test_teleport_not_in_system_cmd_types(self):
        from parol6.ack_policy import SYSTEM_CMD_TYPES, FIRE_AND_FORGET
        from parol6.protocol.wire import CmdType

        assert CmdType.TELEPORT not in SYSTEM_CMD_TYPES
        assert CmdType.TELEPORT in FIRE_AND_FORGET

    def test_teleport_converts_degrees_to_steps(self):
        from parol6.commands.basic_commands import TeleportCommand
        from parol6.protocol.wire import TeleportCmd
        from parol6.server.state import ControllerState

        angles_deg = [90.0, -45.0, 30.0, 0.0, 60.0, 180.0]
        cmd = TeleportCommand(TeleportCmd(angles=angles_deg))
        state = ControllerState()
        cmd.do_setup(state)

        # Steps should be non-zero for non-zero angles
        assert cmd._target_steps[0] != 0  # 90 deg
        assert cmd._target_steps[3] == 0  # 0 deg

    def test_teleport_clears_gripper_command_bits(self):
        """Teleport with tool_positions must clear Gripper_data_out[3]
        to prevent the write-frame JIT from re-arming the gripper ramp."""
        import os
        from parol6.commands.basic_commands import TeleportCommand
        from parol6.protocol.wire import TeleportCmd, CommandCode
        from parol6.server.state import ControllerState

        state = ControllerState()
        state.Gripper_data_out[3] = 1  # simulate in-flight gripper command

        angles = [0.0] * 6
        cmd = TeleportCommand(TeleportCmd(angles=angles, tool_positions=[0.5]))
        cmd.do_setup(state)

        with patch.dict(os.environ, {"PAROL6_FAKE_SERIAL": "1"}):
            cmd.execute_step(state)

        assert state.Command_out == CommandCode.TELEPORT
        assert state.Gripper_data_out[3] == 0
        assert state.tool_teleport_pos == pytest.approx(127.5)


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

    def test_clears_after_timeout(self):
        """Override clears once 100ms has passed since the last teleport."""
        from waldo_commander.main import _maybe_clear_sim_pose_override

        self._seed_program(is_active=False)
        playback_coordination.sim_pose_override = True
        playback_coordination.last_teleport_ts = time.monotonic() - 0.2  # 200ms ago

        _maybe_clear_sim_pose_override()

        assert playback_coordination.sim_pose_override is False
        assert playback_coordination.last_teleport_ts == 0.0

    def test_stays_set_during_active_scrubbing(self):
        """Override is kept if a teleport was sent recently (<100ms)."""
        from waldo_commander.main import _maybe_clear_sim_pose_override

        self._seed_program(is_active=False)
        playback_coordination.sim_pose_override = True
        playback_coordination.last_teleport_ts = time.monotonic()  # just now

        _maybe_clear_sim_pose_override()

        assert playback_coordination.sim_pose_override is True

    def test_stays_set_during_playback(self):
        """Override is kept while simulation playback is active."""
        from waldo_commander.main import _maybe_clear_sim_pose_override

        self._seed_program(is_active=True)
        playback_coordination.sim_pose_override = True
        playback_coordination.last_teleport_ts = time.monotonic() - 0.2

        _maybe_clear_sim_pose_override()

        assert playback_coordination.sim_pose_override is True

    def test_no_clear_without_teleport(self):
        """Override is kept if no teleport was ever sent (ts=0)."""
        from waldo_commander.main import _maybe_clear_sim_pose_override

        self._seed_program(is_active=False)
        playback_coordination.sim_pose_override = True
        playback_coordination.last_teleport_ts = 0.0

        _maybe_clear_sim_pose_override()

        assert playback_coordination.sim_pose_override is True


# ============================================================================
# ScriptExecutionController Lifecycle Tests
# ============================================================================


class TestScriptExecutionLifecycle:
    """Tests for ScriptExecutionController subprocess lifecycle."""

    @pytest.mark.asyncio
    async def test_start_reaps_subprocess_on_late_exception(
        self, tmp_path, monkeypatch
    ):
        """If start() raises *after* run_script succeeds, the subprocess must be killed.

        Without this guarantee, exceptions in the post-run_script section of start()
        (signal_play, log_panel.expand, task creation) leak a process group.
        """
        from waldo_commander.components import script_execution as se
        from tests.helpers.programs import ensure_active_program

        # Seed an active program so start() flips real per-program execution
        # state — without one, is_any_program_running() is vacuously False and
        # the reset assertions below can't catch a missed cleanup.
        active_program = ensure_active_program()

        # Long-running script: only `stop_script` (i.e. our cleanup path) can end it.
        script_path = tmp_path / "long_running.py"
        script_path.write_text("import time\nwhile True:\n    time.sleep(0.1)\n")

        # Capture the real handle as it's returned by run_script so we can verify
        # the subprocess gets reaped.
        captured: dict = {}
        real_run_script = se.run_script

        async def capturing_run_script(*args, **kwargs):
            handle = await real_run_script(*args, **kwargs)
            captured["handle"] = handle
            return handle

        monkeypatch.setattr(se, "run_script", capturing_run_script)

        # Stub UI-coupled imports so start() doesn't need a NiceGUI client context.
        fake_client = MagicMock()
        monkeypatch.setattr(
            se,
            "context",
            type("FakeCtx", (), {"client": fake_client})(),
        )
        monkeypatch.setattr(se.ui, "notify", lambda *a, **k: None)
        monkeypatch.setattr(se.log_panel, "clear", lambda: None)
        monkeypatch.setattr(se.log_panel, "push", lambda line: None)

        # Force log_panel.expand() to raise — this fires after run_script returns
        # successfully, exercising the late-exception path.
        def raise_expand():
            raise RuntimeError("simulated UI failure after subprocess started")

        monkeypatch.setattr(se.log_panel, "expand", raise_expand)

        # Provide the active-tab widget refs start() reads from.
        fake_textarea = MagicMock()
        fake_textarea.value = script_path.read_text()
        fake_filename_input = MagicMock()
        fake_filename_input.value = script_path.name
        ui_state.active_textarea = fake_textarea
        ui_state.active_filename_input = fake_filename_input

        # run_script reads ui_state.active_robot.backend_package; ensure it's set
        # (matches the mock_textarea fixture's pattern).
        old_robot = ui_state.robot
        ui_state.robot = get_robot()

        se.script_exec.set_program_dir(tmp_path)

        try:
            await se.script_exec.start()

            # Contract: handle cleared, state reset. Assert the launching
            # program's own flags were cleared (the reset path ran), not just
            # the global helper which would pass regardless with no program.
            assert se.script_exec.script_handle is None
            assert is_any_program_running() is False
            assert active_program.execution.is_running is False
            assert active_program.dry_run.playback.is_playing is False

            # The subprocess must be dead — this is the regression guard.
            assert "handle" in captured, "run_script did not run; test setup is wrong"
            proc = captured["handle"]["proc"]
            # stop_script awaits proc.wait() after termination, so by the time
            # start()'s except clause returns, returncode must be set.
            assert proc.returncode is not None, (
                f"Subprocess was leaked! PID {proc.pid} still running."
            )
        finally:
            ui_state.active_textarea = None
            ui_state.active_filename_input = None
            ui_state.robot = old_robot
            # Belt-and-suspenders: if the test ever ran without the fix, ensure
            # the subprocess is killed so it doesn't leak into the next test.
            handle = captured.get("handle")
            if handle and handle["proc"].returncode is None:
                from waldo_commander.services.script_runner import stop_script

                await stop_script(handle)

    @pytest.mark.asyncio
    async def test_start_handles_subdir_filename(self, tmp_path, monkeypatch):
        """Filenames with path separators (from files loaded out of subdirs) must
        not break start()'s write to ``.runtime/<filename>``.

        Regression: file tree IDs are relative paths (``str(item.relative_to(base))``),
        so loading ``programs/sub/foo.py`` puts ``"sub/foo.py"`` in the filename
        input. Pre-fix, ``script_path.write_text`` raised FileNotFoundError because
        only ``.runtime`` was created, not ``.runtime/sub``.
        """
        from waldo_commander.components import script_execution as se

        # Stub run_script so we don't actually launch a subprocess — the bug
        # is in the file write that happens before run_script is called.
        async def stub_run_script(*args, **kwargs):
            raise RuntimeError("stub: stop after file write")

        monkeypatch.setattr(se, "run_script", stub_run_script)
        monkeypatch.setattr(se.ui, "notify", lambda *a, **k: None)
        monkeypatch.setattr(se.log_panel, "clear", lambda: None)
        monkeypatch.setattr(se.log_panel, "push", lambda line: None)
        monkeypatch.setattr(se.log_panel, "expand", lambda: None)

        fake_textarea = MagicMock()
        fake_textarea.value = "# subdir regression\n"
        fake_filename_input = MagicMock()
        fake_filename_input.value = "sub/regression.py"
        ui_state.active_textarea = fake_textarea
        ui_state.active_filename_input = fake_filename_input

        se.script_exec.set_program_dir(tmp_path)

        try:
            await se.script_exec.start()

            written = tmp_path / ".runtime" / "sub" / "regression.py"
            assert written.exists(), f"Expected {written} to exist after start() ran"
            assert written.read_text(encoding="utf-8") == "# subdir regression\n"
        finally:
            ui_state.active_textarea = None
            ui_state.active_filename_input = None

    @pytest.mark.asyncio
    async def test_cleanup_preserves_stepping_ipc_across_page_reload(
        self, tmp_path, monkeypatch
    ):
        """Per-page ``cleanup()`` must NOT delete the stepping IPC files.

        Regression: pre-fix, ``cleanup()`` called ``cleanup_stepping()``
        which deleted ``/tmp/.parol_control_X`` and ``/tmp/.parol_events_X``.
        With the subprocess still alive, ``check_should_pause()`` then read
        the missing control file → defaulted to ``paused=True``. (Nowadays a
        missing control file makes ``wait_for_step_or_play`` return
        immediately — free-run, not a hang — but the files must still be
        preserved for stepping to keep working across reloads.)

        After fix: ``cleanup()`` cancels only the event watcher; the step
        controller, session id, and IPC files are preserved so the
        subprocess can keep stepping, and ``set_ui_client`` on the next
        page rebinds a fresh watcher to the new client.
        """
        from waldo_commander.components import script_execution as se
        from waldo_commander.services.stepping_client import GUIStepController

        # Simulate "script is running mid-stepping" — initialize a real
        # step controller (which creates IPC files), then flag the
        # simulation_state so the watcher-restart logic sees it.
        session_id = "test_cross_reload_ipc"
        step_controller = GUIStepController(session_id)
        step_controller.initialize()

        # Sanity: IPC files exist after initialize.
        assert step_controller._control_file.exists()
        assert step_controller._event_file.exists()

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

            # Per-page disconnect cleanup
            se.script_exec.cleanup()

            await asyncio.sleep(0)  # let the cancellation propagate
            assert watcher_cancelled.is_set(), "Watcher task was not cancelled"
            assert se.script_exec._event_watcher_task is None
            # Step controller + session preserved across the disconnect.
            assert se.script_exec._step_controller is step_controller
            assert se.script_exec._step_session_id == session_id
            # IPC files survived.
            assert step_controller._control_file.exists()
            assert step_controller._event_file.exists()

            # New page connecting — set_ui_client should rebind a new
            # watcher because the subprocess is still flagged as running.
            fake_client = MagicMock()
            se.script_exec.set_ui_client(fake_client)
            assert se.script_exec._event_watcher_task is not None
            assert not se.script_exec._event_watcher_task.done()
        finally:
            # Drop the restarted watcher; it polls IPC files we're about
            # to delete and would otherwise log noisy errors.
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


def test_playback_reset_for_test_clears_listeners():
    """``PlaybackController.setup_timers()`` runs once per page build and
    registers both a change-channel and a step-channel listener on
    ``simulation_state``. ``reset_for_test()`` must call ``cleanup()`` so
    neither leaks across tests.
    """
    from waldo_commander.components.playback import playback

    # Baseline counts after import (decorations/log_panel register in
    # __init__ and stay registered on the change channel).
    change_baseline = len(simulation_state._change_listeners)
    step_baseline = len(simulation_state._step_listeners)

    for _ in range(3):
        simulation_state.add_change_listener(playback._on_state_change)
        simulation_state.add_step_listener(playback._on_step_change)
        playback.reset_for_test()

    assert len(simulation_state._change_listeners) == change_baseline, (
        f"Change listeners leaked: baseline={change_baseline}, "
        f"now={len(simulation_state._change_listeners)}"
    )
    assert len(simulation_state._step_listeners) == step_baseline, (
        f"Step listeners leaked: baseline={step_baseline}, "
        f"now={len(simulation_state._step_listeners)}"
    )


def test_editor_panel_cleanup_removes_playback_listener():
    """Production regression guard: ``EditorPanel.cleanup()`` (now called from
    ``_on_disconnect``) must remove the per-page playback listener so a user
    reloading the browser tab doesn't leak one listener per reload.
    """
    from waldo_commander.components.playback import playback
    from waldo_commander.components.editor import EditorPanel

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


def test_editor_panel_cleanup_is_idempotent():
    """``_on_disconnect`` fires per-page; ``_on_shutdown`` also calls
    ``editor_panel.cleanup()``. The composition must tolerate being called
    twice without raising (Timer.cancel, Task.cancel, and
    remove_change_listener are all expected idempotent).
    """
    from waldo_commander.components.editor import EditorPanel

    panel = EditorPanel()
    panel.cleanup()
    panel.cleanup()  # second call must not raise


# ============================================================================
# Per-tab log routing
# ============================================================================


def test_script_output_appends_to_launching_tab_only():
    """Per-tab log isolation: a script's stdout/stderr must land in the
    output_log of the tab that started the script, regardless of which
    tab the user is currently viewing.
    """
    from waldo_commander.components.script_execution import script_exec
    import waldoctl
    from waldoctl import Program

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


# Note: the legacy ``output_log`` cap test (1000-entry FIFO) was a WC-specific
# implementation detail tied to ``ui.log(max_lines=1000)``. ``Program.log.entries``
# is unbounded by design; host applications cap visibly via the widget. The cap
# may be reintroduced as a host-level concern but isn't part of the public surface.


def test_reset_state_clears_launching_tab_id():
    """``_reset_state`` must clear ``_script_tab_id`` so post-completion
    scrubbing in ``playback._apply_time`` falls back to the active tab.
    Otherwise the executing-line highlight stays pinned to the launching
    tab's textarea (often hidden) after the user switches tabs.
    """
    from waldo_commander.components.script_execution import script_exec

    script_exec._script_tab_id = "tab-a"
    assert script_exec.launching_tab_id == "tab-a"

    script_exec._reset_state()

    assert script_exec.launching_tab_id is None


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


class TestObjectTrackPlumbing:
    """Object motion reported by a preview reaches the segment dicts and the
    change detection that decides whether to re-render."""

    def test_segments_that_differ_only_in_an_object_landing_do_not_match(self):
        from waldo_commander.state import PathSegment

        def seg(landing_z: float) -> PathSegment:
            return PathSegment(
                points=[[0.3, 0.0, 0.3], [0.4, 0.0, 0.3]],
                color="#00ff00",
                is_valid=True,
                line_number=3,
                object_tracks=[
                    {
                        "name": "block",
                        "poses": [
                            [0.3, 0.0, 0.04, 1, 0, 0, 0],
                            [0.4, 0.0, landing_z, 1, 0, 0, 0],
                        ],
                        "carried": False,
                        "physics": True,
                    }
                ],
            )

        assert PathVisualizer._segments_match([seg(0.04)], [seg(0.04)])
        assert not PathVisualizer._segments_match([seg(0.04)], [seg(0.30)]), (
            "a program whose only change is where the block lands must re-render"
        )

    def test_tracks_ride_segments_and_tool_actions_sliced_like_the_trajectory(self):
        from unittest.mock import MagicMock

        import numpy as np
        from waldoctl.results import DryRunResultData, ObjectTrack

        from waldo_commander.services.path_preview_client import PathPreviewClient
        from waldo_commander.state import ToolAction

        n = 5
        traj = np.zeros((n, 6))
        poses = np.zeros((n, 3))
        poses[:, 0] = np.linspace(0.3, 0.4, n)
        block = ObjectTrack(
            name="block",
            poses=np.column_stack([poses, np.tile([1.0, 0, 0, 0], (n, 1))]),
            carried=True,
            physics=True,
        )
        stand = ObjectTrack(
            name="stand",
            poses=np.array([[0.3, 0, 0.005, 1, 0, 0, 0]]),
            carried=False,
            physics=True,
        )
        # The last two waypoints are invalid: the run split must slice the
        # moving track exactly like the joint trajectory and leave the parked
        # one whole.
        result = DryRunResultData(
            tcp_poses=np.column_stack([poses, np.zeros((n, 3))]),
            end_joints_rad=np.zeros(6),
            duration=1.0,
            valid=np.array([True, True, True, False, False]),
            joint_trajectory_rad=traj,
            object_tracks=(block, stand),
        )
        inner = MagicMock()
        inner.move_j.return_value = result
        inner.tool.close.return_value = result
        # A real client counts the commands it has recorded, and the
        # preview reads that to map each one back to a source line; a
        # mock that answers with a mock is not a client.
        inner.program_length = 0
        segments: list[dict] = []
        actions: list[ToolAction] = []
        client = PathPreviewClient(
            dry_run_client_cls=MagicMock(return_value=inner),
            segment_collector=segments,
            tool_action_collector=actions,
        )
        client.move_j([0, 0, 0, 0, 0, 0])

        assert len(segments) == 2
        for seg in segments:
            tracks = {t["name"]: t for t in seg["object_tracks"]}
            assert len(tracks["block"]["poses"]) == len(seg["joint_trajectory"])
            assert len(tracks["stand"]["poses"]) == 1
        assert segments[0]["object_tracks"][0]["carried"] is True
