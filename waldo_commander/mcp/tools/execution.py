"""MCP tools for script execution lifecycle — ``execution.*``.

Wired to the GUI's ``script_exec`` controller (the same backend the play button
uses); ``start``/``stop`` reads/writes the editor in the live page's client
context. Running or resuming actuates the robot, so they pass the full
``require_actuation`` gate.
"""

from __future__ import annotations

import asyncio

import waldoctl

from waldo_commander.components.playback import playback
from waldo_commander.components.script_execution import script_exec
from waldo_commander.mcp.server import get_mcp
from waldo_commander.mcp.tools.control import require_actuation, require_control
from waldo_commander.mcp.tools.simulation import _page_client
from waldo_commander.services.programs import is_any_program_running

mcp = get_mcp()


def _ensure_active() -> None:
    if waldoctl.commander.programs.active is None:
        raise RuntimeError("no active program to run")


@mcp.tool(name="execution.run_active")
async def run_active() -> None:
    """Start the active program and return immediately (it runs in a
    subprocess). Raises if a program is already running.

    Follow up with ``execution.wait_active`` — a program that crashed on its
    first line looks exactly like one that finished until you read its log.

    Running a program actuates the robot, so it passes the full actuation gate
    (the control lease plus mode-dependent approval — per-action in
    Inspect/Auto-edits, auto in Autopilot with a hardware consent floor).
    """
    if is_any_program_running():
        raise RuntimeError("a program is already running; stop it first")
    _ensure_active()
    require_actuation("run the active program")
    with _page_client():
        await script_exec.start()


@mcp.tool(name="execution.stop_active")
async def stop_active() -> None:
    """Stop the active program. No-op if it isn't running."""
    require_control()
    with _page_client():
        await script_exec.stop()


@mcp.tool(name="execution.pause_active")
async def pause_active() -> None:
    """Pause the active program. Requires it to be running."""
    require_control()
    with _page_client():
        playback.set_script_playing(False)


@mcp.tool(name="execution.resume_active")
async def resume_active() -> None:
    """Resume the active program from pause.

    Resuming actuates the robot, so it passes the full actuation gate.
    """
    require_actuation("resume the active program")
    with _page_client():
        playback.set_script_playing(True)


@mcp.tool(name="execution.is_running")
async def is_running() -> bool:
    """Whether a program is currently executing."""
    return is_any_program_running()


@mcp.tool(name="execution.wait_active")
async def wait_active(timeout: float = 60.0) -> dict:
    """Block until the running program finishes, up to ``timeout`` seconds.

    Returns ``{"finished": False}`` on timeout (call again to keep waiting),
    or ``{"finished": True, "exit_ok": ..., "log_tail": [...]}`` once it
    stops — ``exit_ok`` is False when the program crashed (``None`` if it was
    stopped externally) and ``log_tail`` is the last ~20 output lines, where
    any traceback will be. Read the tail (or ``programs.get_log``) after
    EVERY run before claiming success. Passive and ungated — it only waits.
    """
    programs = waldoctl.commander.programs
    # The log lives on the tab that launched the run, which may no longer be
    # the active tab by the time it finishes — grab it while it's running.
    p = next((t for t in programs.items if t.execution.is_running), None)
    deadline = asyncio.get_event_loop().time() + timeout
    while is_any_program_running():
        if asyncio.get_event_loop().time() >= deadline:
            return {"finished": False}
        await asyncio.sleep(0.3)
    rc = script_exec.last_exit_code
    if p is None:
        p = programs.active
    tail = [{"stream": e.stream, "text": e.text} for e in (p.log.entries if p else [])][
        -20:
    ]
    return {
        "finished": True,
        "exit_ok": rc == 0 if rc is not None else None,
        "log_tail": tail,
    }
