"""MCP tool modules — each registers its tools on import.

Importing this package causes every sub-module to run its
``@mcp.tool`` decorations, so :func:`waldo_commander.mcp.server.start`
just needs to ``import waldo_commander.mcp.tools`` once before kicking
off the transport.
"""

from waldo_commander.mcp.tools import (  # noqa: F401 — side-effect imports
    execution,
    motion,
    programs,
    robot,
    settings,
    status,
)
