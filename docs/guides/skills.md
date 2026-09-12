# Python skills

For pose grids, transfer helpers, and editable completion notes, see
[Tray patterns](tray-patterns.md).

For observed motion capture and native waypoint replay, see
[Demonstration recording](demonstration-recording.md).

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

The **Skills** panel lists installed skills, their Python parameters and backend
requirements. **Insert call** inserts an import and a call into the active program.
Saved poses and setups are inserted as fixed snapshots; saving different setup
data later does not change that call. To follow saved data on the next run, edit
the Python to load it explicitly with `load_setup`.

**Run once** opens the generated call in a program tab and runs it with the usual
pause and stop controls. During motion recording, a successfully completed run
adds one skill call with its fixed arguments to the recording program. Failed or
cancelled runs do not add a successful call. Edit the generated Python normally;
the panel does not read edited Python back into its fields.

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

@skill(id="mybench.withdraw_twice", version="1.0.0",
       requires=frozenset({"motion.linear"}))
async def withdraw_twice(rbt: RobotClient, *, distance_mm: float = 5) -> int:
    await retract.async_call(rbt, distance_mm=distance_mm)
    return await retract.async_call(rbt, distance_mm=distance_mm)
```

The normal call is `withdraw_twice(rbt, distance_mm=5)`. Inside another async
function, call `await withdraw_twice.async_call(rbt, distance_mm=5)`. Parameters
and return values retain their Python types. Backend-specific implementations
can annotate a concrete backend async client and require `backend.par6` or
`backend.parol6`. These capabilities identify API support, not readiness.

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
Use ordinary Python for arguments that cannot be represented by the panel's
literal fields, such as image sources or custom resource objects.

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
