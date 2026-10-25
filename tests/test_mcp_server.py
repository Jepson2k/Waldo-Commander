"""Integration tests for the MCP server and tools.

The tools are exercised against the live ``waldoctl.commander`` set up
by the ``user`` fixture, via FastMCP's in-memory transport (``Client``
takes the ``FastMCP`` instance directly — no real socket is opened).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastmcp import Client
from nicegui.testing import User

import waldoctl
from tests.helpers.mcp import payload as _payload
from tests.helpers.wait import (
    JOG_SAFE_POSE_DEG,
    teleport_to_jog_pose,
    wait_for_app_ready,
    wait_until,
)
from waldo_commander.mcp.server import get_mcp


@pytest.mark.integration
async def test_read_tools_settings_and_live_joint_speeds(user: User) -> None:
    """The server stays off unless enabled; one tool per read-only category
    returns sensible data, ``settings.set_jog`` updates
    ``commander.settings.jog`` in place, and ``status.get_joints`` reports
    live joint speeds in deg/s."""
    from waldo_commander.mcp import server as server_mod

    await user.open("/")
    await wait_for_app_ready()

    assert waldoctl.commander.settings.mcp.enabled is False
    assert server_mod._server_task is None

    async with Client(get_mcp()) as client:
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

        original = waldoctl.commander.settings.jog.speed
        try:
            await client.call_tool("settings.set_jog", {"speed": 17})
            assert waldoctl.commander.settings.jog.speed == 17
            jog = _payload(await client.call_tool("settings.get_jog"))
            assert jog["speed"] == 17
        finally:
            waldoctl.commander.settings.jog.speed = original

        # J1 swept 20° in 2 s peaks at no less than its 10°/s average, and no
        # profile peaks at more than a few times its average.
        rbt = waldoctl.commander.client
        await teleport_to_jog_pose(rbt)
        target = list(JOG_SAFE_POSE_DEG)
        target[0] -= 20.0
        assert await rbt.move_j(target, duration=2.0) >= 0
        peak = 0.0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            joints = _payload(await client.call_tool("status.get_joints"))
            peak = max(peak, abs(joints["speeds_deg_s"][0]))
            if abs(joints["angles_deg"][0] - target[0]) < 0.1:
                break
            await asyncio.sleep(0.02)
    assert 10.0 <= peak <= 40.0, f"J1 peaked at {peak}, not a deg/s reading"


@pytest.mark.integration
async def test_control_modes_approvals_and_hardware_consent_gate_mcp(
    user: User,
) -> None:
    """The three control modes govern MCP edits and motion, a refused move's
    ``control.wait_approval`` resolves on the human's Allow/Deny, and on real
    hardware the AI's first move needs GUI consent, whose denial is terminal for
    a cooldown and whose grant outlives the session until the human takes
    control back.

    - **Inspect**: a proposed edit stays pending and a move is refused until
      the human approves that specific action, after which the retry runs.
    - **Auto-edits**: a proposed edit auto-applies; a move still prompts.
    - **Autopilot**: a move runs with no prompt (simulator); on hardware the
      consent floor remains.
    """
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
    p = waldoctl.commander.programs.active
    assert p is not None
    p.source = "a\nb\nc\n"
    _DIFF_BB = "@@ -2,1 +2,1 @@\n-b\n+B\n"
    mcp = get_mcp()

    def _reset_gates() -> None:
        control_lease.reset()  # restores INSPECT, drops prompts and denials
        panel._approval_sid = None
        if panel._consent_dialog is not None:
            panel._consent_dialog.close()

    waldoctl.commander.status.simulator_active = True
    try:
        # wait_approval blocks while an action prompt is armed and resolves the
        # moment the human clicks; with nothing armed it does not park.
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
        _reset_gates()

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
            homed = await client.call_tool(
                "motion.home", {"calibrate": True, "wait": True}
            )
            assert _payload(homed) >= 0
        _reset_gates()

        # ---- Autopilot on real hardware: the session-consent floor ----------
        set_control_mode(ControlMode.AUTOPILOT)
        waldoctl.commander.status.simulator_active = False
        async with Client(mcp) as client:
            # Hold the lease first so the consent gate (not the lease) is the
            # blocker. The refusal text depends on whether a live GUI page is
            # connected to prompt on.
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

            with ng_client:
                panel.refresh_control_indicator()
                panel._resolve_approval(True)
            await client.call_tool(
                "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
            )

        # A reconnect is a new session; the grant carries over.
        async with Client(mcp) as client:
            await client.call_tool(
                "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
            )
            assert pending_consents() == {}

            with ng_client:
                panel.refresh_control_indicator()
            user.find(marker="btn-take-control").click()
            assert await wait_until(
                lambda: control_lease.held_by(cl.BROWSER, ui_state.active_client_id)
            ), "Take control must hand the lease to the browser"
            await client.call_tool("control.take_control")
            with pytest.raises(ToolError, match="consent|prompt"):
                await client.call_tool(
                    "motion.jog_j", {"joint": 0, "speed": 0.1, "duration": 0.01}
                )
    finally:
        waldoctl.commander.status.simulator_active = True
        _reset_gates()


@pytest.mark.integration
async def test_mcp_drives_the_same_gui_state_as_the_page(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While an MCP session holds the lease, its pause/resume, play/pause and
    simulator switch drive the same GUI state as the page's own controls:
    the play state and simulation change channel, the preview, and the mode
    button and playback bar styling."""
    import numpy as np

    from waldo_commander.components.playback import playback
    from waldo_commander.services import control_lease as cl
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.services.preview_segments import segments_from_record
    from waldo_commander.state import simulation_state, ui_state

    await user.open("/")
    await wait_for_app_ready()

    panel = ui_state.control_panel
    ng_client = cl.Client.instances[ui_state.active_client_id]
    with ng_client:
        panel.update_robot_btn_visual()
        playback.sync_mode()
    assert panel._robot_btn._props.get("color") == "wc-mode-sim"  # simulator fill

    set_control_mode(ControlMode.AUTOPILOT)  # the subject is mirroring, not gates
    active = waldoctl.commander.programs.active
    assert active is not None
    fired = {"n": 0}

    def _on_change() -> None:
        fired["n"] += 1

    flips: list[bool] = []

    async def _fake_simulator(enabled: bool) -> int:
        flips.append(enabled)
        return 1

    try:
        async with Client(get_mcp()) as client:
            await client.call_tool("control.take_control")

            # pause_active / resume_active mirror the GUI pause path: flip the
            # active program's is_playing and fire the simulation change
            # channel, not just signal the script subprocess.
            active.dry_run.playback.is_playing = True
            simulation_state.add_change_listener(_on_change)
            await client.call_tool("execution.pause_active")
            assert active.dry_run.playback.is_playing is False
            assert fired["n"] >= 1, "pause must fire the simulation change channel"
            await client.call_tool("execution.resume_active")
            assert active.dry_run.playback.is_playing is True
            assert fired["n"] >= 2, "resume must fire the simulation change channel"
            active.dry_run.playback.is_playing = False

            # play_pause starts the preview of a previewed program although the
            # lease is held by MCP, not the browser, and pauses it again.
            rows = 251
            q = np.asarray(waldoctl.commander.status.joints.angles.rad, np.float32)
            commanded = waldoctl.TickIndex(
                row_dt_s=0.02,
                joints_rad=np.tile(q, (rows, 1)),
                tcp=np.zeros((rows, 6), dtype=np.float32),
                tool_closed=np.zeros(rows, dtype=np.float32),
                tool_gripping=np.zeros(rows, dtype=np.bool_),
                blocks=(
                    waldoctl.TickBlock(
                        command=0, start_row=0, rows=rows, line_number=1
                    ),
                ),
                digest=b"commanded",
            )
            active.dry_run.commanded = commanded
            active.dry_run.path_segments = segments_from_record(commanded, [])
            active.dry_run.total_steps = 1
            playback.invalidate_timeline()
            await client.call_tool("simulation.play_pause")
            assert active.dry_run.playback.is_active, (
                "play_pause should start the preview when the MCP session holds "
                "the lease"
            )
            # The page plays the preview without taking the lease back from
            # the session that started it.
            assert await wait_until(
                lambda: active.dry_run.playback.playback_time > 0.2, timeout_s=5.0
            )
            holder = control_lease.holder()
            assert holder is not None and holder.channel == cl.MCP, holder
            await client.call_tool("simulation.play_pause")
            assert not active.dry_run.playback.is_active

            # simulation.set_simulator drives the same GUI sync as the robot/sim
            # toggle, or the mode button keeps simulator styling while real
            # hardware moves. The backend flip itself is stubbed: leaving
            # simulator mode makes the controller open the real serial port,
            # which doesn't exist on a test box.
            monkeypatch.setattr(waldoctl.commander.client, "simulator", _fake_simulator)
            await client.call_tool("simulation.set_simulator", {"enabled": False})
            assert flips == [False]
            assert panel._robot_btn._props.get("color") == "wc-control", (
                "mode button must reflect hardware mode after an MCP switch"
            )
            if playback.speed_fab is not None:
                assert playback.speed_fab.visible is True
                assert playback._speed_2x.visible is False
    finally:
        simulation_state.remove_change_listener(_on_change)
        active.dry_run.playback.is_playing = False
        active.dry_run.playback.is_active = False
        active.dry_run.commanded = None
        active.dry_run.path_segments = []
        active.dry_run.total_steps = 0
        playback.invalidate_timeline()
        waldoctl.commander.status.simulator_active = True
        control_lease.reset()
        # Let the outbox flush the queued GUI updates while the app is alive —
        # an emit racing app teardown logs the spurious reconnect_timeout error.
        await asyncio.sleep(0.1)


@pytest.mark.integration
async def test_mcp_program_tools_edit_and_render_in_editor(
    user: User, tmp_path
) -> None:
    """The ``programs.*`` tools: a proposed edit waits for the human and every
    resolution (cancel, approve, reject) reaches a waiter; new/open/switch/
    close render in the editor exactly like the GUI, through the editor's
    commander.programs change listener; ``new`` activates its tab and reuses
    an open one of the same filename."""
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
    assert editor is not None
    assert p is not None, "user fixture should leave a default program open"
    p.source = "a\nb\nc\n"

    async with Client(get_mcp()) as client:
        numbered = _payload(
            await client.call_tool("programs.get_source", {"numbered": True})
        )
        assert numbered.splitlines() == ["1\ta", "2\tb", "3\tc"], (
            "numbered source is what diff hunks are authored against"
        )

        # propose_edit queues an edit; cancel_pending_edit discards it unapplied.
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

        # wait_edit_decision blocks through the pending window and resolves as
        # soon as the human clicks Approve/Reject in the editor.
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

        # list_library(): the on-disk examples are discoverable with their
        # docstring summaries, so an LLM can open one and learn the program-side
        # motion API instead of guessing it.
        lib = _payload(await client.call_tool("programs.list_library"))
        example = next(e for e in lib if e["filename"] == "draw_circle.py")
        assert example["summary"], "library entries must carry a docstring summary"

        # new(): a tab the browser renders, with no GUI button pressed.
        initial = len(waldoctl.commander.programs.items)
        new_id = _payload(
            await client.call_tool(
                "programs.new", {"filename": "mcp_new.py", "source": "print(1)\n"}
            )
        )
        await asyncio.sleep(0)
        assert len(waldoctl.commander.programs.items) == initial + 1
        await user.should_see(marker=f"editor-tab-{new_id}")

        # open(): read a file from disk into a rendered, non-dirty tab.
        path = tmp_path / "mcp_open.py"
        path.write_text("print('open')\n", encoding="utf-8")
        open_id = _payload(await client.call_tool("programs.open", {"path": str(path)}))
        await asyncio.sleep(0)
        await user.should_see(marker=f"editor-tab-{open_id}")
        opened = waldoctl.commander.programs.get(open_id)
        assert opened is not None and opened.file_path == str(path)
        assert not opened.is_dirty

        # switch(): the active tab follows.
        await client.call_tool("programs.switch", {"program_id": new_id})
        await asyncio.sleep(0)
        assert waldoctl.commander.programs.active_id == new_id
        assert editor.tabs_container.value == new_id

        # close(): the widget is torn down.
        await client.call_tool("programs.close", {"program_id": new_id})
        await asyncio.sleep(0)
        assert waldoctl.commander.programs.get(new_id) is None
        await user.should_not_see(marker=f"editor-tab-{new_id}")

        # new() must make the created tab ACTIVE — propose_edit defaults to the
        # active program, and in the field every edit silently landed on the
        # human's scratch tab instead. A repeated new() with the same filename
        # (a retried call after a reconnect) reuses the open tab; the default
        # untitled.py name is exempt so the scratch tab is never hijacked.
        wave_id = _payload(
            await client.call_tool("programs.new", {"filename": "wave.py"})
        )
        assert waldoctl.commander.programs.active_id == wave_id, (
            "programs.new must switch to the tab it created"
        )
        await client.call_tool(
            "programs.propose_edit", {"diff": "@@ -0,0 +1,1 @@\n+print(1)\n"}
        )
        wave = waldoctl.commander.programs.get(wave_id)
        assert wave is not None and wave.edits.pending, (
            "propose_edit after programs.new must target the created tab"
        )
        again = _payload(
            await client.call_tool("programs.new", {"filename": "wave.py"})
        )
        assert again == wave_id, "same filename must reuse the open tab"
        open_waves = [
            t for t in waldoctl.commander.programs.items if t.filename == "wave.py"
        ]
        assert len(open_waves) == 1, "no duplicate tabs for the same filename"
        u1 = _payload(await client.call_tool("programs.new", {}))
        u2 = _payload(await client.call_tool("programs.new", {}))
        assert u1 != u2, "untitled.py tabs are never deduped"


@pytest.mark.integration
async def test_mcp_runs_programs_and_nothing_else_drives_meanwhile(
    user: User,
) -> None:
    """A program run through MCP reports a crash and its stderr lines with a
    single ``[ERR]`` prefix; while a program holds the robot an MCP move is
    refused with a reason that names the program instead of interleaving with
    the program's own moves, and the refusal ends with the run."""
    from fastmcp.exceptions import ToolError

    from waldo_commander.services.control_lease import (
        ControlMode,
        control_lease,
        set_control_mode,
    )
    from waldo_commander.services.programs import is_any_program_running
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    user.find(marker="tab-program").click()
    await asyncio.sleep(0)

    textarea = ui_state.active_textarea
    assert textarea is not None
    # The dry-run preview runs the source too, so the crash and the hold are
    # gated to the real subprocess (the stepping bootstrap sets
    # WALDO_STEP_SESSION there): in the preview a raise is a simulation ERROR,
    # which trips the unexpected-ERROR-logs teardown check.
    textarea.value = (
        "import os, sys\n"
        'sys.stderr.write("boom\\n")\n'
        'if os.environ.get("WALDO_STEP_SESSION"):\n'
        '    raise RuntimeError("crash")\n'
    )

    set_control_mode(ControlMode.AUTOPILOT)  # simulator: no prompts
    waldoctl.commander.status.simulator_active = True
    try:
        async with Client(get_mcp()) as client:
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
            # Regression: the runner's stream reader and _record_line both
            # prefixed stderr, so every line read ``[ERR] [ERR] ...``.
            stderr_lines = [e["text"] for e in log if e["stream"] == "stderr"]
            assert "[ERR] boom" in stderr_lines, f"stderr lines: {stderr_lines}"
            assert not any(t.startswith("[ERR] [ERR]") for t in stderr_lines), (
                f"doubled [ERR] prefix: {stderr_lines}"
            )

            textarea.value = (
                "import os, time\n"
                'if os.environ.get("WALDO_STEP_SESSION"):\n'
                "    time.sleep(30)\n"
            )
            home = list(waldoctl.commander.status.joints.angles.deg)
            target = [home[0] + 5.0, *home[1:]]
            await client.call_tool("execution.run_active")
            for _ in range(100):
                if is_any_program_running():
                    break
                await asyncio.sleep(0.05)
            assert is_any_program_running()

            with pytest.raises(ToolError, match="A program is moving the robot"):
                await client.call_tool("motion.move_j", {"angles": target})

            await client.call_tool("execution.stop_active")
            await client.call_tool("motion.move_j", {"angles": target, "wait": True})
        assert abs(waldoctl.commander.status.joints.angles.deg[0] - target[0]) < 1.0
    finally:
        if is_any_program_running():
            async with Client(get_mcp()) as client:
                await client.call_tool("execution.stop_active")
        control_lease.reset()
