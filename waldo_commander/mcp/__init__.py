"""MCP (Model Context Protocol) server for Waldo-Commander.

When ``commander.settings.mcp.enabled`` is True, a FastMCP server runs
as a background coroutine on WC's event loop and exposes the public
``waldoctl.commander.*`` surface as MCP tools — status reads, program
lifecycle, settings, script execution, and motion primitives. An LLM
client (Claude Desktop, etc.) connects over HTTP/SSE and drives the
robot through the same API the GUI uses.

Sub-modules:

- :mod:`waldo_commander.mcp.server` — FastMCP instance + lifecycle.
- :mod:`waldo_commander.mcp.tools` — tool registration, grouped by
  namespace (status, robot, programs, execution, settings, motion).
"""

from waldo_commander.mcp.server import start_mcp_server, stop_mcp_server

__all__ = ["start_mcp_server", "stop_mcp_server"]
