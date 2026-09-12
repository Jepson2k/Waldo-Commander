"""Backend-independent Python skills, usable without starting Commander."""

from waldo_commander.skills.motion import align_tool_axis, approach, park, retract
from waldo_commander.skills.gripper import gripper_close, gripper_open
from waldo_commander.skills.demonstrations import replay_demonstration
from waldo_commander.skills.attachments import attach_object, detach_object
from waldo_commander.skills.vision import locate_board
from waldo_commander.skills.transfer import transfer, transfer_with_signal
from waldo_commander.skills.signals import (
    SignalFixture,
    read_signal,
    wait_signal,
    write_signal,
)

__all__ = [
    "attach_object",
    "detach_object",
    "replay_demonstration",
    "transfer",
    "transfer_with_signal",
    "locate_board",
    "SignalFixture",
    "read_signal",
    "wait_signal",
    "write_signal",
    "align_tool_axis",
    "approach",
    "park",
    "retract",
    "gripper_close",
    "gripper_open",
]
