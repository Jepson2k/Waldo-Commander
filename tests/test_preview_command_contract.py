"""A program sees the live client's return contract from the preview.

The command table on ``RobotClient`` says what each call returns: an index a
program may ``wait_command`` for queued work, the 1/0/negative code for a
system or control command, an answer for a query, and no answer at all for
an observation a plan cannot know. A program that branches on
``if rbt.stop() < 0`` or waits on ``rbt.write_io(...)`` must behave the same
way in preview, or the preview passes what the arm refuses.
"""

from __future__ import annotations

import numpy as np
from parol6.client.dry_run_client import DryRunRobotClient
from waldoctl import CommandKind, command_table
from waldoctl.skills import UnresolvedPreview

from waldo_commander.services.path_preview_client import PathPreviewClient

HOME_DEG = [90.0, -90.0, 180.0, 0.0, 0.0, 180.0]
POSE = [0.0, 280.0, 200.0, 90.0, 0.0, 90.0]

# What a program would pass; the values are legal for the PAROL6 planner.
SAMPLE_ARGS: dict[str, tuple] = {
    "move_j": ([80.0, -80.0, 190.0, 10.0, 10.0, 190.0],),
    "move_l": (POSE,),
    "move_c": (POSE, [50.0, 280.0, 200.0, 90.0, 0.0, 90.0]),
    "move_s": ([POSE, [50.0, 280.0, 250.0, 90.0, 0.0, 90.0]],),
    "move_p": ([POSE, [50.0, 280.0, 250.0, 90.0, 0.0, 90.0]],),
    "servo_j": ([80.0, -80.0, 190.0, 10.0, 10.0, 190.0],),
    "servo_l": (POSE,),
    "jog_j": (0, 0.5, 0.1),
    "jog_l": ("WRF", "X", 0.5, 0.1),
    "estimate_payload": (),
    "home": (),
    "checkpoint": ("mark",),
    "delay": (0.1,),
    "write_io": (0, 1),
    "tool_action": ("SSG-48", "calibrate"),
    "reset": (),
    "reset_state": (),
    "set_status_rate": (50,),
    "simulator": (True,),
    "teleport": (HOME_DEG,),
    "freedrive": (False,),
    "set_shapes": ([],),
    "connect_hardware": ("/dev/null",),
    "select_profile": ("RUCKIG",),
    "select_tool": ("NONE",),
    "set_tcp_offset": (0.0, 0.0, 0.0),
    "set_payload": (0.1,),
    "stop": (),
    "estop": (),
    "wait_status": (lambda s: True,),
    "wait_command": (0,),
    "wait_checkpoint": ("mark",),
    "set_execution_speed": (1.0,),
    "pause": (),
    "resume": (),
}


def _preview() -> PathPreviewClient:
    return PathPreviewClient(
        dry_run_client_cls=DryRunRobotClient, initial_joints=np.radians(HOME_DEG)
    )


SAMPLE_KWARGS: dict[str, dict] = {
    "move_j": {"speed": 0.5},
    "move_l": {"speed": 0.5},
    "move_p": {"speed": 0.5},
    "move_s": {"speed": 0.5},
    "move_c": {"speed": 0.5},
}


def _call(client: PathPreviewClient, name: str):
    return getattr(client, name)(
        *SAMPLE_ARGS.get(name, ()), **SAMPLE_KWARGS.get(name, {})
    )


def test_the_preview_honours_the_command_table_for_every_command():
    table = command_table()
    exercised: dict[CommandKind, int] = {}
    problems: list[str] = []
    for name, spec in table.items():
        client = _preview()
        try:
            getattr(client, name)
        except AttributeError:
            continue  # the PAROL6 dry run does not implement this optional command
        exercised[spec.kind] = exercised.get(spec.kind, 0) + 1
        if spec.kind is CommandKind.OBSERVATION:
            try:
                _call(client, name)
            except UnresolvedPreview:
                continue
            problems.append(f"{name}: an observation was answered by a plan")
            continue
        try:
            result = _call(client, name)
        except AttributeError:
            exercised[spec.kind] -= 1
            continue
        if spec.mints_index:
            if not isinstance(result, int) or isinstance(result, bool):
                problems.append(
                    f"{name} returned {result!r}; a queued command returns an index"
                )
            elif result < 0:
                problems.append(
                    f"{name} was refused by the planner: {client.accumulated_errors}"
                )
            elif client.wait_command(result) is not True:
                problems.append(
                    f"{name}: the index it minted is not one wait_command completes"
                )
        elif spec.kind in (CommandKind.MOTION, CommandKind.SYSTEM, CommandKind.CONTROL):
            if not isinstance(result, int) or isinstance(result, bool):
                problems.append(
                    f"{name} returned {result!r}; a state or streamed command "
                    "returns 1/0/negative"
                )
            elif result < 0:
                problems.append(f"{name} was refused: {client.accumulated_errors}")
        elif hasattr(result, "tcp_poses"):
            problems.append(
                f"{name} leaked a planner result where the live client answers"
            )
    assert not problems, "\n".join(problems)
    assert exercised[CommandKind.MOTION] >= 8
    assert exercised[CommandKind.QUEUED] >= 4
    assert exercised[CommandKind.SYSTEM] >= 6
    assert exercised[CommandKind.CONTROL] == 2
    assert exercised[CommandKind.OBSERVATION] >= 3
