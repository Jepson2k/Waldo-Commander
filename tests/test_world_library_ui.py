"""Save and reload authored workcells through the UI and real world history."""

import ast
import asyncio
from dataclasses import replace

import pytest
import waldoctl
from nicegui.testing.user_interaction import UserInteraction
from waldoctl.shapes import Box, ShapeWorld
from waldoctl.world import world_to_dict

from tests.helpers.scene_events import right_click
from tests.helpers.scene_objects import confirmed, field, select
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open, _start_recording
from waldo_commander.services import world_files
from waldo_commander.services.programs import is_any_program_recording


def library(user, view):
    for dialog, _ in view._object_dialogs:
        if not dialog.is_deleted and not dialog.value:
            UserInteraction(user, {dialog}, None).trigger("hide")
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-workcell-library").click()


def value(user, marker, new):
    user.find(marker=marker).elements.pop().set_value(new)


@pytest.mark.integration
async def test_workcell_library_save_load_undo_stale_file_and_insert(
    user, tmp_path, monkeypatch
):
    monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    live = waldoctl.commander.status.joints.angles.rad.copy()
    editor = await _start_recording(user)
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-add-box").click()
    field(user, "shape-dialog-name", "Fixture")
    user.find(marker="shape-dialog-save").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    original = handle.shapes[0]

    library(user, view)
    value(user, "workcell-name", "../escape")
    user.find(marker="workcell-save").click()
    await user.should_see("must be letters")
    value(user, "workcell-name", "bench")
    user.find(marker="workcell-save").click()
    await user.should_see("Saved bench")
    assert world_files.load_entry("bench").program == (original,)
    saved_file = (tmp_path / "bench.py").read_bytes()
    user.find(marker="workcell-save").click()
    await user.should_see("Select Replace")
    assert (tmp_path / "bench.py").read_bytes() == saved_file
    user.find(marker="workcell-close").click()

    select(user, view, "shape:Fixture")
    field(user, "object-x", 500)
    await confirmed(view, lambda: handle.shapes[0].pose[0] == 0.5)
    moved, moved_source = handle.shapes[0], editor.value
    library(user, view)
    user.find(marker="workcell-saved-tab").click()
    value(user, "workcell-saved", "bench")
    await user.should_see("Fixture · box")
    assert await wait_until(
        lambda: user.find(marker="workcell-load").elements.pop().enabled, 3
    )
    user.find(marker="workcell-load").click()
    await confirmed(view, lambda: handle.shapes == [original])
    loaded_source = editor.value
    assert loaded_source != moved_source
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [moved])
    assert editor.value == moved_source
    user.find(marker="object-redo").click()
    await confirmed(view, lambda: handle.shapes == [original])
    assert editor.value == loaded_source

    library(user, view)
    user.find(marker="workcell-saved-tab").click()
    value(user, "workcell-saved", "bench")
    assert await wait_until(
        lambda: user.find(marker="workcell-load").elements.pop().enabled, 3
    )
    world_files.save_entry("bench", ShapeWorld(program=(replace(original, x=0.2),)))
    user.find(marker="workcell-load").click()
    await user.should_see("changed elsewhere")
    assert handle.shapes == [original] and editor.value == loaded_source
    user.find(marker="workcell-close").click()

    # Outside a take an insert goes below the user's cursor.
    user.find(marker="editor-record-btn").click()
    assert await wait_until(lambda: not is_any_program_recording(), 2.0)
    original_source = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.delay(\n        0.1,  # preserve this\n    )\n"
    editor.value = original_source
    waldoctl.commander.programs.active.dry_run.playback.active_cursor_line = 4
    library(user, view)
    user.find(marker="workcell-insert").click()
    tree = ast.parse(editor.value)
    assert "# preserve this" in editor.value
    body = tree.body[1].body
    assert isinstance(body[0], ast.Expr) and body[0].value.func.attr == "delay"
    assert isinstance(body[1], ast.ImportFrom) and body[1].module == "waldoctl"
    assert body[2].value.func.attr == "set_shapes"
    shape_call = body[2].value.args[0].elts[0]
    values = {k.arg: ast.literal_eval(k.value) for k in shape_call.keywords}
    assert values["name"] == original.name and values["pose"] == original.pose
    assert handle.shapes == [original]
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    for gap in ("    # fixture before motion", ""):
        comment_source = f"from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.delay(0.1)\n{gap}\n    rbt.delay(0.2)\n"
        editor.value = comment_source
        waldoctl.commander.programs.active.dry_run.playback.active_cursor_line = 4
        library(user, view)
        user.find(marker="workcell-insert").click()
        tree = ast.parse(editor.value)
        body = tree.body[1].body
        assert len(tree.body) == 2
        assert [n.value.func.attr for n in body if isinstance(n, ast.Expr)] == [
            "delay",
            "set_shapes",
            "delay",
        ]
        assert f"{gap}\n    from waldoctl import" in editor.value

    editor.value = "from parol6 import AsyncRobotClient\nasync def task():\n    async with AsyncRobotClient() as rbt:\n        await rbt.delay(0.1)\n        # saved environment\n        await rbt.delay(0.2)\n"
    waldoctl.commander.programs.active.dry_run.playback.active_cursor_line = 5
    library(user, view)
    user.find(marker="workcell-insert").click()
    body = ast.parse(editor.value).body[1].body[0].body
    expressions = [n.value for n in body if isinstance(n, ast.Expr)]
    assert all(isinstance(n, ast.Await) for n in expressions)
    assert [n.value.func.attr for n in expressions] == ["delay", "set_shapes", "delay"]

    marker = tmp_path / "should-not-run"
    (tmp_path / "computed.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nworld = create_world()\n"
    )
    library(user, view)
    user.find(marker="workcell-saved-tab").click()
    value(user, "workcell-saved", "computed")
    await user.should_see("requires a literal")
    assert not marker.exists()
    assert not user.find(marker="workcell-load").elements.pop().enabled
    user.find(marker="workcell-close").click()

    payload = world_to_dict(ShapeWorld(program=(original,)))
    (tmp_path / "computed.py").write_text(
        f"world = world_from_dict({payload!r})\nif True:\n    world = create_world()\n"
    )
    library(user, view)
    user.find(marker="workcell-saved-tab").click()
    value(user, "workcell-saved", "computed")
    await user.should_see("requires a literal")
    assert not user.find(marker="workcell-load").elements.pop().enabled
    user.find(marker="workcell-close").click()

    for payload, message in (
        (None, "requires a literal"),
        ([], "requires a literal"),
        (
            world_to_dict(ShapeWorld(program=(replace(original, name=42),))),
            "unique, nonempty name",
        ),
        (
            world_to_dict(ShapeWorld(program=(replace(original, name=[]),))),
            "unique, nonempty name",
        ),
    ):
        (tmp_path / "malformed.py").write_text(
            f"world = world_from_dict({payload!r})\n"
        )
        library(user, view)
        user.find(marker="workcell-saved-tab").click()
        value(user, "workcell-saved", "malformed")
        await user.should_see(message)
        assert not user.find(marker="workcell-load").elements.pop().enabled
        assert handle.shapes == [original]
        user.find(marker="workcell-close").click()


@pytest.mark.integration
async def test_dismissed_load_preserves_new_selection(user, tmp_path, monkeypatch):
    monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path)
    view = await _open(user)
    handle = waldoctl.commander.scene
    handle.shapes = [
        Box(name="Fixture", x=0.1, y=0.1, z=0.1, pose=(0.4, 0, 0.05, 0, 0, 0))
    ]
    await confirmed(view)
    saved = replace(handle.shapes[0], pose=(0.5, 0, 0.05, 0, 0, 0))
    world_files.save_entry("bench", ShapeWorld(program=(saved,)))
    library(user, view)
    user.find(marker="workcell-saved-tab").click()
    value(user, "workcell-saved", "bench")
    assert await wait_until(
        lambda: user.find(marker="workcell-load").elements.pop().enabled, 3
    )
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
        user.find(marker="workcell-load").click()
        await asyncio.wait_for(entered.wait(), 5)
        user.find(marker="workcell-close").click()
        dialog = view._object_dialogs[-1][0]
        UserInteraction(user, {dialog}, None).trigger("hide")
        select(user, view, "shape:Fixture")
        release.set()
        await confirmed(view, lambda: handle.shapes == [saved])
        assert view._selected_objects == ["shape:Fixture"]
        await user.should_see(marker="object-inspector")
    finally:
        release.set()
