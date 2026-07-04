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

    # Controller reports the pair with a Pinocchio "_<index>" geom suffix.
    coll = waldoctl.commander.status.collision
    coll.active = True
    coll.pairs = [(f"{a}_0", f"{b}_0")]
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
        ]
    )
    assert "shape:wall" in scene._shape_objects
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
    coll.pairs = [(f"{link}_0", "shape:wall")]
    coll.active = True
    scene.update_from_robot_state()
    assert shape_obj.color == SceneColors.COLLISION_HEX
    assert scene._link_to_meshes[link][0].color == SceneColors.COLLISION_HEX

    # Mode toggle mid-collision: the arm/tool repaint loops don't touch shape
    # objects, so set_appearance_mode must repaint them itself — otherwise the
    # next tick re-snapshots red as the shape's base and it sticks red forever.
    scene.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
    assert shape_obj.color == SceneColors.SHAPE_HEX
    scene.update_from_robot_state()  # still colliding — re-tints from clean base
    assert shape_obj.color == SceneColors.COLLISION_HEX
    coll.active = False
    coll.pairs = []
    scene.update_from_robot_state()
    assert shape_obj.color == SceneColors.SHAPE_HEX


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
        waldoctl.commander.scene.shapes = [
            Box(name="block", x=0.6, y=0.6, z=0.6, pose=(0.0, 0.0, 0.1, 0, 0, 0))
        ]
        import numpy as np

        pairs = robot.colliding_pairs(np.zeros(6))
        assert pairs, "local checker must see the base-encasing shape"
        tinted = {n for p in pairs for n in p if not n.startswith("shape:")}
        link = next(
            name
            for name, meshes in scene._link_to_meshes.items()
            if meshes and f"{name}_0" in tinted
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
        _mark_colliding_segments(robot, [seg, untouched], [], None, None)
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
    _mark_colliding_segments(robot, segs, sels, None, ("NONE", ""))
    assert "collision_step" not in segs[0]
    assert segs[1]["collision_step"] == 0
    assert segs[1]["color"] == SceneColors.COLLISION_HEX
    assert segs[2]["collision_step"] == 0
    assert robot.tool == "NONE"  # restored to the initial tool


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
    assert scene._tool_geom_to_meshes, "tool meshes must be mapped"
    geom_name = next(iter(scene._tool_geom_to_meshes))
    mesh = scene._tool_geom_to_meshes[geom_name][0]

    coll = waldoctl.commander.status.collision
    coll.pairs = [(geom_name, "L5_0")]
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
