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
        async def on_message(self, context, call_next):
            # Presence, not control: any message (initialize, list_tools, ping)
            # marks an MCP client as connected for the ambient glow.
            try:
                from waldo_commander.mcp.tools.control import _session_id
                from waldo_commander.services import control_lease as cl

                cl.mcp_touch(_session_id())
            except Exception:
                logger.debug("mcp presence touch skipped", exc_info=True)
            return await call_next(context)

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
                "Drive a PAROL6 robot arm through Waldo-Commander, a GUI the "
                "human is watching. Read live status freely. Start a session by "
                "reading control.get_controller (who drives, which control "
                "mode) and robot.get_capabilities (once — it doesn't change).\n\n"
                "Put ALL code you write into a VISIBLE program in the editor so "
                "the human can see and scrub it — even a quick throwaway. Never "
                "run code that doesn't appear in the editor, and don't fire a "
                "long series of direct motion commands when a program would let "
                "the human watch the path. Reserve the direct motion.* tools "
                "(jog_j, jog_l, move_j, move_l, home) for single ad-hoc nudges; "
                "they return a command index immediately — pass wait=true or "
                "call motion.wait_motion to know a move finished.\n\n"
                "The program workflow: check programs.list first and switch to "
                "your tab from an earlier attempt instead of creating a "
                "duplicate; otherwise programs.new (with a real filename — it "
                "becomes the active tab) or programs.open. Programs are plain "
                "Python scripts run in a subprocess — they drive the robot "
                "through the backend client library, NOT these MCP tools; call "
                "programs.list_library and open a worked example to learn that "
                "API before authoring your first program. To edit, read "
                "programs.get_source(numbered=true) IMMEDIATELY before "
                "programs.propose_edit and match its line numbers and context "
                "exactly — diffs apply with no fuzzy matching.\n\n"
                "propose_edit returns whether the edit applied or is pending "
                "human approval. When pending, tell the human what you proposed "
                "and block on programs.wait_edit_decision — never spin on "
                "list_pending_edits. To run: execution.run_active starts the "
                "subprocess and returns; block on execution.wait_active and "
                "read its log_tail (or programs.get_log) before claiming "
                "success — a program that crashed on line one looks exactly "
                "like one that finished.\n\n"
                "Approval depends on the human's control mode (from "
                "control.get_controller): in Inspect, every edit and every move "
                "is approved individually; in Auto-edits, edits apply "
                "immediately but each move is still approved; in Autopilot, "
                "both are automatic (real hardware still asks once per "
                "session). A refusal names what's pending — block on "
                "control.wait_approval and retry once it reports allowed; "
                "never spin the refused call. A denial means change approach, "
                "not retry."
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
