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

from tests.helpers.mcp import payload as _payload
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.mcp.server import get_mcp
from waldo_commander.services import control_lease as cl
from waldo_commander.services.control_lease import (
    BROWSER,
    MCP,
    MCP_TTL_SECONDS,
    ControlLease,
    browser_try_acquire,
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

            # Any message marks an MCP client as connected (ambient glow) —
            # presence, not control. It decays after the connected-TTL.
            assert cl.mcp_connected()
            for s in cl._mcp_last_message:
                cl._mcp_last_message[s] -= cl.MCP_CONNECTED_TTL_SECONDS + 1
            assert not cl.mcp_connected()

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


def test_browser_try_acquire(monkeypatch: pytest.MonkeyPatch) -> None:
    """The browser gate: claim a free lease, transfer between browser tabs, and
    soft-reclaim from a live MCP holder (human actuation always seizes)."""
    control_lease.reset()
    # Make "b1"/"b2" look like live nicegui clients so the browser holder isn't
    # treated as stale.
    monkeypatch.setattr(cl.Client, "instances", {"b1": object(), "b2": object()})
    try:
        # Free → the browser claims control.
        assert browser_try_acquire("b1") is True
        assert control_lease.held_by(BROWSER, "b1")
        # Already holds → still True (no churn).
        assert browser_try_acquire("b1") is True
        # A different (active) browser tab transfers the lease to itself.
        assert browser_try_acquire("b2") is True
        assert control_lease.held_by(BROWSER, "b2")
        assert not control_lease.held_by(BROWSER, "b1")
        # Soft reclaim: a browser claim seizes even from a live MCP holder.
        control_lease.seize(MCP, "s1", "MCP s1")
        assert browser_try_acquire("b1") is True
        assert control_lease.held_by(BROWSER, "b1")
        assert not control_lease.held_by(MCP, "s1")
        # No client id (pre-init / headless) never blocks.
        assert browser_try_acquire(None) is True
    finally:
        control_lease.reset()


@pytest.mark.integration
async def test_page_reload_does_not_steal_lease_from_live_mcp_holder(
    user: User,
) -> None:
    """A page (re)load claims the lease only when it's free or held by a prior
    browser tab — never from a live MCP holder. Only human *actuation* soft-
    reclaims; a mere refresh must not silently take control from the AI."""
    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    try:
        async with Client(mcp) as client:
            took = _payload(await client.call_tool("control.take_control"))
            assert took["you_hold_it"] is True

            # Refresh: the old tab's disconnect clears the active slot (as
            # _on_disconnect does), then the page loads anew.
            ui_state.active_client_id = None
            await user.open("/")
            await wait_for_app_ready()

            controller = _payload(await client.call_tool("control.get_controller"))
            assert controller["you_hold_it"] is True, (
                "page reload must not steal the lease from a live MCP holder"
            )

        # Once the MCP holder has aged out, a reload claims as usual.
        assert control_lease._holder is not None
        control_lease._holder.last_seen -= MCP_TTL_SECONDS + 1
        ui_state.active_client_id = None
        await user.open("/")
        await wait_for_app_ready()
        assert control_lease.held_by(BROWSER, ui_state.active_client_id or "")
    finally:
        control_lease.reset()


@pytest.mark.integration
async def test_dismissed_consent_dialog_reprompts(user: User) -> None:
    """ESC/backdrop-dismissing the consent dialog (no Allow/Deny click) must not
    wedge the flow: the request stays pending and the next indicator refresh
    re-opens the prompt."""
    from waldo_commander.services.control_lease import (
        arm_consent_prompt,
        pending_consents,
        reset_consent,
    )

    await user.open("/")
    await wait_for_app_ready()
    panel = ui_state.control_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]

    try:
        arm_consent_prompt("sid-dismiss", "MCP session sid-dism")
        with ng_client:
            panel.refresh_control_indicator()
        assert panel._approval_sid == "sid-dismiss"

        # Simulate ESC/backdrop: the dialog's value flips False, no button hit.
        panel._consent_dialog.value = False
        assert panel._approval_sid is None, (
            "dismissal must clear the armed sid (it is 'not now', not a wedge)"
        )

        assert "sid-dismiss" in pending_consents()
        with ng_client:
            panel.refresh_control_indicator()
        assert panel._approval_sid == "sid-dismiss"  # re-prompted
    finally:
        reset_consent("sid-dismiss")
        panel._approval_sid = None
        if panel._consent_dialog is not None:
            panel._consent_dialog.close()
        control_lease.reset()


@pytest.mark.integration
async def test_lease_survives_single_long_tool_call(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease must stay alive for the whole of a single in-flight tool call
    (e.g. one ``wait_motion`` spanning a long move) — the TTL measures session
    absence, not motion duration."""
    from waldo_commander.services.control_lease import ControlMode, set_control_mode

    await user.open("/")
    await wait_for_app_ready()

    monkeypatch.setattr(cl, "MCP_TTL_SECONDS", 0.3)
    set_control_mode(ControlMode.AUTOPILOT)  # subject is the TTL, not the gate
    mcp = get_mcp()
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")
            # A real jog ~4x the TTL, then block in ONE wait_motion call.
            await client.call_tool(
                "motion.jog_j", {"joint": 0, "speed": 0.3, "duration": 1.2}
            )
            await client.call_tool("motion.wait_motion", {"timeout": 5.0})
            controller = _payload(await client.call_tool("control.get_controller"))
            assert controller["you_hold_it"] is True, (
                "lease must survive a single tool call longer than the TTL"
            )
    finally:
        control_lease.reset()


@pytest.mark.integration
async def test_browser_is_default_holder_and_can_reclaim(user: User) -> None:
    """The active browser tab holds control by default; an MCP session can seize
    it, and the human reclaims it by just driving (soft reclaim)."""
    await user.open("/")
    await wait_for_app_ready()

    browser_id = ui_state.active_client_id
    assert browser_id
    # Default holder: the active tab holds the lease out of the box (index_page).
    assert control_lease.held_by(BROWSER, browser_id)

    mcp = get_mcp()
    try:
        async with Client(mcp) as client:
            # MCP seizes → the browser loses control.
            await client.call_tool("control.take_control")
            assert not control_lease.held_by(BROWSER, browser_id)

            # Soft reclaim: the human just starts driving (browser_try_acquire is
            # the per-action browser gate) and seizes back from the AI.
            assert browser_try_acquire(browser_id) is True
            assert control_lease.held_by(BROWSER, browser_id)
            # The AI is now refused until it takes control again.
            with pytest.raises(ToolError, match="controlled by"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
    finally:
        control_lease.reset()
