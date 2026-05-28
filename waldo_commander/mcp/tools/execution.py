"""MCP tools for script execution lifecycle — ``program.execution.*``."""

from __future__ import annotations

import waldoctl

from waldo_commander.mcp.server import get_mcp

mcp = get_mcp()


def _active():
    p = waldoctl.commander.programs.active
    if p is None:
        raise RuntimeError("no active program to run")
    return p


@mcp.tool(name="execution.run_active")
def run_active() -> None:
    """Start the active program. Raises if any program is already running."""
    _active().execution.run()


@mcp.tool(name="execution.stop_active")
def stop_active() -> None:
    """Stop the active program. No-op if it isn't running."""
    _active().execution.stop()


@mcp.tool(name="execution.pause_active")
def pause_active() -> None:
    """Pause the active program. Requires it to be running."""
    _active().execution.pause()


@mcp.tool(name="execution.resume_active")
def resume_active() -> None:
    """Resume the active program from pause."""
    _active().execution.resume()


@mcp.tool(name="execution.is_running")
def is_running() -> bool:
    """Whether the active program is currently executing."""
    p = waldoctl.commander.programs.active
    return p is not None and p.execution.is_running
