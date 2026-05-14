"""Playback controller: simulation scrubbing, timeline playback, and script execution tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import numpy as np
from nicegui import ui, context

from waldo_commander.common.theme import PathColors
from waldo_commander.services.timeline import Timeline
from waldo_commander.state import (
    robot_state,
    simulation_state,
    ui_state,
    editor_tabs_state,
)

if TYPE_CHECKING:
    from waldo_commander.components.editor import EditorPanel

logger = logging.getLogger(__name__)


class PlaybackController:
    """Owns the bottom playback bar UI and all simulation/script playback logic."""

    def __init__(self, editor: EditorPanel) -> None:
        self._editor = editor

        # Bottom playback bar elements
        self.playback_bar: ui.element | None = None
        self._play_btn: ui.button | None = None
        self._play_btn_tooltip: ui.tooltip | None = None
        self._stop_btn: ui.button | None = None
        self._next_btn: ui.button | None = None
        self._scrub_parent: ui.element | None = None
        self._scrub_container: ui.element | None = None
        self._segment_elements: list[ui.element] = []
        self._checkpoint_markers: list[ui.element] = []
        self._tool_markers: list[ui.element] = []
        self.speed_fab: ui.fab | None = None
        self._scrub_slider: ui.slider | None = None
        self._sim_loading_progress: ui.element | None = None
        self._sim_timer: ui.timer | None = None
        self._timeline: Timeline | None = None
        self._updating_slider: bool = False
        self._last_tick_time: float = 0.0
        self._exec_start_time: float = 0.0
        self._exec_step_index: int = -1
        self._teleport_task: asyncio.Task | None = None
        self._last_highlighted_index: int = -1
        self._last_slider_update: float = 0.0  # throttle slider visual updates
        self._last_tool_selection: tuple[str, str] | None = None

    @property
    def sim_loading_progress(self) -> ui.element | None:
        return self._sim_loading_progress

    # ---- Construction / lifecycle ----

    def build_bar(self) -> None:
        """Build the bottom playback bar with controls.

        Order: Play | Stop | Next | Slider | Speed FAB | Record | Capture | Log toggle
        """
        from waldo_commander.services.motion_recorder import motion_recorder

        with (
            ui.row()
            .classes("w-full items-center gap-2 bottom-playback-bar")
            .style("min-height: 48px;") as bar
        ):
            self.playback_bar = bar

            # 1. Play/Pause button
            self._play_btn = ui.button(
                icon="play_arrow", on_click=self.toggle_play
            ).props("round dense color=positive unelevated")
            with self._play_btn:
                self._play_btn_tooltip = ui.tooltip("Play (Space)")
            self._play_btn.mark("editor-play-btn")

            # 2. Stop button
            self._stop_btn = (
                ui.button(icon="stop", on_click=self._editor._stop_script_process)
                .props("round dense color=negative unelevated")
                .tooltip("Stop")
            )
            self._stop_btn.mark("editor-stop-btn")
            self._stop_btn.set_visibility(False)

            # 3. Next step button
            self._next_btn = (
                ui.button(icon="skip_next", on_click=self.step_forward)
                .props("round dense flat color=white")
                .tooltip("Next step (S)")
            )
            self._next_btn.mark("editor-step-next")
            self._next_btn.set_visibility(False)

            # 4. Timeline scrub area — layered: segments + loading + slider
            with ui.element("div").classes("flex-1"):
                with (
                    ui.element("div").classes("relative w-full").style("height: 24px;")
                ) as scrub_parent:
                    self._scrub_parent = scrub_parent
                    self._scrub_container = (
                        ui.row()
                        .classes("absolute rounded-lg overflow-hidden gap-0")
                        .style(
                            "background: rgba(128, 128, 128, 0.2);"
                            " inset: 0; top: 0; left: 0; right: 0; bottom: 0;"
                            " position: absolute;"
                        )
                    )
                    self._scrub_container.mark("editor-scrub-bar")
                    self._sim_loading_progress = (
                        ui.linear_progress(show_value=False)
                        .classes("absolute")
                        .props("indeterminate rounded color=primary")
                        .style("position: absolute; inset: 0; height: 100%;")
                    )
                    self._sim_loading_progress.visible = False
                    self._scrub_slider = (
                        ui.slider(
                            min=0,
                            max=1.0,
                            step=0,
                            value=0,
                            on_change=self._on_scrub_change,
                        )
                        .classes("absolute timeline-slider")
                        .props(
                            "color=grey-8 thumb-color=grey-9"
                            " label label-color=grey-9 label-text-color=white"
                            ' label-value="0:00.0 / 0:00.0"'
                            " thumb-path='M 9.75 5 C 9.75 4 10.25 4 10.25 5"
                            " L 10.25 15 C 10.25 16 9.75 16 9.75 15 Z'"
                        )
                        .style("position: absolute; inset: 0; z-index: 2;")
                    )
                    self._scrub_slider.mark("editor-scrub-slider")

            # 5. Speed FAB (simulator only)
            with (
                ui.fab(icon="1x_mobiledata", color="amber", direction="up")
                .props("dense unelevated round size=sm")
                .tooltip("Playback Speed") as speed_fab
            ):
                self.speed_fab = speed_fab
                speed_fab.visible = robot_state.simulator_active
                ui.fab_action(
                    "sym_o_speed_0_5x",
                    on_click=lambda: self._set_speed(0.5),
                )
                ui.fab_action(
                    "1x_mobiledata",
                    on_click=lambda: self._set_speed(1.0),
                )
                ui.fab_action(
                    "sym_o_speed_2x",
                    on_click=lambda: self._set_speed(2.0),
                )

            # 6. Record button
            self._editor.record_btn = ui.button(
                icon="fiber_manual_record", on_click=self._editor._toggle_recording
            ).props("round dense color=negative unelevated")
            with self._editor.record_btn:
                self._editor._record_btn_tooltip = ui.tooltip("Start Recording")
            self._editor.record_btn.mark("editor-record-btn")

            # 7. Capture position
            self._editor._capture_btn = (
                ui.button(
                    icon="camera_alt", on_click=motion_recorder.capture_current_pose
                )
                .props("round dense unelevated")
                .tooltip("Capture Current Pose")
            )

            # 8. Log show/hide
            self._editor.log_toggle_btn = (
                ui.button(icon="expand_more", on_click=self._editor._toggle_log)
                .props("round dense flat")
                .classes("text-white")
            )
            with self._editor.log_toggle_btn:
                self._editor._log_toggle_btn_tooltip = ui.tooltip("Show Output")
            self._editor.log_toggle_btn.mark("editor-log-toggle")

    def setup_timers(self) -> None:
        """Create timers and register listeners. Must be called within client context."""
        simulation_state.add_change_listener(self._update_play_button)
        self._sim_timer = ui.timer(1.0 / 50, self._sim_playback_tick, active=False)

    def cleanup(self) -> None:
        """Remove listeners registered by this controller."""
        simulation_state.remove_change_listener(self._update_play_button)

    # ---- Public actions ----

    async def toggle_play(self) -> None:
        """Toggle play/pause for script execution or simulation playback."""
        if simulation_state.script_running:
            if simulation_state.is_playing:
                if self._editor._step_controller:
                    self._editor._step_controller.signal_pause()
                simulation_state.is_playing = False
                logger.debug("Script paused")
            else:
                if self._editor._step_controller:
                    self._editor._step_controller.signal_play()
                simulation_state.is_playing = True
                logger.debug("Script playing")
            self._update_play_button()
        elif robot_state.simulator_active and simulation_state.total_steps > 0:
            if simulation_state.sim_playback_active:
                self._pause_sim_playback()
            else:
                self._start_sim_playback()
        else:
            await self._editor._start_script_process()

    def step_forward(self) -> None:
        """Step forward one segment."""
        if simulation_state.script_running and self._editor._step_controller:
            self._editor._step_controller.signal_step()
            logger.debug("Step forward signal sent to script")
        elif self._timeline and simulation_state.total_steps > 0:
            next_idx = min(
                simulation_state.current_step_index + 1,
                simulation_state.total_steps - 1,
            )
            t = self._timeline.cumulative_times[next_idx]
            self._apply_time(t)

    def sync_mode(self) -> None:
        """Sync slider/speed controls to current robot mode (simulator vs robot)."""
        if self._scrub_slider:
            if robot_state.simulator_active:
                self._scrub_slider.props(remove="readonly")
            else:
                self._scrub_slider.props("readonly")
        if self.speed_fab:
            self.speed_fab.visible = robot_state.simulator_active

    # ---- Bridge API (called by EditorPanel) ----

    def invalidate_timeline(self) -> None:
        """Clear cached timeline so it gets rebuilt from new segments."""
        self._timeline = None
        self._last_tool_selection = None

    def update_scrub_segments(self) -> None:
        """Update the segmented scrub bar to match path_segments.

        Defers the actual update to the next event loop tick to avoid race
        conditions with NiceGUI's background binding refresh timer.
        """
        if not self._scrub_container:
            return
        try:
            client = context.client
        except RuntimeError:
            return

        def deferred():
            try:
                with client:
                    self._do_update_scrub_segments()
            except (RuntimeError, KeyError):
                pass

        ui.timer(0, deferred, once=True)

    def stop_playback(self) -> None:
        """Stop simulation playback and reset to start."""
        self._pause_sim_playback()
        simulation_state.sim_playback_time = 0.0

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable playback controls (except record button)."""
        buttons = [self._play_btn, self._next_btn, self.speed_fab]
        for btn in buttons:
            if btn:
                if enabled:
                    btn.enable()
                else:
                    btn.disable()

    # ---- Script execution bridge ----

    def on_script_start(self) -> None:
        """Called when a script starts running."""
        if self._scrub_slider:
            self._scrub_slider.props("label-always")
        self._update_play_button()

    def on_script_step_start(self, step: int, ui_client: Any) -> None:
        """Called when a script command starts executing."""
        with ui_client:
            self._exec_step_index = step
            self._exec_start_time = time.monotonic()
            self._ensure_timeline()
            if self._sim_timer:
                self._sim_timer.active = True
            simulation_state.current_step_index = step
            self._highlight_current_segment()
            self._editor.highlight_executing_line(step)

    def on_script_step_complete(self, step: int, ui_client: Any) -> None:
        """Called when a script command completes."""
        with ui_client:
            simulation_state.current_step_index = step
            self._highlight_current_segment()
            self._editor.highlight_executing_line(step)
            # Snap slider to segment end
            if self._timeline and self._scrub_slider:
                end_idx = min(step + 1, len(self._timeline.cumulative_times) - 1)
                t = self._timeline.cumulative_times[end_idx]
                self._scrub_slider.value = t
                text = self._format_time(t, self._timeline.total_duration)
                self._scrub_slider.props(f'label-value="{text}"')

    def on_script_stop(self, ui_client: Any) -> None:
        """Called when a script finishes or is stopped."""
        with ui_client:
            self._exec_step_index = -1
            if self._sim_timer:
                self._sim_timer.active = False
            if self._scrub_slider:
                self._scrub_slider.props(remove="label-always")
            # Snap slider to end
            if self._timeline and self._scrub_slider:
                t = self._timeline.total_duration
                self._scrub_slider.value = t
                text = self._format_time(t, t)
                self._scrub_slider.props(f'label-value="{text}"')
            self._update_play_button()

    # ---- Scrub / slider ----

    def _on_scrub_change(self, e) -> None:
        """Handle scrub slider value change (user interaction only, not programmatic)."""
        if (
            self._timeline
            and not self._updating_slider
            and not simulation_state.sim_playback_active
        ):
            self._apply_time(float(e.value), update_slider=False)
            # Update snapshot so position-change checker doesn't re-sim after scrub
            self._snapshot_joints()

    def _apply_time(self, t: float, *, update_slider: bool = True) -> None:
        """Apply a time position to the simulation: update pose, highlights, slider.

        Args:
            t: Time position in seconds.
            update_slider: If False, skip programmatic slider update (caller
                already has the right value, e.g. during user scrubbing).
        """
        tl = self._timeline
        if not tl:
            return
        simulation_state.sim_playback_time = t
        sample = tl.sample(t)

        # Sample tool position once (used for both teleport and URDF animation)
        tool_pos = tl.sample_tool(t) if tl.tool_keyframes else ()

        if sample.joints and ui_state.urdf_scene and robot_state.simulator_active:
            simulation_state.sim_pose_override = True
            ui_state.urdf_scene.set_axis_values(sample.joints)
            robot_state.angles.set_rad(np.asarray(sample.joints))
            if ui_state.control_panel:
                simulation_state.last_teleport_ts = time.monotonic()
                if self._teleport_task and not self._teleport_task.done():
                    self._teleport_task.cancel()
                self._teleport_task = asyncio.create_task(
                    self._teleport(
                        robot_state.angles.deg.tolist(),
                        list(tool_pos) if tool_pos else None,
                    )
                )

        if sample.segment_index != simulation_state.current_step_index:
            simulation_state.current_step_index = sample.segment_index
            self._highlight_current_segment()
            self._editor.highlight_executing_line(sample.segment_index)
            if ui_state.urdf_scene:
                ui_state.urdf_scene.update_playback_opacity()

        # Swap tool mesh when crossing a select_tool boundary
        if tl.tool_selection_keyframes and ui_state.urdf_scene:
            sel = tl.sample_tool_selection(t)
            if sel is not None:
                sel_pair = (sel.tool_key, sel.variant_key)
                if sel_pair != self._last_tool_selection:
                    self._last_tool_selection = sel_pair
                    # Update robot model so FK reflects the new tool
                    ui_state.active_robot.set_active_tool(
                        sel.tool_key,
                        variant_key=sel.variant_key or None,
                    )
                    ui_state.urdf_scene.apply_tool(
                        sel.tool_key,
                        variant_key=sel.variant_key or None,
                    )
                    ui_state.urdf_scene._update_tcp_ball_position()
                    # Sync to controller so readout reflects tool TCP
                    if ui_state.control_panel and ui_state.control_panel.client:
                        asyncio.create_task(
                            ui_state.control_panel.client.select_tool(
                                sel.tool_key,
                                variant_key=sel.variant_key or "",
                            )
                        )

        # Drive tool animation from timeline keyframes
        if (
            tool_pos
            and ui_state.urdf_scene
            and tool_pos != robot_state.tool_status.positions
        ):
            robot_state.tool_status.positions = tool_pos
            robot_state.tool_status.engaged = any(p > 0 for p in tool_pos)
            ui_state.urdf_scene.update_tool_animation()

        if update_slider and self._scrub_slider is not None:
            now = time.monotonic()
            # Throttle slider updates to ~10Hz to reduce WebSocket churn
            if (now - self._last_slider_update) >= 0.09:
                self._last_slider_update = now
                self._updating_slider = True
                self._scrub_slider.value = t
                self._updating_slider = False
                text = self._format_time(t, tl.total_duration)
                self._scrub_slider.props(f'label-value="{text}"')
        elif not update_slider and self._scrub_slider is not None:
            # Scrub: slider already has the right value, just update the label
            text = self._format_time(t, tl.total_duration)
            self._scrub_slider.props(f'label-value="{text}"')

    @staticmethod
    async def _teleport(joints_deg: list[float], tool_pos: list[float] | None) -> None:
        """Send a fire-and-forget teleport to the backend."""
        try:
            await ui_state.control_panel.client.teleport(
                joints_deg,
                tool_positions=tool_pos,
            )
        except Exception as exc:
            logger.warning("teleport failed: %s", exc)

    def _snapshot_joints(self) -> None:
        """Snapshot current robot joint angles to the active tab's last_sim_joints_deg."""
        active_tab = editor_tabs_state.get_active_tab()
        if active_tab is not None:
            n = ui_state.active_robot.joints.count
            active_tab.last_sim_joints_deg = robot_state.angles.deg[:n].copy()

    # ---- Simulation playback engine ----

    def _ensure_timeline(self) -> Timeline | None:
        """Build or return cached timeline from current path segments."""
        if not simulation_state.path_segments:
            self._timeline = None
            return None
        if self._timeline is None:
            self._timeline = Timeline.from_segments(
                simulation_state.path_segments,
                simulation_state.tool_actions or None,
                tool_selections=simulation_state.tool_selections or None,
            )
            simulation_state.sim_total_duration = self._timeline.total_duration
            if self._scrub_slider is not None:
                self._scrub_slider.props(f"max={self._timeline.total_duration}")
        return self._timeline

    def _start_sim_playback(self) -> None:
        """Start continuous simulation playback."""
        tl = self._ensure_timeline()
        if not tl:
            return
        if simulation_state.sim_playback_time >= tl.total_duration:
            simulation_state.sim_playback_time = 0.0
        simulation_state.sim_playback_active = True
        simulation_state.is_playing = True
        self._last_tick_time = time.monotonic()
        if self._sim_timer:
            self._sim_timer.active = True
        if self._scrub_slider:
            self._scrub_slider.props("label-always")
        self._update_play_button()

    def _pause_sim_playback(self) -> None:
        """Pause simulation playback.

        Sets last_teleport_ts so the status loop auto-clears sim_pose_override
        after the 100ms propagation delay, avoiding visual snap-back.
        """
        if self._sim_timer:
            self._sim_timer.active = False
        simulation_state.sim_playback_active = False
        # Let the auto-clear in main.py handle the handback after 100ms
        simulation_state.last_teleport_ts = time.monotonic()
        simulation_state.is_playing = False
        self._last_tool_selection = None
        # Snapshot so position-change checker doesn't re-sim
        self._snapshot_joints()
        if self._scrub_slider:
            self._scrub_slider.props(remove="label-always")
        self._update_play_button()

    def _sim_playback_tick(self) -> None:
        """30Hz tick for simulation playback or script execution slider tracking."""
        if not self._timeline:
            if self._sim_timer:
                self._sim_timer.active = False
            return

        # Script execution mode: smooth slider tracking (no URDF control)
        if simulation_state.script_running and self._exec_step_index >= 0:
            self._script_slider_tick()
            return

        # Simulation playback mode
        if not simulation_state.sim_playback_active:
            if self._sim_timer:
                self._sim_timer.active = False
            return

        now = time.monotonic()
        dt = (now - self._last_tick_time) * simulation_state.playback_speed
        self._last_tick_time = now

        t = simulation_state.sim_playback_time + dt

        if t >= self._timeline.total_duration:
            t = self._timeline.total_duration
            self._apply_time(t)
            self._pause_sim_playback()
            return

        self._apply_time(t)

    def _script_slider_tick(self) -> None:
        """Advance slider smoothly during real script execution."""
        assert self._timeline is not None
        step = self._exec_step_index
        times = self._timeline.cumulative_times
        if step < 0 or step >= len(times) - 1:
            return
        seg_start = times[step]
        seg_dur = times[step + 1] - seg_start
        if seg_dur <= 0:
            return
        elapsed = time.monotonic() - self._exec_start_time
        frac = min(elapsed / seg_dur, 1.0)
        t = seg_start + frac * seg_dur
        if self._scrub_slider is not None:
            self._updating_slider = True
            self._scrub_slider.value = t
            self._updating_slider = False
            text = self._format_time(t, self._timeline.total_duration)
            self._scrub_slider.props(f'label-value="{text}"')

    # ---- Speed control ----

    _SPEED_ICONS = {
        0.5: "sym_o_speed_0_5x",
        1.0: "1x_mobiledata",
        2.0: "sym_o_speed_2x",
    }

    def _set_speed(self, value: float) -> None:
        """Set playback speed and update FAB icon to match."""
        simulation_state.playback_speed = value
        if self.speed_fab:
            icon = self._SPEED_ICONS.get(value, "1x_mobiledata")
            self.speed_fab.props(f'icon="{icon}"')

    # ---- Play button state ----

    def _update_play_button(self) -> None:
        """Update play/pause button icon and stop/step button visibility."""
        if self._play_btn:
            playing = (
                simulation_state.script_running and simulation_state.is_playing
            ) or simulation_state.sim_playback_active
            if playing:
                self._play_btn.props("icon=pause color=warning")
                if self._play_btn_tooltip:
                    self._play_btn_tooltip.text = "Pause (Space)"
            else:
                self._play_btn.props("icon=play_arrow color=positive")
                if self._play_btn_tooltip:
                    self._play_btn_tooltip.text = "Play (Space)"

        if self._stop_btn:
            self._stop_btn.set_visibility(simulation_state.script_running)

        if self._next_btn:
            has_steps = simulation_state.total_steps > 0
            self._next_btn.set_visibility(has_steps)
            at_last = (
                (
                    simulation_state.current_step_index
                    >= simulation_state.total_steps - 1
                )
                if has_steps
                else True
            )
            if at_last and not simulation_state.script_running:
                self._next_btn.disable()
            else:
                self._next_btn.enable()

    # ---- Scrub bar segments ----

    def _do_update_scrub_segments(self) -> None:
        """Rebuild the entire scrub bar: segments, checkpoints, and tool markers.

        All elements live inside _scrub_container. A single .clear() removes
        everything; Python lists are cleared without calling .delete() on
        individual elements (they're already gone after .clear()).
        """
        if not self._scrub_container:
            return

        # 1. Remove all children at once — the only deletion point
        self._scrub_container.clear()
        self._segment_elements.clear()
        self._checkpoint_markers.clear()
        self._tool_markers.clear()
        self._last_highlighted_index = -1

        segments = simulation_state.path_segments
        if not segments:
            return

        # 2. Rebuild timeline
        self._timeline = None
        tl = self._ensure_timeline()
        total_dur = tl.total_duration if tl else 0.0
        if self._scrub_slider:
            self._scrub_slider.props(
                f'label-value="{self._format_time(0.0, total_dur)}"'
            )

        if not tl or total_dur <= 0:
            return

        step = simulation_state.current_step_index
        cum = tl.cumulative_times
        seg_durs = tl.segment_durations

        with self._scrub_container:
            # 3. Segment divs — absolute positioned by timeline position
            for idx, segment in enumerate(segments):
                color = segment.color or PathColors.CARTESIAN
                is_current = idx == step
                left_pct = cum[idx] / total_dur * 100
                width_pct = seg_durs[idx] / total_dur * 100
                opacity = "0.4" if idx < step else "1.0"
                brightness = "1.4" if is_current else "1.0"
                seg_elem = (
                    ui.element("div")
                    .classes("absolute h-full transition-all duration-150")
                    .style(
                        f"left: {left_pct:.2f}%; width: {width_pct:.2f}%;"
                        f" background-color: {color};"
                        f" opacity: {opacity}; filter: brightness({brightness});"
                    )
                )
                self._segment_elements.append(seg_elem)

            # 4. Checkpoint markers — diamonds at checkpoint times
            for cp in tl.checkpoints:
                left_pct = cp.time / total_dur * 100
                marker = (
                    ui.element("div")
                    .classes("absolute")
                    .style(
                        f"left: {left_pct:.2f}%; top: 50%; width: 8px; height: 8px;"
                        f" transform: translate(-50%, -50%) rotate(45deg);"
                        f" background: {PathColors.CHECKPOINT};"
                        f" z-index: 1; pointer-events: none;"
                    )
                )
                self._checkpoint_markers.append(marker)

            # 5. Tool action markers — full-height (blocking) or mini (overlapping)
            kf = tl.tool_keyframes
            ta = simulation_state.tool_actions
            for i in range(0, len(kf) - 1, 2):
                if (
                    kf[i].positions == kf[i + 1].positions
                    or kf[i + 1].time <= kf[i].time
                ):
                    continue
                left_pct = kf[i].time / total_dur * 100
                width_pct = (kf[i + 1].time - kf[i].time) / total_dur * 100
                action_idx = i // 2
                is_blocking = action_idx < len(ta) and ta[action_idx].sleep_offset == 0
                if is_blocking:
                    top, height, radius = "0", "100%", "0"
                else:
                    top, height, radius = "25%", "50%", "3px"
                marker = (
                    ui.element("div")
                    .classes("absolute")
                    .style(
                        f"left: {left_pct:.2f}%; top: {top}; height: {height};"
                        f" width: {max(width_pct, 0.5):.2f}%;"
                        f" background: {PathColors.TOOL_ACTION}; opacity: 0.7;"
                        f" z-index: 1; pointer-events: none;"
                        f" border-radius: {radius};"
                    )
                )
                self._tool_markers.append(marker)

    def _highlight_current_segment(self) -> None:
        """Update segment highlighting to show current position.

        Only updates the previously-highlighted and newly-highlighted elements
        (at most 2) instead of resending styles for all N segments.
        Uses style(add=...) to merge opacity/brightness without wiping
        width/color set during initial build.
        """
        if not self._segment_elements:
            return
        segments = simulation_state.path_segments
        step = simulation_state.current_step_index
        prev = self._last_highlighted_index
        self._last_highlighted_index = step

        indices_to_update = set()
        if 0 <= prev < len(self._segment_elements):
            indices_to_update.add(prev)
        if 0 <= step < len(self._segment_elements):
            indices_to_update.add(step)

        for idx in indices_to_update:
            elem = self._segment_elements[idx]
            if segments and idx < len(segments):
                is_current = idx == step
                opacity = "0.4" if idx < step else "1.0"
                brightness = "1.4" if is_current else "1.0"
                elem.style(f"opacity: {opacity}; filter: brightness({brightness});")

        # Update tool marker opacity based on current playback time
        tl = self._timeline
        if tl and self._tool_markers:
            t = simulation_state.sim_playback_time
            kf = tl.tool_keyframes
            marker_idx = 0
            for i in range(0, len(kf) - 1, 2):
                if (
                    kf[i].positions != kf[i + 1].positions
                    and kf[i + 1].time > kf[i].time
                ):
                    if marker_idx < len(self._tool_markers):
                        opacity = "0.3" if kf[i + 1].time <= t else "0.7"
                        self._tool_markers[marker_idx].style(f"opacity: {opacity};")
                    marker_idx += 1

    # ---- Utility ----

    @staticmethod
    def _format_time(current: float, total: float) -> str:
        """Format time as 'm:ss.s / m:ss.s'."""

        def fmt(s: float) -> str:
            m, s = divmod(max(0.0, s), 60)
            return f"{int(m)}:{s:04.1f}"

        return f"{fmt(current)} / {fmt(total)}"
