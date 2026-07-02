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
    scene.render_shapes(
        [Box(name="wall", x=0.1, y=0.1, z=0.1, pose=(0.3, 0.0, 0.3, 0, 0, 0))]
    )
    assert "shape:wall" in scene._shape_objects
    shape_obj = scene._shape_objects["shape:wall"]
    assert shape_obj.color == SceneColors.SHAPE_HEX

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
    from waldo_commander.services.path_visualizer import PathVisualizer
    from waldo_commander.state import PathSegment, ui_state

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

        # Dry-run preview: a segment whose trajectory passes through the box is
        # recolored and records its first colliding waypoint.
        seg = PathSegment(
            points=[[0, 0, 0]],
            color="#00ff00",
            is_valid=True,
            line_number=1,
            joint_trajectory=[[0.0] * 6, [0.1] * 6],
        )
        untouched = PathSegment(
            points=[[0, 0, 0]], color="#00ff00", is_valid=True, line_number=2
        )
        PathVisualizer._mark_colliding_segments(robot, [seg, untouched])
        assert seg.color == SceneColors.COLLISION_HEX
        assert seg.collision_step == 0
        assert untouched.color == "#00ff00"
        assert untouched.collision_step is None
    finally:
        # The checker is process-global — never leak shapes into other tests.
        waldoctl.commander.scene.shapes = []

    # Clearing shapes re-runs the EDITING highlight: links restore to the mode
    # base and the shape objects are gone.
    assert scene._link_to_meshes[link][0].color == scene.config.edit_color
    assert "shape:block" not in scene._shape_objects
