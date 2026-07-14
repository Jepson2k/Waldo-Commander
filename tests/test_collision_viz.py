"""Collision visualization: red-tint of colliding parts + keep-out shape render.

The ``user`` fixture has no WebGL, but the scene's Python ``Object3D`` colors are
the exact input three.js renders from — asserting them verifies the highlight
logic (name mapping, recolor, restore) deterministically. A browser-level render
check lives in ``test_collision_viz_screen.py``.
"""

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_urdf_ready
from waldo_commander.services.urdf_scene.config import RobotAppearanceMode


@pytest.mark.integration
async def test_collision_highlight_tints_reported_links_and_restores(
    user: User,
) -> None:
    import waldoctl
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None
    links = [name for name, meshes in scene._link_to_meshes.items() if meshes]
    assert len(links) >= 2, "need two link meshes to simulate a self-collision"
    a, b = links[0], links[1]
    obj_a, obj_b = scene._link_to_meshes[a][0], scene._link_to_meshes[b][0]
    before_a, before_b = obj_a.color, obj_b.color
    assert before_a != SceneColors.COLLISION_HEX

    # Controller reports pairs in the display vocabulary: plain URDF link names.
    coll = waldoctl.commander.status.collision
    coll.active = True
    coll.pairs = [(a, b)]
    scene.update_from_robot_state()
    assert obj_a.color == SceneColors.COLLISION_HEX
    assert obj_b.color == SceneColors.COLLISION_HEX

    # Cleared -> restored to the prior (mode) color.
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert obj_a.color == before_a
    assert obj_b.color == before_b


@pytest.mark.integration
async def test_shapes_render_and_can_be_highlighted(user: User) -> None:
    import waldoctl
    from waldoctl import Box
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None
    from waldoctl import Cylinder

    scene.render_shapes(
        [
            Box(name="wall", x=0.1, y=0.1, z=0.1, pose=(0.3, 0.0, 0.3, 0, 0, 0)),
            Cylinder(name="post", radius=0.05, length=0.5),
        ],
        installation=[Box(name="bench", x=0.4, y=0.4, z=0.05)],
    )
    assert "shape:wall" in scene._shape_objects
    # Installation layer renders under its own namespace and color.
    bench = scene._shape_objects["install:bench"]
    assert bench.color == SceneColors.SHAPE_INSTALL_HEX
    shape_obj = scene._shape_objects["shape:wall"]
    assert shape_obj.color == SceneColors.SHAPE_HEX
    # Render wiring applies the Z-up axis correction (three.js is Y-up).
    assert scene._shape_objects["shape:post"].R == [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0],
        [0.0, 1.0, 0.0],
    ]

    link = next(name for name, meshes in scene._link_to_meshes.items() if meshes)
    coll = waldoctl.commander.status.collision
    coll.pairs = [(link, "shape:wall"), (link, "install:bench")]
    coll.active = True
    scene.update_from_robot_state()
    assert shape_obj.color == SceneColors.COLLISION_HEX
    assert bench.color == SceneColors.COLLISION_HEX
    assert scene._link_to_meshes[link][0].color == SceneColors.COLLISION_HEX

    # Mode toggle mid-collision: the arm/tool repaint loops don't touch shape
    # objects, so set_appearance_mode must repaint them itself — otherwise the
    # next tick re-snapshots red as the shape's base and it sticks red forever.
    scene.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
    assert shape_obj.color == SceneColors.SHAPE_HEX
    assert bench.color == SceneColors.SHAPE_INSTALL_HEX  # repaint keeps layer color
    scene.update_from_robot_state()  # still colliding — re-tints from clean base
    assert shape_obj.color == SceneColors.COLLISION_HEX
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert shape_obj.color == SceneColors.SHAPE_HEX
    assert bench.color == SceneColors.SHAPE_INSTALL_HEX


@pytest.mark.integration
async def test_appearance_repaint_keeps_draft_amber(user: User) -> None:
    """An UNCONFIRMED program layer must stay draft-amber through appearance
    repaints (sim toggle / EDITING entry / page-load): pre-fix the repaint
    promoted it to the confirmed slate, displaying an un-enforced keep-out as
    controller-enforced."""
    from waldoctl import Box
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None
    pending = Box(name="pending", x=0.1, y=0.1, z=0.1, pose=(0.4, 0.0, 0.3, 0, 0, 0))
    scene.render_shapes([pending], draft=True)
    obj = scene._shape_objects["shape:pending"]
    assert obj.color == SceneColors.SHAPE_DRAFT_HEX

    scene.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
    assert obj.color == SceneColors.SHAPE_DRAFT_HEX, (
        "repaint promoted an unconfirmed keep-out to the confirmed color"
    )

    # Readback confirmation re-renders; later repaints keep the confirmed slate.
    scene.render_shapes([pending], draft=False)
    obj = scene._shape_objects["shape:pending"]
    assert obj.color == SceneColors.SHAPE_HEX
    scene.set_appearance_mode(RobotAppearanceMode.LIVE)
    assert obj.color == SceneColors.SHAPE_HEX


@pytest.mark.integration
async def test_editing_highlight_and_preview_marking_via_local_checker(
    user: User,
) -> None:
    """commander.scene.shapes feeds this process's checker: the EDITING pose
    tints colliding geometry client-side and the dry-run preview marks
    colliding segments — no controller round-trip."""
    import waldoctl
    from waldoctl import Box
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.services.path_visualizer import _mark_colliding_segments
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None
    robot = ui_state.active_robot
    assert robot.has_collision_checking

    try:
        # A base-encasing box collides at q=0 — deterministic at any test pose.
        block = Box(name="block", x=0.6, y=0.6, z=0.6, pose=(0.0, 0.0, 0.1, 0, 0, 0))
        waldoctl.commander.scene.shapes = [block]
        import numpy as np

        pairs = robot.colliding_pairs(np.zeros(6))
        assert pairs, "local checker must see the base-encasing shape"
        # Pairs arrive in display vocabulary: plain URDF link names.
        tinted = {n for p in pairs for n in p if not n.startswith("shape:")}
        link = next(
            name
            for name, meshes in scene._link_to_meshes.items()
            if meshes and name in tinted
        )

        scene.set_appearance_mode(RobotAppearanceMode.EDITING)
        scene.set_editing_angles([0.0] * 6)
        shape_obj = scene._shape_objects["shape:block"]
        assert shape_obj.color == SceneColors.COLLISION_HEX
        assert scene._link_to_meshes[link][0].color == SceneColors.COLLISION_HEX

        # Interactive drag paths (ghost IK / joint ring / TCP ball) must also
        # refresh the highlight — the status loop is skipped in EDITING.
        class _GhostIkEvent:
            args = {"chain_id": "ghost_ik", "angles": [0.3] * 6}

        scene._on_ik_solved(_GhostIkEvent())
        assert scene._editing_collision_q == tuple(scene._editing_angles)

        # Dry-run preview: a segment whose trajectory passes through the box is
        # recolored and records its first colliding waypoint. (Runs in the
        # dry-run subprocess for real programs; the function is pure on dicts.)
        seg = {
            "points": [[0, 0, 0]],
            "color": "#00ff00",
            "is_valid": True,
            "line_number": 1,
            "joint_trajectory": [[0.0] * 6, [0.1] * 6],
        }
        untouched = {
            "points": [[0, 0, 0]],
            "color": "#00ff00",
            "is_valid": True,
            "line_number": 2,
        }
        # The marking applies the passed world explicitly (a reused pool
        # worker's checker must never inherit a previous run's shapes).
        _mark_colliding_segments(
            robot, [seg, untouched], [], [], [tuple(block.to_wire())], None
        )
        assert seg["color"] == SceneColors.COLLISION_HEX
        assert seg["collision_step"] == 0
        assert untouched["color"] == "#00ff00"
        assert "collision_step" not in untouched
    finally:
        # The checker is process-global — never leak shapes into other tests.
        waldoctl.commander.scene.shapes = []

    # Clearing shapes re-runs the EDITING highlight: links restore to the mode
    # base and the shape objects are gone.
    assert scene._link_to_meshes[link][0].color == scene.config.edit_color
    assert "shape:block" not in scene._shape_objects


def test_preview_marking_replays_tool_boundaries() -> None:
    """Segments after a mid-script select_tool are checked with THAT tool, and
    the checker's tool is restored afterwards (the fallback path shares the
    live checker)."""
    from waldoctl import ToolSelection
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.services.path_visualizer import _mark_colliding_segments

    class _FakeRobot:
        has_collision_checking = True

        def __init__(self):
            self.tool = "NONE"

        def apply_shapes(self, shapes):
            pass

        def set_active_tool(self, key, tcp_offset_m=None, variant_key=None):
            self.tool = key

        def check_trajectory(self, q):
            return 0 if self.tool == "SSG-48" else -1

    def seg(line: int) -> dict:
        return {
            "color": "#00ff00",
            "line_number": line,
            "joint_trajectory": [[0.0] * 6],
        }

    segs = [seg(1), seg(2), seg(3)]
    # Selection recorded after segment 0 -> applies to segments 1 and 2.
    sels = [ToolSelection(tool_key="SSG-48", variant_key="", segment_index=0)]
    robot = _FakeRobot()
    _mark_colliding_segments(robot, segs, sels, [], None, ("NONE", ""))
    assert "collision_step" not in segs[0]
    assert segs[1]["collision_step"] == 0
    assert segs[1]["color"] == SceneColors.COLLISION_HEX
    assert segs[2]["collision_step"] == 0
    assert robot.tool == "NONE"  # restored to the initial tool

    # Back-to-back selections (same segment_index) must replay chronologically
    # — the LAST recorded tool wins, not the alphabetically-last.
    segs = [seg(1), seg(2)]
    sels = [
        ToolSelection(tool_key="SSG-48", variant_key="", segment_index=0),
        ToolSelection(tool_key="VACUUM", variant_key="", segment_index=0),
    ]
    _mark_colliding_segments(_FakeRobot(), segs, sels, [], None, ("NONE", ""))
    assert "collision_step" not in segs[1]  # checked with VACUUM, not SSG-48


def test_preview_marking_replays_shape_boundaries() -> None:
    """Segments after a mid-script set_shapes are checked against THAT world,
    and the submit-time world is restored afterwards (the fallback path shares
    the live checker)."""
    from waldoctl import Box, ShapeChange
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.services.path_visualizer import _mark_colliding_segments

    class _FakeRobot:
        has_collision_checking = True

        def __init__(self):
            self.world: tuple = ()

        def apply_shapes(self, shapes):
            self.world = tuple(s.name for s in shapes)

        def set_active_tool(self, key, tcp_offset_m=None, variant_key=None):
            pass

        def check_trajectory(self, q):
            return 0 if "bar" in self.world else -1

    def seg(line: int) -> dict:
        return {
            "color": "#00ff00",
            "line_number": line,
            "joint_trajectory": [[0.0] * 6],
        }

    segs = [seg(1), seg(2), seg(3)]
    changes = [
        ShapeChange(shapes=(Box(name="bar", x=0.1, y=0.1, z=0.1),), segment_index=0)
    ]
    robot = _FakeRobot()
    _mark_colliding_segments(robot, segs, [], changes, None, ("NONE", ""))
    assert "collision_step" not in segs[0]  # world was empty for segment 0
    assert segs[1]["color"] == SceneColors.COLLISION_HEX
    assert segs[2]["collision_step"] == 0
    assert robot.world == ()  # restored to the submit-time world


def test_shape_render_pose_matches_enforced_geometry() -> None:
    """Cylinders stand along coal's Z axis and planes sit on their halfspace
    surface — the drawn shape must match the blocked volume."""
    import numpy as np

    from waldoctl import Cylinder, Plane
    from waldo_commander.services.urdf_scene.urdf_scene import _shape_render_pose

    # Identity pose: the render rotation is the Y->Z-up correction, not identity.
    pos, rot = _shape_render_pose(Cylinder(name="post", radius=0.05, length=0.5))
    assert pos == (0.0, 0.0, 0.0)
    assert np.allclose(rot, [[1, 0, 0], [0, 0, -1], [0, 1, 0]])

    # z=0.4 ceiling: the slab sits at the surface, normal along +z.
    pos, rot = _shape_render_pose(Plane(name="ceil", nx=0, ny=0, nz=1, offset=0.4))
    assert np.allclose(pos, (0.0, 0.0, 0.4))
    assert np.allclose(rot, np.eye(3))

    # Non-unit normal: coal normalizes Halfspace(n, d) to (n/|n|, d/|n|), so
    # (0,0,2), offset 0.8 enforces z <= 0.4 — the slab must render there.
    pos, rot = _shape_render_pose(Plane(name="c2", nx=0, ny=0, nz=2, offset=0.8))
    assert np.allclose(pos, (0.0, 0.0, 0.4))

    # x-normal wall at x=0.2: slab normal (its local z) maps to +x.
    pos, rot = _shape_render_pose(Plane(name="wall", nx=1, ny=0, nz=0, offset=0.2))
    assert np.allclose(pos, (0.2, 0.0, 0.0))
    assert np.allclose(np.array(rot) @ [0, 0, 1], [1, 0, 0])


@pytest.mark.integration
async def test_engaged_repaint_keeps_collision_highlight(user: User) -> None:
    """Gripper engage/disengage repaints tool meshes — an active red tint must
    re-apply from the new base instead of being silently cleared."""
    import waldoctl
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None
    scene.apply_tool_everywhere("SSG-48")
    assert scene._tool_meshes, "tool meshes must be mapped"
    mesh = scene._tool_meshes[0]

    coll = waldoctl.commander.status.collision
    # tool: names tint the attached tool; link partner arrives as a plain name.
    coll.pairs = [("tool:SSG-48:body", "L5")]
    coll.active = True
    scene.update_from_robot_state()
    assert mesh.color == SceneColors.COLLISION_HEX

    # Engage mid-collision: repaint must not strand the highlight.
    scene._apply_tool_engaged_color(True)
    scene.update_from_robot_state()  # still colliding — re-tints from new base
    assert mesh.color == SceneColors.COLLISION_HEX

    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert mesh.color != SceneColors.COLLISION_HEX  # restored, not stuck red


async def test_shape_push_honors_ack_contract(monkeypatch, caplog) -> None:
    """The push trusts only the ABC return-code contract: 0 (unconfirmed —
    what the real client returns on timeout; it does NOT raise) leaves the
    draft unconfirmed and logs the not-enforced warning; 1 adopts readback
    truth. The stub mirrors the real client's contract exactly."""
    import logging

    import waldoctl
    from waldoctl import Box, ShapeWorld
    from waldo_commander.services.urdf_scene import scene_handle as sh

    box = Box(name="A", x=0.1, y=0.1, z=0.1)

    class _Client:
        code = 0
        world = ShapeWorld(installation=(), program=(box,))

        async def set_shapes(self, shapes):
            return self.code

        async def shapes(self):
            return self.world

    client = _Client()
    # Patch the client on the locator-resolved commander instance — NEVER
    # monkeypatch `waldoctl.commander` itself: that materializes a module
    # attribute which permanently shadows the PEP 562 locator on teardown.
    monkeypatch.setattr(waldoctl.commander, "client", client)

    handle = sh.WcSceneHandle()
    handle._shapes = [box]

    # Unconfirmed (timeout) → still draft, loudly logged.
    with caplog.at_level(logging.ERROR):
        await sh.WcSceneHandle._push_shapes_async(handle)
    assert handle.confirmed is False
    assert any("NOT enforced" in r.message for r in caplog.records)

    # Confirmed → readback adopted, draft cleared.
    client.code = 1
    await sh.WcSceneHandle._push_shapes_async(handle)
    assert handle.confirmed is True
    assert [s.name for s in handle.shapes] == ["A"]


async def test_stale_shape_push_never_overwrites_a_newer_one(monkeypatch) -> None:
    """A push completing after a NEWER assignment must not adopt readback for
    the stale world — the controller may briefly enforce the old one, but the
    display must keep tracking the newest request."""
    import asyncio

    import waldoctl
    from waldoctl import Box, ShapeWorld
    from waldo_commander.services.urdf_scene import scene_handle as sh

    old = Box(name="old", x=0.1, y=0.1, z=0.1)
    new = Box(name="new", x=0.1, y=0.1, z=0.1)
    release = asyncio.Event()

    class _Client:
        async def set_shapes(self, shapes):
            await release.wait()  # old push in flight while a new world lands
            return 1

        async def shapes(self):
            return ShapeWorld(installation=(), program=(old,))

    monkeypatch.setattr(waldoctl.commander, "client", _Client())

    handle = sh.WcSceneHandle.__new__(sh.WcSceneHandle)
    handle._groups = {}
    handle._installation = ()
    handle._confirmed = False
    handle._shapes = [old]

    task = asyncio.create_task(sh.WcSceneHandle._push_shapes_async(handle))
    await asyncio.sleep(0)
    handle._shapes = [new]  # newer assignment supersedes the in-flight push
    release.set()
    await task
    assert [s.name for s in handle.shapes] == ["new"]
    assert handle.confirmed is False  # stale readback was not adopted


@pytest.mark.integration
async def test_preview_script_set_shapes_real_dispatch_no_stale_world(
    user: User,
) -> None:
    """Two dry runs through the REAL preview runner in one process (= a reused
    pool worker). Run 1's script calls ``set_shapes`` — the pre-fix dispatch
    crashed with ``TypeError: object of type 'method' has no len()``. Run 2
    must not see run 1's world — the pre-fix worker leaked it into the next
    run's planning guard as phantom collisions."""
    import numpy as np

    import parol6
    import parol6.client
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.services.path_visualizer import _run_simulation_isolated
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    robot = ui_state.active_robot
    dr = robot.create_dry_run_client()
    assert dr is not None
    dr_cls = type(dr)

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
        dry_run_client_cls=dr_cls,
        shapes_wire=[],
        initial_tool=("NONE", ""),
    )
    try:
        res1 = _run_simulation_isolated(prog_with_shapes, **kwargs)
        assert res1["error"] is None, res1["error"]  # C1: no TypeError crash
        assert res1["segments"], "the move before set_shapes must plan"
        assert all(
            s.get("color") != SceneColors.COLLISION_HEX for s in res1["segments"]
        )

        res2 = _run_simulation_isolated(prog_plain, **kwargs)
        assert res2["error"] is None, res2["error"]  # C3: no phantom guard hit
        assert all(
            s.get("color") != SceneColors.COLLISION_HEX and "collision_step" not in s
            for s in res2["segments"]
        )
    finally:
        for mod, name, val in snapshot:
            if val is _missing:
                try:
                    delattr(mod, name)
                except AttributeError:
                    pass
            else:
                setattr(mod, name, val)
        robot.apply_shapes([])  # live checker shared in-process — leave it clean


@pytest.mark.integration
async def test_world_changed_by_program_reaches_display_via_epoch(user: User) -> None:
    """A world change WC did NOT initiate (a program calling set_shapes on its
    own client) must reach the display through the full pipeline: controller
    epoch bump → status broadcast → readback query → render."""
    import asyncio

    import waldoctl
    from waldoctl import Box
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    handle = waldoctl.commander.scene
    client = waldoctl.commander.client

    async def _until(cond, what: str) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not cond():
            assert asyncio.get_running_loop().time() < deadline, what
            await asyncio.sleep(0.05)

    try:
        # Straight to the controller — scene_handle never sees this push.
        assert (
            await client.set_shapes(
                [Box(name="prog", x=0.1, y=0.1, z=0.1, pose=(0.9, 0.9, 0.9, 0, 0, 0))]
            )
            == 1
        )
        await _until(
            lambda: [s.name for s in handle.shapes] == ["prog"] and handle.confirmed,
            "epoch-driven readback never adopted the program's world",
        )
        assert "shape:prog" in ui_state.urdf_scene._shape_objects
    finally:
        assert await client.set_shapes([]) == 1
        await _until(lambda: handle.shapes == [], "clear never reached display")
    assert "shape:prog" not in ui_state.urdf_scene._shape_objects


@pytest.mark.integration
async def test_shape_edit_is_acked_and_display_adopts_readback(user: User) -> None:
    """Full real path: assigning ``commander.scene.shapes`` renders a draft,
    pushes through the REAL client to the REAL (fake-serial) controller, and
    flips to confirmed only after the readback query returns the applied
    world. Pre-fix, the client faked success and nothing ever confirmed."""
    import asyncio

    import waldoctl
    from waldoctl import Box
    from waldo_commander.common.theme import SceneColors
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    handle = waldoctl.commander.scene

    async def _until_confirmed() -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not handle.confirmed:
            assert asyncio.get_running_loop().time() < deadline, "never confirmed"
            await asyncio.sleep(0.05)

    try:
        handle.shapes = [
            Box(name="rb", x=0.1, y=0.1, z=0.1, pose=(0.9, 0.9, 0.9, 0, 0, 0))
        ]
        assert handle.confirmed is False  # draft until the controller confirms
        assert scene._shape_objects["shape:rb"].color == SceneColors.SHAPE_DRAFT_HEX

        await _until_confirmed()
        assert [s.name for s in handle.shapes] == ["rb"]  # readback truth
        assert scene._shape_objects["shape:rb"].color == SceneColors.SHAPE_HEX
    finally:
        handle.shapes = []
        await _until_confirmed()
    assert "shape:rb" not in scene._shape_objects


@pytest.mark.integration
async def test_stale_readback_cannot_resurrect_cleared_shapes(user: User) -> None:
    """A readback that raced a newer edit is discarded. Pre-fix, a delayed
    ``shapes()`` response captured before a clear re-adopted the old world and
    re-rendered a keep-out the controller no longer enforced."""
    import asyncio

    import waldoctl
    from waldoctl import Box
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    handle = waldoctl.commander.scene

    async def _until(cond, msg: str) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not cond():
            assert asyncio.get_running_loop().time() < deadline, msg
            await asyncio.sleep(0.05)

    client = waldoctl.commander.client
    real_shapes = client.shapes
    release = asyncio.Event()
    held = asyncio.Event()

    async def _delayed_once():
        client.shapes = real_shapes  # delay only this one response
        world = await real_shapes()
        held.set()
        await release.wait()
        return world

    try:
        handle.shapes = [
            Box(name="rb", x=0.1, y=0.1, z=0.1, pose=(0.9, 0.9, 0.9, 0, 0, 0))
        ]
        await _until(lambda: handle.confirmed, "edit never confirmed")

        client.shapes = _delayed_once
        # Same entry the app uses for epoch-moved readbacks (main.py).
        stale = asyncio.create_task(handle.refresh_from_backend())
        await asyncio.wait_for(held.wait(), timeout=5.0)

        handle.shapes = []
        await _until(lambda: handle.confirmed, "clear never confirmed")
        assert "shape:rb" not in scene._shape_objects

        release.set()
        await stale
        assert [s.name for s in handle.shapes] == []
        assert "shape:rb" not in scene._shape_objects
    finally:
        client.shapes = real_shapes
        release.set()
        handle.shapes = []
        await _until(
            lambda: handle.confirmed and not handle.shapes, "cleanup never confirmed"
        )


@pytest.mark.integration
async def test_refresh_during_unacked_clear_does_not_resurrect(user: User) -> None:
    """The complementary race to the delayed-response one above: a readback
    that *starts* after a clear's local apply but before its controller ack
    queries the pre-clear world. Pre-fix it passed the seq guard (it bumped
    the seq itself), adopted the resurrected world, and the clear's own
    post-ack refresh then skipped as superseded — leaving a cleared shape
    rendered and confirmed. Epoch-driven refreshes must wait out in-flight
    pushes (the push adopts readback itself once acked)."""
    import asyncio

    import waldoctl
    from waldoctl import Box
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    handle = waldoctl.commander.scene

    async def _until(cond, msg: str) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not cond():
            assert asyncio.get_running_loop().time() < deadline, msg
            await asyncio.sleep(0.05)

    client = waldoctl.commander.client
    real_set_shapes = client.set_shapes
    release = asyncio.Event()
    held = asyncio.Event()

    async def _held_once(shapes):
        client.set_shapes = real_set_shapes  # hold only this one push's ack
        held.set()
        await release.wait()
        return await real_set_shapes(shapes)

    try:
        handle.shapes = [
            Box(name="ep", x=0.1, y=0.1, z=0.1, pose=(0.9, 0.9, 0.9, 0, 0, 0))
        ]
        await _until(lambda: handle.confirmed, "edit never confirmed")

        client.set_shapes = _held_once
        handle.shapes = []  # local clear renders immediately; ack held
        await asyncio.wait_for(held.wait(), timeout=5.0)

        # The first edit's scene_epoch move lands now — the watcher fires a
        # refresh (same entry as main.py) while the clear is still un-acked,
        # so the controller would still report the pre-clear world.
        await handle.refresh_from_backend()
        assert "shape:ep" not in scene._shape_objects, (
            "a readback during an un-acked clear resurrected the cleared shape"
        )

        release.set()
        await _until(
            lambda: handle.confirmed and not handle.shapes, "clear never confirmed"
        )
        assert "shape:ep" not in scene._shape_objects
    finally:
        client.set_shapes = real_set_shapes
        release.set()
        handle.shapes = []
        await _until(
            lambda: handle.confirmed and not handle.shapes, "cleanup never confirmed"
        )
