"""The strip under the code that shows motion the recorder captured on its own."""

from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from collections.abc import Awaitable, Callable

from nicegui import ui
from waldoctl.recordings import Demonstration

from waldo_commander.common.charts import chart_options
from waldo_commander.demonstrations import Conversion

CHART_ROWS = 2000


def summary_text(recording: Demonstration, conversion: Conversion, guided: bool) -> str:
    moves = sum(1 for s in conversion.spans if s.kind in ("move_l", "move_j"))
    replayed = len(conversion.replayed)
    text = (
        f"{'Hand-guided' if guided else 'Captured'} · "
        f"{recording.duration_s:.1f} s → {moves} move{'s' if moves != 1 else ''}"
    )
    if replayed:
        text += f" ({replayed} replayed)"
    return text


class CaptureReview:
    """Summary, joint chart and a trim range for the span just written into
    the program; Keep leaves the lines, Undo takes them out."""

    def __init__(self) -> None:
        self.recording: Demonstration | None = None
        self.conversion: Conversion | None = None
        self._container: ui.column | None = None
        self._summary: ui.label | None = None
        self._chart: ui.echart | None = None
        self._trim: ui.range | None = None
        self._on_trim: Callable[[float, float], Awaitable[None]] | None = None
        self._on_undo: Callable[[], None] | None = None
        self._on_keep: Callable[[], None] | None = None
        self._trim_task: asyncio.Task | None = None
        self._syncing = False
        self._chart_open = False

    @property
    def visible(self) -> bool:
        return self._container is not None and self._container.visible

    def build(self) -> None:
        self._container = ui.column().classes("capture-review w-full gap-1")
        self._container.mark("capture-review")
        with self._container:
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                ui.icon("timeline").classes("text-warning")
                self._summary = ui.label().classes("text-sm").mark("capture-summary")
                ui.space()
                ui.button(icon="show_chart", on_click=self._toggle_chart).props(
                    "flat dense round"
                ).tooltip("Recorded joint angles").mark("capture-chart-toggle")
                ui.button("Keep", on_click=self._keep).props("flat dense no-caps").mark(
                    "capture-keep"
                )
                ui.button("Undo", on_click=self._undo).props("flat dense no-caps").mark(
                    "capture-undo"
                )
            self._trim = (
                ui.range(min=0.0, max=1.0, step=0.05, value={"min": 0.0, "max": 1.0})
                .props("dense label")
                .classes("w-full px-2")
                .mark("capture-trim")
            )
            self._trim.on_value_change(self._trim_changed)
            self._chart = (
                ui.echart(
                    chart_options(
                        x_name="Time (s)", y_name="Angle (°)", x_type="value"
                    ),
                    renderer="svg",
                )
                .classes("w-full shrink-0")
                .style("height: 150px")
                .mark("capture-chart")
            )
            # The editor is short by default; the chart is there when asked
            # for, and the summary and trim range are what the strip costs.
            self._chart.set_visibility(self._chart_open)
        self._container.set_visibility(False)

    def show(
        self,
        recording: Demonstration,
        conversion: Conversion,
        *,
        guided: bool,
        on_trim: Callable[[float, float], Awaitable[None]],
        on_undo: Callable[[], None],
        on_keep: Callable[[], None],
    ) -> None:
        if self._container is None or self._trim is None or self._chart is None:
            return
        self.recording = recording
        self.conversion = conversion
        self._on_trim, self._on_undo, self._on_keep = on_trim, on_undo, on_keep
        self._set_summary(recording, conversion, guided)
        duration = max(recording.duration_s, 0.05)
        self._syncing = True
        try:
            self._trim.min = 0.0
            self._trim.max = round(duration, 2)
            self._trim.value = {"min": 0.0, "max": round(duration, 2)}
        finally:
            self._syncing = False
        origin = recording.samples[0].observed_ns
        rows = recording.samples[:CHART_ROWS]
        gaps = {gap.sample_index for gap in recording.gaps}
        series = []
        for joint in range(len(rows[0].joints_deg)):
            data = []
            for index, sample in enumerate(rows):
                seconds = (sample.observed_ns - origin) / 1e9
                if index in gaps:
                    data.append([seconds, None])
                data.append([seconds, sample.joints_deg[joint]])
            series.append(
                {
                    "name": f"J{joint + 1}",
                    "type": "line",
                    "showSymbol": False,
                    "data": data,
                }
            )
        self._chart.options["series"] = series
        self._chart.options["legend"]["data"] = [row["name"] for row in series]
        self._chart.update()
        self._container.set_visibility(True)

    def _set_summary(
        self, recording: Demonstration, conversion: Conversion, guided: bool
    ) -> None:
        if self._summary is not None:
            self._summary.set_text(summary_text(recording, conversion, guided))

    def retrimmed(self, recording: Demonstration, conversion: Conversion) -> None:
        """The lines now stand for this sub-span."""
        self.conversion = conversion
        if self._summary is not None and self.recording is not None:
            guided = self._summary.text.startswith("Hand-guided")
            self._set_summary(recording, conversion, guided)

    def selection(self) -> tuple[int, int] | None:
        """Sample indices of the trim range, or None for the whole span."""
        if self._trim is None or self.recording is None or not self._trim.value:
            return None
        first, last = self._trim.value["min"], self._trim.value["max"]
        origin = self.recording.samples[0].observed_ns
        times = [(s.observed_ns - origin) / 1e9 for s in self.recording.samples]
        start = bisect_left(times, first - 1e-6)
        stop = bisect_right(times, last + 1e-6)
        if start == 0 and stop == len(times):
            return None
        return start, stop

    def _trim_changed(self, _event) -> None:
        if self._syncing:
            return
        if self._trim_task is not None and not self._trim_task.done():
            self._trim_task.cancel()
        self._trim_task = asyncio.create_task(self._apply_trim())

    async def _apply_trim(self) -> None:
        await asyncio.sleep(0.3)
        if self._trim is None or self._on_trim is None or not self._trim.value:
            return
        await self._on_trim(self._trim.value["min"], self._trim.value["max"])

    def _toggle_chart(self) -> None:
        self._chart_open = not self._chart_open
        if self._chart is not None:
            self._chart.set_visibility(self._chart_open)

    def hide(self) -> None:
        if self._trim_task is not None and not self._trim_task.done():
            self._trim_task.cancel()
        self.recording = None
        self.conversion = None
        if self._container is not None:
            self._container.set_visibility(False)

    def _keep(self) -> None:
        if self._on_keep is not None:
            self._on_keep()

    def _undo(self) -> None:
        if self._on_undo is not None:
            self._on_undo()


capture_review = CaptureReview()
