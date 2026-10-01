"""Reusable cases exercise the same isolated worker as editor physics."""

import asyncio
import json
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from waldo_commander.services.simulation_scenarios import (
    SimulationCase,
    load_case,
    run_case,
)

requires_par6 = pytest.mark.skipif(
    not os.environ.get("WALDO_PAR6_E2E"),
    reason="native scenario cases run in the PAR6 integration job",
)
FIXTURES = Path(__file__).parents[1] / "examples" / "simulation"


@requires_par6
@pytest.mark.timeout(60)
async def test_a_case_never_consults_a_nearby_controller(monkeypatch):
    """A case replays the packaged model. A controller listening where the
    backend looks for one would otherwise hand the dry run its own
    configuration, which the report's model hash does not describe."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        monkeypatch.setenv("PAR6_HOST", "127.0.0.1")
        monkeypatch.setenv("PAR6_COMMAND_PORT", str(listener.getsockname()[1]))
        report = await run_case(load_case(FIXTURES / "idle.json"))
        assert report["passed"], json.dumps(report, indent=2)
        with pytest.raises(BlockingIOError):
            listener.recv(2048)


@requires_par6
@pytest.mark.timeout(240)
async def test_reusable_cases_report_replay_faults_and_enforce_worker_deadlines(
    tmp_path,
):
    reports = {}
    for name in ("idle", "delayed-noisy", "driver-fault", "held-object"):
        case = load_case(FIXTURES / f"{name}.json")
        report = await run_case(case)
        assert report["passed"], json.dumps(report, indent=2)
        assert report["rows"] > 0 and report["model_sha256"], report
        reports[name] = report
    repeated = await run_case(load_case(FIXTURES / "delayed-noisy.json"))
    assert repeated["digest"] == reports["delayed-noisy"]["digest"]
    assert repeated["digest"] != reports["idle"]["digest"]
    assert reports["driver-fault"]["duration_s"] < reports["idle"]["duration_s"]

    # Loading data does not execute the embedded Python. Running it has a
    # separate wall deadline even when Python never yields a robot command.
    marker = tmp_path / "executed"
    endless = replace(
        load_case(FIXTURES / "idle.json"),
        name="endless",
        # The program records the pid it is spinning in, so the deadline can be
        # checked for what it has to do: end that process. Touching a file only
        # proves the program started.
        program=(
            "import os\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "while True:\n"
            "    pass\n"
        ),
        wall_timeout_s=15,
    )
    path = tmp_path / "endless.json"
    from dataclasses import asdict

    path.write_text(json.dumps(asdict(endless)))
    loaded = load_case(path)
    assert not marker.exists()
    result = await run_case(loaded)
    assert result["stop"] == "wall_timeout" and not result["passed"], result
    assert marker.exists(), "the deadline must interrupt an executing program"
    # The timed-out worker is gone, not left spinning: a report is not a
    # deadline if the process it abandoned keeps burning a core.
    spinning = int(marker.read_text())
    for _ in range(100):
        if not Path(f"/proc/{spinning}").exists():
            break
        await asyncio.sleep(0.05)
    assert not Path(f"/proc/{spinning}").exists(), (
        f"the worker that exceeded its deadline (pid {spinning}) is still running"
    )
    # A tool without variants is selected with an empty variant key, the shape
    # the preview itself seeds with; demanding one left no case able to choose
    # its starting tool.
    replace(load_case(FIXTURES / "idle.json"), initial_tool=("SSG48", ""))
    with pytest.raises(ValueError, match="tool key"):
        replace(load_case(FIXTURES / "idle.json"), initial_tool=("", ""))

    # An unrun case still reports every field a consumer reads.
    assert set(result) == set(reports["idle"]), (
        "a timed-out case reports a different shape than a completed one"
    )
    assert (
        result["rows"] == 0 and result["digest"] == "" and result["duration_s"] == 0.0
    )
    assert result["backend_version"] and result["case_sha256"]
    # A case naming a backend that is not installed fails in the worker, and
    # its report must not need the missing package's version to be written.
    absent = await run_case(
        SimulationCase(
            name="absent",
            program="pass\n",
            initial_joints_deg=(0, -90, 180, 0, 0, 180),
            backend="absent-backend",
        )
    )
    assert absent["stop"] == "error" and not absent["passed"], absent
    assert "absent-backend" in absent["error"]
    assert absent["backend_version"] is None
    # A killed worker cannot poison the next case.
    assert (await asyncio.wait_for(run_case(load_case(FIXTURES / "idle.json")), 30))[
        "passed"
    ]

    wrong_expectation = replace(
        load_case(FIXTURES / "driver-fault.json"), expected_error_code=43
    )
    assert not (await run_case(wrong_expectation))["passed"]
