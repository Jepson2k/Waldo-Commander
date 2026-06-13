"""MCP tools for script execution lifecycle — ``program.execution.*``."""

from __future__ import annotations

import waldoctl

from waldo_commander.mcp.server import get_mcp
from waldo_commander.mcp.tools.control import require_motion_allowed

mcp = get_mcp()


def _active():
    p = waldoctl.commander.programs.active
    if p is None:
        raise RuntimeError("no active program to run")
    return p


@mcp.tool(name="execution.run_active")
async def run_active() -> None:
    """Start the active program. Raises if any program is already running.

    Running a program actuates the robot, so it passes the full motion gate
    (the live ``allow_motion`` toggle plus the control lease).
    """
    require_motion_allowed()
    _active().execution.run()


@mcp.tool(name="execution.stop_active")
async def stop_active() -> None:
    """Stop the active program. No-op if it isn't running."""
    _active().execution.stop()


@mcp.tool(name="execution.pause_active")
async def pause_active() -> None:
    """Pause the active program. Requires it to be running."""
    _active().execution.pause()


@mcp.tool(name="execution.resume_active")
async def resume_active() -> None:
    """Resume the active program from pause.

    Resuming actuates the robot, so it passes the full motion gate
    (``allow_motion`` plus the control lease).
    """
    require_motion_allowed()
    _active().execution.resume()


@mcp.tool(name="execution.is_running")
async def is_running() -> bool:
    """Whether the active program is currently executing."""
    p = waldoctl.commander.programs.active
    return p is not None and p.execution.is_running
