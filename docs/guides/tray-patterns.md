# Tray patterns and transfers

Generate tray poses in Python, then execute them with an ordinary loop. The
`transfer` skill approaches a pickup, closes the selected gripper, retracts,
approaches a placement, opens, and retracts again. The Skills panel can insert
or run one transfer using saved pickup and placement poses.

## Generate poses

`grid_poses` uses the origin's reference frame: columns along X, rows along Y,
and layers along Z. Columns vary first. `serpentine=True` reverses alternate
rows; negative pitch reverses an axis. TCP orientation stays fixed and does
not rotate the grid. Use a named setup frame for a tilted or rotated tray,
then resolve the generated poses to WRF before executing them.

```python
from waldo_commander.setup import load_setup
from waldo_commander.patterns import grid_poses

setup = load_setup("bench")
cells = grid_poses(
    setup.poses["tray_origin"],
    rows=3, columns=4,
    pitch_x_mm=25, pitch_y_mm=25,
    serpentine=True,
)
places = tuple(setup.resolve(pose) for pose in cells)
```

For an irregular pattern, use `offset_poses(origin, offsets_mm)`, passing an
iterable of `(x, y, z)` offsets in the same reference frame. These functions
generate data without connecting to a controller.

## Execute a loop

Begin with an empty, open tool and a referenced arm. Set up the appropriate
tool and its TCP before constructing the program's pose snapshots. This
example assumes the selected gripper is configured and ready:

```python
from parol6 import RobotClient  # use par6 for that backend
from waldo_commander.patterns import PatternProgress, save_progress
from waldo_commander.skills import gripper_open, transfer

progress = PatternProgress.for_poses(places)
pick = setup.resolve("pick")
with RobotClient() as rbt:
    gripper_open(rbt)
    for index in progress.pending():
        transfer(rbt, pick=pick, place=places[index], clearance_mm=20)
        progress = progress.mark(index)
        save_progress("tray-progress.json", progress, client=rbt)
```

Clearance is measured along **positive tool Z at each target**. The six
linear motion legs use the backend's normal planner and collision checks.
Choose pickup, placement, and clearance poses with clear connecting paths;
the helper does not plan a route around obstacles. Its return value is the
final confirmed command index. It does not infer a grasp, model a carried
part, or confirm placement. A failure or cancellation stops the sequence
without an automatic release or recovery move.

`transfer_with_signal` uses a named digital output instead of the selected
gripper API. Pass `grip=setup.signals["grip"]` and the logical closed value
as `closed_value`. Its output readback confirms the electrical state. Start
with the tool open. Preview requires explicit `open_fixture` and
`closed_fixture` values, each a `SignalFixture`; see [Python skills](skills.md).

Both transfers also support `await transfer.async_call(async_client, ...)`.
The normal preview and stepping instrumentation applies to the composed
motions and tool actions.

## Review saved progress

Progress is optional, separate from execution, and editable:

```python
from waldo_commander.patterns import load_progress

progress = load_progress("tray-progress.json", places)
progress = progress.mark(3, completed=False)
progress = progress.mark(4, completed=True)
print(progress.pending())
```

After an interruption, inspect the arm, gripper, and tray before choosing
indices to run. A saved entry records the program's note about an index; it
does not establish the current physical state or authorize continuation.
Loading progress never moves the arm. A changed resolved pose or pose order
rejects the saved file so it cannot silently skip cells in a different pattern.

`save_progress` writes atomically at the explicit path. Pass the same client
used by the loop: with a preview client it returns `False` and leaves any
existing file untouched. Normal execution returns `True` after saving.
