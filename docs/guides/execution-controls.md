# Execution speed and pause

The editor's speed button controls preview playback while previewing a program,
and the controller's queued motion speed while running it. Preview offers 0.5×,
1× and 2×. Live execution offers 0.5× and 1×; the Python API accepts any scale
from 0.1 through 1.0.

Execution speed belongs to the controller. It changes how quickly the controller
advances along an already planned trajectory. A command's `speed`, `accel` and
`duration` still determine that original plan. Jog and streamed servo commands
use their own parameters.

Override transitions use a separate rate ramp and acceleration checks. The
nominal motion profile's jerk ceiling is not guaranteed during a transition.

```python
from parol6 import RobotClient  # or: from par6 import RobotClient

with RobotClient() as rbt:
    if rbt.set_execution_speed(0.5) != 1:
        raise RuntimeError("Speed selection was not confirmed")
    rbt.pause()
    rbt.set_execution_speed(0.3)  # remains paused
    state = rbt.execution_speed()
    print(state.resume_scale, state.applied_scale, state.paused)
    rbt.resume()                 # resumes at the selected 0.3 scale
```

Use `pause()` and `resume()` explicitly. Zero is rejected by
`set_execution_speed()`. Pause retains queued motion and decelerates an active
trajectory to a hold. Stop discards queued motion. Changing the selected speed
while paused never resumes execution.

The execution controls return `1` when the requested state is confirmed, or `0`
when confirmation times out; controller refusals raise. A confirmed pause request
can still be decelerating. Fresh `execution_speed()` readback separates
`target_scale`, `applied_scale` and the retained positive `resume_scale`.
`state.paused` becomes true when the applied scale reaches zero.

Queued delays retain their remaining duration during a pause. Positive speed
overrides do not speed up or slow down delays, tool actuators or homing routines.

## Managed programs and standalone Python

Commander Pause holds native queued motion and cooperatively gates instrumented
program calls. Managed command and skill completion waits exclude time spent
under that debug pause. Resume continues with the remaining timeout budget; it
does not start a new timeout. Single stepping retains the same behavior through
nested skills and blended commands.

This cooperation does not suspend arbitrary Python code, other threads, network
operations or user-created timers. Communication, heartbeat, fault and stale
status deadlines continue to use wall-clock time.

A standalone client or REPL sends the same backend pause request. Its Python
waits retain their ordinary wall-clock deadlines: `wait_command()` returns false
on timeout, and blocking motion calls raise `TimeoutError`. A timed-out wait does
not discard the queued motion. Use Stop when cancellation is intended.

## Preview

An explicit execution-speed selection in a program changes the previewed motion
duration. Planning previews report paused queued operations as
`UnresolvedPreview`; they cannot invent an external Resume action. The 2× editor
setting changes preview playback only.
