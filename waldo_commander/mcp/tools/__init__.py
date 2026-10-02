"""MCP tool modules — each registers its tools on import.

Importing this package runs every sub-module's ``@mcp.tool`` decorations;
:func:`waldo_commander.mcp.server.get_mcp` does that import once (after building
the FastMCP instance) so every consumer sees the full tool catalogue.
"""

from waldo_commander.mcp.tools import (  # noqa: F401 — side-effect imports
    control,
    execution,
    motion,
    programs,
    robot,
    settings,
    simulation,
    status,
    world,
)
