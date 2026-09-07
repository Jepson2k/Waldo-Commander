"""Verify that shipped programs preview or explain a required observation.

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
    """Motion programs preview; the hardware handshake stops at its observation."""
    program_text = (PROGRAMS_DIR / script).read_text()
    result = _run_simulation_isolated(
        program_text,
        dry_run_client_cls=DryRunRobotClient,
    )
    if script == "cycle_start.py":
        assert result["error"] is not None
        assert "UnresolvedPreview: wait_status" in result["error"]
        assert result["segments"], "the initial home/standby path should remain visible"
    else:
        assert result["error"] is None, (
            f"{script} simulation failed:\n{result['error']}"
        )


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


def test_insert_below_line_matches_indentation():
    """At-cursor inserts inherit the anchor line's indentation — one level
    deeper below a block opener — so they can't split an indented suite."""
    from waldo_commander.services.programs import insert_below_line

    # Plain anchor: same indent as the anchor line.
    text = "def run():\n    rbt.home()\n    rbt.move_j([0])\n"
    new, first, count = insert_below_line(text, "time.sleep(1.0)", 2)
    assert new.split("\n")[2] == "    time.sleep(1.0)"
    assert (first, count) == (3, 1)

    # Block opener: one indent level deeper.
    new, first, _ = insert_below_line(text, "time.sleep(1.0)", 1)
    assert new.split("\n")[1] == "    time.sleep(1.0)"

    # Nested opener inherits the opener's indent plus one unit.
    text = "def run():\n    for _ in range(3):\n        rbt.home()\n"
    new, _, _ = insert_below_line(text, "x()", 2)
    assert new.split("\n")[2] == "        x()"

    # Tab-indented file: tabs are reused for both copy and deepen.
    text = "def run():\n\trbt.home()\n"
    new, _, _ = insert_below_line(text, "x()", 2)
    assert new.split("\n")[2] == "\tx()"
    new, _, _ = insert_below_line(text, "x()", 1)
    assert new.split("\n")[1] == "\tx()"

    # Cursor on the last content line: append still gets the anchor's indent.
    text = "def run():\n    rbt.home()"
    new, first, _ = insert_below_line(text, "x()", 2)
    assert new.split("\n")[2] == "    x()"
    assert first == 3

    # Multi-line snippet: prefix applied per line, blank lines untouched.
    text = "def run():\n    rbt.home()\n    rbt.stop()\n"
    new, _, count = insert_below_line(text, "a()\n\nb()", 2)
    got = new.split("\n")[2:5]
    assert got == ["    a()", "", "    b()"]
    assert count == 3

    # Unset cursor (0): EOF append at column 0, unchanged behavior.
    new, first, _ = insert_below_line("    indented()\n", "x()", 0)
    assert new.endswith("x()\n") and not new.endswith(" x()\n")

    # Blank anchor line: column 0.
    text = "a()\n\nb()\n"
    new, _, _ = insert_below_line(text, "x()", 2)
    assert new.split("\n")[2] == "x()"
