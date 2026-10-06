"""The URDF scene on the main page: its joints, what plugins draw on it, and
the workspace envelope that is generated at startup and shown only while its
mode is on."""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import teleport_to_jog_pose, wait_for_urdf_ready, wait_until


@pytest.mark.integration
async def test_scene_joints_plugin_overlays_and_envelope(
    user: User, enable_envelope
) -> None:
    import waldoctl
    from waldoctl import EnvelopeMode

    from waldo_commander.common.theme import hex_of
    from waldo_commander.services.urdf_scene.angle_pipeline import urdf_to_panel
    from waldo_commander.services.urdf_scene.envelope_renderer import workspace_envelope
    from waldo_commander.state import ui_state

    workspace_envelope.reset()
    assert workspace_envelope._generated is False

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    assert scene is not None, "Expected ui_state.urdf_scene to be initialized"
    assert len(scene.get_joint_names()) == 6

    # The arm is drawn where the robot is.
    await teleport_to_jog_pose(ui_state.control_panel.client)

    def drawn_at_robot() -> bool:
        angles = waldoctl.commander.status.joints.angles.deg
        for u, name in enumerate(scene.joint_names):
            panel, deg = urdf_to_panel(u, scene.joint_groups[name].q)
            if abs(deg - angles[panel]) > 0.01:
                return False
        return True

    assert await wait_until(drawn_at_robot, timeout_s=5.0), "the arm is drawn elsewhere"

    # A plugin draws into its own group; drawing again replaces what it drew,
    # and clearing removes it.
    def drawn() -> list[list[list[float]]]:
        groups = [o for o in scene.scene.objects.values() if o.name == "plugin:t"]
        assert len(groups) <= 1, "two overlay groups for one id"
        return [c.args[:2] for g in groups for c in g.children]

    with waldoctl.commander.scene.overlay("t") as s:
        s.line([0, 0, 0], [0.1, 0, 0]).material(hex_of("axis-x"))
    assert drawn() == [[[0, 0, 0], [0.1, 0, 0]]]
    with waldoctl.commander.scene.overlay("t") as s:
        s.line([0, 0, 0], [0, 0.2, 0]).material(hex_of("axis-y"))
    assert drawn() == [[[0, 0, 0], [0, 0.2, 0]]]
    waldoctl.commander.scene.clear("t")
    assert drawn() == []

    # Generated with the scene, so it is ready the moment its mode turns on.
    # Hull generation with 500k samples takes ~2-3s plus process pool overhead.
    assert await wait_until(lambda: workspace_envelope._generated, timeout_s=10.0), (
        "Expected workspace envelope to be pre-generated on scene startup"
    )
    assert workspace_envelope.max_reach > 0

    user.find(marker="tab-settings").click()
    await asyncio.sleep(0)
    select = next(iter(user.find(marker="select-envelope-mode").elements))

    def shown() -> bool:
        hull = scene.envelope_object
        return hull is not None and hull.visible_

    select.set_value(EnvelopeMode.ON.value)
    # A tool change during startup may have reset the hull, forcing a cold
    # rebuild, so the wait allows for a full generation.
    assert await wait_until(shown, timeout_s=30.0), "envelope not shown with mode on"

    select.set_value(EnvelopeMode.OFF.value)
    assert await wait_until(lambda: not shown(), timeout_s=5.0), (
        "envelope still shown with mode off"
    )
