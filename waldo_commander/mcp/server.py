"""FastMCP server lifecycle for Waldo-Commander.

The server starts when ``commander.settings.mcp.enabled`` is True, runs
as a background coroutine on WC's NiceGUI event loop, and shuts down
cleanly on app teardown.

The FastMCP instance is module-global so the tool modules can import it
and register tools at module import time. The instance is constructed
lazily on first ``start_mcp_server`` call so importing this module
doesn't pull fastmcp in until the user opts in.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import waldoctl

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

_mcp: "FastMCP | None" = None
_server_task: asyncio.Task | None = None


def _make_lease_touch_middleware():
    """Build the lease-refresh middleware. Defined here (not module level) so
    importing this module doesn't pull fastmcp in before the user opts in.

    The lease ages out after a TTL of inactivity; without this, a session that
    starts a long move and then only reads status / waits would let its lease
    expire mid-motion (the "MCP is controlling" indicator would vanish while the
    arm is still moving). A keepalive task also re-touches for the whole time a
    call is in flight, so one blocking call longer than the TTL (a long move, a
    ``wait_motion``) can't age the holder out either — the TTL measures session
    absence, not call duration. Touching is best-effort — never blocks a call.
    """
    import asyncio

    from fastmcp.server.middleware import Middleware

    class _LeaseTouchMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            keepalive: asyncio.Task | None = None
            try:
                from waldo_commander.mcp.tools.control import _session_id
                from waldo_commander.services import control_lease as cl

                sid = _session_id()
                cl.control_lease.touch(cl.MCP, sid)

                async def _keep_touching() -> None:
                    while True:
                        await asyncio.sleep(cl.MCP_TTL_SECONDS / 3)
                        cl.control_lease.touch(cl.MCP, sid)

                keepalive = asyncio.create_task(_keep_touching())
            except Exception:
                logger.debug("lease touch skipped", exc_info=True)
            try:
                return await call_next(context)
            finally:
                if keepalive is not None:
                    keepalive.cancel()

    return _LeaseTouchMiddleware()


def get_mcp() -> "FastMCP":
    """Return the module-global FastMCP instance, constructing on demand.

    Tool modules call this at import time to register ``@mcp.tool``
    handlers. Constructed lazily so importing :mod:`waldo_commander.mcp`
    doesn't drag fastmcp into the process when MCP is disabled. The
    first call also triggers the tools side-effect import so every
    consumer (server start, in-memory test client) sees the full
    catalogue.
    """
    global _mcp
    if _mcp is None:
        from fastmcp import FastMCP

        _mcp = FastMCP(
            name="waldo-commander",
            instructions=(
                "Drive a PAROL6 robot arm through Waldo-Commander. "
                "Read live status, edit and run programs, and (when "
                "permitted) issue motion commands. Code edits go through "
                "propose_edit and require human approval in the editor "
                "before they apply."
            ),
        )
        # Refresh the holding session's lease on every tool call (not just
        # gated actuation) so a session that monitors a long move via reads /
        # wait_motion doesn't age out mid-motion and let the indicator vanish.
        _mcp.add_middleware(_make_lease_touch_middleware())
        # Trigger tool registration. Imported inline to avoid a circular
        # import (each tool module does ``from .server import get_mcp``).
        from waldo_commander.mcp import tools  # noqa: F401
    return _mcp


async def start_mcp_server() -> None:
    """Spawn the FastMCP server if enabled; no-op otherwise.

    Honors ``commander.settings.mcp.enabled`` at call time. Idempotent —
    subsequent calls while the server is running are no-ops.
    """
    global _server_task
    settings = waldoctl.commander.settings.mcp
    if not settings.enabled:
        logger.debug("MCP server disabled, not starting")
        return
    if _server_task is not None and not _server_task.done():
        return

    mcp = get_mcp()  # also triggers tool registration
    logger.info("Starting MCP server on http://%s:%d/mcp", settings.host, settings.port)

    async def _run() -> None:
        try:
            await mcp.run_async(
                transport="http",
                host=settings.host,
                port=settings.port,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP server crashed")

    _server_task = asyncio.create_task(_run(), name="mcp-server")


async def stop_mcp_server() -> None:
    """Cancel the background server task if running.

    Bounded by a 2-second timeout so a wedged transport doesn't block
    WC shutdown.
    """
    global _server_task
    if _server_task is None or _server_task.done():
        _server_task = None
        return
    _server_task.cancel()
    try:
        await asyncio.wait_for(_server_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        logger.exception("MCP server stop raised")
    _server_task = None
