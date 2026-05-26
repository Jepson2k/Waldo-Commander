"""Simulation engine: debounced + on-position-change path preview runs.

Reads the active textarea from ``ui_state.active_textarea``, mutates
``simulation_state``, and drives the path-visualizer service. Diagnostics,
line-tooltips, and target anchors are pushed directly to the ``decorations``
singleton via method calls — these are imperative UI operations on a known
recipient, so a state round-trip would be ceremony.

Calls into ``playback`` are direct rather than listener-routed because
the three things this module triggers — loading-progress visibility,
timeline cache invalidation, and scrub-segment rebuild — are imperative
UI operations, not state mutations. Inventing pseudo-state fields just
to drive a listener would be more ceremony than the direct call.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
from nicegui import ui

from waldo_commander.constants import config
import waldoctl
from waldoctl import LogEntry

from waldo_commander.components.editor_decorations import decorations
from waldo_commander.components.log_panel import log_panel
from waldo_commander.components.playback import playback
from waldo_commander.services.path_visualizer import UNCHANGED, path_visualizer
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.state import (
    playback_coordination,
    robot_state,
    simulation_state,
    ui_state,
)

logger = logging.getLogger(__name__)


def get_home_joints_rad() -> list[float]:
    """Get home position in radians from the active robot."""
    return ui_state.active_robot.joints.home.rad.tolist()


def default_python_snippet() -> str:
    """Initial pre-filled Python code with inlined controller host/port."""
    backend = ui_state.active_robot.backend_package
    return f"""import time
from {backend} import RobotClient

rbt = RobotClient(host={config.controller_host!r}, port={config.controller_port})

print("Moving to home position...")
rbt.home()

status = rbt.status()
print(f"Robot status: {{status}}")
"""


def is_default_script(content: str) -> bool:
    """Check if content matches the default script template (whitespace-insensitive)."""
    if not content:
        return False

    def normalize(s: str) -> str:
        return "".join(s.split())

    return normalize(content) == normalize(default_python_snippet())


class SimulationEngine:
    """Owns debounced + on-position-change path preview runs.

    Construction registers no listeners — call sites schedule simulations
    directly.
    """

    def __init__(self) -> None:
        self._simulation_debounce_timer: ui.timer | None = None
        self._debounce_delay: float = 1.0  # seconds of idle before running

    def cleanup(self) -> None:
        """Per-page cleanup — cancel any pending debounced simulation so it
        doesn't fire against a dead client."""
        if self._simulation_debounce_timer is not None:
            self._simulation_debounce_timer.cancel(with_current_invocation=True)
            self._simulation_debounce_timer = None

    def reset_for_test(self) -> None:
        """Restore field defaults by replaying ``__init__`` on this instance."""
        self.cleanup()
        type(self).__init__(self)

    # ---- Core simulation run ----

    async def run_simulation(self, tab_id: str | None = None) -> str | None:
        """Run the simulation for the current script.

        Resolves ``tab_id`` to the active tab when omitted, then captures
        the launching tab's textarea so post-sim decoration writes
        (diagnostics, line metadata, target anchors) land on that tab
        even if the user has switched away by the time the sim completes.
        """
        if tab_id is None:
            tab_id = waldoctl.commander.programs.active_id
        if tab_id is None:
            return None

        textarea = ui_state.textareas_by_tab.get(tab_id)
        content = textarea.value if textarea else ""
        if not content:
            return None

        loading = playback.sim_loading_progress
        if loading:
            loading.visible = True
        try:
            error = await path_visualizer.update_path_visualization(
                content, tab_id=tab_id
            )
        finally:
            if loading:
                loading.visible = False

        # Snapshot robot position so check_position_changed doesn't re-trigger.
        tab = waldoctl.commander.programs.get(tab_id) if tab_id else None
        if tab and (error is None or error == UNCHANGED):
            n = ui_state.active_robot.joints.count
            tab.dry_run.last_sim_joints_deg = robot_state.angles.deg[:n].copy()

        if error == UNCHANGED:
            return None

        playback.on_simulation_complete()

        # Apply initial tool selection from script to scene and controller
        sim_tool_selections = tab.dry_run.tool_selections if tab is not None else []
        if sim_tool_selections and ui_state.urdf_scene:
            first_sel = sim_tool_selections[0]
            if first_sel.segment_index < 0:
                tool_key = first_sel.tool_key
                variant_key = first_sel.variant_key or None
                ui_state.urdf_scene.apply_tool_everywhere(
                    tool_key, variant_key=variant_key
                )
                if ui_state.control_panel and ui_state.control_panel.client:
                    try:
                        await ui_state.control_panel.client.select_tool(
                            tool_key,
                            variant_key=variant_key or "",
                        )
                    except Exception as e:
                        logger.debug("select_tool sync failed: %s", e)

        if error:
            line = f"[SIM ERROR] {error}"
            sim_tab = waldoctl.commander.programs.get(tab_id) if tab_id else None
            if sim_tab is not None:
                sim_tab.log.append(
                    LogEntry(timestamp=time.time(), stream="stderr", text=line)
                )
            if sim_tab is None or sim_tab.id == waldoctl.commander.programs.active_id:
                log_panel.push(line)

        decorations.apply_diagnostics(error, tab_id)
        decorations.push_line_metadata(tab_id)
        decorations.push_target_positions(tab_id)

        return error

    def schedule_debounced_simulation(self, tab_id: str | None = None) -> None:
        """Schedule a debounced simulation run when code changes.

        Cancels any pending *or running* simulation and schedules a new one
        after the debounce delay.  ``cancel(with_current_invocation=True)``
        aborts both the debounce sleep and an in-progress simulation
        subprocess, so edits never pile up stale simulations.
        """
        if tab_id is None:
            tab_id = waldoctl.commander.programs.active_id
        if not tab_id:
            return

        if self._simulation_debounce_timer is not None:
            logger.debug("DEBOUNCE: Cancelling pending/running simulation")
            self._simulation_debounce_timer.cancel(with_current_invocation=True)
            self._simulation_debounce_timer = None

        async def run_simulation_quietly():
            try:
                # Default-script optimization: skip simulation if content
                # is the default snippet. Deferred inside the debounced
                # callback so the O(K) whitespace-normalize check runs once
                # per idle window instead of on every keystroke that
                # schedules a (re)simulation.
                tab = waldoctl.commander.programs.get(tab_id)
                if tab and is_default_script(tab.source):
                    tab.dry_run.final_joints_rad = list(get_home_joints_rad())
                    tab.dry_run.path_segments = []
                    tab.dry_run.targets = []
                    tab.dry_run.tool_actions = []
                    tab.dry_run.tool_selections = []
                    tab.dry_run.total_steps = 0
                    if tab_id == waldoctl.commander.programs.active_id:
                        simulation_state.notify_changed()
                        playback.update_scrub_segments()
                    return
                logger.debug("DEBOUNCE: Starting simulation...")
                await self.run_simulation(tab_id=tab_id)
                logger.debug("DEBOUNCE: Simulation completed successfully")
            except asyncio.CancelledError:
                logger.debug("DEBOUNCE: Simulation cancelled by newer edit")
            except Exception as e:
                logger.error("Auto-simulation failed: %s", e, exc_info=True)
                ui.notify(f"Simulation error: {e}", color="negative", timeout=3000)
            finally:
                if self._simulation_debounce_timer is my_timer:
                    self._simulation_debounce_timer = None

        logger.debug(
            "DEBOUNCE: Scheduling new timer with delay=%.3fs", self._debounce_delay
        )
        my_timer = ui.timer(self._debounce_delay, run_simulation_quietly, once=True)
        self._simulation_debounce_timer = my_timer

    def check_position_changed(self) -> None:
        """Periodically check if robot position changed and re-run path preview."""
        active_tab = waldoctl.commander.programs.active
        is_playback_active = (
            active_tab is not None and active_tab.dry_run.playback.is_active
        )
        if (
            is_any_program_running()
            or robot_state.editing_mode
            or self._simulation_debounce_timer is not None
            or playback_coordination.sim_pose_override
            or is_playback_active
        ):
            return
        if not active_tab or active_tab.dry_run.last_sim_joints_deg is None:
            return

        textarea = ui_state.active_textarea
        if not textarea or not textarea.value:
            return

        current_deg = robot_state.angles.deg[: ui_state.active_robot.joints.count]
        if np.max(np.abs(current_deg - active_tab.dry_run.last_sim_joints_deg)) > 0.5:
            self.schedule_debounced_simulation()


simulation: SimulationEngine = SimulationEngine()
