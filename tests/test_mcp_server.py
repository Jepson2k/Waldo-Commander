"""Integration tests for the MCP server and tools.

The tools are exercised against the live ``waldoctl.commander`` set up
by the ``user`` fixture, via FastMCP's in-memory transport (``Client``
takes the ``FastMCP`` instance directly — no real socket is opened).
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from nicegui.testing import User

import waldoctl
from tests.helpers.mcp import payload as _payload
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.mcp.server import get_mcp


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
async def test_hardware_motion_needs_session_consent(user: User) -> None:
    """The Autopilot hardware floor: even with motion auto-approved, the first
    real move of an MCP session is refused until a human grants consent in the
    GUI; the refusal (a ``ToolError``) tells the LLM to approve and retry."""
    from fastmcp.exceptions import ToolError

    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )

    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    set_control_mode(ControlMode.AUTOPILOT)  # motion auto — only the HW floor remains
    waldoctl.commander.status.simulator_active = False  # real hardware
    try:
        async with Client(mcp) as client:
            # Hold the lease first so the consent gate (not the lease) is the
            # blocker.
            await client.call_tool("control.take_control")
            # Refused with a consent/approve-the-prompt message (the exact text
            # depends on whether a live GUI page is connected to prompt on).
            with pytest.raises(ToolError, match="consent|prompt"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
    finally:
        waldoctl.commander.status.simulator_active = True
        control_lease.reset()  # also restores INSPECT mode


@pytest.mark.integration
async def test_denied_consent_is_terminal_for_a_cooldown(user: User) -> None:
    """Deny in the GUI must stick: the AI's immediate retry gets a terminal
    "denied" error and must NOT re-arm the prompt (no ~1s nag loop). After the
    cooldown a fresh attempt may prompt once again."""
    from fastmcp.exceptions import ToolError

    from waldo_commander.services import control_lease as cl
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        pending_consents,
        set_control_mode,
    )
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.control_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]
    mcp = get_mcp()
    set_control_mode(ControlMode.AUTOPILOT)  # exercise the hardware-consent floor
    waldoctl.commander.status.simulator_active = False  # real hardware
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")
            with pytest.raises(ToolError, match="consent|prompt"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )

            # The GUI surfaces the prompt; the human denies it.
            with ng_client:
                panel.refresh_control_indicator()
                assert panel._approval_sid is not None
                panel._resolve_approval(False)

            # Immediate retry: terminal denied error, no prompt re-armed.
            with pytest.raises(ToolError, match="denied"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
            assert pending_consents() == {}

            # Cooldown elapsed: the next attempt may prompt again.
            for sid in list(cl._denied_at):
                cl._denied_at[sid] -= cl.CONSENT_DENY_COOLDOWN_SECONDS + 1
            with pytest.raises(ToolError, match="consent|prompt"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
            assert pending_consents() != {}
    finally:
        waldoctl.commander.status.simulator_active = True
        control_lease.reset()
        panel._approval_sid = None
        if panel._consent_dialog is not None:
            panel._consent_dialog.close()


@pytest.mark.integration
async def test_set_simulator_syncs_gui_mode_visuals(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP ``simulation.set_simulator`` must drive the same GUI sync as the
    robot/sim toggle — otherwise the mode button and playback bar keep showing
    simulator styling while real hardware moves.

    The backend flip itself is stubbed out: actually leaving simulator mode
    makes the controller open the real serial port, which doesn't exist on a
    test box. The subject here is the GUI-side sync."""
    from waldo_commander.components.playback import playback
    from waldo_commander.services import control_lease as cl
    from waldo_commander.services.control_lease import control_lease
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.control_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]
    with ng_client:
        panel.update_robot_btn_visual()
        playback.sync_mode()
    assert panel._robot_btn._props.get("color") == "amber-8"  # sim styling

    flips: list[bool] = []

    async def _fake_simulator(enabled: bool) -> int:
        flips.append(enabled)
        return 1

    monkeypatch.setattr(waldoctl.commander.client, "simulator", _fake_simulator)

    mcp = get_mcp()
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")
            await client.call_tool("simulation.set_simulator", {"enabled": False})
            assert flips == [False]
            assert panel._robot_btn._props.get("color") == "grey-7", (
                "mode button must reflect hardware mode after an MCP switch"
            )
            if playback.speed_fab is not None:
                assert playback.speed_fab.visible is False
    finally:
        waldoctl.commander.status.simulator_active = True
        control_lease.reset()
        # Let the outbox flush the queued GUI updates while the app is alive —
        # an emit racing app teardown logs the spurious reconnect_timeout error.
        await asyncio.sleep(0.1)


@pytest.mark.integration
async def test_mcp_pause_resume_mirror_play_state(user: User) -> None:
    """``execution.pause_active`` / ``resume_active`` must mirror the GUI pause
    path — flip the active program's ``is_playing`` and fire the simulation
    change channel — not just signal the script subprocess."""
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.state import simulation_state

    await user.open("/")
    await wait_for_app_ready()

    set_control_mode(ControlMode.AUTOPILOT)  # subject is pause/resume mirroring
    active = waldoctl.commander.programs.active
    assert active is not None
    active.dry_run.playback.is_playing = True
    fired = {"n": 0}

    def _on_change() -> None:
        fired["n"] += 1

    simulation_state.add_change_listener(_on_change)
    mcp = get_mcp()
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")
            await client.call_tool("execution.pause_active")
            assert active.dry_run.playback.is_playing is False
            assert fired["n"] >= 1, "pause must fire the simulation change channel"

            await client.call_tool("execution.resume_active")
            assert active.dry_run.playback.is_playing is True
            assert fired["n"] >= 2, "resume must fire the simulation change channel"
    finally:
        simulation_state.remove_change_listener(_on_change)
        active.dry_run.playback.is_playing = False
        control_lease.reset()


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
        numbered = _payload(
            await client.call_tool("programs.get_source", {"numbered": True})
        )
        assert numbered.splitlines() == ["1\ta", "2\tb", "3\tc"], (
            "numbered source is what diff hunks are authored against"
        )

        proposed = _payload(
            await client.call_tool(
                "programs.propose_edit",
                {
                    "diff": "@@ -2,1 +2,1 @@\n-b\n+B\n",
                    "description": "rename b to B",
                },
            )
        )
        assert proposed["status"] == "pending", "Inspect mode: human must approve"
        edit_id = proposed["id"]

        pending = _payload(await client.call_tool("programs.list_pending_edits"))
        assert len(pending) == 1
        assert pending[0]["id"] == edit_id
        assert pending[0]["description"] == "rename b to B"

        await client.call_tool("programs.cancel_pending_edit", {"edit_id": edit_id})

        pending_after = _payload(await client.call_tool("programs.list_pending_edits"))
        assert pending_after == []
        assert p.source == "a\nb\nc\n"  # never applied

        # The withdrawal is on record — a waiter learns it immediately.
        decision = _payload(
            await client.call_tool(
                "programs.wait_edit_decision", {"edit_id": edit_id, "timeout": 5}
            )
        )
        assert decision == {"status": "withdrawn"}


@pytest.mark.integration
async def test_mcp_program_verbs_render_in_editor(user: User, tmp_path) -> None:
    """The ``programs.*`` MCP tools must render in the editor exactly like the
    GUI: ``new``/``open`` build a tab, ``switch`` follows, ``close`` tears it
    down — driven by the editor's commander.programs change listener.
    """
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    assert editor is not None
    mcp = get_mcp()

    async with Client(mcp) as client:
        # list_library(): the on-disk examples are discoverable with their
        # docstring summaries, so an LLM can open one and learn the program-side
        # motion API instead of guessing it.
        lib = _payload(await client.call_tool("programs.list_library"))
        example = next(e for e in lib if e["filename"] == "draw_circle.py")
        assert example["summary"], "library entries must carry a docstring summary"

        # new(): a tab the browser renders, with no GUI button pressed.
        new_id = _payload(
            await client.call_tool(
                "programs.new", {"filename": "mcp_new.py", "source": "print(1)\n"}
            )
        )
        await asyncio.sleep(0)
        await user.should_see(marker=f"editor-tab-{new_id}")

        # open(): read a file from disk into a rendered tab.
        path = tmp_path / "mcp_open.py"
        path.write_text("print('open')\n", encoding="utf-8")
        open_id = _payload(await client.call_tool("programs.open", {"path": str(path)}))
        await asyncio.sleep(0)
        await user.should_see(marker=f"editor-tab-{open_id}")

        # switch(): the active tab follows.
        await client.call_tool("programs.switch", {"program_id": new_id})
        await asyncio.sleep(0)
        assert editor.tabs_container.value == new_id

        # close(): the widget is torn down.
        await client.call_tool("programs.close", {"program_id": new_id})
        await asyncio.sleep(0)
        assert waldoctl.commander.programs.get(new_id) is None
        await user.should_not_see(marker=f"editor-tab-{new_id}")


@pytest.mark.integration
async def test_programs_new_becomes_active_and_reuses_same_filename(
    user: User,
) -> None:
    """``programs.new`` must make the created tab ACTIVE — ``propose_edit``
    defaults to the active program, and in the field every edit silently landed
    on the human's untitled scratch tab instead of the tab just created. A
    repeated ``new`` with the same filename (a retried call after an MCP
    reconnect) must reuse the open tab, not stack duplicates; the default
    ``untitled.py`` name is exempt so the human's scratch tab is never hijacked.
    """
    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    async with Client(mcp) as client:
        new_id = _payload(
            await client.call_tool("programs.new", {"filename": "wave.py"})
        )
        assert waldoctl.commander.programs.active_id == new_id, (
            "programs.new must switch to the tab it created"
        )

        # No program_id: the edit must land on the tab just created.
        await client.call_tool(
            "programs.propose_edit", {"diff": "@@ -0,0 +1,1 @@\n+print(1)\n"}
        )
        p = waldoctl.commander.programs.get(new_id)
        assert p is not None and p.edits.pending, (
            "propose_edit after programs.new must target the created tab"
        )

        again = _payload(
            await client.call_tool("programs.new", {"filename": "wave.py"})
        )
        assert again == new_id, "same filename must reuse the open tab"
        open_waves = [
            t for t in waldoctl.commander.programs.items if t.filename == "wave.py"
        ]
        assert len(open_waves) == 1, "no duplicate tabs for the same filename"

        u1 = _payload(await client.call_tool("programs.new", {}))
        u2 = _payload(await client.call_tool("programs.new", {}))
        assert u1 != u2, "untitled.py tabs are never deduped"


@pytest.mark.integration
async def test_mcp_lease_survives_session_churn(user: User) -> None:
    """A reconnected MCP session (fresh session id) must inherit a lease held
    by a previous MCP session instead of being refused — one field session
    churned through 9 session ids and needed ``take_control`` after every
    reconnect. Seizing from the Browser still requires an explicit
    ``take_control``."""
    from fastmcp.exceptions import ToolError

    from waldo_commander.services.control_lease import BROWSER, MCP, control_lease
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    mcp = get_mcp()
    try:
        # Lease held by a prior MCP session that is still within its TTL.
        control_lease.seize(MCP, "stale-session", "MCP session stale-se")
        async with Client(mcp) as client:
            # Gated by require_control only; a no-op while nothing is running.
            await client.call_tool("execution.stop_active")
            h = control_lease.holder()
            assert h is not None and h.channel == MCP and h.id != "stale-session", (
                "a new MCP session must inherit the lease from a prior one"
            )

        # A live Browser holder is a real arbitration boundary — still refused.
        # Must be the real page client id: liveness for BROWSER holders checks
        # Client.instances, so a made-up id would be dropped as stale.
        assert ui_state.active_client_id is not None
        control_lease.seize(BROWSER, ui_state.active_client_id, "Browser")
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="take_control"):
                await client.call_tool("execution.stop_active")
    finally:
        control_lease.reset()


@pytest.mark.integration
async def test_program_stderr_lines_carry_a_single_err_prefix(user: User) -> None:
    """Regression: stderr lines were prefixed ``[ERR] `` twice — once by the
    script runner's stream reader and again by ``_record_line`` — so every
    traceback line read ``[ERR] [ERR] ...`` in the editor log and via
    ``programs.get_log``."""
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    p = waldoctl.commander.programs.active
    assert p is not None
    # A crashing program (nonzero exit). Program source is ALSO exec'd
    # in-process by the dry-run simulation, so the crash is gated to the real
    # subprocess (the stepping bootstrap sets WALDO_STEP_SESSION there):
    # unconditional SystemExit would sail through the dry-run into the app,
    # and an unconditional RuntimeError would log a simulation ERROR that
    # trips the unexpected-ERROR-logs teardown check.
    code = (
        "import os, sys\n"
        'sys.stderr.write("boom\\n")\n'
        'if os.environ.get("WALDO_STEP_SESSION"):\n'
        '    raise RuntimeError("crash")\n'
    )
    textarea = ui_state.active_textarea
    assert textarea is not None
    textarea.value = code

    mcp = get_mcp()
    set_control_mode(ControlMode.AUTOPILOT)  # simulator: no prompts
    waldoctl.commander.status.simulator_active = True
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")
            await client.call_tool("execution.run_active")
            result = _payload(
                await client.call_tool("execution.wait_active", {"timeout": 20})
            )
            log = _payload(await client.call_tool("programs.get_log"))
        assert result["finished"] is True
        assert result["exit_ok"] is False, "a crashed program must not read as ok"
        tail_texts = [e["text"] for e in result["log_tail"]]
        assert "[ERR] boom" in tail_texts, f"log tail: {tail_texts}"
        stderr_lines = [e["text"] for e in log if e["stream"] == "stderr"]
        assert "[ERR] boom" in stderr_lines, f"stderr lines: {stderr_lines}"
        assert not any(t.startswith("[ERR] [ERR]") for t in stderr_lines), (
            f"doubled [ERR] prefix: {stderr_lines}"
        )
    finally:
        waldoctl.commander.status.simulator_active = True
        control_lease.reset()


@pytest.mark.integration
async def test_control_modes_gate_edits_and_motion(user: User) -> None:
    """The three control modes govern MCP edits + motion end-to-end (simulator):

    - **Inspect**: a proposed edit stays pending (human approves in the editor)
      and a move is refused until the human approves that specific action, after
      which the retry runs.
    - **Auto-edits**: a proposed edit auto-applies; a move still prompts.
    - **Autopilot**: a move runs with no prompt (simulator).
    """
    from fastmcp.exceptions import ToolError

    from waldo_commander.services import control_lease as cl
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.control_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]
    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "a\nb\nc\n"
    _DIFF_BB = "@@ -2,1 +2,1 @@\n-b\n+B\n"

    mcp = get_mcp()
    waldoctl.commander.status.simulator_active = True
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")

            # ---- Inspect: edit stays pending, move needs per-action approval --
            set_control_mode(ControlMode.INSPECT)
            proposed = _payload(
                await client.call_tool("programs.propose_edit", {"diff": _DIFF_BB})
            )
            assert proposed["status"] == "pending"
            await asyncio.sleep(0)
            assert p.edits.pending and p.source == "a\nb\nc\n", "Inspect must not apply"

            with pytest.raises(ToolError, match="approval|approve"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
            with ng_client:
                panel.refresh_control_indicator()
                assert panel._approval_kind == "action"
                panel._resolve_approval(True)
            # Retry the same move: the one-shot approval lets it through.
            await client.call_tool(
                "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
            )

            # ---- Auto-edits: switching through the human funnel (the settings
            # toggle) sweeps in the edit left pending under Inspect ------------
            with ng_client:
                panel._on_mode_toggle(ControlMode.AUTO_EDITS.value)
            await asyncio.sleep(0)
            assert p.edits.pending == [] and p.source == "a\nB\nc\n", (
                "switching to Auto-edits must apply edits already pending"
            )
            # A freshly proposed edit also auto-applies (and the tool says so);
            # a move still prompts.
            proposed = _payload(
                await client.call_tool(
                    "programs.propose_edit", {"diff": "@@ -3,1 +3,1 @@\n-c\n+C\n"}
                )
            )
            assert proposed["status"] == "applied", (
                "propose_edit must report the synchronous auto-apply"
            )
            await asyncio.sleep(0)
            assert p.edits.pending == [] and p.source == "a\nB\nC\n", (
                "Auto-edits must apply the proposed edit without manual approval"
            )
            with pytest.raises(ToolError, match="approval|approve"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 1, "speed": 0.1, "duration": 0.01}
                )

            # ---- Autopilot: move runs with no prompt (simulator) -------------
            set_control_mode(ControlMode.AUTOPILOT)
            await client.call_tool(
                "motion.jog_j", {"joint": 2, "speed": 0.1, "duration": 0.01}
            )
    finally:
        control_lease.reset()  # restores INSPECT
        panel._approval_sid = None
        if panel._consent_dialog is not None:
            panel._consent_dialog.close()


@pytest.mark.integration
async def test_wait_edit_decision_resolves_on_human_decision(user: User) -> None:
    """``programs.wait_edit_decision`` must block through the pending window
    and resolve as soon as the human clicks Approve/Reject in the editor —
    the replacement for spinning on ``list_pending_edits``."""
    from waldoctl import EditId

    from waldo_commander.services import control_lease as cl
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    editor = ui_state.editor_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]
    p = waldoctl.commander.programs.active
    assert editor is not None and p is not None
    p.source = "a\nb\nc\n"

    mcp = get_mcp()
    async with Client(mcp) as client:

        async def _propose_and_wait(diff: str) -> tuple[str, asyncio.Task]:
            proposed = _payload(
                await client.call_tool("programs.propose_edit", {"diff": diff})
            )
            assert proposed["status"] == "pending"
            waiter = asyncio.create_task(
                client.call_tool(
                    "programs.wait_edit_decision",
                    {"edit_id": proposed["id"], "timeout": 10},
                )
            )
            await asyncio.sleep(0.1)  # waiter is inside its poll loop
            assert not waiter.done(), "must still be waiting while pending"
            return proposed["id"], waiter

        edit_id, waiter = await _propose_and_wait("@@ -2,1 +2,1 @@\n-b\n+B\n")
        with ng_client:
            editor._approve_edit(p.id, EditId(edit_id))
        assert _payload(await waiter) == {"status": "applied"}
        assert p.source == "a\nB\nc\n"

        edit_id, waiter = await _propose_and_wait("@@ -3,1 +3,1 @@\n-c\n+C\n")
        with ng_client:
            editor._reject_edit(p.id, EditId(edit_id))
        assert _payload(await waiter) == {"status": "rejected"}
        assert p.source == "a\nB\nc\n"

        unknown = _payload(
            await client.call_tool(
                "programs.wait_edit_decision",
                {"edit_id": "no-such-edit", "timeout": 5},
            )
        )
        assert unknown == {"status": "unknown"}


@pytest.mark.integration
async def test_wait_approval_resolves_on_human_decision(user: User) -> None:
    """``control.wait_approval`` must block while an action prompt is armed and
    resolve the moment the human clicks Allow/Deny — the replacement for
    blind-retrying a refused gated call. With nothing armed it reports
    nothing_pending instead of parking."""
    from fastmcp.exceptions import ToolError

    from waldo_commander.services import control_lease as cl
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.control_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]

    mcp = get_mcp()
    waldoctl.commander.status.simulator_active = True
    try:
        async with Client(mcp) as client:
            await client.call_tool("control.take_control")
            set_control_mode(ControlMode.INSPECT)

            nothing = _payload(
                await client.call_tool("control.wait_approval", {"timeout": 0.1})
            )
            assert nothing == {"outcome": "nothing_pending"}

            async def _refuse_and_wait(joint: int) -> asyncio.Task:
                with pytest.raises(ToolError, match="wait_approval"):
                    await client.call_tool(
                        "motion.jog_j", {"joint": joint, "speed": 0.1, "duration": 0.01}
                    )
                waiter = asyncio.create_task(
                    client.call_tool("control.wait_approval", {"timeout": 10})
                )
                await asyncio.sleep(0.1)  # waiter is inside its poll loop
                assert not waiter.done(), "must still be waiting while armed"
                return waiter

            waiter = await _refuse_and_wait(0)
            with ng_client:
                panel.refresh_control_indicator()
                assert panel._approval_kind == "action"
                panel._resolve_approval(True)
            assert _payload(await waiter) == {"outcome": "allowed"}
            # The one-shot grant lets the retried call through.
            await client.call_tool(
                "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
            )

            waiter = await _refuse_and_wait(1)
            with ng_client:
                panel.refresh_control_indicator()
                panel._resolve_approval(False)
            assert _payload(await waiter) == {"outcome": "denied"}
    finally:
        control_lease.reset()
        panel._approval_sid = None
        if panel._consent_dialog is not None:
            panel._consent_dialog.close()


@pytest.mark.integration
async def test_play_pause_starts_preview_when_mcp_holds_lease(
    user: User, monkeypatch
) -> None:
    """Regression: ``simulation.play_pause`` must START the preview even though
    the MCP session holds the control lease. Before the ``control_verified``
    fix, ``toggle_play``'s browser gate refused (holder.channel == MCP) and the
    tool silently no-oped while popping a misleading toast.
    """
    from waldo_commander.components.playback import playback
    from waldo_commander.services.control_lease import ControlMode, set_control_mode

    await user.open("/")
    await wait_for_app_ready()
    mcp = get_mcp()

    # A previewable program in simulator mode, preview not yet active.
    set_control_mode(ControlMode.AUTOPILOT)  # subject is preview start, not the gate
    waldoctl.commander.status.simulator_active = True
    active = waldoctl.commander.programs.active
    assert active is not None
    active.dry_run.total_steps = 3
    active.dry_run.playback.is_active = False

    started = {"hit": False}
    monkeypatch.setattr(
        playback, "_start_sim_playback", lambda: started.__setitem__("hit", True)
    )

    async with Client(mcp) as client:
        await client.call_tool("control.take_control")  # MCP holds the lease
        await client.call_tool("simulation.play_pause")

    assert started["hit"], (
        "play_pause should start the preview when the MCP session holds the lease"
    )
