"""Small motion sequences composed from the supplied robot client."""

import math
from typing import Literal, cast

import numpy as np

from waldoctl.client import RobotClient
from waldoctl.setup import Pose, PoseValues, SetupSnapshot
from waldoctl.skills import SkillError, report_progress, skill

from waldo_commander.skills._motion import completed, validate_motion


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
    validate_motion(speed, timeout)
    report_progress("Moving along tool Z", fraction=0.0)
    index = await rbt.move_l(
        [0.0, 0.0, distance_mm, 0.0, 0.0, 0.0],
        frame="TRF",
        rel=True,
        speed=speed,
        wait=False,
    )
    await completed(rbt, index, timeout, "Retract")
    report_progress("Retract completed", fraction=1.0)
    return index


@skill(id="waldo.approach", version="1.0.0", requires=frozenset({"motion.linear"}))
async def approach(
    rbt: RobotClient,
    *,
    target: Pose,
    clearance_mm: float = 30.0,
    speed: float = 0.2,
    timeout: float = 30.0,
) -> int:
    """Move to positive target-tool-Z clearance, then linearly to the WRF target.

    Both legs pass through the native planner and collision checks. A refused
    leg stops the sequence; this skill does not search for a detour.
    """
    validate_motion(speed, timeout)
    if target.frame != "WRF":
        raise ValueError("Resolve the target to WRF with your setup snapshot first")
    if not math.isfinite(clearance_mm) or clearance_mm <= 0:
        raise ValueError("clearance_mm must be finite and positive")
    before = target.matrix()
    before[:3, 3] += before[:3, 2] * clearance_mm
    report_progress("Moving to approach clearance", fraction=0.0)
    await completed(
        rbt,
        await rbt.move_l(Pose.from_matrix(before).as_list(), speed=speed, wait=False),
        timeout,
        "Approach clearance",
    )
    report_progress("Moving to target", fraction=0.5)
    index = await completed(
        rbt,
        await rbt.move_l(target.as_list(), speed=speed, wait=False),
        timeout,
        "Approach target",
    )
    report_progress("Approach completed", fraction=1.0)
    return index


@skill(id="waldo.park", version="1.0.0", requires=frozenset({"motion.joint"}))
async def park(
    rbt: RobotClient,
    *,
    setup: SetupSnapshot,
    name: str = "park",
    speed: float = 0.2,
    timeout: float = 30.0,
) -> int:
    """Joint-interpolate to a named park pose from the explicitly supplied setup."""
    validate_motion(speed, timeout)
    target = setup.resolve(name)
    report_progress(f"Moving to {name}", fraction=0.0)
    index = await completed(
        rbt,
        await rbt.move_j(pose=target.as_list(), speed=speed, wait=False),
        timeout,
        "Park",
    )
    report_progress("Park completed", fraction=1.0)
    return index


@skill(
    id="waldo.align_tool_axis", version="1.0.0", requires=frozenset({"motion.linear"})
)
async def align_tool_axis(
    rbt: RobotClient,
    *,
    direction: tuple[float, float, float] = (0.0, 0.0, 1.0),
    tool_axis: Literal["x", "y", "z"] = "z",
    speed: float = 0.1,
    timeout: float = 30.0,
) -> int | None:
    """Align one tool axis with a WRF direction while retaining the TCP position.

    Uses the shortest rotation; the antiparallel case rotates about the next
    current tool axis. Returns None when already aligned, otherwise the completed
    command index. Native limits may refuse the requested orientation.
    """
    validate_motion(speed, timeout)
    vector = np.asarray(direction, dtype=float)
    if (
        vector.shape != (3,)
        or not np.isfinite(vector).all()
        or not 1e-12 < np.linalg.norm(vector) < float("inf")
    ):
        raise ValueError("direction must be three finite values with nonzero length")
    if tool_axis not in ("x", "y", "z"):
        raise ValueError("tool_axis must be x, y or z")
    observed = await rbt.pose()
    if observed is None:
        raise SkillError("No current TCP pose is available")
    transform = Pose(cast(PoseValues, tuple(observed))).matrix()
    axis_index = ("x", "y", "z").index(tool_axis)
    current = transform[:3, axis_index]
    desired = vector / np.linalg.norm(vector)
    dot = float(np.clip(current @ desired, -1, 1))
    if dot > 1 - 1e-12:
        report_progress("Tool axis already aligned", fraction=1.0)
        return None
    if dot < -1 + 1e-10:
        perpendicular = transform[:3, (axis_index + 1) % 3]
        rotation = 2 * np.outer(perpendicular, perpendicular) - np.eye(3)
    else:
        x, y, z = np.cross(current, desired)
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        rotation = np.eye(3) + skew + skew @ skew / (1 + dot)
    transform[:3, :3] = rotation @ transform[:3, :3]
    report_progress("Aligning tool axis", fraction=0.0)
    index = await completed(
        rbt,
        await rbt.move_l(
            Pose.from_matrix(transform).as_list(), speed=speed, wait=False
        ),
        timeout,
        "Tool-axis alignment",
    )
    report_progress("Tool axis aligned", fraction=1.0)
    return index
