"""Diagnostics tab: control-loop health, motor bus, drives, joint torques.

Everything live here comes off the status broadcast the app already
subscribes to — no polling and no second stream. The only query is a
single ``loop_stats()`` when the tab first opens, for the boot constants
that never change afterwards: the loop's target rate and whether the
control thread actually got real-time scheduling.

A backend that reports none of this leaves its section saying so rather
than showing dashes forever.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

import waldoctl
from nicegui import background_tasks, ui

from waldo_commander.state import robot_state, ui_state

logger = logging.getLogger(__name__)

_JOINT_COLORS = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373", "#ba68c8", "#fff176"]


def _ms(seconds: float) -> str:
    return f"{seconds * 1000.0:.2f} ms"


class DiagnosticsPage:
    """The diagnostics tab's content."""

    def __init__(self, client: Any, is_open: Callable[[], bool]) -> None:
        self.client = client
        self._is_open = is_open
        self._joint_count = ui_state.active_robot.joints.count
        self._values: dict[str, ui.label] = {}
        self._rt_fifo: ui.chip | None = None
        self._rt_pinned: ui.chip | None = None
        self._drive_rows: list[tuple[ui.element, list[ui.label]]] = []
        self._drives_note: ui.label | None = None
        self._chart: ui.echart | None = None
        self._target_hz = 0.0
        self._constants_asked = False

    # ---- build ----

    def build(self) -> None:
        with ui.column().classes("w-full gap-2").mark("diagnostics-panel"):
            self._build_loop_section()
            self._build_link_section()
            self._build_drives_section()
            self._build_torque_section()

    @staticmethod
    def _section(title: str) -> ui.column:
        col = ui.column().classes("w-full gap-0")
        with col:
            ui.label(title).classes("text-sm font-medium")
        return col

    def _row(self, name: str, marker: str) -> ui.label:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(name).classes("text-xs text-[var(--ctk-muted)] w-28")
            value = ui.label("—").classes("text-xs font-mono").mark(marker)
        self._values[marker] = value
        return value

    def _build_loop_section(self) -> None:
        with self._section("Control loop"):
            self._row("Rate", "diag-loop-rate")
            self._row("p99 period", "diag-loop-p99")
            self._row("Overruns", "diag-loop-overruns")
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("Scheduling").classes("text-xs text-[var(--ctk-muted)] w-28")
                self._rt_fifo = (
                    ui.chip("real-time", color="grey-7")
                    .props("dense")
                    .mark("diag-rt-fifo")
                )
                self._rt_pinned = (
                    ui.chip("pinned", color="grey-7")
                    .props("dense")
                    .mark("diag-rt-pinned")
                )

    def _build_link_section(self) -> None:
        lh = waldoctl.commander.status.link_health
        with self._section("Motor bus"):
            self._row("State", "diag-link-state").bind_text_from(
                lh, "state", backward=lambda s: s or "not reported"
            )
            self._row("Restarts", "diag-link-restarts").bind_text_from(
                lh, "restarts", backward=str
            )
            self._row("TX errors", "diag-link-tx-errors").bind_text_from(
                lh, "tx_errors", backward=str
            )
            self._row("RX frames", "diag-link-rx-frames").bind_text_from(
                lh, "rx_frames", backward=str
            )

    def _build_drives_section(self) -> None:
        """A row per arm joint, plus the tool's own drive.

        A backend may report one more reading than the arm has joints, so
        the tool row is built and left hidden until a reading for it
        arrives; the joint rows are always there, reading "—" until a
        drive answers.
        """
        with self._section("Drives"):
            self._drives_note = (
                ui.label("waiting for readings")
                .classes("text-xs text-[var(--ctk-muted)]")
                .mark("diag-drives-note")
            )
            with (
                ui.grid(columns=3).classes("w-full gap-x-3 gap-y-0").mark("diag-drives")
            ):
                for head in ("Drive", "°C", "mA"):
                    ui.label(head).classes("text-xs text-[var(--ctk-muted)]")
                names = [f"J{j + 1}" for j in range(self._joint_count)] + ["Tool"]
                for j, name in enumerate(names):
                    label = ui.label(name).classes("text-xs font-mono")
                    cells = [
                        ui.label("—")
                        .classes("text-xs font-mono")
                        .mark(f"diag-drive-{kind}-{j + 1}")
                        for kind in ("temp", "current")
                    ]
                    self._drive_rows.append((label, cells))
            self._row("Supply", "diag-drive-supply")
        self._show_row(self._joint_count, False)

    def _show_row(self, index: int, visible: bool) -> None:
        label, cells = self._drive_rows[index]
        label.set_visibility(visible)
        for cell in cells:
            cell.set_visibility(visible)

    def _build_torque_section(self) -> None:
        n = self._joint_count
        series: list[dict[str, Any]] = []
        for measured in (True, False):
            for j in range(n):
                color = _JOINT_COLORS[j % len(_JOINT_COLORS)]
                series.append(
                    {
                        "name": f"J{j + 1}" if measured else f"J{j + 1} external",
                        "type": "line",
                        "showSymbol": False,
                        "lineStyle": {"width": 1.5 if measured else 1}
                        | ({} if measured else {"type": "dashed"})
                        | {"color": color},
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

    # ---- live update, driven by the status loop ----

    def update(self) -> None:
        """Refresh from ``commander.status``. Called per status tick while
        the tab is open.

        Synchronous on purpose: this runs inside the status loop's client
        context, and the one query it needs is dispatched as its own task
        rather than awaited under that context."""
        if not self._is_open():
            return
        if not self._constants_asked:
            self._constants_asked = True
            background_tasks.create(self._ask_constants(), name="diagnostics-constants")
        self._update_loop()
        self._update_drives()
        self.update_chart()

    async def _ask_constants(self) -> None:
        """The loop's target rate and its scheduling, which are fixed at
        boot and so are worth exactly one query."""
        try:
            stats = await self.client.loop_stats()
        except NotImplementedError:
            return
        except Exception as exc:
            logger.debug("loop_stats failed: %s", exc)
            self._constants_asked = False
            return
        if stats is None:
            self._constants_asked = False
            return
        self._target_hz = float(getattr(stats, "target_hz", 0.0))
        fifo = bool(getattr(stats, "rt_fifo", False))
        pinned = bool(getattr(stats, "rt_pinned", False))
        if self._rt_fifo is not None:
            self._rt_fifo.text = "real-time" if fifo else "not real-time"
            self._rt_fifo.props(f"color={'green-7' if fifo else 'grey-7'}")
        if self._rt_pinned is not None:
            self._rt_pinned.text = "pinned" if pinned else "not pinned"
            self._rt_pinned.props(f"color={'green-7' if pinned else 'grey-7'}")

    def _set(self, marker: str, text: str) -> None:
        label = self._values.get(marker)
        if label is not None and label.text != text:
            label.text = text

    def _update_loop(self) -> None:
        health = waldoctl.commander.status.loop_health
        self._set(
            "diag-loop-rate",
            f"{self._target_hz:.0f} Hz target" if self._target_hz else "—",
        )
        if not health.measured:
            self._set("diag-loop-p99", "not reported by this backend")
            self._set("diag-loop-overruns", "—")
            return
        budget = 1.0 / self._target_hz if self._target_hz else 0.0
        self._set(
            "diag-loop-p99",
            f"{_ms(health.p99_period_s)} of {_ms(budget)} budget"
            if budget
            else _ms(health.p99_period_s),
        )
        self._set("diag-loop-overruns", str(health.overruns))

    def _update_drives(self) -> None:
        if self._drives_note is None:
            return
        health = waldoctl.commander.status.drive_health
        temps = health.temperatures_c
        currents = health.currents_ma
        reported = max(len(temps), len(currents))
        if not reported:
            self._drives_note.text = "This backend's drives report no readings"
            self._drives_note.set_visibility(True)
            self._set("diag-drive-supply", "—")
            return
        self._drives_note.set_visibility(False)
        self._show_row(self._joint_count, reported > self._joint_count)
        for j, (_, cells) in enumerate(self._drive_rows):
            temp = temps[j] if j < len(temps) else math.nan
            current = currents[j] if j < len(currents) else math.nan
            # NaN is a drive that has not answered that register yet.
            cells[0].text = "—" if math.isnan(temp) else f"{temp:.0f}"
            cells[1].text = "—" if math.isnan(current) else f"{current:.0f}"
        volts = health.bus_voltage_v
        self._set("diag-drive-supply", "—" if volts is None else f"{volts:.1f} V")

    def update_chart(self) -> None:
        if self._chart is None:
            return
        result = robot_state.torque_time_series.get_series_if_dirty()
        if result is None:
            return
        timestamps, measured, external = result
        ts_ms = [t * 1000.0 for t in timestamps]
        series: list[dict[str, Any]] = []
        for rows in (measured, external):
            for j in range(self._joint_count):
                series.append(
                    {
                        "data": [
                            [t, round(row[j], 3)]
                            for t, row in zip(ts_ms, rows)
                            if j < len(row)
                        ]
                    }
                )
        self._chart.run_chart_method("setOption", {"series": series})
