"""Collision visualization: red-tint of colliding parts + keep-out shape render.

The ``user`` fixture has no WebGL, but the scene's Python ``Object3D`` colors are
the exact input three.js renders from — asserting them verifies the highlight
logic (name mapping, recolor, restore) deterministically. A browser-level render
check lives in ``test_collision_viz_screen.py``.
"""

import asyncio

import pytest
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from tests.helpers.wait import wait_for_urdf_ready
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode


async def _until(cond, message: str) -> None:
    try:
        async with asyncio.timeout(10):
            while not cond():
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise AssertionError(message) from error


@pytest.mark.integration
async def test_scene_tints_repaints_and_redraws_links_tools_and_shapes(
    user: User,
) -> None:
    """Reported collisions tint links, shapes and the tool and then restore;
    shapes draw per layer, re-render as a diff and keep their layer or draft
    colour through repaints; ``commander.scene.shapes`` feeds the local
    checker for EDITING and the preview, and is redrawn after a page reload."""
    from dataclasses import replace
    from unittest.mock import patch

    import numpy as np
    import waldoctl
    from waldoctl import Box, Cylinder, Physical

    from waldo_commander.common.theme import SceneColors
    from waldo_commander.services.path_visualizer import _mark_colliding_commands
    from waldo_commander.state import simulation_state, ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    assert scene is not None
    coll = waldoctl.commander.status.collision

    # Controller reports pairs in the display vocabulary: plain URDF link names.
    links = [name for name, meshes in scene._link_to_meshes.items() if meshes]
    assert len(links) >= 2, "need two link meshes to simulate a self-collision"
    a, b = links[0], links[1]
    obj_a, obj_b = scene._link_to_meshes[a][0], scene._link_to_meshes[b][0]
    before_a, before_b = obj_a.color, obj_b.color
    assert before_a != SceneColors.COLLISION_HEX
    coll.active = True
    coll.pairs = [(a, b)]
    scene.update_from_robot_state()
    assert obj_a.color == SceneColors.COLLISION_HEX
    assert obj_b.color == SceneColors.COLLISION_HEX
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert obj_a.color == before_a
    assert obj_b.color == before_b

    # The floor a robot stands on is an ordinary installation fixture: it
    # tints like any keep-out, survives a repaint, and displaces the
    # placeholder disc simply by existing.
    assert scene._floor is not None and scene._floor.visible_
    floor_shape = Box(
        name="floor",
        x=6.0,
        y=6.0,
        z=0.2,
        pose=(0.0, 0.0, -0.1, 0, 0, 0),
        physics=Physical(),
    )
    scene.render_shapes(
        [Box(name="wall", x=0.1, y=2.0, z=1.0, pose=(0.8, 0, 0.5, 0, 0, 0))],
        installation=[floor_shape],
    )
    floor = scene._shape_objects["install:floor"]
    assert floor.color == SceneColors.SHAPE_INSTALL_HEX
    assert not scene._floor.visible_, (
        "a described installation displaces the placeholder disc"
    )
    assert scene._shape_objects["shape:wall"].args[:3] == [0.1, 2.0, 1.0]
    coll.pairs = [(a, "install:floor")]
    coll.active = True
    scene.update_from_robot_state()
    assert floor.color == SceneColors.COLLISION_HEX
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert floor.color == SceneColors.SHAPE_INSTALL_HEX
    scene.set_appearance_mode(RobotAppearanceMode.EDITING)
    assert floor.color == SceneColors.SHAPE_INSTALL_HEX
    scene.set_appearance_mode(RobotAppearanceMode.LIVE)
    # A backend describing no installation gets the placeholder back.
    scene.render_shapes([])
    assert "install:floor" not in scene._shape_objects
    assert scene._floor.visible_

    # Each layer renders under its own namespace and colour.
    wall = Box(name="wall", x=0.1, y=0.1, z=0.1, pose=(0.3, 0.0, 0.3, 0, 0, 0))
    post = Cylinder(name="post", radius=0.05, length=0.5)
    bench = Box(name="bench", x=0.4, y=0.4, z=0.05)
    scene.render_shapes([wall, post], installation=[bench])
    bench_obj = scene._shape_objects["install:bench"]
    assert bench_obj.color == SceneColors.SHAPE_INSTALL_HEX
    wall_obj = scene._shape_objects["shape:wall"]
    assert wall_obj.color == SceneColors.SHAPE_HEX
    # Render wiring applies the Z-up axis correction (three.js is Y-up).
    assert scene._shape_objects["shape:post"].R == [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0],
        [0.0, 1.0, 0.0],
    ]
    coll.pairs = [(a, "shape:wall"), (a, "install:bench")]
    coll.active = True
    scene.update_from_robot_state()
    assert wall_obj.color == SceneColors.COLLISION_HEX
    assert bench_obj.color == SceneColors.COLLISION_HEX
    assert scene._link_to_meshes[a][0].color == SceneColors.COLLISION_HEX
    # Mode toggle mid-collision: the arm/tool repaint loops don't touch shape
    # objects, so set_appearance_mode must repaint them itself — otherwise the
    # next tick re-snapshots red as the shape's base and it sticks red forever.
    scene.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
    assert wall_obj.color == SceneColors.SHAPE_HEX
    assert bench_obj.color == SceneColors.SHAPE_INSTALL_HEX
    scene.update_from_robot_state()  # still colliding — re-tints from clean base
    assert wall_obj.color == SceneColors.COLLISION_HEX
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert wall_obj.color == SceneColors.SHAPE_HEX
    assert bench_obj.color == SceneColors.SHAPE_INSTALL_HEX

    # Re-rendering reconciles against what is drawn: a no-op sends nothing, a
    # pose-only change moves the same object, a geometry change recreates it
    # and a dropped shape is deleted — the group persists throughout.
    group = scene._shapes_group
    with patch.object(scene.scene.client, "run_javascript") as sent:
        scene.render_shapes([wall, post], installation=[bench])
    assert sent.call_count == 0, "an unchanged world must not be re-sent"
    assert scene._shape_objects["shape:wall"] is wall_obj
    assert scene._shapes_group is group
    moved = replace(wall, pose=(0.5, 0.0, 0.3, 0, 0, 0))
    scene.render_shapes([moved, post], installation=[bench])
    assert scene._shape_objects["shape:wall"] is wall_obj, "pose-only: same object"
    assert wall_obj.x == pytest.approx(0.5)
    bigger = Box(name="wall", x=0.2, y=0.1, z=0.1, pose=moved.pose)
    scene.render_shapes([bigger], installation=[bench])
    assert scene._shape_objects["shape:wall"] is not wall_obj, "geometry: recreated"
    assert wall_obj.id not in scene.scene.objects
    assert "shape:post" not in scene._shape_objects
    assert scene._shapes_group is group

    # An UNCONFIRMED program layer stays draft-amber through appearance
    # repaints; promoting it would show an un-enforced keep-out as enforced.
    pending = Box(name="pending", x=0.1, y=0.1, z=0.1, pose=(0.4, 0.0, 0.3, 0, 0, 0))
    scene.render_shapes([pending], draft=True)
    obj = scene._shape_objects["shape:pending"]
    assert obj.color == SceneColors.SHAPE_DRAFT_HEX
    scene.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
    assert obj.color == SceneColors.SHAPE_DRAFT_HEX, (
        "repaint promoted an unconfirmed keep-out to the confirmed color"
    )
    scene.render_shapes([pending], draft=False)
    obj = scene._shape_objects["shape:pending"]
    assert obj.color == SceneColors.SHAPE_HEX
    scene.set_appearance_mode(RobotAppearanceMode.LIVE)
    assert obj.color == SceneColors.SHAPE_HEX

    # commander.scene.shapes feeds this process's checker: the EDITING pose
    # tints colliding geometry and the preview marks colliding segments with
    # no controller round-trip.
    handle = waldoctl.commander.scene
    assert handle is not None
    robot = ui_state.active_robot
    assert robot.has_collision_checking
    # A base-encasing box collides at q=0 — deterministic at any test pose.
    block = Box(name="block", x=0.6, y=0.6, z=0.6, pose=(0.0, 0.0, 0.1, 0, 0, 0))
    try:
        handle.shapes = [block]
        await _until(
            lambda: handle.confirmed and not handle._pushes_inflight,
            "the block was never confirmed",
        )
        assert scene._shape_objects["shape:block"].color == SceneColors.SHAPE_HEX

        pairs = robot.colliding_pairs(np.zeros(6))
        assert pairs, "local checker must see the base-encasing shape"
        tinted = {n for p in pairs for n in p if not n.startswith("shape:")}
        link = next(
            name
            for name, meshes in scene._link_to_meshes.items()
            if meshes and name in tinted
        )
        scene.set_appearance_mode(RobotAppearanceMode.EDITING)
        scene.set_editing_angles([0.0] * 6)
        assert scene._shape_objects["shape:block"].color == SceneColors.COLLISION_HEX
        assert scene._link_to_meshes[link][0].color == SceneColors.COLLISION_HEX

        # A joint-ring drag must also refresh the highlight — the status loop
        # is skipped in EDITING.
        ring = scene.joint_groups[scene.joint_names[0]].with_name("edit_joint_group:0")
        UserInteraction(user, {scene.scene}, None).trigger(
            "transform",
            {
                "type": "transform",
                "mode": "rotate",
                "object_id": ring.id,
                "object_name": "edit_joint_group:0",
                **dict.fromkeys(("x", "y", "z", "wx", "wy", "wz"), 0.0),
                **dict.fromkeys(("rx", "ry", "rz"), 0.3),
            },
        )
        assert scene._editing_angles[0] == pytest.approx(0.3)
        assert scene._editing_collision_q == tuple(scene._editing_angles)

        # A command whose rows pass through the box is reported with its first
        # colliding row; a command that owns no rows is never checked. The
        # passed world is applied explicitly (a reused pool worker's checker
        # must never inherit a previous run's shapes).
        q = np.array([[0.0] * 6, [0.1] * 6], dtype=np.float32)
        record = waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=q,
            tcp=np.zeros((2, 6), dtype=np.float32),
            tool_closed=np.zeros(2, dtype=np.float32),
            tool_gripping=np.zeros(2, dtype=np.bool_),
            blocks=(
                waldoctl.TickBlock(command=0, start_row=0, rows=2, line_number=1),
                waldoctl.TickBlock(command=1, start_row=2, rows=0, line_number=2),
            ),
        )
        hits = _mark_colliding_commands(
            robot, record, [], [], [tuple(block.to_wire())], None
        )
        assert hits == {0: 0}
        scene.set_appearance_mode(RobotAppearanceMode.LIVE)

        # Shapes persist on commander.scene across page loads; the rebuilt
        # scene must redraw them or the barrier turns invisible while still
        # enforced. Close the old page first so its heartbeat cannot schedule
        # a competing reload of the same user.
        previous_page = user.client
        assert previous_page is not None
        for handler in previous_page.disconnect_handlers:
            previous_page.safe_invoke(handler)
        previous_page.delete()
        # The closed page's scene stops listening, so it is not kept alive
        # and redrawn on every program change for as long as the app runs.
        assert not any(
            getattr(listener, "__self__", None) is scene
            for listener in simulation_state._change_listeners
        ), "the closed page's scene still listens for simulation changes"
        await user.open("/")
        await wait_for_urdf_ready()
        rebuilt = ui_state.urdf_scene
        assert rebuilt is not None and rebuilt is not scene
        scene = rebuilt
        await _until(
            lambda: "shape:block" in scene._shape_objects,
            "the rebuilt scene did not redraw the keep-out",
        )
        assert scene._shape_objects["shape:block"].color == SceneColors.SHAPE_HEX

        # Clearing shapes re-runs the EDITING highlight: links restore to the
        # mode base and the shape objects are gone.
        scene.set_appearance_mode(RobotAppearanceMode.EDITING)
        scene.set_editing_angles([0.0] * 6)
        assert scene._link_to_meshes[link][0].color == SceneColors.COLLISION_HEX
        handle.shapes = []
        assert scene._link_to_meshes[link][0].color == scene.config.edit_color
        assert "shape:block" not in scene._shape_objects
    finally:
        # The checker is process-global — never leak shapes into other tests.
        handle.shapes = []
        current = ui_state.urdf_scene
        if current is not None:
            current.set_appearance_mode(RobotAppearanceMode.LIVE)
        await _until(
            lambda: handle.confirmed and not handle._pushes_inflight,
            "the clear was never confirmed",
        )

    # Gripper engage/disengage repaints tool meshes — an active red tint must
    # re-apply from the new base instead of being silently cleared.
    scene.apply_tool_everywhere("SSG-48")
    assert scene._tool_meshes, "tool meshes must be mapped"
    mesh = scene._tool_meshes[0]
    # tool: names tint the attached tool; link partner arrives as a plain name.
    coll.pairs = [("tool:SSG-48:body", "L5")]
    coll.active = True
    scene.update_from_robot_state()
    assert mesh.color == SceneColors.COLLISION_HEX
    scene._apply_tool_engaged_color(True)
    scene.update_from_robot_state()  # still colliding — re-tints from new base
    assert mesh.color == SceneColors.COLLISION_HEX
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert mesh.color != SceneColors.COLLISION_HEX  # restored, not stuck red


@pytest.mark.integration
async def test_playback_moves_world_objects_and_restores_their_declared_pose(
    user: User,
) -> None:
    """A tracked object follows the preview's pose during playback as a
    pose-only move of the drawn shape, a guessed track is drawn as a ghost,
    scrubbing the dry run moves it along its track, and clearing the override
    or dropping the timeline puts it back where the program says."""
    import numpy as np
    import waldoctl
    from waldoctl import Box, Cylinder

    from waldo_commander.components.playback import playback
    from waldo_commander.services.preview_segments import segments_from_record
    from waldo_commander.services.timeline import ObjectSample
    from waldo_commander.services.urdf_scene.urdf_scene import (
        _Y_TO_Z_UP,
        SHAPE_OPACITY,
    )
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    assert scene is not None
    scene.render_shapes(
        [
            Box(name="block", x=0.04, y=0.04, z=0.06, pose=(0.3, 0.0, 0.04, 0, 0, 0)),
            Cylinder(
                name="can", radius=0.03, length=0.1, pose=(0.4, 0.0, 0.05, 0, 0, 0)
            ),
        ]
    )
    block = scene._shape_objects["shape:block"]
    can = scene._shape_objects["shape:can"]
    assert (block.x, block.z) == (0.3, 0.04)

    scene.set_object_poses(
        {
            "block": ObjectSample((0.5, 0.1, 0.2, 1.0, 0.0, 0.0, 0.0), physics=False),
            "can": ObjectSample((0.4, 0.0, 0.3, 1.0, 0.0, 0.0, 0.0), physics=True),
        }
    )
    assert (block.x, block.y, block.z) == (0.5, 0.1, 0.2)
    assert block.opacity == pytest.approx(SHAPE_OPACITY * 0.5), (
        "a guessed track is a ghost"
    )
    assert can.z == 0.3 and can.opacity == pytest.approx(SHAPE_OPACITY)
    assert can.R == _Y_TO_Z_UP.tolist(), (
        "a cylinder keeps its Y-up correction while carried"
    )

    # A body leaving the record must not strand its previous override.
    scene.set_object_poses(
        {"block": ObjectSample((0.5, 0.1, 0.2, 1.0, 0.0, 0.0, 0.0), physics=False)}
    )
    assert can.z == 0.05
    assert block.z == 0.2 and block.opacity == pytest.approx(SHAPE_OPACITY * 0.5)

    scene.set_object_poses(None)
    assert (block.x, block.y, block.z) == (0.3, 0.0, 0.04)
    assert block.opacity == pytest.approx(SHAPE_OPACITY)
    assert can.z == 0.05

    # A re-render that moves an object to a new declared pose ends the
    # override, so the ghost look must end with it rather than stranding
    # the object at half opacity nothing will restore.
    scene.set_object_poses(
        {"block": ObjectSample((0.5, 0.1, 0.2, 1.0, 0.0, 0.0, 0.0), physics=False)}
    )
    assert block.opacity == pytest.approx(SHAPE_OPACITY * 0.5)
    scene.render_shapes(
        [
            Box(name="block", x=0.04, y=0.04, z=0.06, pose=(0.2, 0.0, 0.04, 0, 0, 0)),
            Cylinder(
                name="can", radius=0.03, length=0.1, pose=(0.4, 0.0, 0.05, 0, 0, 0)
            ),
        ]
    )
    assert (block.x, block.z) == (0.2, 0.04)
    assert block.opacity == pytest.approx(SHAPE_OPACITY)

    # Playback time drives the same objects through the predicted record.
    scene.render_shapes(
        [Box(name="block", x=0.04, y=0.04, z=0.06, pose=(0.3, 0.0, 0.04, 0, 0, 0))]
    )
    block = scene._shape_objects["shape:block"]
    # Two seconds of lift at the record's rate; the predicted record is the
    # one that knows where the carried block went.
    rows = 101
    tcp = np.zeros((rows, 6), dtype=np.float32)
    tcp[:, 0] = 0.3
    tcp[:, 2] = np.linspace(0.3, 0.5, rows)
    poses = np.zeros((rows, 7), dtype=np.float32)
    poses[:, 0] = 0.3
    poses[:, 2] = np.linspace(0.04, 0.24, rows)
    poses[:, 3] = 1.0

    def record(digest: bytes, objects=()) -> waldoctl.TickIndex:
        return waldoctl.TickIndex(
            row_dt_s=0.02,
            joints_rad=np.zeros((rows, 6), dtype=np.float32),
            tcp=tcp,
            tool_closed=np.zeros(rows, dtype=np.float32),
            tool_gripping=np.zeros(rows, dtype=np.bool_),
            blocks=(
                waldoctl.TickBlock(
                    command=0,
                    start_row=0,
                    rows=rows,
                    line_number=1,
                    move_type="cartesian",
                ),
            ),
            objects=objects,
            digest=digest,
        )

    active = waldoctl.commander.programs.active
    assert active is not None
    commanded = record(b"commanded")
    active.dry_run.commanded = commanded
    active.dry_run.commanded_revision = 1
    active.dry_run.predicted = record(
        b"predicted", (waldoctl.ObjectTicks(name="block", poses=poses),)
    )
    active.dry_run.predicted_revision = 1
    active.dry_run.path_segments = segments_from_record(commanded, [])
    active.dry_run.total_steps = 1
    try:
        playback.invalidate_timeline()
        assert playback._ensure_timeline() is not None
        playback._apply_time(1.0)
        assert block.z == pytest.approx(0.14), "half way through the lift"
        playback._apply_time(2.0)
        assert block.z == pytest.approx(0.24)

        playback.invalidate_timeline()
        assert block.z == 0.04, "declared pose restored once the timeline is dropped"
    finally:
        active.dry_run.commanded = None
        active.dry_run.predicted = None
        active.dry_run.path_segments = []
        active.dry_run.total_steps = 0
        playback.invalidate_timeline()


@pytest.mark.integration
async def test_installation_proposal_is_drawn_exported_and_cleared_by_readback(
    user: User, tmp_path, monkeypatch
) -> None:
    """A keep-out proposed for the installation layer leaves the enforced
    program layer, is drawn in its own colour, exports as the robot config's
    TOML, and clears itself once readback shows the backend enforcing it."""
    import waldoctl
    from waldoctl import Box, ShapeWorld

    from waldo_commander import constants
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.services.urdf_scene.urdf_scene import SHAPE_OPACITY
    from waldo_commander.state import ui_state

    monkeypatch.setattr(constants, "default_program_dir", lambda: tmp_path)
    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    handle = waldoctl.commander.scene
    assert scene is not None and handle is not None

    wall = Box(name="wall", x=0.1, y=0.1, z=0.3, pose=(0.3, 0.0, 0.15, 0, 0, 0))
    handle.shapes = [wall]
    handle.propose_installation(["wall"])
    assert handle.shapes == [] and handle.installation_draft == (wall,)
    assert "shape:wall" not in scene._shape_objects

    def ghost_color(sc):
        return sc._shape_objects["draft:wall"].color

    proposal = scene._shape_objects["draft:wall"]
    assert proposal.color == SceneColors.SHAPE_PROPOSED_HEX
    assert proposal.opacity == pytest.approx(SHAPE_OPACITY)
    with pytest.raises(ValueError, match="proposed for the installation layer"):
        handle.shapes = [wall]
    with pytest.raises(ValueError, match="no program-layer shape"):
        handle.propose_installation(["nothing"])

    with scene.scene.client:
        scene._show_installation_toml_dialog()
    await user.should_see(marker="installation-toml-dialog")
    user.find(marker="installation-toml-save").click()
    await user.should_see("Saved to")
    saved = (tmp_path / "installation_shapes.toml").read_text()
    assert "[[installation_shapes]]" in saved and 'name = "wall"' in saved

    # A proposal is enforced by this process even though no backend enforces
    # it yet — that is what makes it possible to design against.
    encasing = Box(name="cage", x=0.6, y=0.6, z=0.6, pose=(0.0, 0.0, 0.1, 0, 0, 0))
    handle.shapes = [*handle.shapes, encasing]
    handle.propose_installation(["cage"])
    q = waldoctl.commander.status.joints.angles.rad
    assert ui_state.active_robot.in_collision(q), (
        "the proposal must reach this process's collision world"
    )
    handle.discard_installation_draft(["cage"])
    assert not ui_state.active_robot.in_collision(q)

    # An appearance-mode repaint must not turn the proposal into a keep-out.
    scene.set_appearance_mode(RobotAppearanceMode.EDITING)
    assert ghost_color(scene) == SceneColors.SHAPE_PROPOSED_HEX
    scene.set_appearance_mode(RobotAppearanceMode.LIVE)
    assert ghost_color(scene) == SceneColors.SHAPE_PROPOSED_HEX

    # The backend's next boot enforces it: readback adopts the proposal. A
    # refresh is skipped while the proposal's own push awaits its ack, so
    # wait for that readback to land first.
    for _ in range(200):
        if handle.confirmed:
            break
        await asyncio.sleep(0.05)
    assert handle.confirmed, "the program-layer push never confirmed"

    # The config is authored by hand from the exported TOML, so what comes
    # back is the same shape by name, not field for field.
    enforced_wall = Box(
        name="wall", x=0.1, y=0.1, z=0.3, pose=(0.3, 0.0, 0.15, 0, 0, 0), margin=0.01
    )

    async def _enforced() -> ShapeWorld:
        return ShapeWorld(installation=(enforced_wall,), program=())

    monkeypatch.setattr(waldoctl.commander.client, "shapes", _enforced)
    await handle.refresh_from_backend()
    assert handle.installation_draft == ()
    assert "draft:wall" not in scene._shape_objects
    assert scene._shape_objects["install:wall"].color == SceneColors.SHAPE_INSTALL_HEX

    # A withdrawn proposal is a program keep-out again.
    post = Box(name="post", x=0.05, y=0.05, z=0.2, pose=(0.4, 0.0, 0.1, 0, 0, 0))
    handle.shapes = [post]
    handle.propose_installation(["post"])
    scene._withdraw_proposal("post")
    assert handle.installation_draft == () and [s.name for s in handle.shapes] == [
        "post"
    ]

    # A PROGRAM shape arriving under a drafted name is the same collision:
    # readback is truth and is adopted wholesale, so the draft has to lose
    # the name or both layers hold it and `_assign`'s clash check refuses
    # every later edit, including from handlers that do not catch it.
    handle.shapes = [post]
    handle.propose_installation(["post"])
    assert [s.name for s in handle.installation_draft] == ["post"]
    # A readback is skipped while a push awaits its ack (it would query the
    # pre-edit world), so let the proposal's own push land first.
    for _ in range(200):
        if not handle._pushes_inflight:
            break
        await asyncio.sleep(0.05)
    assert not handle._pushes_inflight, "the proposal's push never acked"

    async def _program_post() -> ShapeWorld:
        return ShapeWorld(installation=(), program=(post,))

    monkeypatch.setattr(waldoctl.commander.client, "shapes", _program_post)
    await handle.refresh_from_backend()
    assert handle.installation_draft == ()
    enforced = [s.name for s in handle.enforced_locally]
    assert enforced.count("post") == 1, f"one layer only, got {enforced}"
    handle.shapes = [post]  # an edit still goes through rather than clashing

    # The monkeypatched readback put `wall` into this process's installation
    # checker; the real backend's readback has to take it back out, or a
    # phantom keep-out sits in the middle of every later test's workspace.
    monkeypatch.undo()
    handle.shapes = []
    for _ in range(200):
        if handle.confirmed:
            break
        await asyncio.sleep(0.05)
    assert handle.confirmed and handle.installation == ()


def _one_row_record(commands: int):
    """A record with one row per command, for replaying boundaries over."""
    import numpy as np
    import waldoctl

    return waldoctl.TickIndex(
        row_dt_s=0.02,
        joints_rad=np.zeros((commands, 6), dtype=np.float32),
        tcp=np.zeros((commands, 6), dtype=np.float32),
        tool_closed=np.zeros(commands, dtype=np.float32),
        tool_gripping=np.zeros(commands, dtype=np.bool_),
        blocks=tuple(
            waldoctl.TickBlock(command=i, start_row=i, rows=1, line_number=i + 1)
            for i in range(commands)
        ),
    )


def test_preview_marking_replays_tool_and_shape_boundaries() -> None:
    """Commands after a mid-script select_tool or set_shapes are checked with
    THAT tool or world, and the checker's submit-time tool and world are
    restored afterwards (the fallback path shares the live checker)."""
    from waldoctl import Box, ShapeChange, ToolSelection

    from waldo_commander.services.path_visualizer import _mark_colliding_commands

    class _FakeRobot:
        has_collision_checking = True

        def __init__(self):
            self.tool = "NONE"
            self.world: tuple = ()

        def apply_shapes(self, shapes):
            self.world = tuple(s.name for s in shapes)

        def set_active_tool(self, key, tcp_offset_m=None, variant_key=None):
            self.tool = key

        def check_trajectory(self, q):
            return 0 if self.tool == "SSG-48" or "bar" in self.world else -1

    # Selection recorded on command 0 -> applies to commands 1 and 2.
    sels = [ToolSelection(tool_key="SSG-48", variant_key="", command=0)]
    robot = _FakeRobot()
    assert _mark_colliding_commands(
        robot, _one_row_record(3), sels, [], None, ("NONE", "")
    ) == {
        1: 0,
        2: 0,
    }
    assert robot.tool == "NONE"  # restored to the initial tool

    # Back-to-back selections (same command) must replay chronologically
    # — the LAST recorded tool wins, not the alphabetically-last.
    sels = [
        ToolSelection(tool_key="SSG-48", variant_key="", command=0),
        ToolSelection(tool_key="VACUUM", variant_key="", command=0),
    ]
    assert (
        _mark_colliding_commands(
            _FakeRobot(), _one_row_record(2), sels, [], None, ("NONE", "")
        )
        == {}
    ), "checked with VACUUM, not SSG-48"

    changes = [ShapeChange(shapes=(Box(name="bar", x=0.1, y=0.1, z=0.1),), command=0)]
    robot = _FakeRobot()
    hits = _mark_colliding_commands(
        robot, _one_row_record(3), [], changes, None, ("NONE", "")
    )
    assert hits == {1: 0, 2: 0}, "the world was empty for command 0"
    assert robot.world == ()  # restored to the submit-time world


def test_shape_render_pose_matches_enforced_geometry() -> None:
    """Cylinders stand along coal's Z axis — the drawn shape must match the
    blocked volume."""
    import numpy as np
    from waldoctl import Box, Cylinder

    from waldo_commander.services.urdf_scene.urdf_scene import _shape_render_pose

    # Identity pose: the render rotation is the Y->Z-up correction, not identity.
    pos, rot = _shape_render_pose(Cylinder(name="post", radius=0.05, length=0.5))
    assert pos == (0.0, 0.0, 0.0)
    assert np.allclose(rot, [[1, 0, 0], [0, 0, -1], [0, 1, 0]])

    # A box needs no correction: coal and three.js agree on its axes.
    pos, rot = _shape_render_pose(
        Box(name="crate", pose=(0.2, 0.0, 0.1, 0.0, 0.0, 0.0), x=0.1, y=0.1, z=0.2)
    )
    assert np.allclose(pos, (0.2, 0.0, 0.1))
    assert np.allclose(rot, np.eye(3))


def test_preview_script_set_shapes_real_dispatch_no_stale_world() -> None:
    """Two dry runs through the REAL preview runner in one process (= a reused
    pool worker). Run 1's script calls ``set_shapes`` — the pre-fix dispatch
    crashed with ``TypeError: object of type 'method' has no len()``. Run 2
    must not see run 1's world — the pre-fix worker leaked it into the next
    run's planning guard as phantom collisions."""
    import numpy as np
    import parol6
    import parol6.client
    import waldoctl

    from waldo_commander.services.path_visualizer import _run_simulation_isolated

    # The runner monkeypatches these for the (normally sub-) process; running
    # it in-process for determinism means restoring them ourselves. Some may
    # not pre-exist on the submodule (the runner setattrs them regardless).
    _missing = object()
    snapshot = [
        (mod, name, getattr(mod, name, _missing))
        for mod in (parol6, parol6.client)
        for name in ("RobotClient", "AsyncRobotClient")
    ]
    home = [0.0, -90.0, 180.0, 0.0, 0.0, 180.0]
    prog_with_shapes = (
        "from parol6 import RobotClient\n"
        "from waldoctl import Box\n"
        "rbt = RobotClient()\n"
        f"rbt.move_j({[a + 10.0 if i == 0 else a for i, a in enumerate(home)]}, speed=0.5)\n"
        "rbt.set_shapes([Box(name='cage', x=2.0, y=2.0, z=2.0)])\n"
    )
    prog_plain = (
        "from parol6 import RobotClient\n"
        "rbt = RobotClient()\n"
        f"rbt.move_j({[a + 10.0 if i == 0 else a for i, a in enumerate(home)]}, speed=0.5)\n"
    )
    kwargs = dict(
        initial_joints_rad=np.radians(home),
        backend_package="parol6",
        shapes_wire=[],
        initial_tool=("NONE", ""),
    )
    try:
        res1 = _run_simulation_isolated(prog_with_shapes, **kwargs)
        assert res1["error"] is None, res1["error"]  # C1: no TypeError crash
        assert res1["commanded"].rows > 0, "the move before set_shapes must plan"
        assert not res1["collisions"]

        res2 = _run_simulation_isolated(prog_plain, **kwargs)
        assert res2["error"] is None, res2["error"]  # C3: no phantom guard hit
        assert not res2["collisions"]
    finally:
        for mod, name, val in snapshot:
            if val is _missing:
                try:
                    delattr(mod, name)
                except AttributeError:
                    pass
            else:
                setattr(mod, name, val)
        # The backend's checker is shared in-process — leave it clean.
        waldoctl.commander.robot.apply_shapes([])


@pytest.mark.integration
async def test_shape_pushes_confirm_by_readback_and_never_restore_an_older_world(
    user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """The program layer travels through the REAL client to the REAL
    (fake-serial) controller: a world a program sets reaches the display via
    the epoch readback, an edit is a draft until acked and read back, a lost
    ack leaves it a draft, and no delayed readback or push can bring back a
    world a newer edit replaced."""
    import waldoctl
    from waldoctl import Box

    from waldo_commander.common.theme import SceneColors
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    handle = waldoctl.commander.scene
    client = waldoctl.commander.client
    assert scene is not None and handle is not None
    real_set_shapes = client.set_shapes
    real_shapes = client.shapes
    held_readback, release_readback = asyncio.Event(), asyncio.Event()
    held_clear, release_clear = asyncio.Event(), asyncio.Event()
    held_edit, release_edit = asyncio.Event(), asyncio.Event()

    def _box(name: str) -> Box:
        return Box(name=name, x=0.1, y=0.1, z=0.1, pose=(0.9, 0.9, 0.9, 0, 0, 0))

    try:
        # A world WC did NOT initiate (straight to the controller; scene_handle
        # never sees the push): epoch bump → status broadcast → readback → render.
        assert await client.set_shapes([_box("prog")]) == 1
        await _until(
            lambda: [s.name for s in handle.shapes] == ["prog"] and handle.confirmed,
            "epoch-driven readback never adopted the program's world",
        )
        assert "shape:prog" in scene._shape_objects
        assert await client.set_shapes([]) == 1
        await _until(lambda: handle.shapes == [], "clear never reached display")
        assert "shape:prog" not in scene._shape_objects

        # An edit renders as a draft and flips to confirmed only once the
        # readback returns the applied world.
        handle.shapes = [_box("rb")]
        assert handle.confirmed is False
        assert scene._shape_objects["shape:rb"].color == SceneColors.SHAPE_DRAFT_HEX
        await _until(lambda: handle.confirmed, "edit never confirmed")
        assert [s.name for s in handle.shapes] == ["rb"]
        assert scene._shape_objects["shape:rb"].color == SceneColors.SHAPE_HEX
        handle.shapes = []
        await _until(lambda: handle.confirmed, "clear never confirmed")
        assert "shape:rb" not in scene._shape_objects

        # The real client answers 0 when no ack arrives (it does not raise):
        # the edit stays a draft and the push says it is not enforced.
        async def _unacked(shapes):
            client.set_shapes = real_set_shapes
            return 0

        client.set_shapes = _unacked
        handle.shapes = [_box("lost")]
        async with asyncio.timeout(5):
            while handle._pushes_inflight:
                await asyncio.sleep(0)
        assert handle.confirmed is False
        assert scene._shape_objects["shape:lost"].color == SceneColors.SHAPE_DRAFT_HEX
        records = caplog.get_records("call")
        assert any("NOT enforced" in r.getMessage() for r in records)
        records[:] = [r for r in records if "NOT enforced" not in r.getMessage()]
        handle.shapes = []
        await _until(
            lambda: handle.confirmed and not handle._pushes_inflight,
            "clear after the lost ack never confirmed",
        )

        # A readback captured before a clear, delivered after it, is discarded
        # instead of re-adopting a world the controller no longer enforces.
        handle.shapes = [_box("rb")]
        await _until(lambda: handle.confirmed, "edit never confirmed")

        async def _delayed_once():
            client.shapes = real_shapes  # delay only this one response
            world = await real_shapes()
            held_readback.set()
            await release_readback.wait()
            return world

        client.shapes = _delayed_once
        # Same entry the app uses for epoch-moved readbacks (main.py).
        stale = asyncio.create_task(handle.refresh_from_backend())
        await asyncio.wait_for(held_readback.wait(), timeout=5.0)
        handle.shapes = []
        await _until(lambda: handle.confirmed, "clear never confirmed")
        assert "shape:rb" not in scene._shape_objects
        release_readback.set()
        await stale
        assert [s.name for s in handle.shapes] == []
        assert "shape:rb" not in scene._shape_objects

        # A readback that *starts* after a clear's local apply but before its
        # ack queries the pre-clear world; epoch-driven refreshes must wait
        # out in-flight pushes (the push adopts readback itself once acked).
        handle.shapes = [_box("ep")]
        await _until(lambda: handle.confirmed, "edit never confirmed")

        async def _held_once(shapes):
            client.set_shapes = real_set_shapes  # hold only this one push's ack
            held_clear.set()
            await release_clear.wait()
            return await real_set_shapes(shapes)

        client.set_shapes = _held_once
        # The refresh task is created BEFORE the clear, so it runs before the
        # clear's push coroutine even starts — the exact CI interleaving.
        epoch_refresh = asyncio.create_task(handle.refresh_from_backend())
        handle.shapes = []  # local clear renders immediately; ack held
        await asyncio.wait_for(epoch_refresh, timeout=5.0)
        await asyncio.wait_for(held_clear.wait(), timeout=5.0)
        assert "shape:ep" not in scene._shape_objects, (
            "a readback during an un-acked clear resurrected the cleared shape"
        )
        release_clear.set()
        await _until(
            lambda: handle.confirmed and not handle.shapes, "clear never confirmed"
        )
        assert "shape:ep" not in scene._shape_objects

        # An edit whose push is delayed must not overwrite a newer clear on
        # the controller.
        async def _delayed_edit(shapes):
            if shapes and shapes[0].name == "delayed":
                held_edit.set()
                await release_edit.wait()
            return await real_set_shapes(shapes)

        client.set_shapes = _delayed_edit
        handle.shapes = [_box("delayed")]
        await asyncio.wait_for(held_edit.wait(), 5)
        handle.shapes = []
        # The clear parks on the lock the held edit owns; wait for it to be
        # there rather than for a fixed time.
        await _until(
            lambda: handle._pushes_inflight >= 2,
            "the overlapping clear never reached the push lock",
        )
        release_edit.set()
        await _until(
            lambda: handle._pushes_inflight == 0, "shape requests did not drain"
        )
        world = await real_shapes()
        assert world is not None and not world.program, "older edit overwrote the clear"
    finally:
        client.set_shapes = real_set_shapes
        client.shapes = real_shapes
        for release in (release_readback, release_clear, release_edit):
            release.set()
        handle.shapes = []
        await _until(
            lambda: handle.confirmed
            and not handle.shapes
            and handle._pushes_inflight == 0,
            "cleanup never confirmed",
        )


@pytest.mark.integration
async def test_keepout_editor_places_moves_edits_and_deletes(user: User) -> None:
    """The viewer's keep-out editor, end to end: place a box at a clicked
    point through the dialog, drag it via the scene's transform dispatch,
    resize it through the edit dialog (bad input refused), delete it with
    confirmation — asserting the request path (``commander.scene.shapes``)
    and the rendered scene object at every step."""
    import asyncio
    from types import SimpleNamespace

    import waldoctl

    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    handle = waldoctl.commander.scene
    assert scene is not None and handle is not None

    # Place — exactly what the context menu's "Box Here..." item runs.
    with scene.scene.client:
        scene._show_shape_dialog(kind="box", at=(0.4, 0.1, 0.0))
    await user.should_see(marker="shape-dialog-save")
    name_el = list(user.find(marker="shape-dialog-name").elements)[-1]
    name_el.set_value("bench")
    user.find(marker="shape-dialog-save").click()
    await asyncio.sleep(0)

    bench = next(s for s in handle.shapes if s.name == "bench")
    assert bench.kind == "box"
    # Birth pose sits the box ON the clicked floor point, not half inside it.
    assert bench.pose[:3] == pytest.approx((0.4, 0.1, 0.05))
    assert "shape:bench" in scene._shape_objects

    # Drag — through the scene's transform_end dispatch, the entry the JS
    # controls hit (same event fields the target handler consumes).
    scene._start_shape_move("bench")
    scene._handle_transform_event(
        SimpleNamespace(
            type="transform_end",
            object_name="shape:bench",
            x=0.25,
            y=-0.1,
            z=0.05,
            rx=None,
            ry=None,
            rz=None,
        )
    )
    bench = next(s for s in handle.shapes if s.name == "bench")
    assert bench.pose[:3] == pytest.approx((0.25, -0.1, 0.05))
    # The setter re-rendered the layer and the move re-armed on the new object.
    assert "shape:bench" in scene._shape_objects
    scene._end_shape_move()

    # Edit — grow x to 300 mm through the dialog. A negative dimension must
    # be refused by the shape's own validation, leaving the world untouched.
    with scene.scene.client:
        scene._show_shape_dialog(shape=bench)
    await asyncio.sleep(0)
    dim_x = list(user.find(marker="shape-dialog-dim-x").elements)[-1]
    dim_x.set_value(-50)
    user.find(marker="shape-dialog-save").click()
    await asyncio.sleep(0)
    assert next(s for s in handle.shapes if s.name == "bench").x == pytest.approx(0.1)
    dim_x.set_value(300)
    user.find(marker="shape-dialog-save").click()
    await asyncio.sleep(0)
    assert next(s for s in handle.shapes if s.name == "bench").x == pytest.approx(0.3)

    # Delete — with confirmation.
    with scene.scene.client:
        scene._delete_shape("bench")
    await user.should_see(marker="shape-delete-confirm")
    user.find(marker="shape-delete-confirm").click()
    await asyncio.sleep(0)
    assert all(s.name != "bench" for s in handle.shapes)
    assert "shape:bench" not in scene._shape_objects
    await _until(
        lambda: handle.confirmed and handle._pushes_inflight == 0,
        "deletion was not confirmed by the controller",
    )
    world = await waldoctl.commander.client.shapes()
    assert world is not None and not world.program
