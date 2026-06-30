"""Collision visualization: red-tint of colliding parts + keep-out shape render.

The ``user`` fixture has no WebGL, but the scene's Python ``Object3D`` colors are
the exact input three.js renders from — asserting them verifies the highlight
logic (name mapping, recolor, restore) deterministically. A browser-level render
check lives in ``test_collision_viz_screen.py``.
"""

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_urdf_ready


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
    waldoctl.commander.status.collision.pairs = [(f"{link}_0", "shape:wall")]
    waldoctl.commander.status.collision.active = True
    scene.update_from_robot_state()
    assert shape_obj.color == SceneColors.COLLISION_HEX
    assert scene._link_to_meshes[link][0].color == SceneColors.COLLISION_HEX
