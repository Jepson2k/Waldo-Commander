"""The motion profile shared by Settings, controller restore and preview."""

from nicegui import app
from waldoctl import Robot


def stored_motion_profile(robot: Robot) -> str:
    profiles = robot.motion_profiles
    stored = app.storage.general.get("motion_profile")
    if stored in profiles:
        return stored
    return "TOPPRA" if "TOPPRA" in profiles else profiles[0]
