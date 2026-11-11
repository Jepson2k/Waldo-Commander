"""Authored object transactions through the real UI and fake-serial controller."""

import ast
import asyncio
from dataclasses import replace

import pytest
import waldoctl
from nicegui import ui
from nicegui.testing.user_interaction import UserInteraction

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, new_keepout, select
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open, _start_recording
from waldo_commander.services import world_files
from waldo_commander.services.motion_recorder import motion_recorder
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


async def _fresh_take(user, view):
    """Keep the take in hand, empty the world, and start a new take."""
    handle = waldoctl.commander.scene
    motion_recorder.keep()
    handle.shapes = []
    await confirmed(view, lambda: not handle.shapes)
    return await _start_recording(user)


@pytest.mark.integration
async def test_objects_record_undo_reload_and_recover_from_external_edits(
    user, tmp_path, monkeypatch
):
    """Keep-outs placed and edited in the scene are written into the take and
    confirmed by readback. An aborted drag, a non-positive size or a
    non-finite position changes nothing; undo and redo step through moves,
    resizes, renames, duplicates and deletes back to the program as it was,
    and a new edit after an undo stays undoable until the take's Undo takes
    them all out. Hiding is this view's alone, a locked object refuses every
    edit, and Shift-click on a row extends the selection. A program edit made
    elsewhere, during a drag or while an edit awaits its readback, clears the
    undo history and is kept; a face offered for a keep-out on an object that
    changed since draws nothing. The Add menu draws on the floor, the robot's
    row names its tool, and a capture converting in the take refuses object
    edits, clears their history and stays in the take."""
    from nicegui import run
    from tests.test_capture_recording import _capture
    from waldo_commander.services import motion_recorder as module

    monkeypatch.setenv("WALDO_RECORDING_DIR", str(tmp_path))
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

    new_keepout(user, view, [0.40037, 0.09981, 0])
    await confirmed(view, lambda: len(handle.shapes) == 1)
    name = handle.shapes[0].name
    assert view._selected_objects == [f"shape:{name}"]
    assert handle.shapes[0].pose[:2] == (0.4, 0.1)
    # The card opened the scene panel, and leaves it on the new object.
    assert panel.is_open
    assert view._keepout_card is None
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

    for marker in ("object-undo", "object-redo"):
        assert not user.find(marker=marker).elements.pop().enabled
    assert user.find(marker="object-undo-tooltip").elements.pop().text == (
        "Undo history cleared — the world or program changed elsewhere"
    )
    await user.should_not_see(marker="object-feedback")
    select(user, view)
    await user.should_not_see(marker="scene-feedback")

    # A face offered for a keep-out is the face as it was right-clicked:
    # once its object changes elsewhere, nothing is drawn on it.
    box = handle.shapes[0]
    UserInteraction(user, {view.scene}, None).trigger(
        "context",
        {
            "epoch": view.scene.gestures.epoch,
            "gen": 1,
            "cx": 100,
            "cy": 100,
            "hits": [f"shape:{box.name}"],
            "ground": [0.4, 0.1, 0],
            "surface": {
                "id": f"shape:{box.name}",
                "x": box.pose[0],
                "y": box.pose[1],
                "z": box.pose[2] + box.z / 2,
            },
        },
    )
    user.find(marker="scene-face-keepout").click()
    changed = replace(handle.shapes[0], x=0.3)
    await waldoctl.commander.client.set_shapes([changed, handle.shapes[1]])
    await handle.refresh_from_backend()
    # A readback already under way adopts the change instead of this one.
    assert await wait_until(lambda: handle.shapes[0] == changed, 5)
    user.find(marker="keepout-draw").click()
    await asyncio.sleep(0)
    assert view._drawing is None
    assert "has changed" in user.find(marker="keepout-feedback").elements.pop().text
    assert handle.shapes[0] == changed
    assert len(handle.shapes) == 2
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)
    assert source_before != textarea.value

    # The scene panel's Add menu draws on the floor of the world as it is now.
    await confirmed(view)
    user.find(marker="scene-add-keepout").click()
    assert user.find(marker="keepout-surface").elements.pop().text.startswith("Floor")
    user.find(marker="keepout-draw").click()
    drawing = view.scene.interaction["shapeDraw"]
    assert view._drawing["primitive"] == "box"
    assert drawing["quaternion"] == pytest.approx([0, 0, 0, 1])
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

    # A new edit after an undo stays undoable, and the take's Undo then
    # takes back everything the edits wrote.
    textarea = await _fresh_take(user, view)
    source = textarea.value
    new_keepout(user, view, [0.4, 0.1, 0])
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

    # An edit stays pending until the real readback lands, and a program edit
    # made meanwhile is kept.
    textarea = await _fresh_take(user, view)
    source_before = textarea.value
    actual_readback = client.shapes
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_readback():
        world = await actual_readback()
        entered.set()
        await release.wait()
        return world

    with monkeypatch.context() as held:
        held.setattr(client, "shapes", held_readback)
        try:
            new_keepout(user, view, [0.45, 0, 0])
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
            assert not view.object_history.feedback
            assert not user.find(marker="object-undo").elements.pop().enabled
            assert user.find(marker="object-undo-tooltip").elements.pop().text == (
                "Undo history cleared — the world or program changed elsewhere"
            )
            await user.should_not_see(marker="object-feedback")
            await user.should_not_see(marker="scene-feedback")
        finally:
            release.set()

    # A capture converting in the take: last, as its move leaves the arm
    # away from home.
    textarea = await _fresh_take(user, view)
    recorder = module.motion_recorder
    target = list(await client.angles())
    target[0] += 12
    target[2] -= 8
    index = await client.move_j(target, duration=1.2)
    assert await client.wait_command(index, timeout=10)
    assert await wait_until(lambda: _capture(recorder.session) is not None, 20)
    block = _capture(recorder.session)

    new_keepout(user, view, [0.45, 0, 0])
    await confirmed(view, lambda: bool(view.object_history.undo_stack))
    shapes = list(waldoctl.commander.scene.shapes)
    real_io_bound = run.io_bound
    converting, release_conversion = asyncio.Event(), asyncio.Event()

    async def hold_conversion(function, *args, **kwargs):
        result = await real_io_bound(function, *args, **kwargs)
        if function is module.span_to_lines:
            converting.set()
            await release_conversion.wait()
        return result

    monkeypatch.setattr(run, "io_bound", hold_conversion)
    try:
        user.find(marker="staged-capture-mode").elements.pop().set_value("raw")
        await asyncio.wait_for(converting.wait(), 20)
        field(user, "object-x", 550)
        await asyncio.sleep(0)
        assert waldoctl.commander.scene.shapes == shapes
        assert not view.object_history.undo_stack
        release_conversion.set()
        assert await wait_until(lambda: block.mode == "raw" and not block.busy, 5)
        assert block in recorder.session.blocks
        assert "replay_demonstration" in textarea.value
    finally:
        release_conversion.set()


@pytest.mark.integration
async def test_the_scene_panel_offers_to_carry_targets_and_leaves_the_rest_alone(
    user,
):
    """A target written in world coordinates stays put when the object it
    sat on moves. The scene panel says so, and carries it along on request,
    in the same undo step as the move. The panel's mode buttons and isolation
    item change in place, keeping the focus they were pressed with; the group
    eye and the tab show what is hidden; toggling a row leaves a drawing in
    hand alone; a panel the user closed stays closed through an undo of a
    move, and restoring a deleted object selects it and opens the panel.
    Multi-select mode, Shift-click in the view and row clicks add to the
    selection and take from it."""
    from waldo_commander.components.simulation_engine import simulation

    view = await _open(user)
    await confirmed(view)
    handle = waldoctl.commander.scene
    # A workpiece standing under the first target, 2 cm below it; the
    # second target is clear of it.
    new_keepout(user, view, [0.0, 0.28, 0])
    await confirmed(view, lambda: len(handle.shapes) == 1)
    user.find(marker="object-collision").elements.pop().set_value(False)
    await confirmed(view, lambda: not handle.shapes[0].collision)
    field(user, "object-dim-z", 230)
    await confirmed(view, lambda: handle.shapes[0].z == pytest.approx(0.23))
    field(user, "object-z", 115)
    await confirmed(view, lambda: handle.shapes[0].pose[2] == pytest.approx(0.115))
    name = handle.shapes[0].name
    textarea = ui_state.active_textarea
    source = (
        "from parol6 import RobotClient\n"
        "rbt = RobotClient()\n"
        "rbt.move_l(pose=[0, 280, 250, 90, 0, 90], speed=0.5)\n"
        "rbt.move_l(pose=[100, 280, 250, 90, 0, 90], speed=0.5)\n"
    )
    textarea.value = source
    await simulation.run_simulation()
    program = waldoctl.commander.programs.active
    assert await wait_until(lambda: len(program.dry_run.targets) == 2, 5)

    field(user, "object-x", 50)
    await confirmed(view, lambda: handle.shapes[0].pose[0] == pytest.approx(0.05))
    await user.should_see(f"1 target on {name} stayed where it was")
    assert textarea.value == source
    user.find(marker="stranded-show").click()
    user.find(marker="stranded-move").click()
    assert await wait_until(lambda: textarea.value != source, 3)
    moved, kept = (
        ast.literal_eval(call.value.keywords[0].value)
        for call in ast.parse(textarea.value).body[2:]
    )
    assert moved == pytest.approx([50, 280, 250, 90, 0, 90], abs=1e-6)
    assert kept == [100, 280, 250, 90, 0, 90]
    await user.should_not_see(marker="stranded-text")

    # One undo takes back the move and what it carried.
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes[0].pose[0] == pytest.approx(0.0))
    assert await wait_until(lambda: textarea.value == source, 3)

    # The presses below count keep-outs from an empty world.
    handle.shapes = []
    await confirmed(view, lambda: not handle.shapes)
    panel = ui_state.scene_panel
    for count, point in enumerate(([0.4, 0.1, 0], [0.4, -0.15, 0]), 1):
        new_keepout(user, view, point)
        await confirmed(view, lambda n=count: len(handle.shapes) == n)
    first, second = handle.shapes[0].name, handle.shapes[1].name
    assert view._selected_objects == [f"shape:{second}"]

    # The mode buttons and the isolation item change in place: the one
    # pressed stays, with the keyboard focus it was pressed with.
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
    user.find(marker="scene-add-keepout").click()
    user.find(marker="keepout-draw").click()
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

    new_keepout(user, view, [0.4, 0.3, 0])
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
