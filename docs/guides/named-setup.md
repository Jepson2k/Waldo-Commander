# Named setup data

The **Setup** tab stores static frames, poses and scalar parameters. Create a
frame by entering its translation and roll/pitch/yaw relative to its parent,
or use **Use current TCP** to capture the current tool pose. In the Poses tab,
choose a frame and capture or enter a pose in that frame. Use **Set** to update
the working snapshot and **Save** to persist it under a name.

Translations use millimetres; angles use degrees, with intrinsic XYZ
(`Rx(roll) · Ry(pitch) · Rz(yaw)`), matching the robot clients' numeric poses.
Shape definitions have their own documented rotation convention. A frame's parent can be WRF or another saved
frame. Cycles, missing parents and non-finite values are rejected. TRF remains
the existing native relative-motion frame and cannot be a static setup frame.

```python
from parol6 import RobotClient
from waldo_commander.setup import load_setup

setup = load_setup("bench")
with RobotClient() as rbt:
    rbt.move_l(setup.resolve("pick").as_list(), speed=0.2)
```

`resolve` produces a normal WRF numeric pose. The same function runs in preview,
stepping and standalone Python; the backend still plans and checks every move.
Updating the `fixture` frame updates all poses expressed in that frame on the
next load, including poses in child frames. Existing loaded objects keep their
values. A second `load_setup` explicitly loads the current saved revision.

**Insert load call** inserts the named load at the start of the active stopped
program. **Export snapshot** downloads Python containing the fixed values,
usable without access to the saved setup. Saving setup alone never edits code.

Programs can also create snapshots directly:

```python
from waldoctl.setup import Frame, Parameter, Pose, SetupSnapshot

setup = SetupSnapshot(
    frames={"fixture": Frame((100, 200, 0, 0, 0, 90))},
    poses={"pick": Pose((10, 0, 30, 0, 0, 0), frame="fixture")},
    parameters={"clearance": Parameter(30.0, unit="mm")},
)
pick_wrf = setup.resolve("pick").as_list()
clearance = setup.parameters["clearance"].value
```

The default directory is `~/.waldo-commander/setups`. Set `WALDO_SETUP_DIR` or
pass `directory=...` to `load_setup`/`SetupStore` to select another directory.
Commander passes its directory to launched scripts and isolated previews.
Files use a versioned JSON format and atomic replacement. Corrupt or
unsupported snapshots fail explicitly. Loading or saving setup issues no
motion and does not apply robot configuration.

## TCP position calibration and orientation teaching

In **Setup → TCP**, keep the same physical tip touching one stationary point and
capture at least four poses with varied wrist orientations. Each capture reads
the referenced, stationary arm and removes any existing user TCP correction.
**Solve position** estimates the tip translation and reports RMS and maximum
sample error. A single orientation or inconsistent captures are rejected.
The position solve leaves the displayed orientation unchanged.

To teach orientation, align the physical tool with the axes of WRF or a named
setup frame, select those reference axes, and choose **Teach orientation**.
This changes only roll/pitch/yaw. Both operations use millimetres and intrinsic
XYZ degrees relative to the registered tool. You can also enter all six values
manually after **Read applied** identifies the active tool.

**Set calibration** adds the displayed values to the working setup; **Save**
persists them. Saved entries show measurement provenance and their tool/variant
binding. Saving does not configure the robot or edit the program. **Apply to
controller** explicitly queues the displayed transform, waits for completion,
and checks readback before updating the scene. A different tool or variant is
refused. A disconnected capture session discards its unsaved samples.

Programs apply saved data explicitly:

```python
from parol6 import RobotClient  # or: from par6 import RobotClient
from waldo_commander.setup import load_setup

calibration = load_setup("bench").tcp_calibrations["tip"]
with RobotClient() as rbt:
    # Select the matching physical tool and variant before applying its data.
    index = rbt.set_tcp_transform(*calibration.values)
    if not rbt.wait_command(index):
        raise RuntimeError("TCP application was not confirmed")
    applied = rbt.tcp_transform()
```

The legacy XYZ setter remains available and clears user rotation. Changing the
tool/variant or resetting the controller clears the applied correction. Native
FK, preview and motion share the correction; collision meshes remain attached
to the physical tool links. A calibrated tip does not replace the tool's
physical geometry model.

## Named device signals

Use **Setup → Signals** to name an existing input or output. Select its bank,
zero-based channel, and polarity. For example, channel 0 is **OUTPUT 1** in the
I/O panel. Unchecking **Active high** makes a low electrical level mean `True`.
**Set mapping** changes the current setup; **Save** persists it. Neither action
writes an output. **Read** queries the controller; **Write output** writes the
displayed logical value and waits for the controller to report it.

A mapping records its backend and input/output bank sizes. A loaded mapping
keeps that binding until **Use current robot** explicitly replaces it. A
mismatched backend or layout is refused, and the E-stop status bit cannot be
mapped as an ordinary signal.

```python
from parol6 import RobotClient  # or: from par6 import RobotClient
from waldo_commander.setup import load_setup
from waldo_commander.skills import read_signal, wait_signal, write_signal

setup = load_setup("bench")
with RobotClient() as rbt:
    ready = wait_signal(rbt, setup.signals["part_ready"], timeout=5.0)
    if ready.outcome == "matched":
        write_signal(rbt, setup.signals["valve"], True, timeout=2.0)
    else:
        print("Part did not arrive before the deadline")
```

Async programs use `await wait_signal.async_call(async_rbt, ...)`, and the same
pattern for reads and writes. The **Skills** tab can select a saved mapping and
insert a call containing its fixed values. Editing the setup later does not
change that inserted snapshot.

`read_signal` returns a logical value, a host receipt timestamp, and its source.
`wait_signal` returns `matched` or `timeout` with the latest observation and
elapsed time; it reads the controller's status broadcast rather than asking for
I/O, so a level is seen on the tick it is published and there is no poll
interval to tune. A controller that broadcasts nothing raises `ConnectionError`
instead of reporting a timeout it could not tell apart from a level that never
arrived; a rejected command or an unconfirmed output write raises an error. Controller output readback confirms
the reported electrical level, not that an attached actuator moved or gripped.
Cancelling a skill requests the backend's existing Stop behavior and prevents
further commands from that invocation; it does not undo an output write.

Preview requires explicit observations:

```python
from waldo_commander.skills import SignalFixture, wait_signal

result = wait_signal(
    rbt, setup.signals["part_ready"], timeout=5.0,
    fixture=SignalFixture(False),
)
```

A fixture supplies a constant logical value. If it cannot match the requested
value, preview advances by the wait duration and takes the timeout branch. No
fixture means an unresolved preview. Live clients refuse fixtures.
