"""The motion profile shared by Settings, controller restore and preview."""

import logging

from nicegui import app
from waldoctl import Robot, RobotClient

from waldo_commander.state import robot_state, ui_state

logger = logging.getLogger(__name__)


def stored_motion_profile(robot: Robot) -> str:
    profiles = robot.motion_profiles
    stored = app.storage.general.get("motion_profile")
    if stored in profiles:
        return stored
    return "TOPPRA" if "TOPPRA" in profiles else profiles[0]


def preview_motion_profile(robot: Robot) -> str:
    reported = robot_state.motion_profile
    return reported if reported is not None else stored_motion_profile(robot)


def store_motion_profile(profile: str) -> None:
    app.storage.general["motion_profile"] = profile


async def select_motion_profile(client: RobotClient, profile: str) -> bool:
    """True once the controller confirms ``profile``; False when its status
    stream will settle an unconfirmed change. Raises when it was not applied."""
    result = await client.select_profile(profile)
    if result == 1:
        return True
    if result == 0 and robot_state.motion_profile is not None:
        return False
    # A lost reply can still mean the controller applied the change.
    try:
        applied = await client.profile()
    except Exception as exc:
        logger.warning("Motion profile readback failed: %s", exc)
        applied = None
    if applied in ui_state.active_robot.motion_profiles:
        store_motion_profile(applied)
    if applied != profile:
        raise ValueError("Profile change was not confirmed")
    return True
