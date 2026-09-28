"""A skill that edits the collision world, installed by tests that need one.

It lives in its own small module because a skill preview runs in a worker
process, which imports the skill by name.
"""

from waldoctl import Box
from waldoctl.client import RobotClient
from waldoctl.skills import skill

from waldo_commander.skills import retract


@skill(id="test.fence_then_retract", version="1.0.0")
async def fence_then_retract(rbt: RobotClient) -> None:
    """Fence off a corner of the workspace, then retract."""
    await rbt.set_shapes(
        [
            Box(
                name="preview_fence",
                x=0.05,
                y=0.05,
                z=0.05,
                pose=(-0.4, -0.4, 0.05, 0, 0, 0),
            )
        ]
    )
    await retract.async_call(rbt, distance_mm=2.0)
