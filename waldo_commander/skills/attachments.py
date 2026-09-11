"""Explicit collision-model declarations through the supplied robot client."""

from waldoctl.client import RobotClient
from waldoctl.shapes import Pose6, Shape, ShapeWorld
from waldoctl.skills import SkillError, report_progress, skill


async def _world(
    rbt: RobotClient, name: str, draft: Shape | None
) -> tuple[ShapeWorld, Shape]:
    world = await rbt.shapes()
    if world is None or not world.attachment_epoch:
        raise SkillError("Attachment context readback is unavailable")
    shape = next((s for s in world.program if s.name == name), None)
    if shape is None and draft is not None and draft.name == name:
        # A declaration the backend refused (a stale epoch from a saved
        # world, say) exists only as the caller's draft; it is still what
        # is being reconciled.
        shape = draft
    if shape is None:
        raise ValueError(f"No program shape named {name!r}")
    return world, shape


async def _apply(rbt: RobotClient, world: ShapeWorld, shape: Shape) -> Shape:
    program = tuple(shape if s.name == shape.name else s for s in world.program)
    if all(s.name != shape.name for s in program):
        program = (*program, shape)
    if await rbt.set_shapes(list(program)) != 1:
        raise SkillError("Attachment declaration was not confirmed")
    applied = await rbt.shapes()
    if applied is None or applied.program != program:
        raise SkillError("Attachment declaration readback does not match")
    if shape.attachment is not None and not applied.attachments_valid:
        raise SkillError("Attachment context changed during application")
    report_progress("Collision-model declaration confirmed", fraction=1.0)
    return shape


@skill(
    id="waldo.attach_object", version="1.0.0", requires=frozenset({"world.attachments"})
)
async def attach_object(
    rbt: RobotClient,
    *,
    name: str,
    flange_pose: Pose6,
    allowed_contacts: tuple[str, ...] = (),
    shape: Shape | None = None,
) -> Shape:
    """Declare an existing program shape held at a flange-relative pose.

    The pose uses metres and radians, extrinsic XYZ, independently of the TCP.
    Exact collision-report names exempt only this shape's selected partners.
    This explicit call also reconciles an old declaration against the current
    controller context. Verify the physical scene before doing so; no grasp,
    payload, reference, or tool action is inferred. The backend requires idle
    motion and fresh referencing. Other program shapes are retained. ``shape``
    supplies the declaration when the backend does not hold it (a refused
    draft); it must carry ``name``.
    """
    world, shape = await _world(rbt, name, shape)
    return await _apply(
        rbt,
        world,
        shape.attach(
            flange_pose=flange_pose,
            epoch=world.attachment_epoch,
            allowed_contacts=allowed_contacts,
        ),
    )


@skill(
    id="waldo.detach_object", version="1.0.0", requires=frozenset({"world.attachments"})
)
async def detach_object(
    rbt: RobotClient, *, name: str, world_pose: Pose6, shape: Shape | None = None
) -> Shape:
    """Declare a held shape fixed at an explicit world pose (metres/radians).

    Removes its allowed-contact exemptions and confirms readback. This does
    not release the gripper or assert where a physical object came to rest.
    ``shape`` supplies a declaration the backend refused and never held.
    """
    world, shape = await _world(rbt, name, shape)
    if shape.attachment is None:
        raise ValueError(f"Shape {name!r} is not attached")
    return await _apply(rbt, world, shape.detach(world_pose=world_pose))
