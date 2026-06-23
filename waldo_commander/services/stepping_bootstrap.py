#!/usr/bin/env python3
"""
Bootstrap script for running user scripts with stepping wrapper.

This script is run as the main entry point when GUI-controlled stepping is enabled.
It patches parol6.RobotClient to wrap it with SteppingClientWrapper, then executes
the user's script.

Usage:
    python stepping_bootstrap.py <script_path>

Environment:
    WALDO_STEP_SESSION: Required. Session ID for IPC with GUI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    """Bootstrap and run user script with stepping wrapper."""
    if len(sys.argv) < 2:
        print("Usage: stepping_bootstrap.py <script_path>", file=sys.stderr)
        sys.exit(1)

    script_path = Path(sys.argv[1])
    if not script_path.exists():
        print(f"Script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    session_id = os.environ.get("WALDO_STEP_SESSION")
    if not session_id:
        print("WALDO_STEP_SESSION environment variable not set", file=sys.stderr)
        sys.exit(1)

    import importlib

    from waldo_commander.services.stepping_client import (
        SteppingClientWrapper,
        StepIO,
    )

    step_io = StepIO(session_id)

    # Set by the GUI process.
    backend_package = os.environ.get("WALDO_BACKEND_PACKAGE", "parol6")

    try:
        backend = importlib.import_module(backend_package)
        OriginalRobotClient = backend.RobotClient

        _original_robot_client = OriginalRobotClient

        class WrappedRobotClient:
            """RobotClient replacement that wraps with SteppingClientWrapper."""

            def __new__(cls, *args, **kwargs):
                original = _original_robot_client(*args, **kwargs)
                return SteppingClientWrapper(original, step_io)

        setattr(backend, "RobotClient", WrappedRobotClient)
        if hasattr(backend, "client"):
            setattr(backend.client, "RobotClient", WrappedRobotClient)

        if backend_package in sys.modules:
            setattr(sys.modules[backend_package], "RobotClient", WrappedRobotClient)
        client_mod_name = f"{backend_package}.client"
        if client_mod_name in sys.modules:
            setattr(sys.modules[client_mod_name], "RobotClient", WrappedRobotClient)

    except ImportError as e:
        print(f"Failed to import {backend_package}: {e}", file=sys.stderr)
        sys.exit(1)

    # Drop our bootstrap script from argv so the user script sees correct args.
    sys.argv = [str(script_path)] + sys.argv[2:]

    script_globals = {
        "__name__": "__main__",
        "__file__": str(script_path),
        "__builtins__": __builtins__,
    }

    script_code = script_path.read_text(encoding="utf-8")

    try:
        # Compile with the script's filename for proper tracebacks.
        code = compile(script_code, str(script_path), "exec")
        exec(code, script_globals)
    except SystemExit:
        # Let normal script termination propagate unchanged.
        raise
    except Exception:
        # Re-raise so the user script's traceback is preserved.
        raise


if __name__ == "__main__":
    main()
