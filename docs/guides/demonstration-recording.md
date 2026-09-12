# Demonstration recording

The **Demonstrations** panel records controller-reported joint and tool
observations while you jog, hand-guide, or execute Python. Capture and **End
capture** send no motion commands. The editor's existing Record control continues
to record Python commands and completed skill calls.

Capture ends at its duration/sample limit, on your request, or when the controller
session, reference, enabled state, source mode, or tool identity changes. Save
the observations to keep them after Commander exits. Recordings default to
`~/.waldo-commander/recordings`; `WALDO_RECORDING_DIR` selects another directory.
The TCP transform is captured at the beginning; keep TCP settings fixed during
acquisition. Later TCP changes are not a recorded configuration timeline.

Select a span using zero-based indices and an exclusive end. The chart retains
gaps, and the gap table reports missing publications and long intervals. The
chart shows at most 5,000 observations from the selected span; export retains
all selected observations. Save and export preserve original timestamps.

## Python capture and storage

```python
import asyncio
from parol6 import AsyncRobotClient  # or par6.AsyncRobotClient
from waldo_commander.demonstrations import record_demonstration, save_demonstration

async def capture():
    async with AsyncRobotClient() as rbt:
        recording = await record_demonstration(rbt, duration_s=30)
        save_demonstration("demonstration.json", recording)

asyncio.run(capture())
```

An optional `asyncio.Event` passed as `stop=` ends acquisition. Capture needs an
enabled, referenced controller with the `observation.timed` capability. It never
opens another connection, enables drives, references the robot, or changes tools.
The maximum recording contains 100,000 samples; JSON loading is bounded to 64 MiB.

Each observation includes:

- Controller session ID, publication sequence, and monotonic snapshot timestamp.
- Host delivery timestamp, including the recorder's scheduling delay.
- Observed joints in degrees and controller-reported tool identity, positions,
  state, channels, engagement, part-detection flag and fault code.

Controller and host timestamps use different clocks. Differences within a clock
describe cadence; subtracting the two clocks does not measure network latency.
The controller timestamp identifies its snapshot/publication, without claiming
that every sensor was acquired simultaneously. Tool positions and grasp flags
retain the backend's reporting semantics; some tools report commanded state.

## Convert to a program

**Convert to program** turns the selected span into an ordinary Python program
in a new editor tab. The arm holding still is what separates the moves: each
still span of at least 0.3 s becomes an `rbt.delay`, a gripper position that
changed in one becomes `rbt.tool.set_position`, and the motion between them
becomes a single `rbt.move_l` where the tool travelled in a straight line, or
the joint waypoints that hold its path otherwise, blended so the arm does not
stop at each one. Each move carries the recorded leg's duration, so the program
keeps the demonstration's pace as far as the configured limits allow. A hold at
the start or end of the capture becomes a comment rather than a delay: it is
when you started and stopped recording, not something the arm was asked to do.

Every motion span is planned in the backend's preview and compared against the
recorded path before it is written: within 5 mm of tool position, 2° of tool
orientation, and 2° on every joint. The posture is compared as well as the
path, because a Cartesian move can trace the same line through a flipped wrist
and sweep the cell differently. A span that fails both forms becomes a
`replay_demonstration` call over its sample range, so a converted program can
be part generated moves and part replayed observations; that needs the span
saved first, and the panel says so. The result message reports the worst
deviation and any replayed ranges.

Read the program before running it. Its first statement moves the arm to the
demonstration's starting position at 10 % speed from wherever the arm is.

```python
from waldo_commander.demonstrations import load_demonstration, to_program

recording = load_demonstration("demonstration.json")
conversion = to_program(recording, robot, source_path="demonstration.json")
print(conversion.summary())
open("picked.py", "w").write(conversion.source)
```

## Replay

```python
from parol6 import RobotClient  # or par6.RobotClient
from waldo_commander.demonstrations import load_demonstration
from waldo_commander.skills import replay_demonstration

recording = load_demonstration("demonstration.json")
# Optionally select an uninterrupted portion, preserving its timestamps:
# recording = recording.select(20, 80)

with RobotClient() as rbt:
    result = replay_demonstration(rbt, recording)
    print(result.completed_samples)
```

Move to the first recorded position yourself before invoking replay. The start
must match within 0.5 degrees, with no existing motion/queue, an enabled and
referenced controller, and the recorded backend, simulator/hardware source,
selected tool/variant and TCP. Replay does not approach the start, select a tool,
apply a TCP, reset a controller or home it. After a controller restart, explicitly
reference and reconcile the physical scene before passing
`reconciled_session=True`. A recording never authorizes automatic continuation.

Replay is **point-to-point and stops at every observed waypoint**. Each original
interval is requested as the minimum duration of an ordinary native `move_j`;
the selected native profile lengthens it as needed for its motion limits.
Identical consecutive joint observations become native delays. Settling and
command overhead add time, so dense recordings can replay much more slowly.
This does not reconstruct the continuous demonstrated path between observations.
The backend's normal planner, soft limits, collision checks, speed override and
pause behavior remain in force. Replay refuses gaps; select an uninterrupted
span instead of guessing the missing motion.

`replay_gripper=True` additionally issues normalized observed gripper positions
at waypoint boundaries and waits for each command. It supports coupled positions
through the selected gripper's existing `set_position` implementation: pneumatic
grippers retain their binary position behavior. Tool changes are sequential,
without synchronization to motion within an interval. Recorded part-detection
and engagement flags are never used as fresh grasp confirmation. Other tools
remain observable but have no automatic action replay.

Use `await replay_demonstration.async_call(rbt, recording)` with an async client.
The same skill works in standalone Python and Commander. Its per-command
`timeout` defaults to 30 seconds. Managed Commander pauses preserve the remaining
completion budget; standalone waits use wall-clock time. Controller/session loss,
tool changes and stale status terminate replay. The observation watchdog remains
active during a debug pause, with a default two-second freshness deadline.

**Insert replay call** requires the selected uninterrupted span to be saved.
It inserts a file load and ordinary Python skill call; edit that Python normally.
Inspect its normal program preview before execution. Preview uses the current
simulated start and the tool/TCP configured by the Python program, and runs the
same native waypoint planning. It does not certify live readiness or recreate
recorded sensor verdicts. The panel's observed-data chart and the program's
planned trajectory describe different things.
