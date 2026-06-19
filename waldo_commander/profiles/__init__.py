"""Robot registry — discovers backends via waldoctl entry points.

Each backend registers itself in the ``waldoctl.robots`` entry-point group
via its ``pyproject.toml``.  This module provides the waldo-commander-specific
:func:`get_robot` wrapper with application defaults.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from waldoctl import Robot
from waldoctl.discovery import available_backends, load_robot_class

logger = logging.getLogger(__name__)

DEFAULT_ROBOT = "parol6"

_COMMANDER_DEFAULTS: dict[str, Any] = {
    "normalize_logs": True,
}


def _resolve_robot_name(name: str | None = None, preferred: str | None = None) -> str:
    """Determine which backend to use.

    Priority: explicit *name* > ``WALDO_ROBOT`` env var > *preferred* (the
    persisted GUI selection, honored only if installed) > single-backend
    auto-detect > :data:`DEFAULT_ROBOT`.
    """
    if name is not None:
        return name
    env_name = os.environ.get("WALDO_ROBOT")
    if env_name:
        return env_name
    backends = available_backends()
    if preferred and preferred in backends:
        return preferred
    if preferred:
        logger.warning(
            "Persisted backend %r is not installed (available: %s); "
            "ignoring and auto-detecting",
            preferred,
            ", ".join(backends) or "none",
        )
    if len(backends) == 1:
        return backends[0]
    return DEFAULT_ROBOT


def get_robot(
    name: str | None = None, preferred: str | None = None, **kwargs: Any
) -> Robot:
    """Create a Robot instance by name (or auto-detected default).

    *preferred* is the persisted GUI backend selection: used when no explicit
    *name* / ``WALDO_ROBOT`` override is given and the value is installed; a
    stale selection is ignored rather than raising. Waldo-commander defaults
    (like ``normalize_logs=True``) are applied unless overridden by the caller.
    """
    backends = available_backends()
    if not backends:
        raise RuntimeError(
            "No robot backends installed. Install one, e.g.: "
            "pip install waldo-commander[parol6]"
        )

    resolved = _resolve_robot_name(name, preferred)
    merged = {**_COMMANDER_DEFAULTS, **kwargs}

    try:
        cls = load_robot_class(resolved)
    except LookupError:
        available = ", ".join(backends)
        raise LookupError(
            f"Robot backend {resolved!r} not found. "
            f"Available: {available}. "
            f"Install with: pip install waldo-commander[{resolved}]"
        ) from None

    return cls(**merged)
