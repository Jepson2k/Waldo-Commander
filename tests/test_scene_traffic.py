"""What the 3D scene sends the browser while the arm moves."""

from __future__ import annotations

import asyncio

import pytest
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from tests.helpers.wait import teleport_to_jog_pose
from tests.test_scene_jog_handles import _hover, _open
from waldo_commander.state import ui_state


@pytest.mark.integration
@pytest.mark.xfail(
    strict=True,
    reason="ui.scene sends the joints, the gizmo frame's position and its "
    "rotation as separate messages",
)
async def test_a_status_tick_sends_the_scene_one_message(user: User) -> None:
    """With the gizmo on screen and the arm moving, each status tick reaches
    the scene as at most one message."""
    urdf = await _open(user)
    panel = ui_state.control_panel
    await teleport_to_jog_pose(panel.client)
    _hover(user, urdf, "L6")
    scene = urdf.scene
    UserInteraction(user, {scene}, None).trigger("init", {})
    await asyncio.sleep(0.2)

    marker = f"runMethod({scene.id},"
    sent: list[str] = []
    ticks = 0
    run_javascript = scene.client.run_javascript
    update = urdf.update_from_robot_state

    def counting_send(code: str, *args, **kwargs):
        if marker in code:
            sent.append(code)
        return run_javascript(code, *args, **kwargs)

    def counting_tick() -> None:
        nonlocal ticks
        ticks += 1
        update()

    scene.client.run_javascript = counting_send
    urdf.update_from_robot_state = counting_tick
    try:
        q = list(await panel.client.angles())
        q[0] += 8.0
        assert await panel.client.wait_command(
            await panel.client.move_j(q, duration=1.0), timeout=10
        )
    finally:
        scene.client.run_javascript = run_javascript
        del urdf.update_from_robot_state
    assert ticks >= 10, f"only {ticks} status ticks while the arm moved"
    assert len(sent) <= ticks, (
        f"{len(sent)} scene messages for {ticks} status ticks; first: {sent[:3]}"
    )
