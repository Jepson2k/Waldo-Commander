from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, TypedDict

logger = logging.getLogger(__name__)


class ScriptRunConfig(TypedDict):
    """Configuration for running a Python script."""

    filename: str  # absolute path to saved script
    python_exe: str  # sys.executable path
    env: dict[str, str]  # extra environment variables; optional
    cwd: str  # working directory for the script; default project root


class ScriptProcessHandle(TypedDict):
    """Handle for a running script process."""

    proc: asyncio.subprocess.Process
    stdout_task: asyncio.Task
    stderr_task: asyncio.Task
    start_ts: float


async def _stream_output(
    stream: asyncio.StreamReader, callback: Callable[[str], None]
) -> None:
    """Read lines from stream and forward to callback. Lines are forwarded
    verbatim — stream tagging (e.g. the ``[ERR]`` marker) is the log
    recorder's job, so it happens exactly once."""
    try:
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="ignore").rstrip()
            if line:
                callback(line)
    except Exception as e:
        logger.error("Stream reader error: %s", e)


async def run_script(
    cfg: ScriptRunConfig,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
    session_id: str | None = None,
) -> ScriptProcessHandle:
    """
    Start a Python script as a subprocess and stream output to callbacks.

    Args:
        cfg: Configuration for the script run
        on_stdout: Callback for stdout lines
        on_stderr: Callback for stderr lines
        session_id: Optional stepping session ID for GUI-controlled execution

    Returns:
        Handle for managing the process

    Raises:
        FileNotFoundError: If script file doesn't exist
        PermissionError: If Python executable not found/executable
        OSError: If process creation fails
    """
    script_path = Path(cfg["filename"])
    if not script_path.exists():
        raise FileNotFoundError(f"Script file not found: {cfg['filename']}")

    if not script_path.suffix == ".py":
        raise ValueError(f"Script must be a .py file: {cfg['filename']}")

    python_exe = cfg["python_exe"]
    if not Path(python_exe).exists():
        raise FileNotFoundError(f"Python executable not found: {python_exe}")

    env = {**os.environ, **cfg.get("env", {})}
    if session_id:
        env["WALDO_STEP_SESSION"] = session_id
    # Pass backend package to subprocess so stepping_bootstrap can patch the right module
    from waldo_commander.state import ui_state

    env["WALDO_BACKEND_PACKAGE"] = ui_state.active_robot.backend_package

    if session_id:
        # Bootstrap script injects the stepping wrapper around the user script.
        bootstrap_path = Path(__file__).parent / "stepping_bootstrap.py"
        if not bootstrap_path.exists():
            raise FileNotFoundError(f"Bootstrap script not found: {bootstrap_path}")
        exec_args = [python_exe, "-u", str(bootstrap_path), str(script_path)]
    else:
        exec_args = [python_exe, "-u", str(script_path)]

    # On Unix, create a new process group so we can kill the entire tree.
    kwargs: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": cfg["cwd"],
        "env": env,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(*exec_args, **kwargs)

    if proc.stdout:
        stdout_task = asyncio.create_task(_stream_output(proc.stdout, on_stdout))
    else:
        stdout_task = asyncio.create_task(asyncio.sleep(0))

    if proc.stderr:
        stderr_task = asyncio.create_task(_stream_output(proc.stderr, on_stderr))
    else:
        stderr_task = asyncio.create_task(asyncio.sleep(0))

    handle: ScriptProcessHandle = {
        "proc": proc,
        "stdout_task": stdout_task,
        "stderr_task": stderr_task,
        "start_ts": time.time(),
    }

    logger.info("Started script process: %s (PID: %s)", cfg["filename"], proc.pid)
    return handle


async def stop_script(handle: ScriptProcessHandle, timeout: float = 2.0) -> None:
    """
    Stop a running script process gracefully.

    Args:
        handle: Process handle from run_script
        timeout: Seconds to wait for graceful termination before force kill
    """
    proc = handle["proc"]

    if proc.returncode is not None:
        logger.debug("Script process already terminated (code: %s)", proc.returncode)
        return

    try:
        # On Unix, signal the whole process group so child processes die too.
        if sys.platform != "win32" and proc.pid:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, 15)  # SIGTERM
                logger.debug("Sent SIGTERM to process group %s", pgid)
            except (ProcessLookupError, OSError):
                # Process group gone; fall back to the lone process.
                proc.terminate()
        else:
            proc.terminate()

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            logger.debug("Script process terminated gracefully")
        except asyncio.TimeoutError:
            # Graceful termination timed out; escalate to SIGKILL.
            if sys.platform != "win32" and proc.pid:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, 9)  # SIGKILL
                    logger.debug("Sent SIGKILL to process group %s", pgid)
                except (ProcessLookupError, OSError):
                    proc.kill()
            else:
                proc.kill()
            await proc.wait()
            logger.warning("Script process force-killed after timeout")

    except ProcessLookupError:
        # Process already dead.
        pass
    except Exception as e:
        logger.error("Error stopping script process: %s", e)

    for task in [handle["stdout_task"], handle["stderr_task"]]:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("Error canceling stream task: %s", e)


def create_default_config(filename: str, cwd: str | None = None) -> ScriptRunConfig:
    """Create a default script configuration."""
    return {
        "filename": filename,
        "python_exe": sys.executable,
        "env": {},
        "cwd": cwd or str(Path.cwd()),
    }
