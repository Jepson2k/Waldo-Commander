"""Shared helpers for the MCP server / tool tests."""

from __future__ import annotations

import json
from typing import Any


def payload(result: Any) -> Any:
    """Pull a Python object out of a FastMCP ``CallToolResult``.

    ``structured_content`` is the JSON-decoded tool return value. Fall back to
    the first text block for tools that return primitives.
    """
    if getattr(result, "structured_content", None) is not None:
        sc = result.structured_content
        # FastMCP wraps primitive returns in {"result": value}.
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, AttributeError):
            return text
    return None
