# Python skills

For pose grids, transfer helpers, and editable completion notes, see
[Tray patterns](tray-patterns.md).

For observed motion capture and native waypoint replay, see
[Recording](recording.md).

For attachment declarations and scoped collision contacts, see
[Held-object geometry](held-objects.md).

Skills are reusable Python functions. Write one typed async implementation and
call it from either a synchronous program or an async program. The supplied
robot client owns the connection and command execution.

## Use an installed skill

```python
from parol6 import RobotClient
from waldo_commander.skills import retract

with RobotClient() as rbt:
    retract(rbt, distance_mm=30, speed=0.2)
```

`retract` moves along **positive tool Z**, which depends on the current tool
orientation. It waits for completion and raises on rejection or an unconfirmed
completion. Preview the direction and starting pose before running it.

In an async program, use `await retract.async_call(rbt, distance_mm=30)` with
an async client. Start an async program explicitly with `asyncio.run(main())`,
just as when running its Python file directly. A function definition by itself
does not execute, including in preview.

The editor's **Insert Command** menu lists skills under **Skills**, with
their diagrams: approach, retract, park, align tool axis, the transfers and
board localization, then skills from other packages. Gripper and signal
skills are not listed there; the Gripper and I/O tabs drive those live, and
the `rbt.tool` commands are in the same menu.

Choosing a skill writes its call at the editor's cursor, or at the recording
cursor while recording, with every argument as a field. The first field is
selected; **Tab** and **Shift+Tab** move between the fields and **Escape**
leaves them. The code is the form: what you type in a field is the argument.

```python
_skill_waldo_transfer(rbt, pick=setup.resolve("pick"), place=setup.resolve("place"), clearance_mm=30.0, speed=0.2, timeout=30.0)
```

Arguments that come from a [named setup](named-setup.md) refer to it by name
(`setup.resolve("pick")`, `setup.signals["grip"]`, `setup.cameras["overhead"]`),
so the call follows the setup when a pose is taught again. A field starts on
the entry named like the argument, or on the setup's first entry of that kind.
The imports and the `setup = load_setup("bench")` line the call needs go in at
the top of the program, once: a program that loads a setup keeps it, and one
that does not loads the setup saved most recently (`bench` before any is
saved). Inside an `async def`, the call is written as
`await ….async_call(rbt, …)`.

While the cursor is on a skill call, a strip above the code names the skill
and the field the cursor is in, with its unit, and the 3D view draws the
call's motion as a dashed path from where the arm is now, following the text
as it changes; the robot does not move. When the planner refuses the call
from the current pose, the strip says why. For a pose, signal or camera
field, the strip lists that kind of entry in the program's setup, and choosing
one writes its reference into the field. **Teach now** saves where the arm is
now to the program's setup under the name beside it, in the chosen frame, and
writes its reference into the field; **New frame** saves a frame where the arm
is now, to teach poses in. The setup's poses, frames, signals, cameras and
parameters also complete as you type.

To try a call on the robot, select its lines and choose **Run selection** from
the editor's **⋮** menu. The selection runs as its own small program with the
program's imports and setup loads and the tool the arm carries, with the usual
pause and stop controls, then the editor returns to the program; after a failed run it stays
on the run's tab so its log is in view. While recording, the lines are already
in the program, so the run adds nothing to it.

| Skill | Behavior |
|---|---|
| `retract` | Move a positive distance along current tool Z. |
| `approach` | Move to positive target-tool-Z clearance, then linearly to an explicit WRF `Pose`. |
| `park` | Joint-interpolate to a named pose in an explicit `SetupSnapshot`. |
| `align_tool_axis` | Rotate one tool axis toward a WRF direction while keeping the TCP position. Returns `None` if already aligned. |
| `gripper_open`, `gripper_close` | Command the selected supported gripper and wait for completion. Native calibration requirements still apply. |
| `attach_object`, `detach_object` | Declare a program shape attached to the flange or fixed at an explicit world pose; confirm applied geometry without operating the gripper. |

Each motion goes through the backend planner and collision checks. Approach
does not search for a detour. Gripper command completion does not confirm that
an object was grasped.

## Write and compose skills

```python
from waldoctl.client import RobotClient
from waldoctl.skills import skill
from waldo_commander.skills import retract

@skill(id="mybench.withdraw_twice", version="1.0.0")
async def withdraw_twice(rbt: RobotClient, *, distance_mm: float = 5) -> int:
    await retract.async_call(rbt, distance_mm=distance_mm)
    return await retract.async_call(rbt, distance_mm=distance_mm)
```

The normal call is `withdraw_twice(rbt, distance_mm=5)`. Inside another async
function, call `await withdraw_twice.async_call(rbt, distance_mm=5)`. Parameters
and return values retain their Python types. Backend-specific implementations
annotate a concrete backend async client, which makes passing the wrong one a
type error. A skill that needs an optional feature declares it —
`requires=Requires(force_torque=True)`, one flag per `Robot.has_*` capability —
and is refused before its body runs on a backend whose `Robot` does not report
it. A flag says the backend implements the feature, not that the arm is ready.

Keep shared, backend-independent implementations in `waldo_commander.skills`.
Backend-native implementations belong in the backend package. Personal skills
can live in any importable Python module; they need no registry or panel.
`waldoctl.skills` supplies the decorator, execution contract and discovery.
The standard skill module can be imported without starting Commander or reading
its application state. Pass resources explicitly instead of opening hidden
connections or importing the current UI client.

For optional discovery, an installed package can register its callable:

```toml
[project.entry-points."waldoctl.skills"]
withdraw_twice = "mybench.skills:withdraw_twice"
```

`waldoctl.skills.discover_skills()` returns skills keyed by stable id. Broken
plugins are diagnosed and skipped; duplicate ids exclude all conflicting
providers. A panel may call a skill but the skill does not subclass a panel.
The decorator's `api_version` defaults to `1`. An incompatible API version is
refused before execution and shown in the panel's discovery diagnostics. For
headless discovery, pass a list as `diagnostics=` to collect the same messages.
Skill function names also appear in editor completion, with their import module.
A field holds any Python expression, such as an image source or a custom
resource object; the path preview reads only fixed values and setup entries,
and says so for anything else.

## Preview, stepping and progress

For controller speed selection and completion timeouts during a managed pause,
see [Execution speed and pause](execution-controls.md).

Skills pass through the same native planning and stepping wrappers as direct
commands. Nested motion appears in the preview and advances through the usual
Step control. The existing program log shows skill lifecycle and progress
events; `report_progress(message, fraction=...)` publishes progress from a
skill. The optional fraction is between zero and one.

A preview can compute joint angles, TCP poses and trajectories. Reading live
I/O, a gripper verdict or a hardware status predicate requires an explicit
observation fixture; these currently raise `UnresolvedPreview`. Preview must
not choose a program branch from an invented sensor response.

Async cancellation requests the supplied backend's stop and records whether
it was acknowledged. The runtime prevents subsequent supplied-client calls
from a cancelled invocation, including nested skills. Cancellation remains
cooperative Python execution; await child work and keep motion on the supplied
client. A timeout or an unconfirmed stop is a failure, not proof that motion
has stopped. There is no automatic recovery move or restart after power loss.

Named digital I/O skills (`read_signal`, `wait_signal`, and `write_signal`) use
saved mappings and typed outcomes. See [Named device signals](named-setup.md#named-device-signals)
for configuration, Python calls, and explicit preview fixtures.

`locate_board` returns a ChArUco board pose, detection quality, and a typed
missing/rejected outcome using an explicit camera source. See
[Camera localization](vision-localization.md) for live acquisition, pure image
localization, and preview fixtures.
