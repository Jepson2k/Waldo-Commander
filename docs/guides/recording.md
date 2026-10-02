# Recording

The editor's **Record** button turns what you do with the arm into Python in
the open program. While it is on, every action taken through Commander is
written below the recording cursor as it happens: jogs become `move_j` and
`move_l` calls, gripper and I/O actions become their commands, a skill
inserted from the **Insert Command** menu becomes its call (with the imports
and setup load it needs at the top of the program, part of the same take),
and the time you wait between actions becomes a delay. Filling in the
call's fields does not count as waiting. Selecting that call and choosing **Run
selection** runs it live without writing it again.

Motion that Commander did not command is recorded too. On an arm that can be
hand-guided, put it in freedrive and move it; on any backend, moves sent by
another client or by MCP count the same way. The recorder watches the
controller's status stream while recording is on. The joints or the gripper
moving while no Commander action is under way open a span; the arm standing
still for half a second closes it. The span is converted to ordinary moves in
the background, its place in the program marked *converting…* meanwhile, and
written there like any other recorded action. Stopping or keeping a take while
the arm is still moving keeps the motion up to that moment.

Manual gripper drags keep the latest target and record one final position.
Tool actions requested during a jog are recorded directly after its completed
move. Their order comes from the shared command queue; the recorder adds no
mid-move delay or nonblocking move to recreate overlap. Idle gaps between
separate manual actions are still retained.

## Keeping a take

The lines a recording writes stay marked in the editor, tinted orange, until
you decide. The editor's header shows how many lines the take wrote with
**Keep** and **Undo** where Open and Save usually are. Stopping
the recording does not decide: stop, play the program to watch the arm do it,
then keep the lines or undo them all. Keep or Undo while still recording also
stops it, and starting a new recording keeps the last take.

To record part of a program again, select its lines and press **Record**. The
take starts where the arm is, in place of the selected lines, and Undo puts
them back as they were. Selecting the lines you just recorded and pressing
Record again is a retake.

A captured span carries a badge on its first line saying what it became. With
the cursor in it, the header also offers **Moves** or **Raw**: moves are the
planned conversion below, and raw replays the recorded points from the saved
recording, with its waits and gripper positions kept as statements.

## How captured motion becomes code

The arm holding still is what separates the moves. Each still span of at
least 0.3 s becomes an `rbt.delay`, a gripper position change becomes
`rbt.tool.set_position` where it happened, and the motion between them
becomes a single `rbt.move_l` where the tool travelled in a straight line, or
the joint waypoints that hold its path otherwise, blended so the arm does not
stop at each one. Each move carries the recorded leg's duration, so the program keeps
the demonstration's pace as far as the configured limits allow.

Every motion span is planned in the backend's preview and compared against
the recorded path before it is written: within 5 mm of tool position, 2° of
tool orientation, and 2° on every joint. The posture is compared along the
whole path, in order, as well as the tool position, because a Cartesian move
can trace the same line through a flipped wrist, or skip a wrist swing that
barely moves the tool, and sweep the cell differently. Where the status stream
skipped publications, a planned `move_j` marked `# not observed` crosses the
stretch nobody saw. A span that fails both forms is replayed
instead: the recording is saved under the recordings directory, named after
the program, and the lines call `replay_demonstration` over that sample range.
The span's badge says how many moves were replayed.

Recordings default to `~/.waldo-commander/recordings`; `WALDO_RECORDING_DIR`
selects another directory.

## Recording from Python

The same capture is available without the editor. It needs an enabled,
referenced controller and never sends a motion command of its own.

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

An optional `asyncio.Event` passed as `stop=` ends acquisition. Capture ends at
its duration or sample limit, on request, or when the controller session,
reference, enabled state, source mode or tool identity changes. The maximum
recording holds 100,000 samples; loading is bounded to 64 MiB.

Each sample carries the controller session, publication sequence and snapshot
timestamp, the host delivery timestamp, the joints in degrees, and the
controller-reported tool identity, positions, state, channels, engagement,
part-detection flag and fault code. Controller and host timestamps use
different clocks: differences within a clock describe cadence, and subtracting
one clock from the other does not measure latency. The TCP transform is
captured at the beginning; keep TCP settings fixed during acquisition.

`to_program` converts a saved recording into a complete program, with an
approach to its first position; `span_to_lines` gives the lines the editor
inserts, for a span that continues a program already at the recording's start.

```python
from waldo_commander.demonstrations import load_demonstration, to_program

recording = load_demonstration("demonstration.json")
conversion = to_program(recording, robot, source_path="demonstration.json")
print(conversion.summary())
open("picked.py", "w").write(conversion.source)
```

Read a converted program before running it. Its first statement moves the arm
to the demonstration's starting position at 10 % speed from wherever it is.

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
must match within 0.5 degrees, with no existing motion or queue, an enabled and
referenced controller, and the recorded backend, simulator or hardware source,
selected tool and TCP. Replay does not approach the start, select a tool, apply
a TCP, reset a controller or home it. After a controller restart, reference and
reconcile the physical scene before passing `reconciled_session=True`.

Replay is point-to-point and stops at every observed waypoint. Each original
interval is requested as the minimum duration of an ordinary `move_j`, and the
selected motion profile lengthens it as needed. Identical consecutive
observations become delays. Settling and command overhead add time, so dense
recordings replay more slowly than they were made. The backend's planner, soft
limits, collision checks, speed override and pause behaviour remain in force.
Replay refuses gaps; select an uninterrupted span instead of guessing the
missing motion.

`replay_gripper=True` additionally issues the observed gripper positions at
waypoint boundaries and waits for each. Recorded part-detection and engagement
flags are never used as fresh grasp confirmation. Use
`await replay_demonstration.async_call(rbt, recording)` with an async client.
Its per-command `timeout` defaults to 30 seconds; managed Commander pauses
preserve the remaining budget, standalone waits use wall-clock time.
