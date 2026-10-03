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
import time
from collections.abc import Sequence
from typing import Any, Callable

import waldoctl
from nicegui import background_tasks, ui

from waldo_commander.common.charts import chart_options, expand_chart_button
from waldo_commander.common.panel_theme import joint_colors
from waldo_commander.common.tab_flash import flash_tab
from waldo_commander.components.waldo import RobotFace, waldo
from waldo_commander.constants import CHART_PUSH_INTERVAL_S
from waldo_commander.state import robot_events, robot_state, ui_state

logger = logging.getLogger(__name__)

#: Error-code bands (waldoctl.errors). The band says what kind of thing went
#: wrong, so the icon tells a bus-off entry from a degraded loop at a glance;
#: the colour is the entry's severity. Icons are bare Material Symbols
#: ligatures — the font reads the span's text, so Quasar's ``sym_o_`` spelling
#: would render as the word itself.
_BAND_ICON: tuple[tuple[int, int, str], ...] = (
    (10, 29, "route"),  # IK / trajectory
    (30, 39, "open_with"),  # motion
    (40, 49, "lan"),  # comms
    (50, 64, "memory"),  # system / safety
)
_SEVERITY_COLOUR = {"warning": "text-wc-warning", "error": "text-wc-error"}

#: Normal is quiet. A reading only takes colour once it is outside the range
#: its backend treats as healthy, so a panel with no colour in it is a panel
#: with nothing to say, and the operator scans for the exception instead of
#: reading every number.
OK, WARN, FAULT = 0, 1, 2
_SEVERITY_CLASS = {OK: "diag-ok", WARN: "diag-warn", FAULT: "diag-fault"}

#: What the verdict found, each with the level it counts as.
_Reasons = list[tuple[int, str]]

#: Loop tail as a multiple of the period budget, past which the loop counts as
#: degraded. Matches the rule parol6 applies to itself before it logs
#: "loop overbudget" (server/controller.py). It lives here because no backend
#: puts its own bands on the wire; the right home is waldoctl, so that each
#: declares the thresholds it is actually judged against.
LOOP_WARN_RATIO = 1.25

#: Status older than this, seconds, says nothing about the robot now.
STATUS_STALE_S = 1.0


def _band_icon(code: int) -> str:
    for lo, hi, icon in _BAND_ICON:
        if lo <= code <= hi:
            return icon
    return "warning"


#: Back-off bounds for the boot-constants query \[s\]. A backend that is
#: reachable but not yet answering is the normal case this waits out; the
#: ceiling is what stops a permanently mute one being asked forever at the
#: status rate.
_CONSTANTS_RETRY_MIN_S = 2.0
_CONSTANTS_RETRY_MAX_S = 30.0


def _ms(seconds: float) -> str:
    return f"{seconds * 1000.0:.2f} ms"


def _num(value: float, digits: int = 0) -> str:
    """A reading, or an em dash when the drive has not answered it."""
    return "—" if value != value else f"{value:.{digits}f}"


_DRIVE_KINDS = ("temp", "current", "fault")


class _OverrunRate:
    """Overruns per minute over the stretch of the count this page has seen.

    The controller counts from its own boot, which may be hours before the
    page opened, so its total over the page's age is no rate at all.
    """

    def __init__(self) -> None:
        self._since = 0.0
        self._first = -1

    def per_minute(self, count: int, now: float) -> float | None:
        """The rate up to ``now``, or None until there is a stretch to measure."""
        if self._first < 0 or count < self._first:
            # A count that went down is a controller that restarted.
            self._since, self._first = now, count
            return None
        elapsed = now - self._since
        if elapsed <= 0.0:
            return None
        return (count - self._first) * 60.0 / elapsed


def _faults(drive_health: Any) -> Sequence[Sequence[str]]:
    """Per-drive fault labels, empty on a waldoctl that predates the field —
    a pinned release degrades to no fault reporting rather than raising on
    every status tick."""
    return getattr(drive_health, "faults", ())


def _cells(reading: list[float], n: int) -> list[float | None]:
    """A torque reading as *n* chart cells, blank where it has no joint."""
    cells: list[float | None] = [round(v, 3) for v in reading[:n]]
    return cells + [None] * (n - len(cells))


class DiagnosticsPage:
    """The Diagnostics tab of the bottom panel."""

    def __init__(
        self,
        client: Any,
        is_open: Callable[[], bool],
        attention: ui.element | None = None,
    ) -> None:
        self.client = client
        self._is_open = is_open
        self._attention = attention
        self._joint_count = ui_state.active_robot.joints.count
        self._values: dict[str, ui.label] = {}
        self._sections: dict[str, ui.column] = {}
        self._section_heads: dict[str, ui.row] = {}
        self._verdict: ui.label | None = None
        self._verdict_meta: ui.label | None = None
        self._loop_bar: ui.element | None = None
        self._started_at = time.monotonic()
        self._overrun_rate = _OverrunRate()
        self._drives_reported = False
        self._drive_rows: list[tuple[ui.label, list[ui.label]]] = []
        self._drive_heads: dict[str, ui.label] = {}
        self._drives_grid: ui.grid | None = None
        self._drives_summary: ui.label | None = None
        self._drives_expanded = False
        self._supply_box: ui.element | None = None
        self._chart: ui.echart | None = None
        self._events_html: ui.html | None = None
        self._events_version = -1
        self._events_newest: tuple | None = None
        self._target_hz = 0.0
        self._constants_asked = False
        self._constants_retry_at = 0.0
        self._constants_backoff_s = _CONSTANTS_RETRY_MIN_S
        self._chart_pushed_at = 0.0

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
        return ui_state.active_robot.has_force_torque

    def _has_homing(self) -> bool:
        return bool(waldoctl.commander.status.homing.joints)

    # ---- build ----

    def build(self) -> None:
        with ui.column().classes("w-full gap-2").mark("diagnostics-panel"):
            self._build_verdict()
            with ui.element("div").classes("diag-grid"):
                with ui.column().classes("diag-col gap-2"):
                    self._build_safety_section()
                    self._build_loop_section()
                with ui.column().classes("diag-col gap-2"):
                    self._build_drives_section()
                    self._build_link_section()
                    self._build_homing_section()
                with ui.column().classes("diag-wide gap-2"):
                    self._build_torque_section()
                    self._build_events_section()
            self._nothing = (
                ui.label("This backend reports no diagnostics.")
                .classes("text-xs text-wc-text-muted")
                .mark("diag-nothing")
            )
        self._apply_visibility()
        ui.timer(1.0, self._check_stale)

    def _section(self, key: str, title: str, visible: bool = False) -> ui.column:
        col = ui.column().classes("w-full gap-0").mark(f"diag-section-{key}")
        with col:
            with ui.row().classes("w-full items-center no-wrap") as head:
                ui.label(title).classes("text-sm font-medium")
        self._sections[key] = col
        self._section_heads[key] = head
        col.set_visibility(visible)
        return col

    def _row(self, name: str, marker: str) -> ui.label:
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(name).classes("text-xs text-wc-text-muted w-28")
            value = ui.label("—").classes("text-xs font-mono").mark(marker)
        self._values[marker] = value
        return value

    def _build_verdict(self) -> None:
        """One line that answers "is anything wrong" before any number does.

        An operator crossing the shop floor reads this and nothing else; the
        rows below exist for whoever then wants to know why.
        """
        with ui.row().classes("w-full items-baseline no-wrap gap-2"):
            self._verdict = (
                ui.label("Waiting for the robot")
                .classes("diag-verdict")
                .mark("diag-verdict")
            )
            ui.space()
            self._verdict_meta = (
                ui.label("")
                .classes("text-xs text-wc-text-muted font-mono")
                .mark("diag-verdict-meta")
            )

    def _build_safety_section(self) -> None:
        """The three bits that decide whether the arm will move at all.

        All on the wire already, none of it previously shown here — which is
        the wrong way round, since they are the first things an operator
        checks when nothing happens.
        """
        with self._section("safety", "Robot", visible=True):
            self._row("E-stop", "diag-estop")
            self._row("Controller", "diag-controller")
            self._row("Homed", "diag-homed")

    def _severity(self, marker: str, level: int) -> None:
        """Colour a value by how far outside normal it is."""
        label = self._values.get(marker)
        if label is None:
            return
        keep = _SEVERITY_CLASS[level]
        label.classes(
            add=keep, remove=" ".join(c for c in _SEVERITY_CLASS.values() if c != keep)
        )

    def _build_loop_section(self) -> None:
        with self._section("loop", "Control loop"):
            self._row("Rate", "diag-loop-rate")
            self._row("p99 period", "diag-loop-p99")
            # The bar is what makes "normal" legible without being told: the
            # budget is the full width, so how close the tail runs to its
            # deadline is a position rather than a number to be compared
            # against one an operator has to already know.
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("").classes("w-28")
                with ui.element("div").classes("diag-bar").mark("diag-loop-bar"):
                    self._loop_bar = ui.element("div").classes("diag-bar-fill")
            self._row("Overruns", "diag-loop-overruns")
            # Scheduling is how the loop was set up, not how it is running, so
            # it reads as a quiet footnote rather than as two status chips that
            # look like faults whenever a backend does not use SCHED_FIFO.
            self._row("Scheduling", "diag-loop-sched")

    def _build_link_section(self) -> None:
        with self._section("link", "Motor bus"):
            self._row("State", "diag-link-state")
            self._row("Restarts", "diag-link-restarts")
            self._row("TX errors", "diag-link-tx-errors")
            self._row("RX frames", "diag-link-rx-frames")

    def _build_drives_section(self) -> None:
        """A row per actuator, plus the tool drive some backends report.

        Columns exist only where the backend has that sensor: a bus that
        reports faults and no analog registers gets a fault column and no
        others, rather than two columns of dashes implying broken sensors.
        Like a section, a column stays once shown.
        """
        with self._section("drives", "Drives"):
            self._drives_summary = (
                ui.label("")
                .classes("wc-caption text-wc-text-muted")
                .mark("diag-drives-summary")
            )
            self._drives_grid = (
                ui.grid(columns=1).classes("w-full gap-x-4 gap-y-0").mark("diag-drives")
            )
            self._drives_grid.set_visibility(False)
            with self._drives_grid:
                ui.label("Drive").classes("text-xs text-wc-text-muted").mark(
                    "diag-drives-head-drive"
                )
                for head, kind in (
                    ("°C", "temp"),
                    ("mA", "current"),
                    ("Faults", "fault"),
                ):
                    self._drive_heads[kind] = (
                        ui.label(head)
                        .classes("text-xs text-wc-text-muted")
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
            # The name column is as wide as every section's labels, so the
            # values start where the rows above put theirs.
            n = sum(h.visible for h in self._drive_heads.values())
            self._drives_grid.style(
                f"grid-template-columns: 7rem repeat({n}, max-content)"
            )

    def _build_torque_section(self) -> None:
        n = self._joint_count
        palette = joint_colors()
        series: list[dict[str, Any]] = []
        for measured in (True, False):
            for j in range(n):
                color = palette[j % len(palette)]
                series.append(
                    {
                        "name": f"J{j + 1}" if measured else f"J{j + 1} external",
                        "type": "line",
                        "showSymbol": False,
                        "lineStyle": {"width": 1.5 if measured else 1}
                        | ({} if measured else {"type": "dashed"})
                        | {"color": color},
                        "itemStyle": {"color": color},
                        # Column 0 of the dataset is the time.
                        "encode": {"x": 0, "y": len(series) + 1},
                    }
                )
        with self._section("torques", "Joint torque"):
            with ui.row().classes("w-full items-center gap-2"):
                mode = (
                    ui.select(
                        {
                            "measured": "Measured",
                            "external": "External",
                            "both": "Both",
                        },
                        value="measured",
                    )
                    .props('dense aria-label="Torque source"')
                    .classes("w-28")
                    .mark("diag-torque-source")
                )
                joint = (
                    ui.select(
                        {0: "All joints", **{i: f"J{i}" for i in range(1, n + 1)}},
                        value=0,
                    )
                    .props('dense aria-label="Torque joint"')
                    .classes("w-28")
                    .mark("diag-torque-joint")
                )
            options = chart_options(y_name="Nm")
            options["series"] = series
            # One row per sample, the time once rather than once per series.
            options["dataset"] = {"source": []}
            options["legend"].update(
                {"data": [f"J{i + 1}" for i in range(n)], "selectedMode": False}
            )
            self._chart = (
                ui.echart(options, renderer="svg")
                .classes("w-full")
                .style("height: 230px")
                .mark("diag-torque-chart")
            )

            def select_series():
                assert self._chart is not None
                selected = {}
                for i in range(n):
                    visible = joint.value in (0, i + 1)
                    selected[f"J{i + 1}"] = visible and mode.value != "external"
                    selected[f"J{i + 1} external"] = (
                        visible and mode.value != "measured"
                    )
                self._chart.options["legend"]["data"] = [
                    f"J{i + 1} external" if mode.value == "external" else f"J{i + 1}"
                    for i in range(n)
                ]
                self._chart.options["legend"]["selected"] = selected
                self._chart.update()

            mode.on_value_change(select_series)
            joint.on_value_change(select_series)
            select_series()
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Solid: measured · dashed: external").classes("panel-note")
                expand_chart_button(self._chart, "Joint torque (Nm)").mark(
                    "diag-expand-chart"
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
            with self._section_heads["events"]:
                ui.space()
                ui.button(icon="clear_all", on_click=self._clear_events).props(
                    "flat dense round size=xs"
                ).tooltip("Clear the log").mark("diag-clear-events")
            self._events_html = (
                ui.html("", sanitize=False).classes("w-full").mark("diag-events-log")
            )
            # An empty log is a claim, not a blank: it says the backend has
            # reported nothing since this session started, which is different
            # from the panel having nowhere to put it.
            with (
                ui.row()
                .classes("items-center no-wrap gap-2")
                .mark("diag-events-empty") as self._events_empty
            ):
                waldo(RobotFace.HAPPY, size=28, color="text-muted")
                ui.label("Nothing reported since start.").classes(
                    "text-xs text-wc-text-muted"
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
        always_on = ("events", "safety")
        reported = any(
            col.visible for key, col in self._sections.items() if key not in always_on
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
        if not self._constants_asked and time.monotonic() >= self._constants_retry_at:
            self._constants_asked = True
            # Lazy, so a slow query cannot be started a second time beside
            # itself; the back-off below is what keeps the retry rate sane,
            # because a lazily queued coroutine fires the moment the running
            # one finishes.
            background_tasks.create_lazy(
                self._ask_constants(), name="diagnostics-constants"
            )
        self._apply_visibility()
        worst = OK
        reasons: _Reasons = []
        worst, reasons = self._update_conditions(worst, reasons)
        worst, reasons = self._update_safety(worst, reasons)
        worst, reasons = self._update_loop(worst, reasons)
        worst, reasons = self._update_link(worst, reasons)
        worst, reasons = self._update_drives(worst, reasons)
        self._update_homing()
        self._update_verdict(worst, reasons)
        self.update_chart()

    async def _ask_constants(self) -> None:
        """The loop's target rate and its scheduling, fixed at boot and so
        worth exactly one query."""
        try:
            stats = await self.client.loop_stats()
        except NotImplementedError:
            # A backend that does not implement it never will; asking again
            # is asking the same question of the same code.
            return
        except Exception as exc:
            logger.debug("loop_stats failed: %s", exc)
            self._retry_constants_later()
            return
        if stats is None:
            self._retry_constants_later()
            return
        self._constants_backoff_s = _CONSTANTS_RETRY_MIN_S
        self._target_hz = stats.target_hz
        traits = [
            name
            for name, on in (("real-time", stats.rt_fifo), ("pinned", stats.rt_pinned))
            if on
        ]
        self._set("diag-loop-sched", ", ".join(traits) if traits else "standard")

    def _retry_constants_later(self) -> None:
        """Re-arm the boot-constants query, backing off as it keeps failing.

        Clearing the latch alone re-fired the query on the very next status
        tick, so a backend that answered nothing was queried at the status
        rate for as long as the tab stayed open.
        """
        self._constants_asked = False
        self._constants_retry_at = time.monotonic() + self._constants_backoff_s
        self._constants_backoff_s = min(
            self._constants_backoff_s * 2, _CONSTANTS_RETRY_MAX_S
        )

    def _set(self, marker: str, text: str) -> None:
        label = self._values.get(marker)
        if label is not None and label.text != text:
            label.text = text

    def _update_conditions(self, worst: int, reasons: _Reasons) -> tuple[int, _Reasons]:
        """What the backend itself says is wrong, ahead of anything inferred:
        its latched error has stopped it, and its warnings degrade it."""
        error = robot_state.standing_error
        if error is not None:
            worst, reasons = FAULT, [*reasons, (FAULT, error.title)]
        warnings = waldoctl.commander.status.warnings.entries
        if warnings:
            worst = max(worst, WARN)
            reasons = [*reasons, (WARN, warnings[0].title)]
        return worst, reasons

    def _update_safety(self, worst: int, reasons: _Reasons) -> tuple[int, _Reasons]:
        """E-stop, controller and homed: whether the arm will move at all."""
        status = waldoctl.commander.status
        # estop == 1 is the chain intact, matching the controller wire format.
        pressed = status.io.estop == 0
        self._set("diag-estop", "pressed" if pressed else "clear")
        self._severity("diag-estop", FAULT if pressed else OK)
        if pressed:
            worst, reasons = max(worst, FAULT), [*reasons, (FAULT, "e-stop pressed")]

        ctrl = status.controller
        if ctrl.mode:
            mode = ctrl.mode.lower()
            self._set(
                "diag-controller",
                mode if ctrl.enabled else f"{mode} · disabled",
            )
            self._severity("diag-controller", OK if ctrl.enabled else FAULT)
            if not ctrl.enabled:
                worst = max(worst, FAULT)
                reasons = [*reasons, (FAULT, "controller disabled")]
        else:
            self._set("diag-controller", "—")

        homed = bool(robot_state.homed)
        self._set("diag-homed", "homed" if homed else "not homed")
        self._severity("diag-homed", OK if homed else WARN)
        if not homed:
            worst, reasons = max(worst, WARN), [*reasons, (WARN, "not homed")]
        return worst, reasons

    def _update_loop(self, worst: int, reasons: _Reasons) -> tuple[int, _Reasons]:
        health = waldoctl.commander.status.loop_health
        self._set(
            "diag-loop-rate",
            f"{self._target_hz:.0f} Hz target" if self._target_hz else "—",
        )
        if not health.measured:
            self._set("diag-loop-p99", "not reported by this backend")
            self._set("diag-loop-overruns", "—")
            return worst, reasons
        budget = 1.0 / self._target_hz if self._target_hz else 0.0
        self._set(
            "diag-loop-p99",
            f"{_ms(health.p99_period_s)} of {_ms(budget)} budget"
            if budget
            else _ms(health.p99_period_s),
        )
        if budget:
            ratio = health.p99_period_s / budget
            over = ratio >= LOOP_WARN_RATIO
            self._severity("diag-loop-p99", WARN if over else OK)
            if self._loop_bar is not None:
                # Capped at the full width: past the budget the bar is already
                # saying everything it can, and the number carries the rest.
                self._loop_bar.style(f"width: {min(ratio, 1.0) * 100:.0f}%")
                self._loop_bar.classes(
                    add="over" if over else "", remove="" if over else "over"
                )
            if over:
                worst = max(worst, WARN)
                reasons = [*reasons, (WARN, f"loop tail {ratio:.0%} of budget")]
        # A bare count since boot says nothing without a time base: nine
        # overruns in a minute and nine in a day are different machines.
        rate = self._overrun_rate.per_minute(health.overruns, time.monotonic())
        self._set(
            "diag-loop-overruns",
            f"{health.overruns} since start"
            + ("" if rate is None else f" · {rate:.1f}/min"),
        )
        return worst, reasons

    def _update_link(self, worst: int, reasons: _Reasons) -> tuple[int, _Reasons]:
        """Bus state, where anything but Up is the whole story."""
        lh = waldoctl.commander.status.link_health
        state = lh.state
        self._set("diag-link-state", state)
        self._set("diag-link-restarts", str(lh.restarts))
        self._set("diag-link-tx-errors", str(lh.tx_errors))
        self._set("diag-link-rx-frames", str(lh.rx_frames))
        if not state:
            return worst, reasons
        # Backends spell the CAN states either way: ErrorPassive, ERROR_PASSIVE.
        normalised = state.lower().replace("_", "")
        level = OK if normalised in ("up", "unknown") else FAULT
        if normalised == "errorpassive":
            level = WARN
        self._severity("diag-link-state", level)
        if level:
            worst = max(worst, level)
            reasons = [*reasons, (level, f"motor bus {state}")]
        return worst, reasons

    def _update_verdict(self, worst: int, reasons: _Reasons) -> None:
        """The headline, and the two constants worth carrying beside it."""
        if self._verdict is None:
            return
        text = {
            OK: "Running normally",
            WARN: "Running degraded",
            FAULT: "Stopped",
        }[worst]
        if worst:
            # The first finding at the worst level is the one that explains it.
            text = f"{text} — {next(r for level, r in reasons if level == worst)}"
        self._show_verdict(text, worst)
        if self._verdict_meta is not None:
            up = time.monotonic() - self._started_at
            rate = f"{self._target_hz:.0f} Hz · " if self._target_hz else ""
            meta = f"{rate}up {int(up) // 60}m{int(up) % 60:02d}s"
            if self._verdict_meta.text != meta:
                self._verdict_meta.text = meta

    def _show_verdict(self, text: str, level: int) -> None:
        assert self._verdict is not None
        if self._verdict.text != text:
            self._verdict.text = text
        keep = _SEVERITY_CLASS[level]
        self._verdict.classes(
            add=keep, remove=" ".join(c for c in _SEVERITY_CLASS.values() if c != keep)
        )

    def _check_stale(self) -> None:
        """Say so once status stops arriving: every reading below is then
        the last one heard, not the robot as it is."""
        last = waldoctl.commander.status.last_update
        if self._verdict is None or not last or not self._is_open():
            return
        age = time.time() - last
        if age >= STATUS_STALE_S:
            self._show_verdict(f"No status for {int(age)} s", FAULT)

    def _update_drives(self, worst: int, reasons: _Reasons) -> tuple[int, _Reasons]:
        health = waldoctl.commander.status.drive_health
        temps = health.temperatures_c
        currents = health.currents_ma
        faults = _faults(health)
        reported = max(len(temps), len(currents), len(faults))
        faulted: list[str] = []
        if reported:
            # A fault column of dashes says nothing; the table appears once
            # there is a reading or a fault to put in it, and then stays.
            if not self._drives_expanded and (temps or currents or any(faults)):
                self._drives_expanded = True
                if self._drives_grid is not None:
                    self._drives_grid.set_visibility(True)
                if self._drives_summary is not None:
                    self._drives_summary.set_visibility(False)
            if not self._drives_expanded and self._drives_summary is not None:
                count = min(reported, self._joint_count)
                self._drives_summary.set_text(f"{count} drives · no faults")
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
                        cells[2].classes(add="diag-warn")
                    else:
                        cells[2].classes(remove="diag-warn")
                if labels:
                    faulted.append(self._drive_rows[j][0].text)
        elif self._drives_reported:
            # Readings the backend stopped sending are unknown now, not
            # whatever they last were.
            for _, cells in self._drive_rows:
                for cell in cells:
                    cell.text = "—"
                cells[2].classes(remove="diag-warn")
        self._drives_reported = reported > 0
        volts = health.bus_voltage_v
        if volts is not None:
            if self._supply_box is not None and not self._supply_box.visible:
                self._supply_box.set_visibility(True)
            self._set("diag-drive-supply", f"{volts:.1f} V")
        elif self._supply_box is not None and self._supply_box.visible:
            self._set("diag-drive-supply", "—")
        if faulted:
            worst = max(worst, WARN)
            reasons = [*reasons, (WARN, f"drive fault on {', '.join(faulted)}")]
        return worst, reasons

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
        # Only an event that just landed slides in, not the log a page opens on.
        first_draw = self._events_version == -1
        self._events_version = robot_events.version
        if not self._is_open():
            flash_tab(self._attention)
        newest = robot_events.entries[-1] if robot_events.entries else None
        arrived = not first_draw and newest is not None
        arrived = arrived and newest is not self._events_newest
        self._events_newest = newest
        parts: list[str] = []
        for ts, code, title, cause, effect, remedy, severity in reversed(
            robot_events.entries
        ):
            icon = _band_icon(code)
            colour = _SEVERITY_COLOUR.get(severity, "text-wc-warning")
            esc = html_mod.escape
            detail = " → ".join(x for x in (esc(cause), esc(effect)) if x)
            fresh = " diag-event-new" if arrived and not parts else ""
            parts.append(
                f'<div class="diag-event{fresh}">'
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
        self._events_empty.set_visibility(not parts)

    def update_chart(self) -> None:
        if self._chart is None or not self._sections["torques"].visible:
            return
        now = time.monotonic()
        if now - self._chart_pushed_at < CHART_PUSH_INTERVAL_S:
            return
        result = robot_state.torque_time_series.get_series_if_dirty()
        if result is None:
            return
        self._chart_pushed_at = now
        timestamps, measured, external = result
        n = self._joint_count
        source = [
            [round(t * 1000.0), *_cells(m, n), *_cells(e, n)]
            for t, m, e in zip(timestamps, measured, external)
        ]
        with self._chart.props.suspend_updates():
            self._chart.options["dataset"]["source"] = source
        self._chart.run_chart_method("setOption", {"dataset": {"source": source}})

    # ---- actions ----

    def _clear_events(self) -> None:
        robot_events.clear()
        self._update_events()
