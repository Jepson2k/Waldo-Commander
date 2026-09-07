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
