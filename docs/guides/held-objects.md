# Held-object geometry

An attachment declares that a program shape moves with the robot flange.
PAR6 and PAROL6 use it for collision checking during planned and streamed
motion. Commander draws the same flange-relative geometry in the 3D view.

Right-click a program shape and choose **Attach to Flange**. Enter its position
and orientation relative to the flange, in millimetres and degrees. Existing
attachments offer **Reconcile Attachment** and **Detach to World**. Detachment
requires an explicit world pose. Application waits for controller confirmation;
an invalid contact name or a busy controller leaves the applied world unchanged.

These are model declarations. They do not operate the gripper, confirm a grasp,
estimate payload, or assert where a released physical object actually rests.
Attached shapes must have collision checking enabled and cannot declare
`physics` in this API version.

## Python

Use the same functions from an ordinary Python program or Commander:

```python
from waldo_commander.skills import attach_object, detach_object

# "part" must already exist in the program collision world.
attach_object(
    rbt,
    name="part",
    flange_pose=(0, 0, 0.12, 0, 0, 0),
    allowed_contacts=("shape:fixture",),
)

# Issue normal motion commands here, subject to normal collision checks.

detach_object(rbt, name="part", world_pose=(0.4, 0.2, 0.1, 0, 0, 0))
```

The Python geometry poses use **metres and radians**, with extrinsic XYZ
rotation (`Rz @ Ry @ Rx`). They are independent of the selected tool's TCP
offset. The flange is `L6` on PAROL6 and `gripper` on PAR6. Async programs use
`await attach_object.async_call(rbt, ...)` and `await detach_object.async_call(...)`.
Both skills require `world.attachments`, use the supplied client, retain the
other program shapes, and verify readback. They raise on refusal or unconfirmed
application. The controller requires idle motion and a fresh position reference
when applying attachments.

Allowed contacts are exact names from collision reports: URDF link names,
`shape:name`, `install:name`, and tool geometry as `tool:KEY:role` — the
selected tool's key and the part that reported, for example
`tool:SSG48:moving`. A name the controller does not know is refused with the
list it does know.
Only pairs involving the declaring attached shape are exempted. Wildcards,
unknown partners, self names, duplicates, and more than 32 partners are refused.
Unrelated robot and fixture collision checks remain active.

## Context changes

`rbt.shapes()` returns `attachment_epoch` and `attachments_valid`. Controller
restart, reference loss, source changes and tool changes invalidate attachment
assumptions. Arm motion is refused until the old declarations are removed or
explicitly reconciled. Commander shows stale attachments in amber and identifies
them in the context menu.

After checking the physical scene and restoring the required reference, call
`attach_object` again with the verified pose and contacts. For several stale
attachments, reconcile the complete set in one `set_shapes` call:

```python
world = rbt.shapes()
verified = [
    shape.attach(
        flange_pose=shape.pose,
        epoch=world.attachment_epoch,
        allowed_contacts=shape.attachment.allowed_contacts,
    ) if shape.attachment else shape
    for shape in world.program
]
assert rbt.set_shapes(verified) == 1
assert rbt.shapes().attachments_valid
```

Use that example only after verifying **every** retained attachment. Saved
world files retain declarations but never restore a fresh controller context.
Import does not authorize attachment or motion after power loss.

Planning preview copies the submitted tool and world into an isolated client.
It binds valid submitted attachments to that preview's own context and refuses
stale submissions. It does not renew live controller declarations. Calls made
by the program itself must still use the preview's current context, just as
live programs must use the live controller's current context.
