"""Simulation engine: debounced + on-position-change path preview runs.

Reads the active textarea from ``editor_tabs_state.active_textarea``, mutates
``simulation_state``, and drives the path-visualizer service. Diagnostics,
line-tooltips, and target anchors are pushed directly to the ``decorations``
singleton (the strings are consumed by exactly one listener, so a state
round-trip would be ceremony).

Calls into ``playback`` are direct, not listener-based, because
``bindable_dataclass`` field assignment doesn't fire
``ChangeNotifierMixin._change_listeners``. Loading-progress visibility,
timeline invalidation, and scrub-segment rebuilds therefore call
``playback.X(...)`` explicitly after the simulation completes.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from nicegui import Client, context, ui

from waldo_commander.constants import config
from waldo_commander.components.editor_decorations import decorations
from waldo_commander.components.log_panel import log_panel
from waldo_commander.components.playback import playback
from waldo_commander.services.path_visualizer import UNCHANGED, path_visualizer
from waldo_commander.state import (
    editor_tabs_state,
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
    directly. ``set_ui_client`` is called once from ``EditorPanel.build()``
    so background tasks can route through the page client.
    """

    def __init__(self) -> None:
        self._simulation_debounce_timer: ui.timer | None = None
        self._debounce_delay: float = 1.0  # seconds of idle before running
        self._ui_client: Client | None = None

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

    # ---- Wiring ----

    def set_ui_client(self, client: Client | None) -> None:
        self._ui_client = client

    # ---- Core simulation run ----

    async def run_simulation(self, tab_id: str | None = None) -> str | None:
        """Run the simulation for the current script."""
        if tab_id is None:
            tab_id = editor_tabs_state.active_tab_id

        textarea = editor_tabs_state.active_textarea
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
        tab = editor_tabs_state.find_tab_by_id(tab_id) if tab_id else None
        if tab and (error is None or error == UNCHANGED):
            n = ui_state.active_robot.joints.count
            tab.last_sim_joints_deg = robot_state.angles.deg[:n].copy()

        if error == UNCHANGED:
            return None

        playback.invalidate_timeline()
        simulation_state.sim_playback_time = 0.0
        playback.update_scrub_segments()

        # Apply initial tool selection from script to scene and controller
        if simulation_state.tool_selections and ui_state.urdf_scene:
            first_sel = simulation_state.tool_selections[0]
            if first_sel.segment_index < 0:
                tool_key = first_sel.tool_key
                variant_key = first_sel.variant_key or None
                ui_state.active_robot.set_active_tool(
                    tool_key,
                    variant_key=variant_key,
                )
                ui_state.urdf_scene.apply_tool(
                    tool_key,
                    variant_key=variant_key,
                )
                ui_state.urdf_scene._update_tcp_ball_position()
                if ui_state.control_panel and ui_state.control_panel.client:
                    try:
                        await ui_state.control_panel.client.select_tool(
                            tool_key,
                            variant_key=variant_key or "",
                        )
                    except Exception as e:
                        logger.debug("select_tool sync failed: %s", e)

        if error:
            log_panel.push(f"[SIM ERROR] {error}")

        decorations.apply_diagnostics(error)
        decorations.push_line_metadata()
        decorations.push_target_positions()

        return error

    def schedule_debounced_simulation(self, tab_id: str | None = None) -> None:
        """Schedule a debounced simulation run when code changes.

        Cancels any pending *or running* simulation and schedules a new one
        after the debounce delay.  ``cancel(with_current_invocation=True)``
        aborts both the debounce sleep and an in-progress simulation
        subprocess, so edits never pile up stale simulations.
        """
        if tab_id is None:
            tab_id = editor_tabs_state.active_tab_id
        if not tab_id:
            return

        if self._simulation_debounce_timer is not None:
            logger.debug("DEBOUNCE: Cancelling pending/running simulation")
            self._simulation_debounce_timer.cancel(with_current_invocation=True)
            self._simulation_debounce_timer = None

        # Default-script optimization: skip simulation if content is the default snippet
        tab = editor_tabs_state.find_tab_by_id(tab_id)
        if tab and is_default_script(tab.content):
            tab.final_joints_rad = list(get_home_joints_rad())
            tab.path_segments = []
            tab.targets = []
            tab.tool_actions = []
            tab.tool_selections = []
            if tab_id == editor_tabs_state.active_tab_id:
                simulation_state.path_segments = []
                simulation_state.targets = []
                simulation_state.tool_actions = []
                simulation_state.tool_selections = []
                simulation_state.total_steps = 0
                try:
                    ui_client = self._ui_client or context.client
                    with ui_client:
                        simulation_state.notify_changed()
                except RuntimeError:
                    simulation_state.notify_changed()
                playback.update_scrub_segments()
            return

        async def run_simulation_quietly():
            try:
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
        if (
            simulation_state.script_running
            or robot_state.editing_mode
            or self._simulation_debounce_timer is not None
            or simulation_state.sim_pose_override
            or simulation_state.sim_playback_active
        ):
            return

        active_tab = editor_tabs_state.get_active_tab()
        if not active_tab or active_tab.last_sim_joints_deg is None:
            return

        textarea = editor_tabs_state.active_textarea
        if not textarea or not textarea.value:
            return

        current_deg = robot_state.angles.deg[: ui_state.active_robot.joints.count]
        if np.max(np.abs(current_deg - active_tab.last_sim_joints_deg)) > 0.5:
            self.schedule_debounced_simulation()


simulation: SimulationEngine = SimulationEngine()
