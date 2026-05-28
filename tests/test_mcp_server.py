"""Integration tests for the MCP server and tools.

The tools are exercised against the live ``waldoctl.commander`` set up
by the ``user`` fixture, via FastMCP's in-memory transport (``Client``
takes the ``FastMCP`` instance directly — no real socket is opened).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from nicegui.testing import User

import waldoctl
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.mcp.server import get_mcp


def _payload(result: Any) -> Any:
    """Pull a Python object out of a FastMCP CallToolResult.

    ``structured_content`` is the JSON-decoded tool return value.
    Fall back to the first text block for tools that return primitives.
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


@pytest.mark.integration
async def test_mcp_server_disabled_by_default(user: User) -> None:
    """``settings.mcp.enabled`` defaults to False, so the background server
    task never spawns."""
    from waldo_commander.mcp import server as server_mod

    await user.open("/")
    await wait_for_app_ready()

    assert waldoctl.commander.settings.mcp.enabled is False
    assert server_mod._server_task is None


@pytest.mark.integration
async def test_status_tools_roundtrip(user: User) -> None:
    """One tool per read-only category returns sensible data via the
    in-memory FastMCP client."""
    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    async with Client(mcp) as client:
        pose = _payload(await client.call_tool("status.get_pose"))
        assert set(pose) >= {"x", "y", "z", "rx", "ry", "rz", "tcp_speed"}

        joints = _payload(await client.call_tool("status.get_joints"))
        assert "angles_deg" in joints and "angles_rad" in joints
        assert len(joints["angles_deg"]) == len(joints["angles_rad"])

        caps = _payload(await client.call_tool("robot.get_capabilities"))
        assert caps["name"]
        assert caps["joints"]["count"] >= 1

        connected = _payload(await client.call_tool("status.get_connected"))
        assert set(connected) == {"connected", "simulator_active"}


@pytest.mark.integration
async def test_settings_tool_writes_propagate(user: User) -> None:
    """``settings.set_jog`` updates ``commander.settings.jog`` in place."""
    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    async with Client(mcp) as client:
        original = waldoctl.commander.settings.jog.speed
        try:
            await client.call_tool("settings.set_jog", {"speed": 17})
            assert waldoctl.commander.settings.jog.speed == 17
            jog = _payload(await client.call_tool("settings.get_jog"))
            assert jog["speed"] == 17
        finally:
            waldoctl.commander.settings.jog.speed = original


@pytest.mark.integration
async def test_motion_tool_refuses_when_allow_motion_off(user: User) -> None:
    """When the user disables motion, every motion tool raises cleanly
    without touching the controller. FastMCP surfaces tool-side
    exceptions as ``ToolError`` on the client side; the message must
    mention the gating flag so the LLM can see what to fix."""
    from fastmcp.exceptions import ToolError

    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    waldoctl.commander.settings.mcp.allow_motion = False
    try:
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="allow_motion"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
    finally:
        waldoctl.commander.settings.mcp.allow_motion = True


@pytest.mark.integration
async def test_propose_and_cancel_edit_via_mcp(user: User) -> None:
    """``programs.propose_edit`` queues an edit; ``cancel_pending_edit``
    discards it. Source is unchanged because nothing was approved."""
    await user.open("/")
    await wait_for_app_ready()

    p = waldoctl.commander.programs.active
    assert p is not None, "user fixture should leave a default program open"
    p.source = "a\nb\nc\n"

    mcp = get_mcp()
    async with Client(mcp) as client:
        edit_id = _payload(
            await client.call_tool(
                "programs.propose_edit",
                {
                    "diff": "@@ -2,1 +2,1 @@\n-b\n+B\n",
                    "description": "rename b to B",
                },
            )
        )
        assert isinstance(edit_id, str) and edit_id

        pending = _payload(await client.call_tool("programs.list_pending_edits"))
        assert len(pending) == 1
        assert pending[0]["id"] == edit_id
        assert pending[0]["description"] == "rename b to B"

        await client.call_tool("programs.cancel_pending_edit", {"edit_id": edit_id})

        pending_after = _payload(await client.call_tool("programs.list_pending_edits"))
        assert pending_after == []
        assert p.source == "a\nb\nc\n"  # never applied
