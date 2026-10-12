"""Authored object transactions through the real UI and fake-serial controller."""

import asyncio
from dataclasses import replace

import pytest
import waldoctl
from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, select
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open, _start_recording
from waldo_commander.services import world_files
from waldo_commander.state import ui_state


def row(user, name):
    """The scene panel's row for the object ``name``."""
    return user.find(marker=f"scene-row-{name}").elements.pop()


def listed(user):
    """The objects the scene panel's tree lists, in order."""
    tree = user.find(marker="scene-tree").elements.pop()
    return [
        marker.removeprefix("scene-row-")
        for element in tree.descendants()
        for marker in element._markers
        if marker.startswith("scene-row-")
    ]


def robot_row(user):
    """What the tree's robot row says: the robot's name, then its tool."""
    robot = user.find(marker="scene-robot").elements.pop()
    return [e.text for e in robot.descendants() if isinstance(e, ui.label)]


@pytest.mark.integration
async def test_objects_record_undo_reload_and_recover_from_external_edits(
    user, tmp_path, monkeypatch
):
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    live = waldoctl.commander.status.joints.angles.rad.copy()
    textarea = await _start_recording(user)
    source_before = textarea.value
    panel = ui_state.scene_panel
    await user.should_see(marker="scene-robot")
    assert robot_row(user)[0] == ui_state.active_robot.name
    user.find(marker="scene-close").click()
    assert not panel.is_open

    right_click(user, view.scene, [], [0.40037, 0.09981, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    name = handle.shapes[0].name
    assert view._selected_objects == [f"shape:{name}"]
    assert handle.shapes[0].pose[:2] == (0.4, 0.1)
    # Selecting an object opens the scene panel on it.
    assert panel.is_open
    assert listed(user) == [name]
    assert "scene-row--selected" in row(user, name).classes
    assert user.find(marker="object-name").elements.pop().value == name
    assert "set_shapes(" in textarea.value
    source_created = textarea.value
    original = handle.shapes[0]

    arrows = view.scene.interaction["shapeMove"]
    Drag(user, view.scene, "shape").begin(**arrows).abort()
    assert handle.shapes == [original]
    assert len(view.object_history.undo_stack) == 1
    Drag(user, view.scene, "shape").begin(**arrows).release(
        **arrows, x=0.5, y=0.1, z=0.05
    )
    await confirmed(view, lambda: handle.shapes[0].pose[0] == 0.5)
    assert len(view.object_history.undo_stack) == 2
    source_moved = textarea.value
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [original])
    assert textarea.value == source_created
    user.find(marker="object-redo").click()
    await confirmed(view, lambda: handle.shapes[0].pose[0] == 0.5)
    assert textarea.value == source_moved

    for invalid in (0, -5):
        field(user, "object-dim-x", invalid)
        await asyncio.sleep(0)
        assert handle.shapes[0].x == original.x
    for invalid in (float("nan"), float("inf")):
        arrows = view.scene.interaction["shapeMove"]
        Drag(user, view.scene, "shape").begin(**arrows).release(
            **arrows, x=invalid, y=0.1, z=0.05
        )
        await asyncio.sleep(0)
        assert handle.shapes[0].pose[0] == 0.5
    field(user, "object-dim-x", 240)
    await confirmed(view, lambda: handle.shapes[0].x == 0.24)
    field(user, "object-name", "fixture-edited")
    await confirmed(view, lambda: handle.shapes[0].name == "fixture-edited")
    assert view._selected_objects == ["shape:fixture-edited"]

    # Hidden only from this view: the object stays in the world the
    # controller checks for collisions.
    key = "shape:fixture-edited"
    edited = list(handle.shapes)
    user.find(marker="object-hide").click()
    assert view._hidden_objects == {key}
    assert not view._shape_objects[key].visible_
    assert "scene-row--hidden" in row(user, "fixture-edited").classes
    select(user, view)
    assert handle.shapes == edited
    assert (await waldoctl.commander.client.shapes()).program == tuple(edited)
    user.find(marker="scene-eye-fixture-edited").trigger("click.stop")
    assert not view._hidden_objects
    assert view._shape_objects[key].visible_

    user.find(marker="scene-row-fixture-edited").click()
    assert view._selected_objects == [key]
    assert "scene-row--selected" in row(user, "fixture-edited").classes
    user.find(marker="scene-lock-fixture-edited").trigger("click.stop")
    assert view._locked_objects == {key}
    assert view.scene.interaction["shapeMove"] is None
    await user.should_see(marker="object-readonly")
    for marker in ("object-name", "object-x", "object-dim-x", "object-delete"):
        assert not user.find(marker=marker).elements.pop().enabled
    right_click(user, view.scene, [key])
    for marker in (
        "shape-menu-edit",
        "shape-menu-delete",
        "shape-menu-move",
        "shape-menu-propose",
    ):
        assert not next(iter(user.find(marker=marker).elements)).enabled
    user.find(marker="object-lock").click()
    assert not view._locked_objects
    assert view.scene.interaction["shapeMove"] is not None
    user.find(marker="object-duplicate").click()
    await confirmed(view, lambda: len(handle.shapes) == 2)
    duplicated = list(handle.shapes)
    copy = duplicated[1].name
    assert view._selected_objects == [f"shape:{copy}"]
    assert listed(user) == ["fixture-edited", copy]
    assert "scene-row--selected" in row(user, copy).classes
    assert "scene-row--selected" not in row(user, "fixture-edited").classes

    user.find(marker="scene-hide-all-shape").click()
    assert view._hidden_objects == {key, f"shape:{copy}"}
    assert handle.shapes == duplicated
    user.find(marker="scene-hide-all-shape").click()
    assert not view._hidden_objects

    # Shift-click on a row adds its object to the selection, and takes it
    # out again.
    user.find(marker="scene-row-fixture-edited").trigger("click", {"shiftKey": True})
    assert view._selected_objects == [f"shape:{copy}", key]
    await user.should_see(marker="object-title", content="2 keep-outs")
    user.find(marker="scene-row-fixture-edited").trigger("click", {"shiftKey": True})
    assert view._selected_objects == [f"shape:{copy}"]

    user.find(marker="object-delete").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    assert listed(user) == ["fixture-edited"]
    user.find(marker="object-undo").click()
    await confirmed(
        view,
        lambda: handle.shapes == duplicated
        and view._selected_objects == [f"shape:{copy}"],
    )
    assert listed(user) == ["fixture-edited", copy]

    monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
    world = await waldoctl.commander.client.shapes()
    path = world_files.save_entry("authored-fixture", world)
    assert path.is_file()
    loaded = world_files.load_entry("authored-fixture")
    assert loaded.program == world.program
    assert loaded.installation == world.installation

    edits = len(view.object_history.undo_stack)
    for remaining in range(edits - 1, -1, -1):
        user.find(marker="object-undo").click()
        await confirmed(view, lambda: len(view.object_history.undo_stack) == remaining)
    assert handle.shapes == []
    assert textarea.value == source_before
    redo_count = len(view.object_history.redo_stack)
    for remaining in range(redo_count - 1, redo_count - edits - 1, -1):
        user.find(marker="object-redo").click()
        await confirmed(view, lambda: len(view.object_history.redo_stack) == remaining)
    assert handle.shapes == duplicated

    select(user, view, "shape:fixture-edited")
    arrows = view.scene.interaction["shapeMove"]
    drag = Drag(user, view.scene, "shape").begin(**arrows)
    textarea.value += "# changed outside the scene\n"
    assert await wait_until(lambda: view._shape_move_active is None, 3)
    drag.release(**arrows, x=0.8, y=0.1, z=0.05)
    await asyncio.sleep(0)
    assert handle.shapes == duplicated
    assert not view.object_history.undo_stack

    # A drawing chosen from a menu opened before an external change is
    # refused: the world it was offered for is gone.
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    changed = replace(handle.shapes[0], x=0.3)
    await waldoctl.commander.client.set_shapes([changed, handle.shapes[1]])
    await handle.refresh_from_backend()
    # A readback already under way adopts the change instead of this one.
    assert await wait_until(lambda: handle.shapes[0] == changed, 5)
    user.find(marker="scene-draw-box").click()
    await asyncio.sleep(0)
    assert view._drawing is None
    assert handle.shapes[0] == changed
    assert len(handle.shapes) == 2
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
    assert source_before != textarea.value

    # The scene panel's draw menu draws on the floor of the world as it is now.
    await confirmed(view)
    user.find(marker="scene-panel-draw-box").click()
    drawing = view.scene.interaction["shapeDraw"]
    assert view._drawing["primitive"] == "box"
    assert drawing["label"] == "Floor"
    UserInteraction(user, {view.scene}, None).trigger(
        "draw_cancel", {"session": drawing["session"]}
    )
    assert view._drawing is None

    # The robot's row names the tool it carries.
    client = waldoctl.commander.client
    tool = ui_state.active_robot.tools["PNEUMATIC"].display_name
    try:
        assert await client.wait_command(
            await client.select_tool("PNEUMATIC"), timeout=5
        )
        assert await wait_until(
            lambda: robot_row(user) == [ui_state.active_robot.name, tool], 5
        ), robot_row(user)
    finally:
        assert await client.wait_command(await client.select_tool("NONE"), timeout=5)
    assert await wait_until(
        lambda: robot_row(user) == [ui_state.active_robot.name], 5
    ), robot_row(user)


@pytest.mark.integration
async def test_the_scene_panel_leaves_alone_what_a_press_does_not_change(user):
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    panel = ui_state.scene_panel
    for count, point in enumerate(([0.4, 0.1, 0], [0.4, -0.15, 0]), 1):
        right_click(user, view.scene, [], point)
        user.find(marker="scene-add-box").click()
        await confirmed(view, lambda n=count: len(handle.shapes) == n)
    first, second = handle.shapes[0].name, handle.shapes[1].name
    assert view._selected_objects == [f"shape:{second}"]

    # The mode and isolation buttons change in place: the one pressed stays,
    # with the keyboard focus it was pressed with.
    for marker in ("object-mode-resize", "object-isolate"):
        button = user.find(marker=marker).elements.pop()
        user.find(marker=marker).click()
        await asyncio.sleep(0)
        assert not button.is_deleted, marker
    assert view._object_mode == "resize"

    # With one of two hidden the group's eye shows them all, and the tab
    # says something is hidden while the panel is closed.
    user.find(marker=f"scene-eye-{first}").trigger("click.stop")
    assert view._hidden_objects == {f"shape:{first}"}
    assert "alert" in panel.tab.props and "1 hidden" in panel.tip.text
    user.find(marker="scene-hide-all-shape").click()
    assert not view._hidden_objects
    assert "alert" not in panel.tab.props

    # Toggling a row the work in hand is not about leaves that work alone.
    user.find(marker="scene-panel-draw-box").click()
    drawing = view._drawing
    assert drawing is not None
    user.find(marker=f"scene-lock-{first}").trigger("click.stop")
    user.find(marker=f"scene-eye-{first}").trigger("click.stop")
    assert view._drawing is drawing
    UserInteraction(user, {view.scene}, None).trigger(
        "draw_cancel", {"session": view.scene.interaction["shapeDraw"]["session"]}
    )
    user.find(marker=f"scene-eye-{first}").trigger("click.stop")
    user.find(marker=f"scene-lock-{first}").trigger("click.stop")

    # A panel the user closed stays closed through an undo of the selected
    # object's move; restoring a deleted object selects it, and opens it.
    user.find(marker=f"scene-row-{second}").click()
    user.find(marker="scene-close").click()
    arrows = view.scene.interaction["shapeMove"]
    moved = handle.shapes[1]
    Drag(user, view.scene, "shape").begin(**arrows).release(
        **arrows, x=0.5, y=-0.15, z=0.05
    )
    await confirmed(view, lambda: handle.shapes[1].pose[0] == 0.5)
    view.undo_edit()
    await confirmed(view, lambda: handle.shapes[1] == moved)
    assert view._selected_objects == [f"shape:{second}"]
    assert not panel.is_open
    right_click(user, view.scene, [f"shape:{second}"])
    user.find(marker="shape-menu-delete").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    view.undo_edit()
    await confirmed(view, lambda: len(handle.shapes) == 2)
    assert view._selected_objects == [f"shape:{second}"]
    assert panel.is_open

    right_click(user, view.scene, [], [0.4, 0.3, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: len(handle.shapes) == 3)
    third = handle.shapes[-1].name
    select(user, view)
    user.find(marker="scene-multi-select").click()
    select(user, view, f"shape:{first}")
    select(user, view, f"shape:{second}")
    assert view._selected_objects == [f"shape:{first}", f"shape:{second}"]
    select(user, view, f"shape:{first}")
    assert view._selected_objects == [f"shape:{second}"]
    select(user, view, f"shape:{first}")
    user.find(marker="scene-multi-select").click()
    assert view._selected_objects == [f"shape:{second}", f"shape:{first}"]
    select(user, view, f"shape:{third}")
    assert view._selected_objects == [f"shape:{third}"]
    select(user, view, f"shape:{first}", extend=True)
    assert view._selected_objects == [f"shape:{third}", f"shape:{first}"]
    user.find(marker="scene-multi-select").click()
    user.find(marker=f"scene-row-{second}").click()
    assert view._selected_objects == [
        f"shape:{third}",
        f"shape:{first}",
        f"shape:{second}",
    ]
    user.find(marker=f"scene-row-{second}").click()
    assert view._selected_objects == [f"shape:{third}", f"shape:{first}"]
    select(user, view)
    assert view._selected_objects == []


@pytest.mark.integration
async def test_object_edit_stays_pending_until_real_readback_and_preserves_new_source(
    user, monkeypatch
):
    view = await _open(user)
    await confirmed(view)
    handle = waldoctl.commander.scene
    textarea = await _start_recording(user)
    source_before = textarea.value
    client = waldoctl.commander.client
    actual_readback = client.shapes
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_readback():
        world = await actual_readback()
        entered.set()
        await release.wait()
        return world

    monkeypatch.setattr(client, "shapes", held_readback)
    try:
        right_click(user, view.scene, [], [0.45, 0, 0])
        user.find(marker="scene-add-box").click()
        await asyncio.wait_for(entered.wait(), 5)
        assert view.object_history.busy
        assert not handle.confirmed
        assert textarea.value == source_before
        assert (await actual_readback()).program == tuple(handle.shapes)
        external = source_before + "# newer program edit\n"
        textarea.value = external
        release.set()
        await confirmed(view)
        assert textarea.value == external
        assert not view.object_history.undo_stack
        assert "program changed" in view.object_history.feedback
    finally:
        release.set()


@pytest.mark.integration
async def test_scene_undo_redo_and_new_edits_remain_undoable_as_a_recording_take(user):
    view = await _open(user)
    await confirmed(view)
    handle = waldoctl.commander.scene
    textarea = await _start_recording(user)
    source = textarea.value
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    original = handle.shapes[0]
    field(user, "object-x", 500)
    await confirmed(view, lambda: handle.shapes[0].pose[0] == 0.5)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [original])
    user.find(marker="object-redo").click()
    await confirmed(view, lambda: handle.shapes[0].pose[0] == 0.5)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [original])
    field(user, "object-dim-x", 220)
    await confirmed(view, lambda: handle.shapes[0].x == 0.22)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [original])
    user.find(marker="staged-undo").click()
    assert await wait_until(lambda: textarea.value == source, 3)


@pytest.mark.integration
async def test_capture_conversion_invalidates_object_history_and_keeps_live_blocks(
    user, monkeypatch, tmp_path
):
    from nicegui import run
    from tests.test_capture_recording import _capture
    from waldo_commander.services import motion_recorder as module

    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
    view = await _open(user)
    await confirmed(view)
    textarea = await _start_recording(user)
    recorder = module.motion_recorder
    client = waldoctl.commander.client
    target = list(await client.angles())
    target[0] += 12
    target[2] -= 8
    index = await client.move_j(target, duration=1.2)
    assert await client.wait_command(index, timeout=10)
    assert await wait_until(lambda: _capture(recorder.session) is not None, 20)
    block = _capture(recorder.session)

    right_click(user, view.scene, [], [0.45, 0, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: bool(view.object_history.undo_stack))
    shapes = list(waldoctl.commander.scene.shapes)
    real_io_bound = run.io_bound
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_conversion(function, *args, **kwargs):
        result = await real_io_bound(function, *args, **kwargs)
        if function is module.span_to_lines:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(run, "io_bound", hold_conversion)
    try:
        user.find(marker="staged-capture-mode").elements.pop().set_value("raw")
        await asyncio.wait_for(entered.wait(), 20)
        field(user, "object-x", 550)
        await asyncio.sleep(0)
        assert waldoctl.commander.scene.shapes == shapes
        assert not view.object_history.undo_stack
        release.set()
        assert await wait_until(lambda: block.mode == "raw" and not block.busy, 5)
        assert block in recorder.session.blocks
        assert "replay_demonstration" in textarea.value
    finally:
        release.set()
