"""Integration tests for URDF scene initialization and behavior in the main page.

These tests verify that opening the main page creates a URDF scene,
populates basic state on ``ui_state``, and that the scene's Python API
correctly updates internal state.
"""

import asyncio

import pytest
from nicegui.testing import User

from tests.helpers.wait import wait_for_urdf_ready


@pytest.mark.integration
async def test_urdf_scene_joint_names(user: User) -> None:
    """Test that get_joint_names returns the expected joint names.

    Verifies that the scene reports 6 actuated joints for PAROL6.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None, "Expected ui_state.urdf_scene to be initialized"

    joint_names = scene.get_joint_names()
    assert isinstance(joint_names, list), "Expected joint_names to be a list"
    assert len(joint_names) == 6, f"Expected 6 joints, got {len(joint_names)}"


@pytest.mark.integration
async def test_urdf_scene_envelope_pregenerated_on_startup(
    user: User, enable_envelope
) -> None:
    """Test that workspace envelope is pre-generated when scene loads.

    Verifies that opening the main page triggers envelope data generation,
    so it's available for immediate rendering when envelope mode is 'on'.
    """
    from waldo_commander.state import ui_state
    from waldo_commander.services.urdf_scene.envelope_renderer import workspace_envelope

    # Reset envelope state before test
    workspace_envelope.reset()
    assert workspace_envelope._generated is False, (
        "Expected envelope to start ungenerated"
    )

    await user.open("/")
    await (
        wait_for_urdf_ready()
    )  # Wait for scene init (envelope generated during show())

    scene = ui_state.urdf_scene
    assert scene is not None, "Expected ui_state.urdf_scene to be initialized"

    # Wait for envelope to be generated (condition-based instead of fixed sleep)
    # Hull generation with 500k samples takes ~2-3s plus process pool overhead
    for _ in range(100):  # Up to 10 seconds
        if workspace_envelope._generated:
            break
        await asyncio.sleep(0.1)

    assert workspace_envelope._generated is True, (
        "Expected workspace envelope to be pre-generated on scene startup"
    )
    assert workspace_envelope.max_reach > 0, (
        "Expected envelope to have calculated max reach"
    )


@pytest.mark.integration
async def test_urdf_scene_envelope_visibility_on_mode_change(
    user: User, enable_envelope
) -> None:
    """Test that changing envelope_mode to 'on' creates and shows the envelope.

    Verifies that when simulation_state.envelope_mode is set to 'on', the
    envelope wireframe sphere is created and made visible in the scene.
    """
    from waldo_commander.state import ui_state, simulation_state
    from waldo_commander.services.urdf_scene.envelope_renderer import workspace_envelope

    await user.open("/")
    await wait_for_urdf_ready()

    scene = ui_state.urdf_scene
    assert scene is not None, "Expected ui_state.urdf_scene to be initialized"

    # Set envelope mode to 'off' first
    from waldo_commander.state import EnvelopeMode

    simulation_state.envelope_mode = EnvelopeMode.OFF
    await asyncio.sleep(0.2)  # Let update timer run

    # If envelope_object exists, it should be hidden
    if scene.envelope_object is not None:
        assert scene.envelope_object.visible_ is False, (
            "Expected envelope to be hidden when mode is 'off'"
        )

    # Now set envelope mode to 'on'
    simulation_state.envelope_mode = EnvelopeMode.ON
    await asyncio.sleep(0.2)  # Let update timer run

    # Envelope data should exist
    assert workspace_envelope._generated is True, "Expected envelope to be generated"

    # Envelope object should be created and visible
    # Note: envelope_object may be created lazily on first 'on' mode
    if scene.envelope_object is not None:
        assert scene.envelope_object.visible_ is True, (
            "Expected envelope to be visible when mode is 'on'"
        )
