"""Diagnostics tab: what this backend can actually tell you, and nothing else.

Backends differ enormously in what they report. One publishes per-drive
temperatures, currents, a fieldbus link and measured joint torques; another
has none of that and says so by leaving those fields empty. A fixed layout
serves the first and leaves the second showing a screenful of dashes, which
reads as "everything is zero" rather than "nobody asked this robot".

So each section declares when it applies, and a section that has never had
anything to report is never shown. Once a section has appeared it stays:
a drive that stops answering a register shows an unknown reading, which is
information, rather than making its whole section vanish.

Everything live comes off the status broadcast the app already subscribes
to. The only query is one ``loop_stats()`` when the tab first opens, for the
boot constants that never change: the loop's target rate and whether the
control thread actually got real-time scheduling.
"""

from __future__ import annotations

import html as html_mod
import logging
import math
from collections.abc import Sequence
from typing import Any, Callable

import waldoctl
from nicegui import background_tasks, ui

from waldo_commander.common.tab_flash import flash_tab
from waldo_commander.state import robot_events, robot_state, ui_state

logger = logging.getLogger(__name__)

_JOINT_COLORS = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373", "#ba68c8", "#fff176"]

#: Error-code bands (waldoctl.errors). The band says what kind of thing went
#: wrong, which is more use in a log than a severity word: a bus-off entry
#: and a degraded loop are both "warning" and want telling apart at a glance.
#: Icons are bare Material Symbols ligatures — the font reads the span's text,
#: so Quasar's ``sym_o_`` spelling would render as the word itself.
_BAND_STYLE: tuple[tuple[int, int, str, str], ...] = (
    (10, 29, "route", "text-purple-400"),  # IK / trajectory
    (30, 39, "open_with", "text-sky-400"),  # motion
    (40, 49, "lan", "text-amber-400"),  # comms
    (50, 64, "memory", "text-orange-400"),  # system / safety
)
_DEFAULT_STYLE = ("warning", "text-amber-400")


def _band(code: int) -> tuple[str, str]:
    for lo, hi, icon, colour in _BAND_STYLE:
        if lo <= code <= hi:
            return icon, colour
    return _DEFAULT_STYLE


def _ms(seconds: float) -> str:
    return f"{seconds * 1000.0:.2f} ms"


def _num(value: float, digits: int = 0) -> str:
    """A reading, or an em dash when the drive has not answered it."""
    return "—" if value != value else f"{value:.{digits}f}"


_DRIVE_KINDS = ("temp", "current", "fault")


def _faults(drive_health: Any) -> Sequence[Sequence[str]]:
    """Per-drive fault labels, empty on a waldoctl that predates the field —
    a pinned release degrades to no fault reporting rather than raising on
    every status tick."""
    return getattr(drive_health, "faults", ())


class DiagnosticsPage:
    """The diagnostics tab's content."""

    def __init__(self, client: Any, is_open: Callable[[], bool], tab: ui.tab) -> None:
        self.client = client
        self._is_open = is_open
        self._tab = tab
        self._joint_count = ui_state.active_robot.joints.count
        self._values: dict[str, ui.label] = {}
        self._sections: dict[str, ui.column] = {}
        self._rt_fifo: ui.chip | None = None
        self._rt_pinned: ui.chip | None = None
        self._drive_rows: list[tuple[ui.label, list[ui.label]]] = []
        self._drive_heads: dict[str, ui.label] = {}
        self._drives_grid: ui.grid | None = None
        self._supply_box: ui.element | None = None
        self._chart: ui.echart | None = None
        self._events_html: ui.html | None = None
        self._events_version = -1
        self._target_hz = 0.0
        self._constants_asked = False

    # ---- availability ----
    #
    # Each predicate answers "has this backend ever reported this?". They read
    # the emptiness conventions waldoctl documents per field: an empty list is
    # a backend without the sensor, not a backend whose sensor reads zero.

    def _has_loop(self) -> bool:
        return waldoctl.commander.status.loop_health.measured or self._target_hz > 0.0

    def _has_link(self) -> bool:
        return bool(waldoctl.commander.status.link_health.state)

    def _has_drives(self) -> bool:
        dh = waldoctl.commander.status.drive_health
        return (
            bool(dh.temperatures_c or dh.currents_ma or _faults(dh))
            or dh.bus_voltage_v is not None
        )

    def _has_torques(self) -> bool:
        return bool(getattr(ui_state.active_robot, "has_force_torque", False))

    def _has_homing(self) -> bool:
        return bool(waldoctl.commander.status.homing.joints)

    # ---- build ----

    def build(self) -> None:
        with ui.column().classes("w-full gap-2").mark("diagnostics-panel"):
            self._build_loop_section()
            self._build_link_section()
            self._build_drives_section()
            self._build_torque_section()
            self._build_homing_section()
            self._build_events_section()
            self._nothing = (
                ui.label("This backend reports no diagnostics.")
                .classes("text-xs text-[var(--ctk-muted)]")
                .mark("diag-nothing")
            )
        self._apply_visibility()

    def _section(self, key: str, title: str, visible: bool = False) -> ui.column:
        col = ui.column().classes("w-full gap-0").mark(f"diag-section-{key}")
        with col:
            ui.label(title).classes("text-sm font-medium")
        self._sections[key] = col
        col.set_visibility(visible)
        return col

    def _row(self, name: str, marker: str) -> ui.label:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(name).classes("text-xs text-[var(--ctk-muted)] w-28")
            value = ui.label("—").classes("text-xs font-mono").mark(marker)
        self._values[marker] = value
        return value

    def _build_loop_section(self) -> None:
        with self._section("loop", "Control loop"):
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
        with self._section("link", "Motor bus"):
            self._row("State", "diag-link-state").bind_text_from(lh, "state")
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
        """A row per actuator, plus the tool drive some backends report.

        Columns exist only where the backend has that sensor: a bus that
        reports faults and no analog registers gets a fault column and no
        others, rather than two columns of dashes implying broken sensors.
        Like a section, a column stays once shown.
        """
        with self._section("drives", "Drives"):
            self._drives_grid = (
                ui.grid(columns=1).classes("w-full gap-x-3 gap-y-0").mark("diag-drives")
            )
            with self._drives_grid:
                ui.label("Drive").classes("text-xs text-[var(--ctk-muted)]").mark(
                    "diag-drives-head-drive"
                )
                for head, kind in (
                    ("°C", "temp"),
                    ("mA", "current"),
                    ("Faults", "fault"),
                ):
                    self._drive_heads[kind] = (
                        ui.label(head)
                        .classes("text-xs text-[var(--ctk-muted)]")
                        .mark(f"diag-drives-head-{kind}")
                    )
                    self._drive_heads[kind].set_visibility(False)
                names = [f"J{j + 1}" for j in range(self._joint_count)] + ["Tool"]
                for j, name in enumerate(names):
                    label = ui.label(name).classes("text-xs font-mono")
                    cells = [
                        ui.label("—")
                        .classes("text-xs font-mono")
                        .mark(f"diag-drive-{kind}-{j + 1}")
                        for kind in _DRIVE_KINDS
                    ]
                    for cell in cells:
                        cell.set_visibility(False)
                    self._drive_rows.append((label, cells))
            with ui.column().classes("w-full gap-0") as self._supply_box:
                self._row("Supply", "diag-drive-supply")
            self._supply_box.set_visibility(False)
        self._show_row(self._joint_count, False)

    def _show_row(self, index: int, visible: bool) -> None:
        label, cells = self._drive_rows[index]
        label.set_visibility(visible)
        for kind, cell in zip(_DRIVE_KINDS, cells, strict=True):
            cell.set_visibility(visible and self._drive_heads[kind].visible)

    def _show_column(self, kind: str) -> None:
        head = self._drive_heads[kind]
        if head.visible:
            return
        head.set_visibility(True)
        col = _DRIVE_KINDS.index(kind)
        for label, cells in self._drive_rows:
            cells[col].set_visibility(label.visible)
        if self._drives_grid is not None:
            n = 1 + sum(h.visible for h in self._drive_heads.values())
            self._drives_grid.style(
                f"grid-template-columns: repeat({n}, minmax(0, 1fr))"
            )

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
        with self._section("torques", "Torques [Nm] (solid measured, dashed external)"):
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

    def _build_homing_section(self) -> None:
        with self._section("homing", "Homing"):
            self._row("Sequence step", "diag-homing-step")
            self._row("Joints", "diag-homing-joints")

    def _build_events_section(self) -> None:
        """Warnings and errors, with room for what the readout could not show.

        The wire carries a six-part structured error; a one-line strip could
        only ever show the title, which is the half that does not tell you
        what to do about it.
        """
        with self._section("events", "Events", visible=True):
            with ui.row().classes("w-full items-center no-wrap"):
                ui.space()
                ui.button(icon="clear_all", on_click=self._clear_events).props(
                    "flat dense round size=sm"
                ).tooltip("Clear the log").mark("diag-clear-events")
            self._events_html = (
                ui.html("", sanitize=False).classes("w-full").mark("diag-events-log")
            )

    # ---- visibility ----

    def _apply_visibility(self) -> None:
        """Reveal a section the first time its backend has something to say.

        Latched: once shown a section stays, so a drive that stops answering
        reads as an unknown value rather than a section that disappears.
        """
        for key, available in (
            ("loop", self._has_loop),
            ("link", self._has_link),
            ("drives", self._has_drives),
            ("torques", self._has_torques),
            ("homing", self._has_homing),
        ):
            section = self._sections[key]
            if not section.visible and available():
                section.set_visibility(True)
        reported = any(
            col.visible for key, col in self._sections.items() if key != "events"
        )
        self._nothing.set_visibility(not reported and not robot_events.entries)

    # ---- live update, driven by the status loop ----

    def update(self) -> None:
        """Refresh from ``commander.status``, once per status tick.

        The event log is rendered whether or not the tab is open, since a
        warning that lands behind a shut tab still has to announce itself;
        everything else costs nothing to leave until someone looks.

        Synchronous on purpose: this runs inside the status loop's client
        context, and the one query it needs is dispatched as its own task
        rather than awaited under that context.
        """
        self._update_events()
        if not self._is_open():
            return
        # Rendering the log to an open tab is what counts as having seen it.
        robot_events.mark_read()
        if not self._constants_asked:
            self._constants_asked = True
            background_tasks.create(self._ask_constants(), name="diagnostics-constants")
        self._apply_visibility()
        self._update_loop()
        self._update_drives()
        self._update_homing()
        self.update_chart()

    async def _ask_constants(self) -> None:
        """The loop's target rate and its scheduling, fixed at boot and so
        worth exactly one query."""
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
        health = waldoctl.commander.status.drive_health
        temps = health.temperatures_c
        currents = health.currents_ma
        faults = _faults(health)
        reported = max(len(temps), len(currents), len(faults))
        if reported:
            if temps:
                self._show_column("temp")
            if currents:
                self._show_column("current")
            if faults:
                self._show_column("fault")
            self._show_row(self._joint_count, reported > self._joint_count)
            for j, (_, cells) in enumerate(self._drive_rows):
                temp = temps[j] if j < len(temps) else math.nan
                current = currents[j] if j < len(currents) else math.nan
                labels = faults[j] if j < len(faults) else ()
                cells[0].text = _num(temp)
                cells[1].text = _num(current)
                fault_text = ", ".join(labels) if labels else "—"
                if cells[2].text != fault_text:
                    cells[2].text = fault_text
                    if labels:
                        cells[2].classes(add="text-amber-400")
                    else:
                        cells[2].classes(remove="text-amber-400")
        volts = health.bus_voltage_v
        if volts is not None:
            if self._supply_box is not None and not self._supply_box.visible:
                self._supply_box.set_visibility(True)
            self._set("diag-drive-supply", f"{volts:.1f} V")
        elif self._supply_box is not None and self._supply_box.visible:
            self._set("diag-drive-supply", "—")

    def _update_homing(self) -> None:
        homing = waldoctl.commander.status.homing
        if not homing.joints:
            return
        self._set("diag-homing-step", str(homing.sequence_step))
        self._set(
            "diag-homing-joints",
            ", ".join(f"{state}/{phase}" for state, phase in homing.joints),
        )

    def _update_events(self) -> None:
        """Redraw the log, and pulse the tab when it changed out of sight.

        The badge says how many landed; the flash is what makes anyone look.
        """
        if self._events_html is None or robot_events.version == self._events_version:
            return
        self._events_version = robot_events.version
        if not self._is_open():
            flash_tab(self._tab)
        parts: list[str] = []
        for ts, code, title, cause, effect, remedy in reversed(robot_events.entries):
            icon, colour = _band(code)
            esc = html_mod.escape
            detail = " → ".join(x for x in (esc(cause), esc(effect)) if x)
            parts.append(
                f'<div class="diag-event">'
                f'<span class="material-symbols-outlined {colour}">{icon}</span>'
                f'<span class="diag-event-time">{ts}</span>'
                f"<b>{esc(title)}</b>"
                f'<span class="diag-event-code">[{code}]</span>'
                + (f'<div class="diag-event-detail">{detail}</div>' if detail else "")
                + (
                    f'<div class="diag-event-remedy">{esc(remedy)}</div>'
                    if remedy
                    else ""
                )
                + "</div>"
            )
        self._events_html.set_content("".join(parts))

    def update_chart(self) -> None:
        if self._chart is None or not self._sections["torques"].visible:
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

    # ---- actions ----

    def _clear_events(self) -> None:
        robot_events.clear()
        self._events_version = -1
        self._update_events()
