"""Populated scenes shared by the opt-in review captures."""

from contextlib import contextmanager

import numpy as np
import waldoctl
from waldoctl.shapes import Box

from tests.helpers.browser_helpers import run_in_app
from tests.helpers.scene_screen import (
    EDIT_PROGRAM,
    TCP_VIEW,
    confirmed,
    program_previewed,
)
from tests.helpers.scene_surface import scene_js, wait_for_scene_idle
from waldo_commander.state import ui_state


FIXTURE_VIEW = [0.44, 0.18, 0.16, 0.3, 0, 0.045]
SWEEP_VIEW = [0.35, 0.56, 0.44, 0, 0.19, 0.22]


def clearance_view(clearance):
    midpoint = np.mean([clearance.part_point, clearance.obstacle_point], axis=0)
    gap = np.subtract(clearance.obstacle_point, clearance.part_point)
    side = np.array([-gap[1], gap[0], 0.0])
    if np.linalg.norm(side) < 1e-6:
        side = np.array([0.0, 1.0, 0.0])
    side *= 0.30 / np.linalg.norm(side)
    return [*(midpoint + side + [0, 0, 0.12]), *midpoint]


# Planned moves into a keep-out are rejected before they have any recorded rows.
# A bounded jog records its requested path for the preview's collision checker.
SWEEP_PROGRAM = """from parol6 import RobotClient
from parol6.config import LIMITS
rbt = RobotClient()
rbt.select_tool("NONE")
rbt.move_j([85, -85, 175, 5, 5, 175], speed=0.5)
rbt.move_l([0, 260, 300, 90, 0, 90], speed=0.5)
rbt.move_l([-110, 210, 250, 90, 0, 90], speed=0.5)
rbt.jog_l("WRF", axes=["X", "Y"], speeds_list=[0.5, 0.3], duration=0.4 / LIMITS.cart.jog.velocity.linear)
"""


def bracket_stl(path):
    """A 120 × 80 × 60 mm L-bracket with 216 triangles."""
    from stl import Mode, mesh

    triangles = []
    for low, high in (((0, 0, 0), (120, 80, 8)), ((0, 0, 8), (120, 8, 60))):
        for axis in range(3):
            u, v = (axis + 1) % 3, (axis + 2) % 3
            for side in (0, 1):
                for i in range(3):
                    for j in range(3):
                        corners = []
                        for a, b in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)):
                            p = np.array(low, dtype=float)
                            p[axis] = (low, high)[side][axis]
                            p[u] += (high[u] - low[u]) * a / 3
                            p[v] += (high[v] - low[v]) * b / 3
                            corners.append(p)
                        for indices in ((0, 1, 2), (0, 2, 3)):
                            triangles.append(
                                [
                                    corners[k]
                                    for k in (indices if side else indices[::-1])
                                ]
                            )
    model = mesh.Mesh(np.zeros(len(triangles), dtype=mesh.Mesh.dtype))
    model.vectors[:] = triangles
    model.save(str(path), mode=Mode.BINARY)
    return path


def framed_program(directory):
    from waldoctl.setup import Frame, SetupSnapshot

    from waldo_commander.setup import SetupStore

    snapshot = SetupSnapshot(frames={"Tilted-nest": Frame((65, 250, 200, 25, 15, 35))})
    run_in_app(lambda: SetupStore(directory).save("review-bench", snapshot))
    return (
        "from waldo_commander.setup import load_setup\n"
        f"setup = load_setup('review-bench', directory={str(directory)!r})\n"
        + EDIT_PROGRAM
    )


def tilted_target_program():
    pose = run_in_app(
        lambda: ui_state.urdf_scene._ensure_ik_solver()
        .forward_kinematics(np.radians([90, -75, 210, 0, 70, 180]))
        .copy()
    )
    literal = [float(v) for v in pose[:3] * 1000] + [
        float(v) for v in np.degrees(pose[3:])
    ]
    source = (
        "from parol6 import RobotClient\n"
        "rbt = RobotClient()\n"
        "rbt.move_j([90, -85, 210, 0, 65, 180], speed=0.5)\n"
        f"rbt.move_j(pose={literal!r}, speed=0.5)\n"
    )
    camera = [*(np.array(TCP_VIEW[:3]) - TCP_VIEW[3:] + pose[:3]), *pose[:3]]
    return source, camera


@contextmanager
def swept_preview(screen, *, obstacle: Box | None = None):
    handle = waldoctl.commander.scene
    original = list(handle.shapes)
    obstacle = obstacle or Box(
        name="Sweep keep-out",
        x=0.045,
        y=0.045,
        z=0.16,
        pose=(0.04, 0.30, 0.25, 0, 0, 0),
    )

    def restore():
        if run_in_app(lambda: handle.shapes != original):
            run_in_app(lambda: setattr(handle, "shapes", original))
            confirmed(screen, lambda: handle.shapes == original)

    try:
        run_in_app(lambda: setattr(handle, "shapes", [obstacle]))
        confirmed(screen, lambda: handle.shapes == [obstacle])
        with program_previewed(SWEEP_PROGRAM):
            try:
                wait_for_scene_idle(screen)
                blocks = scene_js(screen, "return S.inspection().records[0].blocks")
                move = next((b for b in blocks if b["name"] == "jog_l"), None)
                assert move and move["valid"] and move["end"] - move["start"] > 1, (
                    blocks
                )
                assert move["collisionRow"] is not None, move
                endpoint = run_in_app(
                    lambda: waldoctl.commander.programs.active.dry_run.commanded.tcp[
                        -1, :3
                    ]
                )
                assert np.allclose(endpoint, [0.09, 0.33, 0.25], atol=0.002), endpoint
                yield move
            finally:
                # Restore the world before re-previewing the caller's planned moves.
                restore()
    finally:
        restore()
