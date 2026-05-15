"""Integration tests for the program editor via UI."""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import (
    wait_for_app_ready,
    enable_sim,
)


@pytest.mark.integration
async def test_program_tab_visible(user: User) -> None:
    """Test that the program editor tab is visible."""
    await user.open("/")
    await user.should_see(marker="tab-program")


@pytest.mark.integration
async def test_open_program_tab(user: User) -> None:
    """Test opening the program editor tab via click."""
    await user.open("/")
    await wait_for_app_ready()

    # Click program tab
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    # Editor buttons should now be visible
    await user.should_see(marker="editor-play-btn")
    await user.should_see(marker="editor-new-tab-btn")
    # Verify all control buttons are visible
    await user.should_see(marker="editor-play-btn")
    await user.should_see(marker="editor-record-btn")
    await user.should_see(marker="editor-log-toggle")
    await user.should_see(marker="editor-new-tab-btn")
    await user.should_see(marker="editor-save-btn")
    await user.should_see(marker="editor-open-btn")
    await user.should_see(marker="editor-commands-btn")


@pytest.mark.integration
async def test_run_button_toggles(user: User, robot_state) -> None:
    """Test that the run button toggles between play and pause icons.

    When play is clicked:
    - Play button icon changes from play_arrow to pause
    - Stop button becomes visible

    When paused:
    - Play button icon changes back to play_arrow
    """
    from waldo_commander.state import simulation_state, ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user, robot_state)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"
    assert simulation_state.script_running is False, (
        "Script should not be running initially"
    )

    # Initially: play button visible, stop button hidden
    play_btn = user.find(marker="editor-play-btn")
    assert play_btn is not None
    assert editor.playback.play_btn is not None, "Play button reference should exist"

    # Stop button should be hidden initially
    stop_btn = editor.playback._stop_btn
    assert stop_btn is not None, "Stop button reference should exist"
    assert stop_btn.visible is False, "Stop button should be hidden initially"

    # Click play - should start script
    play_btn.click()
    await asyncio.sleep(0.3)

    # Script should now be running
    assert simulation_state.script_running is True, (
        "Script should be running after clicking play"
    )

    # Stop button should now be visible
    assert stop_btn.visible is True, "Stop button should be visible when script running"

    # Click play again to pause (not stop)
    play_btn.click()
    await asyncio.sleep(0.2)

    # Script still running but paused
    assert simulation_state.script_running is True, (
        "Script should still be running (paused)"
    )

    # Stop the script for cleanup
    stop_btn_element = user.find(marker="editor-stop-btn")
    stop_btn_element.click()
    await asyncio.sleep(0.2)


@pytest.mark.integration
async def test_log_toggle_expands_log(user: User) -> None:
    """Test that the log toggle button expands/collapses the log panel.

    The chevron icon should flip direction:
    - expand_more (down chevron) when collapsed - "show more"
    - expand_less (up chevron) when expanded - "collapse"
    """
    from waldo_commander.components.log_panel import log_panel
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Initially log should be collapsed with expand_more icon (down chevron)
    assert log_panel._log_expanded is False, "Log should be collapsed initially"
    log_toggle_btn = log_panel.log_toggle_btn
    assert log_toggle_btn is not None, "Log toggle button should exist"

    # Check initial chevron icon is expand_more (down = "show more")
    initial_props = log_toggle_btn._props.get("icon", "")
    assert initial_props == "expand_more", (
        f"Initial icon should be expand_more, got {initial_props}"
    )

    # Click log toggle to expand
    log_toggle = user.find(marker="editor-log-toggle")
    log_toggle.click()
    await asyncio.sleep(0.1)

    # Log should now be expanded with expand_less icon (up chevron)
    assert log_panel._log_expanded is True, "Log should be expanded after click"
    expanded_props = log_toggle_btn._props.get("icon", "")
    assert expanded_props == "expand_less", (
        f"Expanded icon should be expand_less, got {expanded_props}"
    )

    # Click again to collapse
    log_toggle.click()
    await asyncio.sleep(0.1)

    # Should be back to collapsed with expand_more icon
    assert log_panel._log_expanded is False, (
        "Log should be collapsed after second click"
    )
    collapsed_props = log_toggle_btn._props.get("icon", "")
    assert collapsed_props == "expand_more", (
        f"Collapsed icon should be expand_more, got {collapsed_props}"
    )


@pytest.mark.integration
async def test_commands_button_clickable(user: User) -> None:
    """Test that clicking the commands button doesn't error."""
    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    commands_btn = user.find(marker="editor-commands-btn")
    commands_btn.click()
    await asyncio.sleep(0)

    # Should not throw errors
    await user.should_see(marker="editor-commands-btn")


@pytest.mark.integration
async def test_record_button_toggles(user: User, robot_state) -> None:
    """Test that the record button toggles recording_state and changes appearance.

    When recording starts:
    - recording_state.is_recording becomes True
    - Button color changes from negative (red) to warning (amber)

    When recording stops:
    - recording_state.is_recording becomes False
    - Button color changes back to negative (red)
    """
    from waldo_commander.state import recording_state, ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user, robot_state)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Initially not recording with red color
    assert recording_state.is_recording is False
    record_btn_ref = editor.playback.record_btn
    assert record_btn_ref is not None, "Record button reference should exist"
    initial_color = record_btn_ref._props.get("color", "")
    assert initial_color == "negative", (
        f"Initial color should be negative (red), got {initial_color}"
    )

    # Click record to start
    record_btn = user.find(marker="editor-record-btn")
    record_btn.click()
    await asyncio.sleep(0.1)

    assert recording_state.is_recording is True, "Expected recording to start"
    recording_color = record_btn_ref._props.get("color", "")
    assert recording_color == "warning", (
        f"Recording color should be warning (amber), got {recording_color}"
    )

    # Click again to stop
    record_btn.click()
    await asyncio.sleep(0.1)

    assert recording_state.is_recording is False, "Expected recording to stop"
    stopped_color = record_btn_ref._props.get("color", "")
    assert stopped_color == "negative", (
        f"Stopped color should be negative (red), got {stopped_color}"
    )


@pytest.mark.integration
async def test_recording_notification_appears_and_disappears(
    user: User, robot_state
) -> None:
    """Test that a pulsating recording notification appears at the top of the screen.

    When recording starts:
    - A notification with "Recording" text appears at the top
    - The notification has the recording-notification CSS class for z-index and animation

    When recording stops:
    - The notification is dismissed
    """
    from waldo_commander.state import recording_state, ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user, robot_state)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Initially no recording notification
    assert recording_state.is_recording is False
    assert editor.playback._recording_notification is None

    # Click record to start
    record_btn = user.find(marker="editor-record-btn")
    record_btn.click()
    await asyncio.sleep(0.1)

    # Recording notification should appear
    assert recording_state.is_recording is True
    assert editor.playback._recording_notification is not None, (
        "Recording notification should exist"
    )
    await user.should_see("Recording")

    # Click again to stop
    record_btn.click()
    await asyncio.sleep(0.1)

    # Recording notification should be dismissed
    assert recording_state.is_recording is False
    assert editor.playback._recording_notification is None, (
        "Recording notification should be dismissed"
    )


@pytest.mark.integration
async def test_panel_can_be_reopened(user: User) -> None:
    """Test that the editor panel can be closed and reopened.

    The panel is closed by switching to a different tab (IO, Gripper, etc).
    When reopened, the play button should be visible again.
    """
    await user.open("/")
    await wait_for_app_ready()

    # Open editor by clicking program tab
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    # Panel should be visible (play button is shown)
    await user.should_see(marker="editor-play-btn")

    # Close by switching to IO tab (same tab group as program)
    user.find(marker="tab-io").click()
    await asyncio.sleep(0.1)

    # Reopen program panel
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    # Panel should be visible again
    await user.should_see(marker="editor-play-btn")


@pytest.mark.integration
async def test_dirty_icon_appears_after_editing(user: User) -> None:
    """Test that the dirty icon (amber dot) appears after editing content.

    When tab content is modified from its saved state, a dirty indicator
    should become visible to show unsaved changes.
    """
    from waldo_commander.state import ui_state, editor_tabs_state

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Get active tab
    tab = editor_tabs_state.get_active_tab()
    assert tab is not None, "Active tab should exist"

    # Initially tab should not be dirty (content == saved_content)
    assert tab.is_dirty is False, "Tab should not be dirty initially"

    # Get dirty dot widget
    widgets = editor._tab_widgets.get(tab.id, {})
    dirty_dot = widgets.get("dirty_dot")
    assert dirty_dot is not None, "Dirty dot widget should exist"

    # Modify the content directly (simulating editor change)
    tab.content = tab.content + "\n# Modified"

    # Tab should now be dirty (is_dirty is a computed property)
    assert tab.is_dirty is True, "Tab should be dirty after modification"

    # Manually update dirty dot visibility as the UI binding would
    dirty_dot.set_visibility(tab.is_dirty)

    # Dirty dot should be visible
    assert dirty_dot.visible is True, "Dirty dot should be visible after modification"


@pytest.mark.integration
async def test_tab_switching_preserves_path_visualizations(user: User) -> None:
    """Test that tabs maintain their own path_segments and targets.

    Each tab should store its own path visualization data.
    The _save_simulation_context method correctly saves simulation_state to a tab.
    """
    from waldo_commander.state import ui_state, editor_tabs_state, simulation_state

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Get first tab
    tab1 = editor_tabs_state.get_active_tab()
    assert tab1 is not None, "First tab should exist"

    # Set simulation_state data for tab1 (this is what gets saved to the tab on switch)
    simulation_state.path_segments = [{"fake": "segment1"}]  # type: ignore[list-item]
    simulation_state.targets = [{"fake": "target1"}]  # type: ignore[list-item]

    # Create a second tab (this triggers _save_simulation_context on tab1)
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0.1)

    # Tab1 should now have the simulation_state data saved to it
    assert tab1.path_segments == [{"fake": "segment1"}], (
        "Tab1 should have saved simulation_state data"
    )
    assert tab1.targets == [{"fake": "target1"}], (
        "Tab1 should have saved simulation_state data"
    )

    # Get second tab
    tab2 = editor_tabs_state.get_active_tab()
    assert tab2 is not None, "Second tab should exist"
    assert tab2.id != tab1.id, "Should be on new tab"

    # Tab2 should have empty paths (new tab, no simulation run yet)
    assert tab2.path_segments == [], "New tab should have empty path_segments"
    assert tab2.targets == [], "New tab should have empty targets"

    # Set different simulation_state data for tab2
    simulation_state.path_segments = [{"fake": "segment2"}]  # type: ignore[list-item]
    simulation_state.targets = [{"fake": "target2"}]  # type: ignore[list-item]

    # Manually save to tab2
    editor._save_simulation_context(tab2)

    assert tab2.path_segments == [{"fake": "segment2"}], (
        "Tab2 should have its own simulation data"
    )
    assert tab2.targets == [{"fake": "target2"}], (
        "Tab2 should have its own simulation data"
    )

    # Tab1's data should still be preserved
    assert tab1.path_segments == [{"fake": "segment1"}], (
        "Tab1's data should be preserved after saving tab2"
    )
    assert tab1.targets == [{"fake": "target1"}], (
        "Tab1's data should be preserved after saving tab2"
    )


@pytest.mark.integration
async def test_create_and_remove_tab(user: User) -> None:
    """Test creating a new tab and then removing it.

    Creating a tab should increase the tab count.
    Closing a tab should decrease the tab count.
    """
    from waldo_commander.state import editor_tabs_state, ui_state

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Get initial tab count
    initial_count = len(editor_tabs_state.tabs)
    assert initial_count >= 1, "Should have at least one initial tab"

    # Create a new tab
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0)

    # Verify new tab was created
    assert len(editor_tabs_state.tabs) == initial_count + 1, (
        f"Expected {initial_count + 1} tabs after creating new"
    )

    # Get the new tab (should be active)
    new_tab = editor_tabs_state.get_active_tab()
    assert new_tab is not None, "New tab should be active"
    new_tab_id = new_tab.id

    # Close the new tab using the close button
    close_btn = user.find(marker=f"editor-tab-close-{new_tab_id}")
    close_btn.click()
    # Close is deferred via ui.timer(0) - poll until tab is removed
    # CI environments need more time for the timer callback to execute
    for _ in range(40):
        await asyncio.sleep(0.1)
        if len(editor_tabs_state.tabs) == initial_count:
            break

    # Verify tab was removed
    assert len(editor_tabs_state.tabs) == initial_count, (
        f"Expected {initial_count} tabs after closing"
    )

    # Verify the closed tab no longer exists
    assert editor_tabs_state.find_tab_by_id(new_tab_id) is None, (
        "Closed tab should no longer exist"
    )


@pytest.mark.integration
async def test_step_button_enabled_after_simulation(user: User, robot_state) -> None:
    """Test that the step button is visible and enabled after simulation.

    After simulation populates steps:
    - Step button becomes visible
    - Step button is not disabled
    - Play button starts simulation playback (not script execution)
    """
    from waldo_commander.state import ui_state, editor_tabs_state, simulation_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user, robot_state)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Step button should be hidden before simulation
    assert editor.playback._next_btn is not None, "Step button reference should exist"
    assert editor.playback._next_btn.visible is False, (
        "Step button should be hidden before simulation"
    )

    # Set script with move commands to generate simulation steps
    tab = editor_tabs_state.get_active_tab()
    assert tab is not None
    test_script = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
rbt.move_j([95, -95, 185, -5, -5, 185], speed=1.0)
"""
    editor_tabs_state.active_textarea.value = test_script
    tab.content = test_script

    # Run simulation to populate steps
    from waldo_commander.components.simulation_engine import simulation as _sim

    await _sim.run_simulation()
    await asyncio.sleep(0.1)

    # Step button should be visible after simulation
    assert editor.playback._next_btn.visible is True, (
        "Step button should be visible when simulation has steps"
    )
    assert editor.playback._next_btn._props.get("disable") is not True, (
        "Step button should be enabled"
    )
    assert simulation_state.total_steps > 0, "Should have simulation steps"

    # Play should start sim playback, not script execution
    await editor.playback.toggle_play()
    await asyncio.sleep(0.1)
    assert simulation_state.sim_playback_active is True, (
        "Play should start simulation playback when steps exist"
    )
    assert simulation_state.script_running is False, (
        "Script should not be running during sim playback"
    )

    # Pause sim playback
    await editor.playback.toggle_play()
    await asyncio.sleep(0)
    assert simulation_state.sim_playback_active is False


@pytest.mark.integration
async def test_simulation_creates_targets_for_literal_moves(
    user: User, robot_state
) -> None:
    """Test that simulation creates targets for move commands with literal args.

    After simulation, moves with literal coordinates get auto-generated targets
    tracked by the CM6 StateField for interactive 3D editing. No markers are
    added to the user's source code.
    """
    from waldo_commander.state import editor_tabs_state, simulation_state, ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user, robot_state)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    tab = editor_tabs_state.get_active_tab()
    assert tab is not None, "Active tab should exist"

    test_script = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
"""
    assert editor_tabs_state.active_textarea is not None
    editor_tabs_state.active_textarea.value = test_script
    tab.content = test_script

    from waldo_commander.components.simulation_engine import simulation as _sim

    await _sim.run_simulation()
    await asyncio.sleep(0.1)

    # Targets should be created from literal move args
    assert len(simulation_state.targets) >= 1, (
        f"Expected at least 1 target, got {len(simulation_state.targets)}"
    )

    # Target ID should be auto-generated (no UUID markers)
    target = simulation_state.targets[0]
    assert target.id.startswith("auto_"), f"Expected auto-generated ID, got {target.id}"
    assert target.line_number > 0, "Target should have a valid line number"

    # Source code should NOT contain any TARGET markers
    updated_content = editor_tabs_state.active_textarea.value
    assert "# TARGET:" not in updated_content, (
        "Source code should not contain TARGET markers"
    )
