"""Save, reload and insert workcells through the library in the scene panel,
beside a scene that stays editable, with real world history."""

import ast
import asyncio
from dataclasses import replace

import pytest
import waldoctl
from waldoctl.shapes import Box, ShapeWorld
from waldoctl.world import world_to_dict

from tests.helpers.scene_events import right_click
from tests.helpers.scene_objects import confirmed, field, select
from tests.helpers.wait import wait_until
from tests.test_scene_jog_handles import _open, _start_recording
from waldo_commander.services import world_files
from waldo_commander.services.programs import is_any_program_recording
from waldo_commander.state import ui_state


def library(user, view):
    """Open the library from the scene's right-click menu."""
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-workcell-library").click()


def value(user, marker, new):
    user.find(marker=marker).elements.pop().set_value(new)


def element(user, marker):
    try:
        return user.find(marker=marker).elements.pop()
    except AssertionError:
        return None


async def listed(user, name):
    """What the library's row for the saved *name* says, once it is listed."""
    assert await wait_until(
        lambda: element(user, f"workcell-preview:{name}") is not None, 3
    ), f"{name} was never listed"
    return element(user, f"workcell-preview:{name}").text


def cursor(line):
    waldoctl.commander.programs.active.dry_run.playback.active_cursor_line = line


def shape_keywords(call):
    """The keyword values of the first constructor in a ``set_shapes`` call."""
    shape = call.args[0].elts[0]
    # One keyword per line keeps the inserted code readable.
    assert len({k.lineno for k in shape.keywords}) == len(shape.keywords)
    return {k.arg: ast.literal_eval(k.value) for k in shape.keywords}


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
    take_source = editor.value
    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    # A recorded edit's import joins the program's imports, not the take.
    assert editor.value.startswith(
        "from parol6 import RobotClient\nfrom waldoctl import Box\nwith RobotClient()"
    )
    field(user, "object-name", "Fixture")
    await confirmed(view, lambda: handle.shapes[0].name == "Fixture")
    original = handle.shapes[0]

    # The panel's Workcells button opens the library at the top of the panel.
    panel = ui_state.scene_panel
    user.find(marker="scene-close").click()
    assert not panel.is_open
    user.find(marker="scene-workcells").click()
    await user.should_see(marker="workcell-library")
    assert panel.is_open
    assert panel.body in element(user, "workcell-library").ancestors()
    assert await wait_until(lambda: element(user, "workcell-empty") is not None, 3)
    assert "Nothing saved yet" in element(user, "workcell-empty").text
    value(user, "workcell-name", "../escape")
    await user.should_see("must be letters")
    assert not element(user, "workcell-save").enabled
    value(user, "workcell-name", "bench")
    user.find(marker="workcell-save").click()
    await user.should_see("Saved bench")
    assert element(user, "workcell-name").value == ""
    assert world_files.load_entry("bench").program == (original,)
    assert await listed(user, "bench") == "1 object: Fixture"
    # The same objects under a saved name replace nothing worth a warning.
    value(user, "workcell-name", "bench")
    await user.should_not_see("Replace overwrites")
    assert element(user, "workcell-save").text == "Save"

    # The scene stays editable beside the library; Save then asks before
    # overwriting the saved, now different, workcell.
    select(user, view, "shape:Fixture")
    field(user, "object-x", 500)
    await confirmed(view, lambda: handle.shapes[0].pose[0] == 0.5)
    moved, moved_source = handle.shapes[0], editor.value
    saved_file = (tmp_path / "bench.py").read_bytes()
    user.find(marker="workcell-save").click()
    await user.should_see("Replace overwrites the saved bench")
    assert element(user, "workcell-save").text == "Replace"
    assert (tmp_path / "bench.py").read_bytes() == saved_file
    value(user, "workcell-name", "")
    await user.should_not_see("Replace overwrites")

    user.find(marker="workcell-load:bench").click()
    await confirmed(view, lambda: handle.shapes == [original])
    await user.should_see("Loaded. Undo restores", retries=30)
    loaded_source = editor.value
    assert loaded_source != moved_source
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [moved])
    assert editor.value == moved_source
    user.find(marker="object-redo").click()
    await confirmed(view, lambda: handle.shapes == [original])
    assert editor.value == loaded_source

    # A file changed since it was listed is read again, not loaded unseen.
    world_files.save_entry("bench", ShapeWorld(program=(replace(original, x=0.2),)))
    user.find(marker="workcell-load:bench").click()
    await user.should_see("Changed on disk", retries=30)
    assert handle.shapes == [original] and editor.value == loaded_source
    value(user, "workcell-name", "bench")
    await user.should_see("Replace overwrites the saved bench")
    user.find(marker="workcell-save").click()
    await user.should_see("Saved bench")
    assert world_files.load_entry("bench").program == (original,)

    # During a take an insert is staged with it: the take's Undo takes it out
    # with everything else the take wrote.
    user.find(marker="workcell-insert").click()
    await user.should_see("Inserted 1 object")
    assert editor.value.count("set_shapes(") > loaded_source.count("set_shapes(")
    assert editor.value.count("from waldoctl import") == 1
    user.find(marker="staged-undo").click()
    assert await wait_until(lambda: editor.value == take_source, 3)
    assert not is_any_program_recording()

    # Outside a take it goes below the statement at the user's cursor, and
    # its import joins the program's imports.
    editor.value = "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.delay(\n        0.1,  # preserve this\n    )\n"
    cursor(4)
    user.find(marker="workcell-insert").click()
    tree = ast.parse(editor.value)
    assert "# preserve this" in editor.value
    assert [ast.unparse(n) for n in tree.body[:2]] == [
        "from parol6 import RobotClient",
        "from waldoctl import Box",
    ]
    body = tree.body[2].body
    assert [n.value.func.attr for n in body] == ["delay", "set_shapes"]
    values = shape_keywords(body[1].value)
    assert values["name"] == original.name
    assert values["pose"] == pytest.approx(original.pose, abs=5e-5)
    assert handle.shapes == [original]
    assert waldoctl.commander.status.joints.angles.rad == pytest.approx(live)

    for gap in ("    # fixture before motion", ""):
        editor.value = f"from parol6 import RobotClient\nwith RobotClient() as rbt:\n    rbt.delay(0.1)\n{gap}\n    rbt.delay(0.2)\n"
        cursor(4)
        user.find(marker="workcell-insert").click()
        tree = ast.parse(editor.value)
        assert len(tree.body) == 3
        assert [n.value.func.attr for n in tree.body[2].body] == [
            "delay",
            "set_shapes",
            "delay",
        ]
        assert f"{gap}\n    rbt.set_shapes([\n        Box(\n" in editor.value

    editor.value = "from parol6 import AsyncRobotClient\nasync def task():\n    async with AsyncRobotClient() as rbt:\n        await rbt.delay(0.1)\n        # saved environment\n        await rbt.delay(0.2)\n"
    cursor(5)
    user.find(marker="workcell-insert").click()
    tree = ast.parse(editor.value)
    assert ast.unparse(tree.body[1]) == "from waldoctl import Box"
    expressions = [n.value for n in tree.body[2].body[0].body]
    assert all(isinstance(n, ast.Await) for n in expressions)
    assert [n.value.func.attr for n in expressions] == ["delay", "set_shapes", "delay"]

    # A saved workcell goes in as code without loading it, rounded to 0.1 mm
    # and 1e-4 rad in the units the constructors take.
    precise = Box(
        name="Precise",
        x=0.12345678,
        y=0.1,
        z=0.00002,
        pose=(0.2502414425235, 8.2473712e-05, -3e-06, -2e-07, 0.0, 0.5235987755982988),
    )
    world_files.save_entry("precise", ShapeWorld(program=(precise,)))
    library(user, view)
    await listed(user, "precise")
    editor.value = "from parol6 import RobotClient\nrbt = RobotClient()\n"
    cursor(2)
    user.find(marker="workcell-insert-saved:precise").click()
    await user.should_see("Inserted at line 4")
    tree = ast.parse(editor.value)
    assert ast.unparse(tree.body[1]) == "from waldoctl import Box"
    assert shape_keywords(tree.body[3].value) == {
        "name": "Precise",
        "x": 0.1235,
        "y": 0.1,
        "z": 0.0001,
        "pose": (0.2502, 0.0001, 0.0, 0.0, 0.0, 0.5236),
    }
    assert "e-0" not in editor.value and "-0.0" not in editor.value
    assert handle.shapes == [original]

    marker = tmp_path / "should-not-run"
    (tmp_path / "computed.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nworld = create_world()\n"
    )
    library(user, view)
    assert "requires a literal" in await listed(user, "computed")
    assert not marker.exists()
    assert not element(user, "workcell-load:computed").enabled

    payload = world_to_dict(ShapeWorld(program=(original,)))
    (tmp_path / "computed.py").write_text(
        f"world = world_from_dict({payload!r})\nif True:\n    world = create_world()\n"
    )
    library(user, view)
    assert "requires a literal" in await listed(user, "computed")
    assert not element(user, "workcell-load:computed").enabled

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
        assert message in await listed(user, "malformed")
        assert not element(user, "workcell-load:malformed").enabled
        assert not element(user, "workcell-insert-saved:malformed").enabled
        assert handle.shapes == [original]

    user.find(marker="workcell-library").trigger("keydown.escape")
    await user.should_not_see(marker="workcell-library")


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
    await listed(user, "bench")
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
        user.find(marker="workcell-load:bench").click()
        await asyncio.wait_for(entered.wait(), 5)
        user.find(marker="workcell-close").click()
        await user.should_not_see(marker="workcell-library")
        select(user, view, "shape:Fixture")
        release.set()
        await confirmed(view, lambda: handle.shapes == [saved])
        assert view._selected_objects == ["shape:Fixture"]
        await user.should_see(marker="object-inspector")
    finally:
        release.set()
