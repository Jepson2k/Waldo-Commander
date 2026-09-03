"""Diagnostics tab: control-loop health, motor-bus link, drive telemetry
and live joint torques.

The loop and link sections work on any backend that answers
``loop_stats()`` and publishes ``link_health``. The drives section needs a
backend that streams telemetry (par6's ``set_recipe`` and
``open_telemetry``); on one without it the section says so instead of
showing blanks. Everything here polls only while the tab is open.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

import waldoctl
from nicegui import ui
from waldo_commander.state import robot_state, ui_state

logger = logging.getLogger(__name__)

_JOINT_COLORS = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373", "#ba68c8", "#fff176"]
_TELEMETRY_RECIPE = "diagnostics"
_TELEMETRY_STALE_S = 2.0
_LOOP_STATS_PERIOD_S = 1.0
_LIVE_PERIOD_S = 0.25


class DriveTelemetry:
    """Reads the backend's telemetry stream on a thread and keeps the newest
    frame's fields; the page polls :meth:`latest` from a UI timer."""

    def __init__(self, open_reader: Callable[[], Any]) -> None:
        self._open_reader = open_reader
        self._lock = threading.Lock()
        self._fields: dict[str, Any] | None = None
        self._received_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="wc-drive-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            reader = self._open_reader()
        except Exception as exc:
            self.error = f"telemetry socket: {exc}"
            logger.warning("drive telemetry unavailable: %s", exc)
            return
        try:
            with reader:
                while not self._stop.is_set():
                    frame = reader.recv(timeout=0.2)
                    if frame is None or frame.get("recipe") != _TELEMETRY_RECIPE:
                        continue
                    with self._lock:
                        self._fields = frame["fields"]
                        self._received_at = time.monotonic()
        except Exception as exc:
            self.error = f"telemetry stream: {exc}"
            logger.warning("drive telemetry stopped: %s", exc)

    def latest(self, max_age_s: float = _TELEMETRY_STALE_S) -> dict[str, Any] | None:
        with self._lock:
            if self._fields is None:
                return None
            if time.monotonic() - self._received_at > max_age_s:
                return None
            return self._fields


def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000.0:.2f} ms"


class DiagnosticsPage:
    """Diagnostics tab page: loop, link, drives and torque plot."""

    def __init__(self, client: Any, is_open: Callable[[], bool]) -> None:
        self.client = client
        self._is_open = is_open
        self._joint_count = ui_state.active_robot.joints.count
        self._loop_values: dict[str, ui.label] = {}
        self._rt_fifo: ui.chip | None = None
        self._rt_pinned: ui.chip | None = None
        self._drive_cells: list[list[ui.label]] = []
        self._drives_note: ui.label | None = None
        self._chart: ui.echart | None = None
        self._telemetry: DriveTelemetry | None = None
        self._recipe_requested = False
        self._loop_stats_inflight = False

    # ---- build ----

    def build(self) -> None:
        with ui.column().classes("w-full gap-2").mark("diagnostics-panel"):
            self._build_loop_section()
            self._build_link_section()
            self._build_drives_section()
            self._build_torque_section()
        ui.timer(_LOOP_STATS_PERIOD_S, self._poll_loop_stats)
        ui.timer(_LIVE_PERIOD_S, self._refresh_live)

    @staticmethod
    def _section(title: str) -> ui.column:
        col = ui.column().classes("w-full gap-0")
        with col:
            ui.label(title).classes("text-sm font-medium")
        return col

    @staticmethod
    def _value_row(name: str, marker: str) -> ui.label:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(name).classes("text-xs text-[var(--ctk-muted)] w-28")
            value = ui.label("—").classes("text-xs font-mono").mark(marker)
        return value

    def _build_loop_section(self) -> None:
        with self._section("Control loop"):
            self._loop_values["rate"] = self._value_row("Rate", "diag-loop-rate")
            self._loop_values["period"] = self._value_row("Period", "diag-loop-period")
            self._loop_values["tail"] = self._value_row("p99 / max", "diag-loop-tail")
            self._loop_values["overruns"] = self._value_row(
                "Overruns", "diag-loop-overruns"
            )
            self._loop_values["bus_age"] = self._value_row(
                "Bus frame age", "diag-loop-bus-age"
            )
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("RT scheduling").classes(
                    "text-xs text-[var(--ctk-muted)] w-28"
                )
                self._rt_fifo = (
                    ui.chip("FIFO", color="grey-7").props("dense").mark("diag-rt-fifo")
                )
                self._rt_pinned = (
                    ui.chip("pinned", color="grey-7")
                    .props("dense")
                    .mark("diag-rt-pinned")
                )

    def _build_link_section(self) -> None:
        lh = waldoctl.commander.status.link_health
        with self._section("Motor bus"):
            self._value_row("State", "diag-link-state").bind_text_from(lh, "state")
            self._value_row("Restarts", "diag-link-restarts").bind_text_from(
                lh, "restarts", backward=str
            )
            self._value_row("TX errors", "diag-link-tx-errors").bind_text_from(
                lh, "tx_errors", backward=str
            )
            self._value_row("RX frames", "diag-link-rx-frames").bind_text_from(
                lh, "rx_frames", backward=str
            )

    def _build_drives_section(self) -> None:
        with self._section("Drives"):
            self._drives_note = (
                ui.label("")
                .classes("text-xs text-[var(--ctk-muted)]")
                .mark("diag-drives-note")
            )
            with (
                ui.grid(columns=4).classes("w-full gap-x-3 gap-y-0").mark("diag-drives")
            ):
                for head in ("Joint", "°C", "V", "mA"):
                    ui.label(head).classes("text-xs text-[var(--ctk-muted)]")
                for j in range(self._joint_count):
                    ui.label(f"J{j + 1}").classes("text-xs font-mono")
                    cells = [
                        ui.label("—")
                        .classes("text-xs font-mono")
                        .mark(f"diag-drive-{k}-{j + 1}")
                        for k in ("temp", "volt", "cur")
                    ]
                    self._drive_cells.append(cells)
        if not self._backend_streams_telemetry():
            self._drives_note.text = "Drive telemetry is not available on this backend"

    def _build_torque_section(self) -> None:
        n = self._joint_count
        series: list[dict[str, Any]] = []
        for j in range(n):
            color = _JOINT_COLORS[j % len(_JOINT_COLORS)]
            series.append(
                {
                    "name": f"J{j + 1}",
                    "type": "line",
                    "showSymbol": False,
                    "lineStyle": {"width": 1.5, "color": color},
                    "itemStyle": {"color": color},
                    "data": [],
                }
            )
        for j in range(n):
            color = _JOINT_COLORS[j % len(_JOINT_COLORS)]
            series.append(
                {
                    "name": f"J{j + 1} external",
                    "type": "line",
                    "showSymbol": False,
                    "lineStyle": {"width": 1, "type": "dashed", "color": color},
                    "itemStyle": {"color": color},
                    "data": [],
                }
            )
        with self._section("Torques [Nm] (solid measured, dashed external)"):
            self._chart = (
                ui.echart(
                    {
                        "animation": False,
                        "renderer": "svg",
                        "grid": {
                            "top": 24,
                            "right": 8,
                            "bottom": 4,
                            "left": 40,
                            "containLabel": False,
                        },
                        "legend": {
                            "data": [f"J{j + 1}" for j in range(n)],
                            "top": 0,
                            "left": 44,
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
                        "yAxis": {
                            "type": "value",
                            "axisLabel": {"fontSize": 11},
                            "splitLine": {
                                "lineStyle": {"color": "rgba(128,128,128,0.15)"}
                            },
                        },
                        "series": series,
                    }
                )
                .classes("w-full")
                .style("height: 140px;")
                .mark("diag-torque-chart")
            )

    # ---- backend capability ----

    def _backend_streams_telemetry(self) -> bool:
        return callable(getattr(self.client, "open_telemetry", None)) and callable(
            getattr(self.client, "set_recipe", None)
        )

    # ---- polling ----

    async def _poll_loop_stats(self) -> None:
        if not self._is_open() or self._loop_stats_inflight:
            return
        self._loop_stats_inflight = True
        try:
            stats = await self.client.loop_stats()
        except NotImplementedError:
            self._loop_values["rate"].text = "not supported by this backend"
            return
        except Exception as exc:
            logger.debug("loop_stats failed: %s", exc)
            return
        finally:
            self._loop_stats_inflight = False
        if stats is None:
            return
        self.show_loop_stats(stats)

    def show_loop_stats(self, stats: Any) -> None:
        """Render one ``loop_stats()`` result."""
        v = self._loop_values
        target = float(getattr(stats, "target_hz", 0.0))
        v["rate"].text = f"{float(stats.mean_hz):.1f} of {target:.0f} Hz"
        v["period"].text = (
            f"{_fmt_ms(float(stats.mean_period_s))} "
            f"± {float(stats.std_period_s) * 1000.0:.3f} jitter"
        )
        v[
            "tail"
        ].text = f"{_fmt_ms(float(stats.p99_period_s))} / {_fmt_ms(float(stats.max_period_s))}"
        v[
            "overruns"
        ].text = f"{int(stats.overrun_count)} of {int(stats.loop_count)} ticks"
        age_min = getattr(stats, "can_frame_age_min_ticks", None)
        age_max = getattr(stats, "can_frame_age_max_ticks", None)
        if age_min is None or age_max is None:
            v["bus_age"].text = "no fieldbus"
        else:
            v["bus_age"].text = f"{int(age_min)}–{int(age_max)} ticks"
        rt_fifo = getattr(stats, "rt_fifo", None)
        rt_pinned = getattr(stats, "rt_pinned", None)
        if self._rt_fifo is not None:
            self._rt_fifo.props(f"color={'green-7' if rt_fifo else 'grey-7'}")
            self._rt_fifo.text = "FIFO" if rt_fifo else "no FIFO"
        if self._rt_pinned is not None:
            self._rt_pinned.props(f"color={'green-7' if rt_pinned else 'grey-7'}")
            self._rt_pinned.text = "pinned" if rt_pinned else "not pinned"

    async def _refresh_live(self) -> None:
        if not self._is_open():
            self._stop_telemetry()
            return
        await self._ensure_telemetry()
        self._update_drives()
        self.update_chart()

    async def _ensure_telemetry(self) -> None:
        if not self._backend_streams_telemetry() or self._telemetry is not None:
            return
        if not self._recipe_requested:
            self._recipe_requested = True
            try:
                await self.client.set_recipe(_TELEMETRY_RECIPE)
            except Exception as exc:
                logger.warning("set_recipe(%s) failed: %s", _TELEMETRY_RECIPE, exc)
                if self._drives_note is not None:
                    self._drives_note.text = f"telemetry recipe refused: {exc}"
                return
        self._telemetry = DriveTelemetry(self.client.open_telemetry)
        self._telemetry.start()

    def _stop_telemetry(self) -> None:
        if self._telemetry is not None:
            self._telemetry.stop()
            self._telemetry = None
        self._recipe_requested = False

    def _update_drives(self) -> None:
        if self._telemetry is None or self._drives_note is None:
            return
        fields = self._telemetry.latest()
        if fields is None:
            self._drives_note.text = self._telemetry.error or "waiting for telemetry"
            return
        self._drives_note.text = ""
        temps = fields.get("motor_temperatures_c") or []
        volts = fields.get("motor_voltages_mv") or []
        currents = fields.get("motor_currents_ma") or []
        for j, cells in enumerate(self._drive_cells):
            # NaN is a register the drive has not answered yet.
            if j < len(temps) and math.isfinite(temps[j]):
                cells[0].text = f"{float(temps[j]):.0f}"
            if j < len(volts) and math.isfinite(volts[j]):
                cells[1].text = f"{float(volts[j]) / 1000.0:.1f}"
            if j < len(currents) and math.isfinite(currents[j]):
                cells[2].text = f"{float(currents[j]):.0f}"

    def update_chart(self) -> None:
        if self._chart is None:
            return
        result = robot_state.torque_time_series.get_series_if_dirty()
        if result is None:
            return
        timestamps, measured, external = result
        ts_ms = [t * 1000.0 for t in timestamps]
        n = self._joint_count
        series: list[dict[str, Any]] = []
        for j in range(n):
            series.append(
                {
                    "data": [
                        [t, round(row[j], 3)]
                        for t, row in zip(ts_ms, measured)
                        if j < len(row)
                    ]
                }
            )
        for j in range(n):
            series.append(
                {
                    "data": [
                        [t, round(row[j], 3)]
                        for t, row in zip(ts_ms, external)
                        if j < len(row)
                    ]
                }
            )
        self._chart.run_chart_method("setOption", {"series": series})
