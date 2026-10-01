"""The URDF scene on the main page: its joints, and the workspace envelope
that is generated at startup and shown only while its mode is on."""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_urdf_ready, wait_until


@pytest.mark.integration
async def test_scene_reports_its_joints_and_shows_the_envelope_only_when_on(
    user: User, enable_envelope
) -> None:
    from waldoctl import EnvelopeMode

    from waldo_commander.services.urdf_scene.envelope_renderer import workspace_envelope
    from waldo_commander.state import ui_state

    workspace_envelope.reset()
    assert workspace_envelope._generated is False

    await user.open("/")
    await wait_for_urdf_ready()
    scene = ui_state.urdf_scene
    assert scene is not None, "Expected ui_state.urdf_scene to be initialized"
    assert len(scene.get_joint_names()) == 6

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
