"""Gripper commands with confirmed command completion, not grasp inference."""

from typing import cast

from waldoctl.client import RobotClient
from waldoctl.skills import MissingCapability, report_progress, skill
from waldoctl.tools import GripperTool, ToolType

from waldo_commander.skills._motion import completed, validate_motion


def _gripper(rbt: RobotClient) -> GripperTool:
    try:
        tool = rbt.tool
    except RuntimeError as error:
        raise MissingCapability(
            "Select a supported gripper before running this skill"
        ) from error
    if tool.tool_type != ToolType.GRIPPER:
        raise MissingCapability("Select a supported gripper before running this skill")
    return cast(GripperTool, tool)


@skill(id="waldo.gripper_open", version="1.0.0", requires=frozenset({"tool.gripper"}))
async def gripper_open(rbt: RobotClient, *, timeout: float = 10.0) -> int:
    """Open the selected gripper and wait for its native command completion."""
    validate_motion(1.0, timeout)
    gripper = _gripper(rbt)
    report_progress("Opening gripper", fraction=0.0)
    index = await completed(rbt, gripper.open(wait=False), timeout, "Gripper open")
    report_progress("Open command completed", fraction=1.0)
    return index


@skill(id="waldo.gripper_close", version="1.0.0", requires=frozenset({"tool.gripper"}))
async def gripper_close(rbt: RobotClient, *, timeout: float = 10.0) -> int:
    """Close the selected gripper; completion does not imply a sensed grasp."""
    validate_motion(1.0, timeout)
    gripper = _gripper(rbt)
    report_progress("Closing gripper", fraction=0.0)
    index = await completed(rbt, gripper.close(wait=False), timeout, "Gripper close")
    report_progress("Close command completed", fraction=1.0)
    return index
