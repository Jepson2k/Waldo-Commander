"""Unit tests for script runner service."""

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _seed_robot_descriptor():
    """run_script reads ui_state.active_robot for the backend package; these
    unit tests run without the app, so seed the descriptor the way main() does."""
    from waldo_commander.profiles import get_robot
    from waldo_commander.state import ui_state

    if ui_state.robot is not None:
        yield
        return
    ui_state.robot = get_robot()
    yield
    ui_state.robot = None


@pytest.mark.unit
async def test_run_script_streams_a_backend_program_or_refuses_a_missing_file(
    tmp_path: Path,
) -> None:
    """A script that imports the backend's clients runs to exit code 0 with
    its stdout streamed in order; a script that does not exist is refused
    with FileNotFoundError."""
    from waldo_commander.services.script_runner import create_default_config, run_script

    script_path = tmp_path / "test_script.py"
    script_path.write_text(
        """
print("Line 1")
print("Line 2")
print("Line 3")

from parol6 import RobotClient, AsyncRobotClient

assert RobotClient is not None
assert AsyncRobotClient is not None
"""
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    handle = await run_script(
        create_default_config(str(script_path)),
        on_stdout=stdout_lines.append,
        on_stderr=stderr_lines.append,
    )
    return_code = await handle["proc"].wait()
    # The reader tasks deliver the last lines after the process exits.
    for task in (handle["stdout_task"], handle["stderr_task"]):
        await task

    assert return_code == 0, f"Script failed with code {return_code}: {stderr_lines}"
    assert len(stdout_lines) >= 3, stdout_lines
    assert "Line 1" in stdout_lines[0]
    assert "Line 2" in stdout_lines[1]
    assert "Line 3" in stdout_lines[2]

    with pytest.raises(FileNotFoundError):
        await run_script(
            create_default_config("/path/to/nonexistent/script.py"),
            on_stdout=lambda line: None,
            on_stderr=lambda line: None,
        )


@pytest.mark.unit
async def test_stop_script_terminates_process(tmp_path: Path) -> None:
    """Test that stop_script successfully terminates a running process.

    Verifies that long-running scripts can be stopped cleanly.
    """
    from waldo_commander.services.script_runner import (
        run_script,
        stop_script,
        create_default_config,
    )

    # Write a long-running script
    script_path = tmp_path / "long_script.py"
    script_path.write_text(
        """
import time
for i in range(100):
    print(f"Iteration {i}")
    time.sleep(0.1)
"""
    )

    # Run the script
    config = create_default_config(str(script_path))
    handle = await run_script(
        config,
        on_stdout=lambda line: None,
        on_stderr=lambda line: None,
    )

    # Give it time to start
    import asyncio

    await asyncio.sleep(0)

    # Stop the script
    await stop_script(handle, timeout=2.0)

    # Assert that the process has terminated
    assert handle["proc"].returncode is not None, "Expected process to be terminated"


@pytest.mark.unit
async def test_bootstrap_endpoint_injection(tmp_path: Path) -> None:
    """Bare ``RobotClient()`` constructions follow the GUI's exported
    controller endpoint; an explicit host or port is always respected, and a
    malformed exported port is skipped with a warning instead of crashing."""
    import asyncio
    import os

    from waldo_commander.services import stepping_bootstrap
    from waldo_commander.services.stepping_client import GUIStepController

    bootstrap = Path(stepping_bootstrap.__file__)
    # A managed program needs its GUI's end of the stepping link to start.
    gui = GUIStepController("endpoint-test")
    gui.initialize()

    async def run_case(script: str, env_overrides: dict[str, str]) -> tuple:
        script_path = tmp_path / "endpoint_case.py"
        script_path.write_text(script)
        env = {
            **os.environ,
            "WALDO_STEP_SESSION": "endpoint-test",
            "WALDO_CONTROLLER_IP": "127.0.0.1",
            "WALDO_CONTROLLER_PORT": "6001",
            **env_overrides,
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(bootstrap),
            str(script_path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        return proc.returncode, stdout.decode(), stderr.decode()

    clients = (
        "from parol6 import RobotClient\n"
        "for kwargs in ({}, {'port': 7001}, {'host': '10.0.0.5'}):\n"
        "    rbt = RobotClient(**kwargs)\n"
        "    print('EP', rbt.host, rbt.port)\n"
    )
    bare = 'from parol6 import RobotClient\nrbt = RobotClient()\nprint("EP", rbt.host, rbt.port)\n'
    try:
        code, out, err = await run_case(clients, {})
        assert code == 0, err
        assert "EP 127.0.0.1 6001" in out, out
        assert "EP 127.0.0.1 7001" in out, out
        assert "EP 10.0.0.5 5001" in out, (
            f"an explicit host must keep the backend's default port: {out}"
        )

        code, out, err = await run_case(bare, {"WALDO_CONTROLLER_PORT": "nonsense"})
        assert code == 0, f"invalid port must not crash the program: {err}"
        assert "EP 127.0.0.1 5001" in out, out
        assert "Ignoring invalid WALDO_CONTROLLER_PORT" in err
    finally:
        gui.cleanup()
