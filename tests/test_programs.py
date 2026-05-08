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
