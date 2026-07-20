"""MCP tools mirroring WC's GUI simulator + playback controls — ``simulation.*``.

These expose the same operations as the GUI's robot/sim toggle and the bottom
playback bar's play/pause/step buttons, so an LLM can drive the simulator and
preview timeline exactly as a human would. ``set_simulator`` is a backend-only
mode switch; ``play_pause`` / ``step`` reuse the playback controller and run in
the live page's client context so any scrub-bar updates land on the browser.
"""

from __future__ import annotations

import contextlib

import waldoctl
from nicegui import Client

from waldo_commander.components.playback import playback
from waldo_commander.components.script_execution import script_exec
from waldo_commander.mcp.server import get_mcp
from waldo_commander.mcp.tools.control import require_actuation, require_control
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.state import ui_state

mcp = get_mcp()


@contextlib.contextmanager
def _page_client():
    """Enter the connected WC page's client context so UI / JS side effects land
    on the browser. Raises if no page is connected (playback controls only make
    sense with the GUI open)."""
    cid = ui_state.active_client_id
    client = Client.instances.get(cid) if cid else None
    if client is None or client._deleted:
        raise RuntimeError(
            "no active Waldo-Commander page is connected; open the GUI first"
        )
    with client:
        yield


@mcp.tool(name="simulation.set_simulator")
async def set_simulator(enabled: bool) -> dict:
    """Switch the controller between simulator and real-hardware mode.

    Mirrors the GUI's robot/sim toggle: stops any running script first (safety),
    flips the backend, and re-enables. A mode switch, not an actuation — needs
    only the control lease.
    """
    require_control()
    client = waldoctl.commander.client
    if is_any_program_running():
        # stop()'s ui.notify needs a client context (this runs in the MCP
        # background task, which has none of its own).
        with _page_client():
            await script_exec.stop()
    await client.simulator(enabled)
    waldoctl.commander.status.simulator_active = enabled
    await client.reset()
    # Same GUI sync as the robot/sim toggle — the human must never see
    # simulator styling while the backend drives real hardware. Best-effort:
    # a headless mode switch (no page connected) is still fine.
    with contextlib.suppress(RuntimeError):
        with _page_client():
            if ui_state._control_panel is not None:
                ui_state.control_panel.sync_sim_mode_visuals()
    return {"simulator_active": enabled}


@mcp.tool(name="simulation.get_mode")
async def get_mode() -> dict:
    """Report simulator mode and whether a script / preview is currently playing."""
    prog = waldoctl.commander.programs.active
    return {
        "simulator_active": waldoctl.commander.status.simulator_active,
        "is_playing": (prog.dry_run.playback.is_playing if prog is not None else False),
        "script_running": is_any_program_running(),
    }


@mcp.tool(name="simulation.play_pause")
async def play_pause() -> dict:
    """Toggle play/pause (mirrors the GUI play button).

    Controls the live script when one is running, otherwise the dry-run preview
    timeline. Passes the mode-aware actuation gate (in hardware mode this can
    launch a real program move).
    """
    require_actuation("play/pause the timeline")
    with _page_client():
        # require_actuation() above is the gate; tell toggle_play the caller
        # already holds control so its browser-side gate doesn't refuse (the
        # lease is held by MCP, not the browser).
        await playback.toggle_play(control_verified=True)
    return await get_mode()


@mcp.tool(name="simulation.step")
async def step() -> dict:
    """Step forward one segment (mirrors the GUI step button)."""
    require_actuation("step the timeline forward")
    with _page_client():
        playback.step_forward()
    return await get_mode()
