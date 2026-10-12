"""Fixture authoring through the upload UI and the real simulator world."""

import asyncio

import pytest
import waldoctl
from nicegui import app, run, ui
from stl import Mode, mesh
from waldoctl.shapes import Box, ShapeWorld

from tests.helpers.scene_events import Drag, right_click
from tests.helpers.scene_objects import confirmed, field, fixture_stl, select
from tests.helpers.wait import reload_page, wait_until
from tests.test_scene_jog_handles import _open
from waldo_commander.common.theme import SceneColors
from waldo_commander.services import world_files
from waldo_commander.services.fixture_meshes import MAX_STL_BYTES, STORAGE_KEY
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
    first_solid = ascii_path.read_bytes()
    second = mesh.Mesh.from_file(str(source))
    second.vectors[:, :, 0] += 100
    second.save(str(ascii_path), mode=Mode.ASCII)
    await upload(user, "multi.stl", first_solid + ascii_path.read_bytes())
    await user.should_see("112 × 8 × 4 mm")
    second.vectors[0, 0, 0] = float("nan")
    second.save(str(ascii_path), mode=Mode.ASCII, update_normals=False)
    await upload(user, "invalid-second.stl", first_solid + ascii_path.read_bytes())
    await user.should_see("non-finite coordinates")
    assert not element(user, "mesh-add").enabled
    await upload(user, "large.stl", b" " * (MAX_STL_BYTES + 1))
    await user.should_see("at most 10 MiB")
    assert not element(user, "mesh-add").enabled
    with monkeypatch.context() as limits:
        from waldo_commander.services import fixture_meshes

        limits.setattr(fixture_meshes, "MAX_STL_TRIANGLES", 3)
        await upload(user, "dense.stl", data)
        await user.should_see("at most 100,000 triangles")
        assert not element(user, "mesh-add").enabled
    await upload(user, ascii_path.name, first_solid)
    await user.should_see("12 × 8 × 4 mm")
    user.find(marker="mesh-units-m").click()
    await user.should_see("at most 10 m per side")
    assert not element(user, "mesh-add").enabled
    assert not (library / "assets").exists()
    importer = view._mesh_import
    user.find(marker="mesh-import-close").click()
    assert importer.data is None and importer.bounds is None
    assert view._mesh_import is None

    right_click(user, view.scene, [], [0.4, 0.1, 0])
    user.find(marker="scene-import-mesh-here").click()
    await upload(user, source.name, data)
    await user.should_see("12 × 8 × 4 mm")
    user.find(marker="mesh-units-in").click()
    await user.should_see("304.8 × 203.2 × 101.6 mm")
    user.find(marker="mesh-units-cm").click()
    await user.should_see("120 × 80 × 40 mm")
    importer = view._mesh_import
    user.find(marker="mesh-add").click()
    await confirmed(view, lambda: len(view.fixture_meshes) == len(handle.shapes) == 1)
    assert importer.data is None and view._mesh_import is None
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
    from fastmcp import Client
    from waldo_commander.mcp.server import get_mcp
    from waldo_commander.services.control_lease import BROWSER, control_lease

    async with Client(get_mcp()) as mcp:
        await mcp.call_tool("control.take_control")
        try:
            await mcp.call_tool("world.library_save", {"name": "bench"})
            assert world_files.read_authoring_entry("bench")[1] == (fixture,)
            await mcp.call_tool(
                "world.library_save",
                {"name": "bench", "shapes": [list(proxy.to_wire())]},
            )
            assert world_files.read_authoring_entry("bench")[1] == (fixture,)
        finally:
            control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")
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


@pytest.mark.integration
async def test_fixture_identity_proxy_deletion_and_partial_asset_recovery(
    user, tmp_path, monkeypatch
):
    from dataclasses import replace

    library = tmp_path / "worlds"
    monkeypatch.setattr(world_files, "library_dir", lambda: library)
    source = fixture_stl(tmp_path / "fixture.stl")
    data = source.read_bytes()
    view = await _open(user)
    handle = waldoctl.commander.scene
    await confirmed(view)
    for name in ("Fixture", "Other"):
        user.find(marker="scene-import-mesh").click()
        await upload(user, f"{name}.stl", data)
        await user.should_see("12 × 8 × 4 mm")
        user.find(marker="mesh-add").click()
        await confirmed(view, lambda: view._mesh_import is None)
    fixture, other = view.fixture_meshes
    proxy = fixture.proxy_names[0]
    await user.should_see(marker="object-isolate")

    right_click(user, view.scene, [], [0.5, 0.2, 0])
    user.find(marker="scene-add-box").click()
    await confirmed(view, lambda: len(handle.shapes) == 3)
    field(user, "object-name", "mesh:Fixture")
    await confirmed(view, lambda: any(s.name == "mesh:Fixture" for s in handle.shapes))
    move = view.scene.interaction["shapeMove"]
    assert move and move["node"] == view._shape_objects["shape:mesh:Fixture"].id
    Drag(user, view.scene, "shape").begin(**move).release(**move, x=0.6, y=0.2, z=0.05)
    await confirmed(
        view,
        lambda: next(s for s in handle.shapes if s.name == "mesh:Fixture").pose[0]
        == 0.6,
    )
    assert view.fixture_meshes == (fixture, other)

    select(user, view, fixture.key, extend=True)
    move = view.scene.interaction["shapeMove"]
    x, y, z = move["center"]
    Drag(user, view.scene, "shape").begin(**move).release(**move, x=x + 0.02, y=y, z=z)
    await confirmed(
        view,
        lambda: view.fixture_meshes[0].pose[0] == pytest.approx(fixture.pose[0] + 0.02),
    )
    assert next(s for s in handle.shapes if s.name == "mesh:Fixture").pose[
        0
    ] == pytest.approx(0.62)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: view.fixture_meshes == (fixture, other))

    select(user, view, f"shape:{proxy}")
    user.find(marker="object-delete").click()
    await confirmed(view, lambda: not view.fixture_meshes[0].proxy_names)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: view.fixture_meshes[0] == fixture)
    user.find(marker="object-redo").click()
    await confirmed(view, lambda: not view.fixture_meshes[0].proxy_names)
    select(user, view, "shape:mesh:Fixture")
    field(user, "object-name", proxy)
    await confirmed(view, lambda: any(s.name == proxy for s in handle.shapes))
    select(user, view, fixture.key)
    assert view._selected_objects == [fixture.key]
    user.find(marker="object-delete").click()
    await confirmed(view, lambda: len(view.fixture_meshes) == 1)
    assert any(s.name == proxy for s in handle.shapes)
    user.find(marker="object-undo").click()
    await confirmed(view, lambda: len(view.fixture_meshes) == 2)

    missing = view.fixture_meshes[0].to_dict()
    (library / fixture.asset).unlink()
    await reload_page(user)
    assert await wait_until(lambda: ui_state.urdf_scene not in (None, view))
    view = ui_state.urdf_scene
    await user.should_see(marker="fixture-recovery-feedback")
    await user.should_see("Unavailable fixtures")
    assert view.fixture_meshes == (other,)
    assert other.key in view._shape_objects
    user.find(marker="scene-import-mesh").click()
    await upload(user, "Third.stl", data)
    await user.should_see("12 × 8 × 4 mm")
    user.find(marker="mesh-add").click()
    await confirmed(view, lambda: len(view.fixture_meshes) == 2)
    assert missing in app.storage.general[STORAGE_KEY]
    (library / fixture.asset).write_bytes(data)
    await reload_page(user)
    assert await wait_until(lambda: ui_state.urdf_scene not in (None, view))
    view = ui_state.urdf_scene
    await confirmed(view, lambda: len(view.fixture_meshes) == 3)
    await user.should_not_see(marker="fixture-recovery-feedback")

    nested = []
    for folder, fixture in zip(("a", "b", "c"), view.fixture_meshes):
        asset = f"assets/{folder}/part.stl"
        path = library / asset
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        nested.append(replace(fixture, asset=asset))
    world_files.save_entry(
        "nested", ShapeWorld(program=tuple(handle.shapes)), meshes=tuple(nested)
    )
    user.find(marker="scene-workcells").click()
    await user.should_see(marker="workcell-load:nested")
    user.find(marker="workcell-load:nested").click()
    await confirmed(view, lambda: view.fixture_meshes == tuple(nested))
    urls = [view._shape_objects[m.key].args[0] for m in nested]
    assert len(set(urls)) == 3


@pytest.mark.integration
async def test_fixture_upload_invalidation_and_reload_during_confirmation(
    user, tmp_path, monkeypatch
):
    from nicegui.elements.upload_files import LargeFileUpload

    monkeypatch.setattr(world_files, "library_dir", lambda: tmp_path / "worlds")
    source = fixture_stl(tmp_path / "Fixture.stl")
    view = await _open(user)
    await confirmed(view)
    user.find(marker="scene-import-mesh").click()
    importer = view._mesh_import
    entered, release, read_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_read = LargeFileUpload.read

    async def held_read(file):
        data = await original_read(file)
        entered.set()
        await release.wait()
        read_finished.set()
        return data

    with monkeypatch.context() as delayed:
        delayed.setattr(LargeFileUpload, "read", held_read)
        await element(user, "mesh-upload").handle_uploads(
            [LargeFileUpload("Old.stl", "model/stl", source)]
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            user.find(marker="mesh-upload").trigger("removed")
            await upload(user, "Current.stl", source.read_bytes())
            await user.should_see("12 × 8 × 4 mm")
            release.set()
            await asyncio.wait_for(read_finished.wait(), 5)
            assert importer.filename == "Current.stl"
        finally:
            release.set()
            await asyncio.wait_for(read_finished.wait(), 5)

    from waldo_commander.components import mesh_import

    entered, release, parse_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_io = run.io_bound

    async def held_parse(function, *args, **kwargs):
        result = await actual_io(function, *args, **kwargs)
        if function is mesh_import.stl_bounds:
            entered.set()
            await release.wait()
            parse_finished.set()
        return result

    with monkeypatch.context() as delayed:
        delayed.setattr(run, "io_bound", held_parse)
        await upload(user, "Removed.stl", importer.data)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            user.find(marker="mesh-upload").trigger("removed")
            release.set()
            await asyncio.wait_for(parse_finished.wait(), 5)
            assert importer.data is None and not element(user, "mesh-add").enabled
        finally:
            release.set()
            if entered.is_set():
                await asyncio.wait_for(parse_finished.wait(), 5)
    await upload(
        user, "Current.stl", fixture_stl(tmp_path / "Current.stl").read_bytes()
    )
    await user.should_see("12 × 8 × 4 mm")

    client = waldoctl.commander.client
    actual_readback = client.shapes
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_readback():
        world = await actual_readback()
        entered.set()
        await release.wait()
        return world

    with monkeypatch.context() as delayed:
        delayed.setattr(client, "shapes", held_readback)
        try:
            user.find(marker="mesh-add").click()
            await asyncio.wait_for(entered.wait(), 5)
            reloading = asyncio.create_task(reload_page(user))
            assert await wait_until(
                lambda: ui_state.urdf_scene not in (None, view)
                and ui_state.urdf_scene.scene is not None
            )
            replacement = ui_state.urdf_scene
            release.set()
            await reloading
            await confirmed(replacement, lambda: len(replacement.fixture_meshes) == 1)
            fixture = replacement.fixture_meshes[0]
            assert fixture.name == "Current"
            assert fixture.key in replacement._shape_objects
            assert app.storage.general[STORAGE_KEY] == [fixture.to_dict()]
        finally:
            release.set()
