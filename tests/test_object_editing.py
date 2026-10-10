"""Authored object transactions through the real UI and fake-serial controller."""

import asyncio
from dataclasses import replace

import pytest
import waldoctl

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, select
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open, _start_recording
from waldo_commander.services import world_files


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

    right_click(user, view.scene, [], [0.40037, 0.09981, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    assert view._selected_objects == [f"shape:{handle.shapes[0].name}"]
    assert handle.shapes[0].pose[:2] == (0.4, 0.1)
    await user.should_see(marker="object-title", content="Keep-out")
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

    user.find(marker="object-hide").click()
    select(user, view)
    await user.should_see(marker="object-show-hidden")
    assert (await waldoctl.commander.client.shapes()).program == tuple(handle.shapes)
    user.find(marker="object-show-hidden").click()
    assert not view._hidden_objects
    select(user, view, "shape:fixture-edited")
    user.find(marker="object-lock").click()
    right_click(user, view.scene, ["shape:fixture-edited"])
    for marker in (
        "shape-menu-edit",
        "shape-menu-delete",
        "shape-menu-move",
        "shape-menu-propose",
    ):
        assert not next(iter(user.find(marker=marker).elements)).enabled
    user.find(marker="object-lock").click()
    user.find(marker="object-duplicate").click()
    await confirmed(view, lambda: len(handle.shapes) == 2)
    duplicated = list(handle.shapes)
    user.find(marker="object-delete").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == duplicated)

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
