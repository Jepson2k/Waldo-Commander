"""MCP tools for script execution lifecycle — ``execution.*``.

Wired to the GUI's ``script_exec`` controller (the same backend the play button
uses); ``start``/``stop`` reads/writes the editor in the live page's client
context. Running or resuming actuates the robot, so they pass the full
``require_actuation`` gate.
"""

from __future__ import annotations

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
    """Start the active program. Raises if a program is already running.

    Running a program actuates the robot, so it passes the full actuation gate
    (the control lease plus, on real hardware, one-time per-session consent).
    """
    if is_any_program_running():
        raise RuntimeError("a program is already running; stop it first")
    _ensure_active()
    require_actuation()
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
    require_actuation()
    with _page_client():
        playback.set_script_playing(True)


@mcp.tool(name="execution.is_running")
async def is_running() -> bool:
    """Whether a program is currently executing."""
    return is_any_program_running()
