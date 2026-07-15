"""Verify that all programs/ scripts simulate without errors.

Runs each program through the path visualizer's dry-run simulation
(the same code path used when viewing scripts in the editor).
This catches IK failures, missing imports, and API misuse before
the user hits them in the UI.
"""

import subprocess
from pathlib import Path

import pytest

from parol6.client.dry_run_client import DryRunRobotClient
from waldo_commander.services.path_visualizer import _run_simulation_isolated

REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRAMS_DIR = REPO_ROOT / "programs"

# Only test programs tracked in git — `programs/` also contains user-local
# scripts (gitignored) that may bypass the RobotClient abstraction and can't
# run under the dry-run simulator.
_tracked = subprocess.check_output(
    ["git", "ls-files", "programs/*.py"], cwd=REPO_ROOT, text=True
).splitlines()

PROGRAMS = sorted(
    Path(rel).name
    for rel in _tracked
    if (REPO_ROOT / rel).exists()
    and (REPO_ROOT / rel).stat().st_size > 10
    and not Path(rel).name.startswith(("test_", "__"))
)


@pytest.mark.parametrize("script", PROGRAMS)
def test_program_simulates(script):
    """Each program should simulate without errors in the path visualizer."""
    program_text = (PROGRAMS_DIR / script).read_text()
    result = _run_simulation_isolated(
        program_text,
        dry_run_client_cls=DryRunRobotClient,
    )
    assert result["error"] is None, f"{script} simulation failed:\n{result['error']}"


def test_preview_mirrors_unhomed_motion_gate():
    """Seeded from an unhomed robot, the preview refuses planned moves with
    the actionable not-homed error — matching the controller's gate — and a
    home() line establishes references, so the first move after it previews
    cleanly."""
    template = (
        "from parol6 import RobotClient\n"
        "rbt = RobotClient(host='127.0.0.1', port=5001)\n"
    )
    move = "rbt.move_j([90.0, -90.0, 180.0, 0.0, 0.0, 170.0], speed=0.5)\n"

    blind = _run_simulation_isolated(
        template + move,
        dry_run_client_cls=DryRunRobotClient,
        initial_homed=False,
    )
    assert blind["error"] is not None and "not homed" in blind["error"], (
        f"unhomed preview must refuse a planned move: {blind['error']!r}"
    )

    homed_first = _run_simulation_isolated(
        template + "rbt.home()\n" + move,
        dry_run_client_cls=DryRunRobotClient,
        initial_homed=False,
    )
    assert homed_first["error"] is None, (
        f"the first move after home() must preview cleanly: {homed_first['error']!r}"
    )
