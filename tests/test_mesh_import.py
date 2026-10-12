"""Fixture authoring through the upload UI and the real simulator world."""

import asyncio

import pytest
import waldoctl
from nicegui import app, ui
from stl import Mode, mesh
from waldoctl.shapes import Box, ShapeWorld

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, fixture_stl, select
from tests.helpers.wait import reload_page, wait_until
from tests.test_scene_jog_handles import _open
from waldo_commander.common.theme import SceneColors
from waldo_commander.services import world_files
from waldo_commander.services.fixture_meshes import STORAGE_KEY
from waldo_commander.state import ui_state


def element(user, marker):
    return user.find(marker=marker).elements.pop()


async def upload(user, name, data):
    await element(user, "mesh-upload").handle_uploads(
        [ui.upload.SmallFileUpload(name=name, content_type="model/stl", _data=data)]
    )
    await asyncio.sleep(0)


@pytest.mark.integration
async def test_fixture_upload_group_edit_undo_and_workcell_roundtrip(
    user, tmp_path, monkeypatch
):
    library = tmp_path / "worlds"
    monkeypatch.setattr(world_files, "library_dir", lambda: library)
    source = fixture_stl(tmp_path / "Fixture.stl")
    data = source.read_bytes()
    legacy = Box(name="Old-box", x=0.1, y=0.1, z=0.1, pose=(0.4, 0, 0.05, 0, 0, 0))
    world_files.save_entry("legacy", ShapeWorld(program=(legacy,)))
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)

    user.find(marker="scene-import-mesh").click()
    await user.should_see(marker="mesh-import")
    assert ui_state.scene_panel.body in element(user, "mesh-import").ancestors()
    assert element(user, "mesh-units-mm")._props["aria-pressed"] == "true"
    await upload(user, "bad.stl", b"not an STL")
    await user.should_see("Cannot read this STL")
    assert not element(user, "mesh-add").enabled
    for invalid in (float("nan"), float("inf"), 0.0):
        model = mesh.Mesh.from_file(str(source))
        model.vectors[:, :, 0] = invalid
        bad = tmp_path / "bad.stl"
        model.save(str(bad), mode=Mode.BINARY, update_normals=False)
        await upload(user, bad.name, bad.read_bytes())
        await user.should_see("Cannot read this STL")
        assert not element(user, "mesh-add").enabled
    assert not handle.shapes and not view.fixture_meshes
    assert not (library / "assets").exists()

    ascii_path = tmp_path / "ascii.stl"
    mesh.Mesh.from_file(str(source)).save(str(ascii_path), mode=Mode.ASCII)
    await upload(user, ascii_path.name, ascii_path.read_bytes())
    await user.should_see("12 × 8 × 4 mm")
    user.find(marker="mesh-units-m").click()
    await user.should_see("at most 10 m per side")
    assert not element(user, "mesh-add").enabled
    assert not (library / "assets").exists()
    user.find(marker="mesh-import-close").click()

    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-import-mesh-here").click()
    await upload(user, source.name, data)
    await user.should_see("12 × 8 × 4 mm")
    user.find(marker="mesh-units-in").click()
    await user.should_see("304.8 × 203.2 × 101.6 mm")
    user.find(marker="mesh-units-cm").click()
    await user.should_see("120 × 80 × 40 mm")
    user.find(marker="mesh-add").click()
    await confirmed(view, lambda: len(view.fixture_meshes) == len(handle.shapes) == 1)
    fixture = view.fixture_meshes[0]
    proxy = handle.shapes[0]
    key, proxy_key = fixture.key, f"shape:{proxy.name}"
    assert (library / fixture.asset).read_bytes() == data
    assert proxy.kind == "box" and (proxy.x, proxy.y, proxy.z) == pytest.approx(
        (0.12, 0.08, 0.04)
    )
    assert proxy.pose == pytest.approx((0.4, 0.1, 0.02, 0, 0, 0))
    assert fixture.pose[:3] == pytest.approx((0.29, 0.09, 0.02))
    assert (await waldoctl.commander.client.shapes()).program == (proxy,)
    assert view._shape_objects[key].color == SceneColors.OBJECT_HEX
    assert app.storage.general[STORAGE_KEY] == [fixture.to_dict()]
    await user.should_see(marker="scene-mesh-Fixture")
    assert view._selected_objects == [key, proxy_key]
    assert len(view.object_history.undo_stack) == 1

    select(user, view)
    user.find(marker="scene-mesh-Fixture").click()
    assert view._selected_objects == [key, proxy_key]
    user.find(marker="scene-mesh-eye-Fixture").trigger("click.stop")
    assert all(not view._shape_objects[k].visible_ for k in (key, proxy_key))
    assert handle.shapes == [proxy]
    user.find(marker="scene-mesh-eye-Fixture").trigger("click.stop")
    user.find(marker="scene-mesh-lock-Fixture").trigger("click.stop")
    assert view.scene.interaction["shapeMove"] is None
    assert not element(user, "mesh-add-proxy").enabled
    user.find(marker="scene-mesh-lock-Fixture").trigger("click.stop")

    select(user, view, proxy_key, key)
    assert view._selected_objects == [key, proxy_key]
    move = view.scene.interaction["shapeMove"]
    assert set(move["members"]) == {view._shape_objects[k].id for k in (key, proxy_key)}
    x, y, z = move["center"]
    Drag(user, view.scene, "shape").begin(**move).release(**move, x=x + 0.02, y=y, z=z)
    await confirmed(view, lambda: handle.shapes[0].pose[0] == pytest.approx(0.42))
    assert view.fixture_meshes[0].pose[0] == pytest.approx(0.31)
    assert len(view.object_history.undo_stack) == 2
    user.find(marker="object-undo").click()
    await confirmed(
        view, lambda: handle.shapes == [proxy] and view.fixture_meshes == (fixture,)
    )

    user.find(marker=f"scene-row-{proxy.name}").click()
    assert view._selected_objects == [proxy_key]
    field(user, "object-dim-x", 160)
    await confirmed(view, lambda: handle.shapes[0].x == 0.16)
    assert view.fixture_meshes == (fixture,)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: handle.shapes == [proxy])
    user.find(marker="scene-mesh-Fixture").click()
    user.find(marker="mesh-add-proxy").click()
    await confirmed(view, lambda: len(handle.shapes) == 2)
    assert set(view.fixture_meshes[0].proxy_names) == {s.name for s in handle.shapes}
    user.find(marker="scene-mesh-Fixture").click()
    user.find(marker="object-delete").click()
    await confirmed(view, lambda: not handle.shapes and not view.fixture_meshes)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: len(handle.shapes) == 2 and bool(view.fixture_meshes))
    user.find(marker="object-undo").click()
    await confirmed(
        view, lambda: handle.shapes == [proxy] and view.fixture_meshes == (fixture,)
    )
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: not handle.shapes and not view.fixture_meshes)
    assert key not in view._shape_objects
    user.find(marker="object-redo").click()
    await confirmed(
        view, lambda: view.fixture_meshes == (fixture,) and handle.shapes == [proxy]
    )

    user.find(marker="scene-workcells").click()
    element(user, "workcell-name").set_value("bench")
    user.find(marker="workcell-save").click()
    await user.should_see("Saved bench")
    await user.should_see(marker="workcell-load:bench")
    world, meshes, _ = world_files.read_authoring_entry("bench")
    assert meshes == (fixture,) and world.program == (proxy,)
    assert world_files.load_entry("bench").program == (proxy,)
    assert "meshes =" in (library / "bench.py").read_text()
    assert "meshes =" not in (library / "legacy.py").read_text()
    user.find(marker="workcell-load:legacy").click()
    await confirmed(view, lambda: handle.shapes == [legacy] and not view.fixture_meshes)
    user.find(marker="workcell-load:bench").click()
    await confirmed(
        view, lambda: handle.shapes == [proxy] and view.fixture_meshes == (fixture,)
    )

    user.find(marker="tab-program").click()
    editor = ui_state.active_textarea
    editor.value = (
        "from parol6 import RobotClient\nwith RobotClient() as rbt:\n    pass\n"
    )
    waldoctl.commander.programs.active.dry_run.playback.active_cursor_line = 3
    user.find(marker="workcell-insert-saved:bench").click()
    await user.should_see("Inserted at line")
    assert "set_shapes(" in editor.value and "Box(" in editor.value
    assert fixture.asset not in editor.value and "meshes" not in editor.value

    await reload_page(user)
    assert await wait_until(lambda: ui_state.urdf_scene not in (None, view))
    view = ui_state.urdf_scene
    handle = waldoctl.commander.scene
    # Controller confirmation survives a reload; the new page still has to draw.
    await confirmed(
        view,
        lambda: view.fixture_meshes == (fixture,) and key in view._shape_objects,
    )
    assert view._shape_objects[key].visible_
    handle.shapes = []
    await confirmed(view, lambda: not handle.shapes)
    await user.should_see(marker="mesh-no-keepout")
    assert view._shape_objects[key].visible_
    user.find(marker="scene-mesh-Fixture").click()
    user.find(marker="mesh-add-proxy").click()
    await confirmed(view, lambda: len(handle.shapes) == 1)
    assert handle.shapes[0].pose == pytest.approx(proxy.pose)
    await user.should_not_see(marker="mesh-no-keepout")
