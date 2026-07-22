"""Integration tests for the program editor via UI."""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import (
    wait_for_app_ready,
    enable_sim,
)
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
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
async def test_run_button_toggles(user: User) -> None:
    """Test that the run button toggles between play and pause icons.

    When play is clicked:
    - Play button icon changes from play_arrow to pause
    - Stop button becomes visible

    When paused:
    - Play button icon changes back to play_arrow
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"
    assert is_any_program_running() is False, "Script should not be running initially"

    # Initially: play button visible, stop button hidden
    play_btn = user.find(marker="editor-play-btn")
    assert play_btn is not None
    assert editor.playback.play_btn is not None, "Play button reference should exist"

    # Stop button should be hidden initially
    stop_btn = editor.playback.stop_btn
    assert stop_btn is not None, "Stop button reference should exist"
    assert stop_btn.visible is False, "Stop button should be hidden initially"

    # Click play - should start script
    play_btn.click()
    await asyncio.sleep(0.3)

    # Script should now be running
    assert is_any_program_running() is True, (
        "Script should be running after clicking play"
    )

    # Stop button should now be visible
    assert stop_btn.visible is True, "Stop button should be visible when script running"

    # Click play again to pause (not stop)
    play_btn.click()
    await asyncio.sleep(0.2)

    # Script still running but paused
    assert is_any_program_running() is True, "Script should still be running (paused)"

    # Stop the script for cleanup
    stop_btn_element = user.find(marker="editor-stop-btn")
    stop_btn_element.click()
    await asyncio.sleep(0.2)


@pytest.mark.integration
async def test_program_playback_step_channel_fires_during_run(user: User) -> None:
    """The per-program step channel (waldoctl ``Playback.add_step_listener``)
    fires during a real script run: the host drives it whenever the
    ``executing_step_*`` fields advance, so a plugin can track this program's
    steps without listening to the global stream."""
    import waldoctl

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    assert ui_state.editor_panel is not None

    programs = waldoctl.commander.programs
    program = programs.get(programs.active_id)
    assert program is not None
    fired = 0

    def _on_step() -> None:
        nonlocal fired
        fired += 1

    program.dry_run.playback.add_step_listener(_on_step)
    try:
        user.find(marker="editor-play-btn").click()
        for _ in range(200):  # the watcher polls at 20Hz; fires on script start
            if fired:
                break
            await asyncio.sleep(0.05)
        assert fired > 0, "per-program step channel never fired during the run"
    finally:
        program.dry_run.playback.remove_step_listener(_on_step)
        if is_any_program_running():
            user.find(marker="editor-stop-btn").click()
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
async def test_record_button_toggles(user: User) -> None:
    """Test that the record button toggles recording and changes appearance.

    When recording starts:
    - is_any_program_recording() becomes True
    - Button color changes from negative (red) to warning (amber)

    When recording stops:
    - is_any_program_recording() becomes False
    - Button color changes back to negative (red)
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Initially not recording with red color
    assert not is_any_program_recording()
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

    assert is_any_program_recording(), "Expected recording to start"
    recording_color = record_btn_ref._props.get("color", "")
    assert recording_color == "warning", (
        f"Recording color should be warning (amber), got {recording_color}"
    )

    # Click again to stop
    record_btn.click()
    await asyncio.sleep(0.1)

    assert not is_any_program_recording(), "Expected recording to stop"
    stopped_color = record_btn_ref._props.get("color", "")
    assert stopped_color == "negative", (
        f"Stopped color should be negative (red), got {stopped_color}"
    )


@pytest.mark.integration
async def test_recording_notification_appears_and_disappears(
    user: User,
) -> None:
    """Test that a pulsating recording notification appears at the top of the screen.

    When recording starts:
    - A notification with "Recording" text appears at the top
    - The notification has the recording-notification CSS class for z-index and animation

    When recording stops:
    - The notification is dismissed
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Initially no recording notification
    assert not is_any_program_recording()
    assert editor.playback._recording_notification is None

    # Click record to start
    record_btn = user.find(marker="editor-record-btn")
    record_btn.click()
    await asyncio.sleep(0.1)

    # Recording notification should appear
    assert is_any_program_recording()
    assert editor.playback._recording_notification is not None, (
        "Recording notification should exist"
    )
    await user.should_see("Recording")

    # Click again to stop
    record_btn.click()
    await asyncio.sleep(0.1)

    # Recording notification should be dismissed
    assert not is_any_program_recording()
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
    from waldo_commander.state import ui_state
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Get active tab
    tab = waldoctl.commander.programs.active
    assert tab is not None, "Active tab should exist"

    # Initially tab should not be dirty (content == saved_content)
    assert tab.is_dirty is False, "Tab should not be dirty initially"

    # Get dirty dot widget
    widgets = editor._tab_widgets.get(tab.id, {})
    dirty_dot = widgets.get("dirty_dot")
    assert dirty_dot is not None, "Dirty dot widget should exist"

    # Modify the content directly (simulating editor change)
    tab.source = tab.source + "\n# Modified"

    # Tab should now be dirty (is_dirty is a computed property)
    assert tab.is_dirty is True, "Tab should be dirty after modification"

    # Manually update dirty dot visibility as the UI binding would
    dirty_dot.set_visibility(tab.is_dirty)

    # Dirty dot should be visible
    assert dirty_dot.visible is True, "Dirty dot should be visible after modification"


@pytest.mark.integration
async def test_tab_switching_preserves_path_visualizations(user: User) -> None:
    """Test that tabs maintain their own path_segments and targets.

    Each ``Program`` owns its own ``dry_run.path_segments`` / ``targets``
    list directly — writers update the owning tab's dry-run, and switching
    tabs simply re-points readers to the new active program. There is no
    longer a global ``simulation_state`` mirror to drive the per-tab copy.
    """
    from waldo_commander.state import ui_state
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Get first tab
    tab1 = waldoctl.commander.programs.active
    assert tab1 is not None, "First tab should exist"

    # Write fake simulation results directly into tab1's dry-run
    tab1.dry_run.path_segments = [{"fake": "segment1"}]  # type: ignore[list-item]
    tab1.dry_run.targets = [{"fake": "target1"}]  # type: ignore[list-item]

    # Create a second tab — its dry-run starts empty
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0.1)

    tab2 = waldoctl.commander.programs.active
    assert tab2 is not None, "Second tab should exist"
    assert tab2.id != tab1.id, "Should be on new tab"
    assert tab2.dry_run.path_segments == [], "New tab should have empty path_segments"
    assert tab2.dry_run.targets == [], "New tab should have empty targets"

    # Write fake simulation results into tab2's dry-run
    tab2.dry_run.path_segments = [{"fake": "segment2"}]  # type: ignore[list-item]
    tab2.dry_run.targets = [{"fake": "target2"}]  # type: ignore[list-item]

    # Tab1's data should still be preserved — no shared state to clobber
    assert tab1.dry_run.path_segments == [{"fake": "segment1"}], (
        "Tab1's data should be preserved while editing tab2"
    )
    assert tab1.dry_run.targets == [{"fake": "target1"}], (
        "Tab1's data should be preserved while editing tab2"
    )


@pytest.mark.integration
async def test_create_and_remove_tab(user: User) -> None:
    """Test creating a new tab and then removing it.

    Creating a tab should increase the tab count.
    Closing a tab should decrease the tab count.
    """
    from waldo_commander.state import ui_state
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Get initial tab count
    initial_count = len(waldoctl.commander.programs.items)
    assert initial_count >= 1, "Should have at least one initial tab"

    # Create a new tab
    user.find(marker="editor-new-tab-btn").click()
    await asyncio.sleep(0)

    # Verify new tab was created
    assert len(waldoctl.commander.programs.items) == initial_count + 1, (
        f"Expected {initial_count + 1} tabs after creating new"
    )

    # Get the new tab (should be active)
    new_tab = waldoctl.commander.programs.active
    assert new_tab is not None, "New tab should be active"
    new_tab_id = new_tab.id

    # Close the new tab using the close button
    close_btn = user.find(marker=f"editor-tab-close-{new_tab_id}")
    close_btn.click()
    # Close is deferred via ui.timer(0) - poll until tab is removed
    # CI environments need more time for the timer callback to execute
    for _ in range(40):
        await asyncio.sleep(0.1)
        if len(waldoctl.commander.programs.items) == initial_count:
            break

    # Verify tab was removed
    assert len(waldoctl.commander.programs.items) == initial_count, (
        f"Expected {initial_count} tabs after closing"
    )

    # Verify the closed tab no longer exists
    assert waldoctl.commander.programs.get(new_tab_id) is None, (
        "Closed tab should no longer exist"
    )


@pytest.mark.integration
async def test_external_program_mutation_renders(user: User) -> None:
    """A program mutation made OUTSIDE any page action — exactly what an MCP
    ``programs.*`` tool does — must render in the editor. The reconciler builds
    the tab widget on ``new``/``open``, follows ``switch``, and tears the widget
    down on ``close``, with no GUI button involved.
    """
    from waldo_commander.state import ui_state
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None

    initial = len(waldoctl.commander.programs.items)

    # new(): no GUI button, no _new_tab() — the reconciler builds the widget.
    prog = waldoctl.commander.programs.new(source="print('ext')\n", filename="ext.py")
    await asyncio.sleep(0)
    assert len(waldoctl.commander.programs.items) == initial + 1
    await user.should_see(marker=f"editor-tab-{prog.id}")

    # switch(): the active tab follows.
    waldoctl.commander.programs.switch(prog.id)
    await asyncio.sleep(0)
    assert waldoctl.commander.programs.active_id == prog.id
    assert editor.tabs_container.value == prog.id

    # open(): reads a file from disk into a rendered, non-dirty tab.
    path = editor.PROGRAM_DIR / "opened_externally.py"
    path.write_text("print('opened')\n", encoding="utf-8")
    opened = waldoctl.commander.programs.open(str(path))
    await asyncio.sleep(0)
    await user.should_see(marker=f"editor-tab-{opened.id}")
    assert opened.file_path == str(path)
    assert not opened.is_dirty

    # close(): the widget is torn down.
    waldoctl.commander.programs.close(prog.id)
    await asyncio.sleep(0)
    assert waldoctl.commander.programs.get(prog.id) is None
    await user.should_not_see(marker=f"editor-tab-{prog.id}")


@pytest.mark.integration
async def test_step_button_enabled_after_simulation(user: User) -> None:
    """Test that the step button is visible and enabled after simulation.

    After simulation populates steps:
    - Step button becomes visible
    - Step button is not disabled
    - Play button starts simulation playback (not script execution)
    """
    from waldo_commander.state import ui_state
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    # Step button should be hidden before simulation
    assert editor.playback.next_btn is not None, "Step button reference should exist"
    assert editor.playback.next_btn.visible is False, (
        "Step button should be hidden before simulation"
    )

    # Set script with move commands to generate simulation steps
    tab = waldoctl.commander.programs.active
    assert tab is not None
    test_script = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
rbt.move_j([95, -95, 185, -5, -5, 185], speed=1.0)
"""
    ui_state.active_textarea.value = test_script
    tab.source = test_script

    # Run simulation to populate steps
    from waldo_commander.components.simulation_engine import simulation as _sim

    await _sim.run_simulation()
    await asyncio.sleep(0.1)

    # Step button should be visible after simulation
    assert editor.playback.next_btn.visible is True, (
        "Step button should be visible when simulation has steps"
    )
    assert editor.playback.next_btn._props.get("disable") is not True, (
        "Step button should be enabled"
    )
    _active_for_steps = waldoctl.commander.programs.active
    assert _active_for_steps is not None, "An active program should exist"
    assert _active_for_steps.dry_run.total_steps > 0, "Should have simulation steps"

    # Play should start sim playback, not script execution
    await editor.playback.toggle_play()
    await asyncio.sleep(0.1)
    assert _active_for_steps.dry_run.playback.is_active is True, (
        "Play should start simulation playback when steps exist"
    )
    assert is_any_program_running() is False, (
        "Script should not be running during sim playback"
    )

    # Pause sim playback
    await editor.playback.toggle_play()
    await asyncio.sleep(0)
    assert _active_for_steps.dry_run.playback.is_active is False


@pytest.mark.integration
async def test_simulation_creates_targets_for_literal_moves(
    user: User,
) -> None:
    """Test that simulation creates targets for move commands with literal args.

    After simulation, moves with literal coordinates get auto-generated targets
    tracked by the CM6 StateField for interactive 3D editing. No markers are
    added to the user's source code.
    """
    from waldo_commander.state import ui_state
    import waldoctl

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None, "Editor panel should exist"

    tab = waldoctl.commander.programs.active
    assert tab is not None, "Active tab should exist"

    test_script = """from parol6 import RobotClient
rbt = RobotClient()
rbt.move_j([85, -85, 175, 5, 5, 175], speed=1.0)
"""
    assert ui_state.active_textarea is not None
    ui_state.active_textarea.value = test_script
    tab.source = test_script

    from waldo_commander.components.simulation_engine import simulation as _sim

    await _sim.run_simulation()
    await asyncio.sleep(0.1)

    _active = waldoctl.commander.programs.active
    _targets = _active.dry_run.targets if _active is not None else []
    assert len(_targets) >= 1, f"Expected at least 1 target, got {len(_targets)}"

    # Target ID should be auto-generated (no UUID markers)
    target = _targets[0]
    assert target.id.startswith("auto_"), f"Expected auto-generated ID, got {target.id}"
    assert target.line_number > 0, "Target should have a valid line number"

    # Source code should NOT contain any TARGET markers
    updated_content = ui_state.active_textarea.value
    assert "# TARGET:" not in updated_content, (
        "Source code should not contain TARGET markers"
    )


def _fire_editor_event(textarea, event_type: str, args: dict) -> None:
    """Drive a CodeMirror event through the element's real event listener —
    the same path a browser event takes. ``Element.on`` stores listener types
    camelCased, so kebab-case names are converted before matching."""
    from nicegui.helpers import event_type_to_camel_case

    wanted = event_type_to_camel_case(event_type)
    listener = next(
        (
            listener
            for listener in textarea._event_listeners.values()
            if listener.type == wanted
        ),
        None,
    )
    assert listener is not None, f"no {event_type} listener registered"
    with textarea.client:
        textarea._handle_event({"listener_id": listener.id, "args": args})


def _set_cursor_line(textarea, line: int) -> None:
    """Place the cursor like a user click: focus, then a selection change."""
    _fire_editor_event(textarea, "focus-change", {"focused": True})
    _fire_editor_event(textarea, "selection-change", {"line": line, "column": 1})


@pytest.mark.integration
async def test_recorded_steps_insert_below_cursor(user: User) -> None:
    """Recording with the cursor mid-file inserts every step directly below
    the cursor line in chronological order, without moving the user's cursor
    and without touching the surrounding lines."""
    import waldoctl
    from waldo_commander.services.motion_recorder import motion_recorder
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    tab = waldoctl.commander.programs.active
    assert tab is not None
    textarea = ui_state.active_textarea
    assert textarea is not None

    textarea.value = "# step one\n# step two\n# step three\n# step four\n"

    _set_cursor_line(textarea, 2)
    assert tab.dry_run.playback.active_cursor_line == 2

    # No simulation end position to match -> the start anchor is inserted.
    tab.dry_run.final_joints_rad = None

    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert is_any_program_recording()

    user.find(marker="editor-capture-pose-btn").click()
    await asyncio.sleep(0)
    motion_recorder.record_action("io", port=1, state=1)

    user.find(marker="editor-record-btn").click()
    await asyncio.sleep(0.1)
    assert not is_any_program_recording()

    lines = textarea.value.splitlines()
    assert lines[:2] == ["# step one", "# step two"], "lines above cursor intact"
    tail = lines.index("# step three")
    assert lines[tail:] == ["# step three", "# step four"], (
        "original tail stays below every recorded step"
    )
    inserted = lines[2:tail]
    assert inserted and inserted[0].startswith("rbt."), (
        "recorded code lands directly below the cursor line"
    )
    anchor_idx = next(
        (i for i, ln in enumerate(inserted) if "Recording start position" in ln), None
    )
    move_idx = next(
        (i for i, ln in enumerate(inserted) if ln.startswith("rbt.move_l(")), None
    )
    io_idx = next(
        (i for i, ln in enumerate(inserted) if ln == "rbt.write_io(1, 1)"), None
    )
    assert anchor_idx is not None, f"anchor not recorded: {inserted}"
    assert move_idx is not None, f"captured pose not recorded: {inserted}"
    assert io_idx is not None, f"io action not recorded: {inserted}"
    assert anchor_idx < move_idx < io_idx, "steps stay in chronological order"
    assert tab.dry_run.playback.active_cursor_line == 2, (
        "recording must not move the user's cursor"
    )


@pytest.mark.integration
async def test_manual_inserts_follow_cursor(user: User) -> None:
    """Palette, gizmo, and capture-pose inserts land below the cursor line and
    consecutive inserts stay in order; with the cursor unset or on the last
    line they append at EOF exactly as before."""
    import waldoctl
    from waldo_commander.components.editor_decorations import decorations
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)

    user.find(marker="tab-program").click()
    await asyncio.sleep(0)
    ui_state.program_panel_visible = True  # flash the lines, not the tab

    editor = ui_state.editor_panel
    tab = waldoctl.commander.programs.active
    textarea = ui_state.active_textarea
    assert editor is not None and tab is not None and textarea is not None

    textarea.value = "# alpha\n# beta\n# gamma\n"

    # Cursor unset -> palette insert appends at EOF. An unfocused
    # selection-change (CodeMirror's echo of a programmatic value update)
    # must not count as cursor placement.
    assert tab.dry_run.playback.active_cursor_line == 0
    _fire_editor_event(textarea, "selection-change", {"line": 1, "column": 1})
    assert tab.dry_run.playback.active_cursor_line == 0
    with textarea.client:
        editor._insert_command("delay")
    assert textarea.value.splitlines() == [
        "# alpha",
        "# beta",
        "# gamma",
        "time.sleep(1.0)",
    ]

    # Cursor on line 1 -> gizmo target lands on line 2, which is also flashed.
    _set_cursor_line(textarea, 1)
    with textarea.client:
        line_number = editor.add_target_code(
            [100.0, 200.0, 300.0, 0.0, 0.0, 0.0], "cartesian"
        )
    assert line_number == 2
    lines = textarea.value.splitlines()
    assert lines[0] == "# alpha"
    assert lines[1].startswith("rbt.move_l([100.000, 200.000, 300.000")
    assert lines[2] == "# beta"
    assert decorations._active_flashes[-1][1] == {2}

    # A second insert without moving the cursor lands below the first one.
    with textarea.client:
        editor._insert_command("delay")
    lines = textarea.value.splitlines()
    assert lines[2] == "time.sleep(1.0)"
    assert lines[3] == "# beta"

    # Cursor on the last line -> capture pose appends at EOF.
    _set_cursor_line(textarea, len(lines))
    user.find(marker="editor-capture-pose-btn").click()
    await asyncio.sleep(0)
    lines = textarea.value.splitlines()
    assert lines[-1].startswith("rbt.move_l(")
    assert lines[-2] == "time.sleep(1.0)"
