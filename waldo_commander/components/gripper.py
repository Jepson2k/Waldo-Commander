import logging
import time
from collections.abc import Callable

from nicegui import ui

import waldoctl
from waldoctl import (
    ElectricGripperTool,
    GripperTool,
    RobotClient,
)

from waldo_commander.constants import config
from waldo_commander.services.camera_service import camera_service
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.state import robot_state

logger = logging.getLogger(__name__)

# Chart colors
_CLR_POS = "#2dd4bf"  # teal-400
_CLR_CUR = "#fbbf24"  # amber-400


def _make_mark_line(value: float, color: str, name: str) -> dict:
    return {
        "silent": True,
        "symbol": "none",
        "animation": False,
        "lineStyle": {"type": "dashed", "color": color, "width": 1},
        "label": {"show": False},
        "data": [{"yAxis": value, "name": name}],
    }


# Tool state dot colors (by ToolStatus.state int)
_STATE_DOTS: dict[int, tuple[str, str]] = {
    0: ("var(--ctk-muted)", "Off"),
    1: ("var(--color-sky-400)", "Idle"),
    2: ("var(--color-emerald-400)", "Active"),
    3: ("var(--color-red-400)", "Error"),
}


class GripperPage:
    """Gripper tab page — camera, time series, status, and controls."""

    def __init__(self, client: RobotClient) -> None:
        self.client = client
        self._last_current_tool_key: str | None = None
        self._current_range_listener: Callable | None = None
        # Live slider state
        self._slider_drag_ts: float = 0.0
        self._last_slider_send: float = 0.0
        self._user_dragging: bool = False
        # Target position initialized flag
        self._target_initialized: bool = False

    # ---- Helpers ----

    def _get_active_gripper(self) -> GripperTool | None:
        try:
            tool = self.client.tool
        except (RuntimeError, KeyError, NotImplementedError):
            return None
        return tool if isinstance(tool, GripperTool) else None

    def _is_electric(self) -> bool:
        tool = self._get_active_gripper()
        return isinstance(tool, ElectricGripperTool)

    # ---- Actions ----

    async def _grip_set(self, position: float, label: str) -> None:
        try:
            tool = self._get_active_gripper()
            if tool is None:
                ui.notify("No gripper available", color="warning")
                return
            spd_kwargs: dict = {}
            if isinstance(tool, ElectricGripperTool):
                if waldoctl.commander.settings.gripper.speed_sync:
                    spd_kwargs["speed"] = waldoctl.commander.settings.jog.speed / 100.0
                else:
                    spd_kwargs["speed"] = (
                        waldoctl.commander.settings.gripper.speed / 100.0
                    )
                if self._cur_slider:
                    spd_kwargs["current"] = int(self._cur_slider.value)
            await tool.set_position(position, **spd_kwargs)
            motion_recorder.record_action("gripper", position=position, **spd_kwargs)
        except Exception as e:
            logger.error("Gripper %s failed: %s", label.lower(), e)
            ui.notify(f"{label} failed: {e}", color="negative")

    # ---- Build ----

    def build(self) -> None:
        self._pos_slider: ui.slider | None = None
        self._cur_slider: ui.slider | None = None
        self._combined_chart: ui.echart | None = None
        self._camera_card: ui.card | None = None
        self._camera_image: ui.interactive_image | None = None
        self._last_camera_active: bool = camera_service.active

        # Status elements (updated from status consumer)
        self._state_dot: ui.icon | None = None
        self._state_label: ui.label | None = None
        self._part_dot: ui.icon | None = None
        self._engaged_dot: ui.icon | None = None
        self._fault_label: ui.label | None = None

        # Dirty checking for status updates
        self._last_status_key: tuple = ()

        # Current max for chart Y axis — set when chart is built lazily
        self._current_max: float = 0.0
        # Dirty flag for markLine-only changes (avoids competing update() calls)
        self._mark_lines_dirty: bool = False

        _tile = "bg-neutral-800 p-2 rounded"
        with ui.column().classes("w-full gap-2"):
            # Camera section (visible when camera is active)
            self._camera_card = (
                ui.card()
                .props("flat")
                .classes("w-full p-0 overflow-hidden rounded bg-neutral-800")
            )
            with self._camera_card:
                self._camera_image = (
                    ui.interactive_image(
                        "/tool/camera/stream",
                        cross=True,
                    )
                    .classes("w-full rounded")
                    .mark("gripper-camera-section")
                )
            self._camera_card.set_visibility(camera_service.active)

            with ui.row().classes("w-full gap-2 items-stretch"):
                with (
                    ui.card()
                    .props("flat")
                    .classes(f"flex-1 min-w-65 overflow-hidden {_tile}")
                ):
                    self._chart_column = ui.column().classes("w-full")
                with ui.card().props("flat").classes(f"shrink-0 {_tile}"):
                    self._build_status_column()
                with ui.card().props("flat").classes(f"shrink-0 {_tile}"):
                    self._build_controls_column()

    # ---- Combined dual-axis chart ----

    def _build_chart(self) -> None:
        y_axis_left: dict = {
            "type": "value",
            "name": "%",
            "nameTextStyle": {"fontSize": 11, "color": _CLR_POS},
            "axisLabel": {"fontSize": 11, "color": _CLR_POS},
            "splitLine": {"lineStyle": {"color": "rgba(128,128,128,0.15)"}},
            "min": 0,
            "max": 100,
        }
        y_axis_right: dict = {
            "type": "value",
            "name": "mA",
            "nameTextStyle": {"fontSize": 11, "color": _CLR_CUR},
            "axisLabel": {"fontSize": 11, "color": _CLR_CUR},
            "splitLine": {"show": False},
            "min": 0,
        }
        if self._current_max > 0:
            y_axis_right["max"] = self._current_max

        self._combined_chart = (
            ui.echart(
                {
                    "animation": True,
                    "animationDuration": 50,
                    "animationEasing": "linear",
                    "renderer": "svg",
                    "grid": {
                        "top": 24,
                        "right": 48,
                        "bottom": 4,
                        "left": 38,
                        "containLabel": False,
                    },
                    "legend": {
                        "data": ["Position", "Current"],
                        "top": 0,
                        "left": 40,
                        "textStyle": {"fontSize": 11, "color": "var(--ctk-text)"},
                        "itemWidth": 12,
                        "itemHeight": 8,
                    },
                    "xAxis": {
                        "type": "time",
                        "axisLabel": {"show": False},
                        "axisTick": {"show": False},
                        "splitLine": {"show": False},
                        "axisLine": {"show": False},
                    },
                    "yAxis": [y_axis_left, y_axis_right],
                    "series": [
                        {
                            "name": "Position",
                            "type": "line",
                            "yAxisIndex": 0,
                            "showSymbol": False,
                            "smooth": True,
                            "lineStyle": {"width": 1.5, "color": _CLR_POS},
                            "itemStyle": {"color": _CLR_POS},
                            "markLine": _make_mark_line(0, _CLR_POS, "target"),
                            "data": [],
                        },
                        {
                            "name": "Current",
                            "type": "line",
                            "yAxisIndex": 1,
                            "showSymbol": False,
                            "smooth": True,
                            "lineStyle": {"width": 1.5, "color": _CLR_CUR},
                            "itemStyle": {"color": _CLR_CUR},
                            "markLine": _make_mark_line(0, _CLR_CUR, "limit"),
                            "data": [],
                        },
                    ],
                }
            )
            .classes("w-full")
            .style("height: 100px;")
            .mark("gripper-chart")
        )

    def _ensure_chart_built(self) -> bool:
        """Build chart lazily when tool is first available. Returns True if chart exists."""
        if self._combined_chart is not None:
            return True
        tool = self._get_active_gripper()
        if tool is None:
            return False
        # Compute current max from tool spec
        if isinstance(tool, ElectricGripperTool):
            for ch in tool.channel_descriptors:
                if ch.name == "Current" and ch.max > 0:
                    self._current_max = ch.max
        with self._chart_column:
            self._build_chart()
        return True

    def update_chart(self) -> None:
        if not self._ensure_chart_built():
            return
        result = robot_state.tool_time_series.get_series_if_dirty()
        if result is None and not self._mark_lines_dirty:
            return
        self._mark_lines_dirty = False

        target_pos_pct = round(
            waldoctl.commander.settings.gripper.target_position * 100, 1
        )
        current_limit = waldoctl.commander.settings.gripper.current

        if result is not None:
            timestamps, positions, currents = result
            ts_ms = [t * 1000 for t in timestamps]
            self._combined_chart.run_chart_method(  # ty: ignore[unresolved-attribute]
                "setOption",
                {
                    "series": [
                        {
                            "data": [
                                [t, round(p * 100, 1)] for t, p in zip(ts_ms, positions)
                            ],
                            "markLine": _make_mark_line(
                                target_pos_pct, _CLR_POS, "target"
                            ),
                        },
                        {
                            "data": [[t, round(c, 1)] for t, c in zip(ts_ms, currents)],
                            "markLine": _make_mark_line(
                                current_limit, _CLR_CUR, "limit"
                            ),
                        },
                    ]
                },
            )
        else:
            # markLines-only update (no new time series data)
            self._combined_chart.run_chart_method(  # ty: ignore[unresolved-attribute]
                "setOption",
                {
                    "series": [
                        {
                            "markLine": _make_mark_line(
                                target_pos_pct, _CLR_POS, "target"
                            )
                        },
                        {"markLine": _make_mark_line(current_limit, _CLR_CUR, "limit")},
                    ]
                },
            )

    def set_target_position(self, position: float) -> None:
        """Set target position and update the slider. Called by control panel actions."""
        waldoctl.commander.settings.gripper.target_position = position
        if self._pos_slider is not None:
            self._pos_slider.set_value(round(position * 100))
        self._update_mark_lines()

    def set_target_current(self, current: int) -> None:
        """Set target current and update the slider. Called by control panel adjust."""
        waldoctl.commander.settings.gripper.current = current
        if self._cur_slider is not None:
            self._cur_slider.set_value(current)
        self._update_mark_lines()

    def _update_mark_lines(self) -> None:
        """Mark markLines dirty so the next update_chart() tick pushes them."""
        self._mark_lines_dirty = True

    # ---- Live slider ----

    def _on_slider_pan(self, e) -> None:
        """Track user drag state via Quasar pan event (not fired on programmatic changes)."""
        self._user_dragging = e.args in ("start", True)

    async def _on_slider_drag(self, e) -> None:
        """Throttled handler for continuous slider drag — streams position at jog rate."""
        if not self._user_dragging:
            return
        self._slider_drag_ts = time.monotonic()
        now = self._slider_drag_ts
        if now - self._last_slider_send < self._slider_interval:
            return
        self._last_slider_send = now
        value = e.value
        pos = value / 100.0
        waldoctl.commander.settings.gripper.target_position = pos
        self._update_mark_lines()
        await self._grip_set(pos, "Set")

    async def _on_current_slider_change(self, e) -> None:
        """Sync current slider value to ui_state, update markLine, and send to gripper."""
        value = e.value
        waldoctl.commander.settings.gripper.current = int(value)
        self._mark_lines_dirty = True
        if not self._user_dragging:
            return
        # Send position command with current target position and updated current limit
        tool = self._get_active_gripper()
        if isinstance(tool, ElectricGripperTool):
            try:
                await tool.set_position(
                    waldoctl.commander.settings.gripper.target_position,
                    current=int(value),
                )
            except Exception as exc:
                logger.debug("Current limit update failed: %s", exc)

    # ---- Status updates (called from status consumer) ----

    def update_status(self) -> None:
        """Update all status fields from robot_state. Called from status consumer."""
        # Sync camera card visibility and panel size
        if self._camera_card is not None:
            cam_active = camera_service.active
            self._camera_card.set_visibility(cam_active)
            if cam_active != self._last_camera_active:
                self._last_camera_active = cam_active
                preset = "camera" if cam_active else "default"
                ui.run_javascript(f'PanelResize.resizePanel("gripper", "{preset}")')
                # Force MJPEG stream reconnect by re-setting the source
                if cam_active and self._camera_image is not None:
                    self._camera_image.set_source(
                        f"/tool/camera/stream?t={time.time()}"
                    )

        ts = robot_state.tool_status
        pub_tool = waldoctl.commander.status.tool
        tool_position = pub_tool.positions[0] if pub_tool.positions else 0.0
        tool_current = pub_tool.channels[0] if pub_tool.channels else 0.0
        status_key = (
            ts.state,
            tool_position,
            tool_current,
            ts.part_detected,
            ts.engaged,
            ts.fault_code,
        )
        if status_key == self._last_status_key:
            return
        self._last_status_key = status_key

        # Initialize target from feedback on first status
        if not self._target_initialized and tool_position > 0:
            self._target_initialized = True
            waldoctl.commander.settings.gripper.target_position = tool_position
            if self._pos_slider is not None:
                self._pos_slider.set_value(round(tool_position * 100))

        # State dot + label
        s = ts.state
        color, label = _STATE_DOTS.get(s, _STATE_DOTS[0])
        if self._state_dot is not None:
            self._state_dot.style(f"font-size: 10px; color: {color};")
        if self._state_label is not None:
            self._state_label.text = label

        # Part detected dot
        if self._part_dot is not None:
            part_color = (
                "var(--color-emerald-400)" if ts.part_detected else "var(--ctk-muted)"
            )
            self._part_dot.style(f"font-size: 10px; color: {part_color};")

        # Engaged dot
        if self._engaged_dot is not None:
            eng_color = "var(--color-emerald-400)" if ts.engaged else "var(--ctk-muted)"
            self._engaged_dot.style(f"font-size: 10px; color: {eng_color};")

        # Fault code
        if self._fault_label is not None:
            if ts.fault_code != 0:
                self._fault_label.text = f"Fault: {ts.fault_code}"
                self._fault_label.set_visibility(True)
            else:
                self._fault_label.set_visibility(False)

    # ---- Status + Controls ----
    def _build_status_column(self) -> None:
        _lbl = "text-xs text-[var(--ctk-muted)]"
        _dot_s = "font-size: 10px;"

        with ui.grid(columns="auto auto 3.5rem").classes(
            "gap-x-1 gap-y-1 items-center"
        ):
            # State
            ui.label("State").classes(_lbl)
            self._state_dot = ui.icon("circle").style(
                f"{_dot_s} color: var(--ctk-muted);"
            )
            self._state_label = ui.label("Off").classes("text-xs")

            # Position — bind to the bindable ``positions`` tuple and project
            # the first DOF for single-axis gripper display.
            ui.label("Position").classes(_lbl)
            ui.icon("circle").style(f"{_dot_s} color: {_CLR_POS};")
            (
                ui.label("0 %")
                .classes("text-sm font-medium")
                .bind_text_from(
                    waldoctl.commander.status.tool,
                    "positions",
                    backward=lambda p: f"{(p[0] if p else 0.0) * 100:.0f} %",
                )
            )

            # Current — bind to ``channels`` tuple, project first channel.
            ui.label("Current").classes(_lbl)
            ui.icon("circle").style(f"{_dot_s} color: {_CLR_CUR};")
            (
                ui.label("0 mA")
                .classes("text-sm")
                .bind_text_from(
                    waldoctl.commander.status.tool,
                    "channels",
                    backward=lambda c: f"{(c[0] if c else 0.0):.0f} mA",
                )
            )

            # Part detected
            ui.label("Part").classes(_lbl)
            self._part_dot = ui.icon("circle").style(
                f"{_dot_s} color: var(--ctk-muted);"
            )
            ui.label()

            # Engaged
            ui.label("Engaged").classes(_lbl)
            self._engaged_dot = ui.icon("circle").style(
                f"{_dot_s} color: var(--ctk-muted);"
            )
            ui.label()

            # Fault code
            self._fault_label = ui.label("").classes(
                "text-xs text-[var(--color-red-400)] col-span-3"
            )
            self._fault_label.set_visibility(False)

    def _build_controls_column(self) -> None:
        with (
            ui.grid(columns="auto 100px 2rem")
            .classes("w-full gap-y-0 gap-x-4")
            .style("align-items: center; justify-items: start;")
        ):
            self._build_sliders()
            self._build_speed_section()

    def _build_sliders(self) -> None:
        self._slider_interval = config.webapp_control_interval_s

        # Position slider + input (target-only: init from feedback, then tracks target)
        ui.label("Pos").classes("text-xs text-[var(--ctk-muted)]")
        self._pos_slider = (
            ui.slider(min=0, max=100, value=0, step=1)
            .on_value_change(self._on_slider_drag)
            .on("pan", self._on_slider_pan)
        )
        pos_input = ui.number(min=0, max=100, step=1, value=0).props("dense borderless")
        pos_input.bind_value_from(self._pos_slider, "value")

        # Current slider + input (electric only)
        def _electric_visible(k: str) -> bool:
            return k != "NONE" and self._is_electric()

        ui.label("mA").classes("text-xs text-[var(--ctk-muted)]").bind_visibility_from(
            waldoctl.commander.status.tool,
            "key",
            backward=_electric_visible,
        )
        self._cur_slider = (
            ui.slider(min=0, max=1000, value=500, step=10)
            .on("pan", self._on_slider_pan)
            .on_value_change(self._on_current_slider_change)
        ).bind_visibility_from(
            waldoctl.commander.status.tool,
            "key",
            backward=_electric_visible,
        )
        cur_input = ui.number(min=0, max=1000, step=10, value=500).props(
            "dense borderless"
        )
        cur_input.bind_value_from(self._cur_slider, "value")
        cur_input.bind_visibility_from(
            waldoctl.commander.status.tool,
            "key",
            backward=_electric_visible,
        )

        # Update current slider range when tool changes
        def _update_current_range() -> None:
            if self._cur_slider is None:
                return
            if waldoctl.commander.status.tool.key == self._last_current_tool_key:
                return
            self._last_current_tool_key = waldoctl.commander.status.tool.key
            tool = self._get_active_gripper()
            if isinstance(tool, ElectricGripperTool):
                lo, hi = tool.current_range
                self._cur_slider._props["min"] = lo
                self._cur_slider._props["max"] = hi
                cur_input._props["min"] = lo
                cur_input._props["max"] = hi
                self._cur_slider.value = min(lo + 80, hi)
                self._cur_slider.update()

        self._current_range_listener = _update_current_range
        robot_state.add_change_listener(_update_current_range)
        _update_current_range()  # apply immediately if tool already set

    def _build_speed_section(self) -> None:
        ui.label("Speed").classes("text-xs text-[var(--ctk-muted)] pt-2")
        with ui.row().classes("col-span-2 w-full items-center gap-2 no-wrap pt-2"):
            (
                ui.switch("Sync", value=waldoctl.commander.settings.gripper.speed_sync)
                .props("dense")
                .bind_value(waldoctl.commander.settings.gripper, "speed_sync")
            )
            # Synced: show read-only system speed
            (
                ui.label("")
                .bind_text_from(
                    waldoctl.commander.settings.jog, "speed", backward=lambda v: f"{v}%"
                )
                .bind_visibility_from(waldoctl.commander.settings.gripper, "speed_sync")
                .classes("text-xs text-[var(--ctk-muted)]")
            )
            # Independent: slider
            (
                ui.slider(
                    min=1,
                    max=100,
                    value=waldoctl.commander.settings.gripper.speed,
                    step=1,
                )
                .bind_value(waldoctl.commander.settings.gripper, "speed")
                .bind_visibility_from(
                    waldoctl.commander.settings.gripper,
                    "speed_sync",
                    backward=lambda v: not v,
                )
                .classes("flex-1")
            )

    # ---- Cleanup ----

    def cleanup(self) -> None:
        """Remove listeners when panel is destroyed."""
        if self._current_range_listener is not None:
            robot_state.remove_change_listener(self._current_range_listener)
