"""Tests for the single-controller lease (``services.control_lease``).

Two layers:
- the lease state machine, exercised directly with synthetic ids;
- the MCP-side gating, exercised through FastMCP's in-memory client against the
  live ``commander`` the ``user`` fixture sets up — a live browser holder blocks
  MCP actuation until the MCP session calls ``control.take_control``.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready
from tests.test_mcp_server import _payload
from waldo_commander.mcp.server import get_mcp
from waldo_commander.services.control_lease import (
    BROWSER,
    MCP,
    MCP_TTL_SECONDS,
    ControlLease,
    control_lease,
)
from waldo_commander.state import ui_state


# --------------------------------------------------------------------------
# State machine (no app)
# --------------------------------------------------------------------------


def test_lease_starts_free_and_seize_release():
    lease = ControlLease()
    assert lease.is_free()
    assert lease.describe() == "no one"

    lease.seize(MCP, "s1", "MCP s1")
    assert lease.held_by(MCP, "s1")
    assert not lease.held_by(MCP, "s2")
    assert not lease.held_by(BROWSER, "s1")  # channel-specific
    assert lease.describe() == "MCP s1"

    lease.release(MCP, "s2")  # wrong id — no-op
    assert lease.held_by(MCP, "s1")
    lease.release(MCP, "s1")
    assert lease.is_free()


def test_lease_anyone_can_seize():
    lease = ControlLease()
    lease.seize(MCP, "s1", "MCP s1")
    lease.seize(MCP, "s2", "MCP s2")  # seizing from a live holder is allowed
    assert lease.held_by(MCP, "s2")
    assert not lease.held_by(MCP, "s1")


def test_lease_mcp_holder_ages_out():
    lease = ControlLease()
    lease.seize(MCP, "s1", "MCP s1")
    assert lease._holder is not None
    lease._holder.last_seen -= MCP_TTL_SECONDS + 1  # push past the TTL
    assert lease.is_free()
    assert lease.describe() == "no one"


def test_lease_browser_holder_stale_when_not_connected():
    lease = ControlLease()
    # An id that isn't a live nicegui Client is treated as gone immediately.
    lease.seize(BROWSER, "ghost-client", "Browser tab")
    assert lease.is_free()


def test_lease_reset_drops_holder():
    lease = ControlLease()
    lease.seize(MCP, "s1", "MCP s1")
    lease.reset()
    assert lease.is_free()


# --------------------------------------------------------------------------
# MCP gating (in-memory client against the live commander)
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_mcp_blocked_while_browser_holds_then_take_control(user: User) -> None:
    """A live browser holder blocks MCP actuation; ``take_control`` seizes it,
    and reads are never blocked."""
    await user.open("/")
    await wait_for_app_ready()

    browser_id = ui_state.active_client_id
    assert browser_id, "the active browser tab should hold the active-client slot"

    mcp = get_mcp()
    try:
        # Make the live browser tab the controller.
        control_lease.seize(BROWSER, browser_id, "Browser tab")

        async with Client(mcp) as client:
            # Reads are open to a non-holder.
            controller = _payload(await client.call_tool("control.get_controller"))
            assert controller["holder"] == "Browser tab"
            assert controller["you_hold_it"] is False

            # Actuation is refused while the browser holds control.
            with pytest.raises(ToolError, match="controlled by"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )

            # Seizing transfers the lease to this MCP session.
            took = _payload(await client.call_tool("control.take_control"))
            assert took["you_hold_it"] is True
            assert not control_lease.held_by(BROWSER, browser_id)

            controller = _payload(await client.call_tool("control.get_controller"))
            assert controller["you_hold_it"] is True

            # Releasing frees the lease again.
            await client.call_tool("control.release_control")
            controller = _payload(await client.call_tool("control.get_controller"))
            assert controller["holder"] == "no one"
    finally:
        control_lease.reset()
