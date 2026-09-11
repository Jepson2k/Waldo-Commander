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

Saving writes `programs/setups/<name>.py`: an ordinary Python module holding
one `SetupSnapshot` literal, the way the recorder writes programs. Copy it,
diff it, keep it under version control with the programs that use it, and
load it either way:

```python
from parol6 import RobotClient
from waldo_commander.setup import load_setup

setup = load_setup("bench")            # imports programs/setups/bench.py
with RobotClient() as rbt:
    rbt.move_l(setup.resolve("pick").as_list(), speed=0.2)
```

```python
from setups.bench import setup         # the same module, imported directly
```

The direct import works wherever `programs/` is on the import path: a
program run from Commander, or `python programs/pick.py` from that folder.
`load_setup` also works from the in-process preview, so prefer it in
programs that must preview and run unchanged.

A setup saved as JSON by an earlier release, including the old
`~/.waldo-commander/setups` store, is converted to its `.py` twin the first
time the store is opened, and the JSON is removed.

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
elapsed time. Missing replies raise `ConnectionError`; a rejected command or an
unconfirmed output write raises an error. Controller output readback confirms
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

## Camera calibration

The **Hand-Eye Calibration** panel supports **Camera on tool** and **Fixed
camera** placement. For a tool camera, keep the printed ChArUco board fixed in
the workspace. For a fixed camera, attach the board rigidly to the tool and
leave the camera stationary. The fixed-camera controls can list and start a
video device independently of a tool's camera assignment.

Reference the arm, hold it stationary, and capture at least four views with
rotation about multiple wrist axes; 10–15 diverse views are preferable. Capture
waits for a subsequent camera frame and reads the controller's tool/TCP binding.
Changing the camera session, tool, TCP transform or image dimensions requires
clearing the sample set. The timestamp is host receipt time, not hardware
exposure time: this acquisition workflow requires stationary observations.

**Solve** estimates pinhole intrinsics and the camera transform. A tool camera
is expressed relative to the current TCP; a fixed camera is expressed in WRF.
Both report reprojection error, rotational/translational residuals and board
position spread. Review those measurements against the accuracy your task needs.
The fixed-camera case uses inverse robot poses with OpenCV's
[hand-eye calibration solver](https://docs.opencv.org/4.8.0/d9/d0c/group__calib3d.html).

In **Saved camera data**, select the setup and camera name before pressing
**Save**. A fixed camera can be expressed in a static named frame already in
that setup; saving records the frame's WRF transform as its reference.
**Load / check** reads the saved measurement and checks it against the current
camera and controller. **Export snapshot** downloads ordinary Python containing
the setup's fixed values. Saving, loading and exporting issue no robot motion
or configuration commands.

```python
from waldo_commander.setup import load_setup

setup = load_setup("bench")
camera = setup.cameras["overhead"]
K = camera.intrinsics.camera_matrix  # flat row-major 3×3, pixels
size = camera.intrinsics.image_size  # width, height
quality = camera.quality
```

`CameraCalibration.validate` takes the setup plus the observed `camera_id`,
`image_size`, robot `backend`, and (for tool cameras) a current `TcpCalibration`.
`world_pose` accepts the same context and the image's WRF `tcp_pose` for tool
cameras. It returns a `Pose` after validation. Camera optical axes are +X right,
+Y down and +Z forward; translations use millimetres. A tool camera's stored
pose has frame `TCP`; use `world_pose` to resolve it before motion calculations.

Tool, variant and all six TCP components must match a tool-mounted calibration.
Fixed cameras remain usable across tool changes but reject a changed reference
frame, including changes to its parents. Changed camera source, resolution or
backend is rejected in either mode. These checks use the explicitly supplied
snapshot and observations; a running program's loaded setup remains immutable.

The camera service provides `snapshot(max_age_s=...)` and bounded
`next_snapshot(timeout_s=...)` for acquisition in the app process. Observations
include JPEG bytes, host receipt time, source identity, sequence and capture
session. Inactive, stale and missing images raise `CameraUnavailable`; display
placeholders are never returned as observations.

The built-in source identity fingerprints the configured device and requested
capture dimensions without exporting the device string. Replacing a camera at
the same device index, moving its mount, changing its lens/focus or moving the
robot base may be invisible to software. Recalibrate after those changes.

Existing per-tool measurements remain available in the panel. **Import existing
hand-eye measurement** requires confirmation of the same physical camera, lens
and mount, then checks the recorded tool, TCP and resolution. Import binds the
original intrinsics, transform, date and quality measurements to the current
source and tool variant, and saves a named setup entry. The original measurement
is preserved.
