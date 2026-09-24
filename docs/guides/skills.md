# Python skills

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

The editor's **Insert Command** menu lists the skills that take a form under
**Skills**, with their diagrams: approach, retract, park and align tool axis,
then skills from other packages. Gripper skills are not listed there; the
Gripper tab drives the gripper live and `rbt.tool` commands are in the same
menu. Choosing a skill opens its parameters where the side panels open, and
the 3D view draws its motion as a dashed path from where the arm is now,
following the values as they are filled in; the robot does not move.
**Insert** puts an import and a call at the editor's cursor, or at the
recording cursor while recording. Saved poses and setups are inserted as fixed
snapshots; saving different setup data later does not change that call. To
follow saved data on the next run, edit the Python to load it explicitly with
`load_setup`. The form does not read edited Python back into its fields.

To try a call on the robot, select its lines and choose **Run selection** from
the editor's **⋮** menu. The selection runs as its own small program with the
program's imports and the tool the arm carries, with the usual pause and stop
controls, then the editor returns to the program; after a failed run it stays
on the run's tab so its log is in view. While recording, the lines are already
in the program, so the run adds nothing to it.

| Skill | Behavior |
|---|---|
| `retract` | Move a positive distance along current tool Z. |
| `approach` | Move to positive target-tool-Z clearance, then linearly to an explicit WRF `Pose`. |
| `park` | Joint-interpolate to a named pose in an explicit `SetupSnapshot`. |
| `align_tool_axis` | Rotate one tool axis toward a WRF direction while keeping the TCP position. Returns `None` if already aligned. |
| `gripper_open`, `gripper_close` | Command the selected supported gripper and wait for completion. Native calibration requirements still apply. |

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
Use ordinary Python for arguments that cannot be represented by the panel's
literal fields, such as image sources or custom resource objects.

## Preview, stepping and progress

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
