"""Completion checks shared by composed motion skills."""

import math
from collections.abc import Awaitable

from waldoctl.client import RobotClient
from waldoctl.skills import SkillError

from waldo_commander.services.completion_budget import completion_scope


def validate_motion(speed: float, timeout: float) -> None:
    if not math.isfinite(speed) or not 0 < speed <= 1:
        raise ValueError("speed must be finite and in (0, 1]")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")


async def completed(
    rbt: RobotClient, dispatch: Awaitable[int], timeout: float, label: str
) -> int:
    try:
        with completion_scope(timeout) as budget:
            index = await dispatch
            if index < 0:
                raise SkillError(f"{label} command was rejected or not acknowledged")
            if budget.confirmed_index != index:
                remaining = budget.remaining
                if remaining <= 0 or not await rbt.wait_command(
                    index, timeout=remaining
                ):
                    raise TimeoutError("Completion deadline expired")
            return index
    except TimeoutError as error:
        confirmed = await rbt.stop() > 0
        raise SkillError(
            f"{label} completion was not confirmed; stop confirmed: {confirmed}"
        ) from error
