"""Completion checks shared by composed motion skills."""

import math

from waldoctl.client import RobotClient
from waldoctl.skills import SkillError


def validate_motion(speed: float, timeout: float) -> None:
    if not math.isfinite(speed) or not 0 < speed <= 1:
        raise ValueError("speed must be finite and in (0, 1]")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")


async def completed(rbt: RobotClient, index: int, timeout: float, label: str) -> int:
    if index < 0:
        raise SkillError(f"{label} command was rejected or not acknowledged")
    if not await rbt.wait_command(index, timeout=timeout):
        confirmed = await rbt.stop() > 0
        raise SkillError(
            f"{label} completion was not confirmed; stop confirmed: {confirmed}"
        )
    return index
