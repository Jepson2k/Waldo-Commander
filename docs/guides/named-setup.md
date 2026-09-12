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
