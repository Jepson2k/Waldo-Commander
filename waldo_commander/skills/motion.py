"""Small motion sequences composed from the supplied robot client."""

import math

from waldoctl.client import RobotClient
from waldoctl.skills import SkillError, report_progress, skill


@skill(id="waldo.retract", version="1.0.0", requires=frozenset({"motion.linear"}))
async def retract(
    rbt: RobotClient,
    *,
    distance_mm: float = 30.0,
    speed: float = 0.2,
    timeout: float = 30.0,
) -> int:
    """Move along positive tool Z and wait for completion.

    Check your tool orientation before using this direction as a withdrawal.
    The backend plans and collision-checks the linear move. Returns the completed
    command index; rejection and unconfirmed completion raise ``SkillError``.
    """
    if not math.isfinite(distance_mm) or distance_mm <= 0:
        raise ValueError("distance_mm must be finite and positive")
    if not math.isfinite(speed) or not 0 < speed <= 1:
        raise ValueError("speed must be finite and in (0, 1]")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    report_progress("Moving along tool Z", fraction=0.0)
    index = await rbt.move_l(
        [0.0, 0.0, distance_mm, 0.0, 0.0, 0.0],
        frame="TRF",
        rel=True,
        speed=speed,
        wait=False,
    )
    if index < 0:
        raise SkillError("Retract command was rejected or not acknowledged")
    if not await rbt.wait_command(index, timeout=timeout):
        confirmed = await rbt.stop() > 0
        raise SkillError(
            f"Retract completion was not confirmed; stop confirmed: {confirmed}"
        )
    report_progress("Retract completed", fraction=1.0)
    return index
