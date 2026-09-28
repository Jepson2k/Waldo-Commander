import asyncio
import logging
import time
from collections.abc import Callable

import waldoctl
from nicegui import background_tasks, ui
from waldoctl import (
    ElectricGripperTool,
    GripperTool,
    RobotClient,
)

from waldo_commander.common.panel_theme import chart_grid, chart_text
from waldo_commander.common.theme import css, hex_of
from waldo_commander.constants import CHART_PUSH_INTERVAL_S, config
from waldo_commander.services.camera_service import camera_service
from waldo_commander.services.control_lease import (
    control_lease,
    require_browser_control,
)
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.motion_guard import motion_guard
from waldo_commander.services.programs import is_any_program_running
from waldo_commander.state import robot_state, ui_state

logger = logging.getLogger(__name__)


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
    0: (css("text-muted"), "Off"),
    1: (css("info"), "Idle"),
    2: (css("positive"), "Active"),
    3: (css("error"), "Error"),
}


class GripperPage:
    """Gripper tab page — camera, time series, status, and controls."""

    def __init__(self, client: RobotClient) -> None:
        self.client = client
        self._clr_pos = hex_of("measure-position")
        self._clr_cur = hex_of("measure-current")
        self._last_current_tool_key: str | None = None
        self._current_range_listener: Callable | None = None
        self._current_range: tuple[int, int] = (0, 0)
        self._slider_drag_ts: float = 0.0
        self._slider_task: asyncio.Task | None = None
        self._pending_position: float | None = None
        self._slider_final = False
        self._gesture_has_value = False
        self._finished_slider_targets: list[float] = []
        self._slider_cancelled = False
        self._slider_context: tuple | None = None
        self._last_final_position: float | None = None
        self._page_client = None
        self._last_lease_block_notify: float = 0.0
        self._user_dragging: bool = False
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

    def _can_actuate(self) -> bool:
        """Lease gate for the gripper slider paths. The toast is debounced to
        once per few seconds so a sustained drag while an MCP session holds
        control doesn't stack dozens of identical warnings per gesture."""
        now = time.monotonic()
        should_notify = now - self._last_lease_block_notify > 3.0
        ok = (
            self._page_client is not None
            and self._page_client.id == ui_state.active_client_id
            and motion_guard.owner is None
            and not is_any_program_running()
            and require_browser_control(ui_state.active_client_id, notify=should_notify)
        )
        if not ok and should_notify:
            self._last_lease_block_notify = now
        return ok

    async def _grip_set(self, position: float, label: str) -> None:
        if not self._can_actuate():
            return
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
                spd_kwargs["current"] = (
                    waldoctl.commander.settings.gripper.current / 100.0
                )
            await motion_recorder.owned_tool_move(
                self.client,
                tool.set_position(position, **spd_kwargs),
                lambda: motion_recorder.record_action(
                    "gripper", position=position, **spd_kwargs
                ),
            )
        except Exception as e:
            logger.error("Gripper %s failed: %s", label.lower(), e)
            ui.notify(f"{label} failed: {e}", color="negative")

    # ---- Build ----

    def build(self) -> None:
        self._page_client = ui.context.client
        self._page_client.on_disconnect(self._cancel_slider)
        self._drop_stop_listener = motion_guard.add_stop_listener(
            lambda *_: self._cancel_slider()
        )
        self._pos_slider: ui.slider | None = None
        self._cur_slider: ui.slider | None = None
        self._combined_chart: ui.echart | None = None
        self._camera_card: ui.card | None = None
        self._camera_image: ui.interactive_image | None = None
        self._last_camera_active: bool = camera_service.active

        self._state_dot: ui.icon | None = None
        self._state_label: ui.label | None = None
        self._part_dot: ui.icon | None = None
        self._engaged_dot: ui.icon | None = None
        self._fault_label: ui.label | None = None

        self._last_status_key: tuple = ()

        # Chart Y-axis current max; set lazily when the chart is built.
        self._current_max: float = 0.0
        # Defer markLine-only changes to the next update_chart tick to avoid competing update() calls.
        self._mark_lines_dirty: bool = False
        self._chart_pushed_at: float = 0.0

        _tile = "well p-2"
        with ui.column().classes("w-full gap-2"):
            self._camera_card = (
                ui.card().props("flat").classes("w-full p-0 overflow-hidden well")
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
        clr_pos, clr_cur = self._clr_pos, self._clr_cur
        clr_axis = chart_text()
        y_axis_left: dict = {
            "type": "value",
            "name": "%",
            "nameTextStyle": {"fontSize": 11, "color": clr_axis},
            "axisLabel": {"fontSize": 11, "color": clr_axis},
            "splitLine": {"lineStyle": {"color": chart_grid()}},
            "min": 0,
            "max": 100,
        }
        y_axis_right: dict = {
            "type": "value",
            "name": "mA",
            "nameTextStyle": {"fontSize": 11, "color": clr_axis},
            "axisLabel": {"fontSize": 11, "color": clr_axis},
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
                        "textStyle": {"fontSize": 11, "color": hex_of("text")},
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
                            "lineStyle": {"width": 1.5, "color": clr_pos},
                            "itemStyle": {"color": clr_pos},
                            "markLine": _make_mark_line(0, clr_pos, "target"),
                            "data": [],
                        },
                        {
                            "name": "Current",
                            "type": "line",
                            "yAxisIndex": 1,
                            "showSymbol": False,
                            "smooth": True,
                            "lineStyle": {"width": 1.5, "color": clr_cur},
                            "itemStyle": {"color": clr_cur},
                            "markLine": _make_mark_line(0, clr_cur, "limit"),
                            "data": [],
                        },
                    ],
                },
                # The legend's colour is a CSS variable, which only a DOM
                # element resolves — a canvas fillStyle ignores it.
                renderer="svg",
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
        now = time.monotonic()
        if now - self._chart_pushed_at < CHART_PUSH_INTERVAL_S:
            return
        result = robot_state.tool_time_series.get_series_if_dirty()
        if result is None and not self._mark_lines_dirty:
            return
        self._chart_pushed_at = now
        self._mark_lines_dirty = False

        target_pos_pct = round(
            waldoctl.commander.settings.gripper.target_position * 100, 1
        )
        # The chart plots measured mA; the setting is a percent of the range.
        lo, hi = self._current_range
        current_limit = lo + waldoctl.commander.settings.gripper.current / 100.0 * (
            hi - lo
        )
        clr_pos, clr_cur = self._clr_pos, self._clr_cur

        chart = self._combined_chart
        if chart is None:
            return
        with chart.props.suspend_updates():
            if result is not None:
                timestamps, positions, currents = result
                ts_ms = [t * 1000 for t in timestamps]
                chart.options["series"][0]["data"] = [
                    [t, round(p * 100, 1)] for t, p in zip(ts_ms, positions)
                ]
                chart.options["series"][1]["data"] = [
                    [t, round(c, 1)] for t, c in zip(ts_ms, currents)
                ]
            chart.options["series"][0]["markLine"] = _make_mark_line(
                target_pos_pct, clr_pos, "target"
            )
            chart.options["series"][1]["markLine"] = _make_mark_line(
                current_limit, clr_cur, "limit"
            )
        chart.run_chart_method("setOption", {"series": chart.options["series"]})

    def set_target_position(self, position: float) -> None:
        """Set target position and update the slider. Called by control panel actions."""
        self._target_initialized = True
        waldoctl.commander.settings.gripper.target_position = position
        if self._pos_slider is not None:
            self._pos_slider.set_value(round(position * 100))
        self._update_mark_lines()

    def set_target_current(self, current: int) -> None:
        """Set the target current (percent) and update the slider. Called by
        control panel adjust."""
        waldoctl.commander.settings.gripper.current = current
        if self._cur_slider is not None:
            self._cur_slider.set_value(current)
        self._update_mark_lines()

    def _update_mark_lines(self) -> None:
        """Mark markLines dirty so the next update_chart() tick pushes them."""
        self._mark_lines_dirty = True

    # ---- Live slider ----

    def _input_context(self) -> tuple:
        return (
            motion_guard.stop_generation,
            ui_state.active_client_id,
            waldoctl.commander.status.simulator_active,
            waldoctl.commander.status.tool.key,
            waldoctl.commander.status.tool.variant_key,
            control_lease.generation,
        )

    def _cancel_slider(self) -> None:
        self._slider_cancelled = True
        self._user_dragging = False
        self._pending_position = None
        self._finished_slider_targets.clear()
        self._slider_context = None
        if self._slider_task is not None:
            self._slider_task.cancel()
            self._slider_task = None

    def _on_slider_pan(self, e) -> None:
        if self._pos_slider is None:
            return
        self._user_dragging = e.args in ("start", True)
        if self._user_dragging:
            if not self._can_actuate():
                self._cancel_slider()
                return
            self._slider_cancelled = False
            self._slider_final = False
            self._last_final_position = None
            self._slider_context = self._input_context()
            # Quasar updates its value before emitting pan start.
            self._queue_position(float(self._pos_slider.value))
        elif self._slider_context is not None:
            self._slider_final = True
            self._queue_position(float(self._pos_slider.value))

    def _on_slider_drag(self, e) -> None:
        if self._user_dragging or self._slider_context is not None:
            self._queue_position(float(e.value))

    def _on_slider_change(self, e) -> None:
        # change is also emitted by keyboard/click input, without a pan.
        if self._slider_cancelled:
            return
        value = float(e.args)
        if self._slider_context is None:
            if not self._can_actuate():
                return
            if self._last_final_position == value / 100.0:
                return
            self._slider_context = self._input_context()
        self._user_dragging = False
        self._slider_final = True
        self._queue_position(value)

    def _on_slider_input_start(self) -> None:
        if (
            self._slider_context is not None
            and self._slider_context != self._input_context()
        ):
            self._cancel_slider()
        if not self._can_actuate():
            return
        # Seal the preceding endpoint before this pointer/keyboard event can
        # update the value. The previous command may still be completing.
        if self._slider_context is not None and self._slider_final:
            self._finished_slider_targets.append(
                waldoctl.commander.settings.gripper.target_position
            )
            self._pending_position = None
            self._gesture_has_value = False
        # Only a fresh physical interaction can re-arm a cancelled gesture.
        self._slider_cancelled = False

    def _queue_position(self, value: float) -> None:
        if self._slider_context != self._input_context() or not self._can_actuate():
            self._cancel_slider()
            return
        position = max(0.0, min(1.0, value / 100.0))
        self._target_initialized = True
        waldoctl.commander.settings.gripper.target_position = position
        self._pending_position = position
        self._gesture_has_value = True
        self._slider_drag_ts = time.monotonic()
        self._update_mark_lines()
        if self._slider_task is None:
            self._slider_task = background_tasks.create(self._drain_positions())

    async def _drain_positions(self) -> None:
        """One queued command at a time; retain the latest unsent target.

        Keep the observer's owned window across the entire gesture, then
        record only its final accepted target. A trailing Quasar update may
        arrive up to 50 ms after pan end.
        """
        task = asyncio.current_task()
        last_position = None
        last_kwargs: dict = {}
        try:
            with motion_recorder.owned():
                while self._slider_context is not None:
                    await asyncio.sleep(self._slider_interval)
                    if (
                        self._slider_context != self._input_context()
                        or not self._can_actuate()
                    ):
                        self._slider_cancelled = True
                        return
                    finished = bool(self._finished_slider_targets)
                    if finished:
                        position = self._finished_slider_targets.pop(0)
                    else:
                        position, self._pending_position = self._pending_position, None
                    if position is not None and position != last_position:
                        tool = self._get_active_gripper()
                        if tool is None:
                            return
                        kwargs = {}
                        if isinstance(tool, ElectricGripperTool):
                            settings = waldoctl.commander.settings
                            kwargs = {
                                "speed": (
                                    settings.jog.speed
                                    if settings.gripper.speed_sync
                                    else settings.gripper.speed
                                )
                                / 100.0,
                                "current": settings.gripper.current / 100.0,
                            }
                        index = await tool.set_position(position, **kwargs)
                        if index < 0 or not await self.client.wait_command(
                            index, timeout=10.0
                        ):
                            return
                        last_position, last_kwargs = position, kwargs
                    if (
                        finished
                        and self._slider_context == self._input_context()
                        and self._can_actuate()
                    ):
                        motion_recorder.record_action(
                            "gripper", position=last_position, **last_kwargs
                        )
                    if (
                        self._slider_final
                        and not self._finished_slider_targets
                        and self._pending_position is None
                        and time.monotonic() - self._slider_drag_ts >= 0.06
                    ):
                        if (
                            self._gesture_has_value
                            and last_position is not None
                            and self._slider_context == self._input_context()
                            and self._can_actuate()
                        ):
                            motion_recorder.record_action(
                                "gripper", position=last_position, **last_kwargs
                            )
                            self._last_final_position = last_position
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Gripper slider failed: %s", exc)
        finally:
            if self._slider_task is task:
                self._slider_task = None
                self._slider_context = None
                self._pending_position = None
                self._finished_slider_targets.clear()
                self._user_dragging = False

    def _on_current_slider_change(self, e) -> None:
        waldoctl.commander.settings.gripper.current = int(e.value)
        self._mark_lines_dirty = True

    async def _on_current_slider_commit(self, _e) -> None:
        await self._grip_set(
            waldoctl.commander.settings.gripper.target_position, "Set current"
        )

    # ---- Status updates (called from status consumer) ----

    def update_status(self) -> None:
        """Update all status fields from robot_state. Called from status consumer."""
        if self._camera_card is not None:
            cam_active = camera_service.active
            self._camera_card.set_visibility(cam_active)
            if cam_active != self._last_camera_active:
                self._last_camera_active = cam_active
                preset = "camera" if cam_active else "default"
                ui.run_javascript(f'PanelResize.resizePanel("gripper", "{preset}")')
                # Re-set the source to force the MJPEG stream to reconnect.
                if cam_active and self._camera_image is not None:
                    self._camera_image.set_source(
                        f"/tool/camera/stream?t={time.time()}"
                    )

        ts = robot_state.tool_status
        pub_tool = waldoctl.commander.status.tool
        tool_position = pub_tool.position
        tool_current = pub_tool.current
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

        # Seed the target from feedback on the first non-zero status.
        if not self._target_initialized and tool_position > 0:
            self._target_initialized = True
            waldoctl.commander.settings.gripper.target_position = tool_position
            if self._pos_slider is not None:
                self._pos_slider.set_value(round(tool_position * 100))

        s = ts.state
        color, label = _STATE_DOTS.get(s, _STATE_DOTS[0])
        if self._state_dot is not None:
            self._state_dot.style(f"color: {color};")
        if self._state_label is not None:
            self._state_label.text = label

        if self._part_dot is not None:
            part_color = css("positive") if ts.part_detected else css("text-muted")
            self._part_dot.style(f"color: {part_color};")

        if self._engaged_dot is not None:
            eng_color = css("positive") if ts.engaged else css("text-muted")
            self._engaged_dot.style(f"color: {eng_color};")

        if self._fault_label is not None:
            if ts.fault_code != 0:
                self._fault_label.text = f"Fault: {ts.fault_code}"
                self._fault_label.set_visibility(True)
            else:
                self._fault_label.set_visibility(False)

    # ---- Status + Controls ----
    def _build_status_column(self) -> None:
        _lbl = "wc-caption text-wc-text-muted"
        _dot_size = "10px"

        with ui.grid(columns="auto auto 3.5rem").classes(
            "gap-x-1 gap-y-1 items-center"
        ):
            ui.label("State").classes(_lbl)
            self._state_dot = ui.icon("circle", size=_dot_size).style(
                f"color: {css('text-muted')};"
            )
            self._state_label = ui.label("Off").classes("text-xs")

            # Project the first DOF of the bindable ``positions`` tuple for single-axis gripper display.
            ui.label("Position").classes(_lbl)
            ui.icon("circle", size=_dot_size).style(
                f"color: {css('measure-position')};"
            )
            (
                ui.label("0 %")
                .classes("text-sm font-medium")
                .bind_text_from(
                    waldoctl.commander.status.tool,
                    "positions",
                    backward=lambda p: f"{(p[0] if p else 0.0) * 100:.0f} %",
                )
            )

            # Project the first channel of the bindable ``channels`` tuple.
            ui.label("Current").classes(_lbl)
            ui.icon("circle", size=_dot_size).style(f"color: {css('measure-current')};")
            (
                ui.label("0 mA")
                .classes("text-sm")
                .bind_text_from(
                    waldoctl.commander.status.tool,
                    "channels",
                    backward=lambda c: f"{(c[0] if c else 0.0):.0f} mA",
                )
            )

            ui.label("Part").classes(_lbl)
            self._part_dot = ui.icon("circle", size=_dot_size).style(
                f"color: {css('text-muted')};"
            )
            ui.label()

            ui.label("Engaged").classes(_lbl)
            self._engaged_dot = ui.icon("circle", size=_dot_size).style(
                f"color: {css('text-muted')};"
            )
            ui.label()

            self._fault_label = ui.label("").classes(
                "wc-caption text-wc-error col-span-3"
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

        # Target-only slider: seeded from feedback, then tracks the target.
        ui.label("Pos").classes("wc-caption text-wc-text-muted")
        self._pos_slider = (
            ui.slider(min=0, max=100, value=0, step=1)
            .on_value_change(self._on_slider_drag)
            .on("pan", self._on_slider_pan)
            .on("change", self._on_slider_change)
            .on("pointerdown", self._on_slider_input_start)
            .on("keydown", self._on_slider_input_start)
            .mark("gripper-position")
        )
        pos_input = ui.number(min=0, max=100, step=1, value=0).props("dense borderless")
        pos_input.bind_value_from(self._pos_slider, "value")

        # Current slider, shown for electric grippers only.
        def _electric_visible(k: str) -> bool:
            return k != "NONE" and self._is_electric()

        ui.label("Current %").classes(
            "wc-caption text-wc-text-muted"
        ).bind_visibility_from(
            waldoctl.commander.status.tool,
            "key",
            backward=_electric_visible,
        )
        cur_pct = waldoctl.commander.settings.gripper.current
        self._cur_slider = (
            ui.slider(min=0, max=100, value=cur_pct, step=1)
            .on("change", self._on_current_slider_commit)
            .on_value_change(self._on_current_slider_change)
        ).bind_visibility_from(
            waldoctl.commander.status.tool,
            "key",
            backward=_electric_visible,
        )
        cur_input = ui.number(min=0, max=100, step=1, value=cur_pct).props(
            "dense borderless"
        )
        cur_input.bind_value_from(self._cur_slider, "value")
        cur_input.bind_visibility_from(
            waldoctl.commander.status.tool,
            "key",
            backward=_electric_visible,
        )

        def _update_current_range() -> None:
            if waldoctl.commander.status.tool.key == self._last_current_tool_key:
                return
            self._last_current_tool_key = waldoctl.commander.status.tool.key
            tool = self._get_active_gripper()
            if isinstance(tool, ElectricGripperTool):
                self._current_range = tool.current_range
                self._mark_lines_dirty = True

        self._current_range_listener = _update_current_range
        robot_state.add_change_listener(_update_current_range)
        _update_current_range()  # apply immediately if tool already set

    def _build_speed_section(self) -> None:
        ui.label("Speed").classes("wc-caption text-wc-text-muted pt-2")
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
                .classes("wc-caption text-wc-text-muted")
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
        self._cancel_slider()
        if hasattr(self, "_drop_stop_listener"):
            self._drop_stop_listener()
        if self._current_range_listener is not None:
            robot_state.remove_change_listener(self._current_range_listener)
