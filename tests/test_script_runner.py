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
async def test_run_script_happy_path(tmp_path: Path) -> None:
    """Test that run_script successfully executes a simple script.

    Verifies basic process execution and stdout capture.
    """
    from waldo_commander.services.script_runner import run_script

    # Write a simple test script that prints to stdout
    script_path = tmp_path / "test_script.py"
    script_path.write_text(
        """
print("Line 1")
print("Line 2")
print("Line 3")
"""
    )

    # Collect stdout lines
    stdout_lines = []

    def on_stdout(line: str) -> None:
        stdout_lines.append(line)

    # Run the script with config
    from waldo_commander.services.script_runner import create_default_config

    config = create_default_config(str(script_path))
    handle = await run_script(
        config,
        on_stdout=on_stdout,
        on_stderr=lambda line: None,
    )

    # Wait for completion
    if handle and handle["proc"]:
        await handle["proc"].wait()

    # Assert stdout was captured in order
    assert len(stdout_lines) >= 3, "Expected at least 3 lines of stdout"
    assert "Line 1" in stdout_lines[0]
    assert "Line 2" in stdout_lines[1]
    assert "Line 3" in stdout_lines[2]


@pytest.mark.unit
async def test_run_script_missing_file_raises() -> None:
    """Test that run_script raises FileNotFoundError for non-existent script.

    Verifies error handling for missing files.
    """
    from waldo_commander.services.script_runner import run_script, create_default_config

    # Try to run a non-existent file
    config = create_default_config("/path/to/nonexistent/script.py")
    with pytest.raises(FileNotFoundError):
        await run_script(
            config,
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
def test_create_default_config() -> None:
    """Test that create_default_config returns a valid configuration.

    Verifies that default configuration has all required fields.
    """
    from waldo_commander.services.script_runner import create_default_config

    config = create_default_config("/tmp/test.py")

    # Assert required fields are present
    assert "filename" in config
    assert "python_exe" in config
    assert config["filename"] == "/tmp/test.py"
    assert config["python_exe"] == sys.executable


@pytest.mark.unit
async def test_script_can_import_parol6_libraries(tmp_path: Path) -> None:
    """Test that user scripts have access to parol6 robot libraries.

    Verifies that scripts run via the script runner can import the
    parol6 library and its key components. This ensures users can
    write robot control scripts that use the expected API.
    """
    from waldo_commander.services.script_runner import run_script, create_default_config

    # Write a script that imports the key parol6 modules
    script_path = tmp_path / "test_imports.py"
    script_path.write_text(
        """
# Test that key backend imports are available
from parol6 import RobotClient, AsyncRobotClient

# Verify classes are actually importable (not just module stubs)
assert RobotClient is not None
assert AsyncRobotClient is not None

print("All parol6 imports successful")
"""
    )

    # Collect stdout lines
    stdout_lines = []
    stderr_lines = []

    def on_stdout(line: str) -> None:
        stdout_lines.append(line)

    def on_stderr(line: str) -> None:
        stderr_lines.append(line)

    # Run the script
    config = create_default_config(str(script_path))
    handle = await run_script(config, on_stdout=on_stdout, on_stderr=on_stderr)

    # Wait for completion
    if handle and handle["proc"]:
        return_code = await handle["proc"].wait()
    else:
        return_code = -1

    # Assert script completed successfully (exit code 0 means imports worked)
    assert return_code == 0, (
        f"Script failed with code {return_code}. Stderr: {stderr_lines}"
    )

    # Assert the success message was printed
    assert any("All parol6 imports successful" in line for line in stdout_lines), (
        f"Expected success message in stdout: {stdout_lines}"
    )


@pytest.mark.unit
async def test_bootstrap_endpoint_injection(tmp_path: Path) -> None:
    """Bare ``RobotClient()`` constructions follow the GUI's exported
    controller endpoint; an explicit host or port is always respected, and a
    malformed exported port is skipped with a warning instead of crashing."""
    import asyncio
    import os

    from waldo_commander.services import stepping_bootstrap

    bootstrap = Path(stepping_bootstrap.__file__)

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

    bare = 'from parol6 import RobotClient\nrbt = RobotClient()\nprint("EP", rbt.host, rbt.port)\n'
    code, out, err = await run_case(bare, {})
    assert code == 0, err
    assert "EP 127.0.0.1 6001" in out, out

    explicit_port = 'from parol6 import RobotClient\nrbt = RobotClient(port=7001)\nprint("EP", rbt.host, rbt.port)\n'
    code, out, err = await run_case(explicit_port, {})
    assert code == 0, err
    assert "EP 127.0.0.1 7001" in out, out

    explicit_host = 'from parol6 import RobotClient\nrbt = RobotClient(host="10.0.0.5")\nprint("EP", rbt.host, rbt.port)\n'
    code, out, err = await run_case(explicit_host, {})
    assert code == 0, err
    assert "EP 10.0.0.5 5001" in out, (
        f"an explicit host must keep the backend's default port: {out}"
    )

    code, out, err = await run_case(bare, {"WALDO_CONTROLLER_PORT": "nonsense"})
    assert code == 0, f"invalid port must not crash the program: {err}"
    assert "EP 127.0.0.1 5001" in out, out
    assert "Ignoring invalid WALDO_CONTROLLER_PORT" in err
