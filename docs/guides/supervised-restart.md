# Supervised restart

Use the editor's **Supervised restart** button after an interrupted program to
select an explicit Python entry function. Review the previous run, read fresh
controller state, select the entry, check the physical setup, then click
**Start from entry**. Optional [run records](../run-records.md) retain the selected
entry and the state observed at launch.

```python
from parol6 import RobotClient
from waldoctl.restart import restart_entry
from waldo_commander.setup import load_setup


@restart_entry
def after_pick():
    """Continue with the held part checked and the destination clear."""
    setup = load_setup("bench")
    with RobotClient() as rbt:
        place = setup.resolve("place")
        rbt.move_l(place.as_list(), speed=0.2)


def main():
    # Perform the complete application sequence here.
    after_pick()


if __name__ == "__main__":
    main()
```

An entry is a top-level, zero-argument callable marked with `@restart_entry`.
Async functions are supported too. The decorator preserves ordinary Python
calling behavior; calling an entry directly in a standalone program does not
perform Commander's review checks.

Each selection launches a new Python process with fresh globals and calls only
the selected entry. It does not reconstruct locals or resume at a source line.
Load the data and create the client inside functions. Module-level imports,
literal constants, function declarations, docstrings and a conventional
`if __name__ == "__main__":` block are supported. Function defaults must be
literals; initialization calls and arbitrary decorators are refused in entry
mode. `@waldoctl.skills.skill(...)` with literal metadata is supported.
Imported packages and Python declarations remain trusted application code;
this validation is not a Python sandbox.

The controller must report advancing status publications, valid references,
enabled drives, no fault, an empty queue and stopped joints. PAR6's normal
gravity-compensated idle permits hand-guiding; keep hands clear for execution.
Commander reads state again immediately before launch. A changed publisher,
collision-world epoch, tool/variant, TCP, joint position over one degree, or
edited source requires another review. These observations are not an atomic
lock on other clients: avoid concurrent command sources during review and run.

After controller or power loss, establish references when needed and reconcile
the actual arm, tool, held part and work area. Stored progress or a recorded
pose does not authorize continuation. Restart never homes the robot or restores
configuration automatically. **Stop** clears old queued motion; **Pause** leaves
it pending and therefore prevents a new restart.

Ordinary **Start** always runs from the beginning, including for programs without
entries. Supervised restart works with run recording disabled.
