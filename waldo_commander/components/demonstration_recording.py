"""Observed motion capture, inspection and explicit Python replay insertion."""

from __future__ import annotations

import asyncio
import math
import os
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import ClassVar

from nicegui import ui
from waldoctl import Commander, Panel, PanelSlot
from waldoctl.recordings import Demonstration
from waldoctl.setup import validate_name

from waldo_commander.common.charts import chart_options, expand_chart_button
from waldo_commander.demonstrations import (
    load_demonstration,
    record_demonstration,
    save_demonstration,
    to_program,
)


class DemonstrationPanel(Panel):
    id: ClassVar[str] = "demonstrations"
    display_name: ClassVar[str] = "Demonstrations"
    slot: ClassVar[PanelSlot] = PanelSlot.LEFT_TOP_TAB
    tab_icon: ClassVar[str] = "timeline"
    tab_tooltip: ClassVar[str] = "Record and inspect observed motion"
    order: ClassVar[int] = 30
    default_width: ClassVar[int] = 490
    default_height: ClassVar[int] = 720
    min_width: ClassVar[int] = 390
    min_height: ClassVar[int] = 420
    resizable: ClassVar[bool] = True

    def __init__(self) -> None:
        self.recording: Demonstration | None = None
        self._capture: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._samples = 0
        self._message = ""

    async def stop(self) -> None:
        self._stop.set()
        if self._capture is not None:
            await self._capture

    def build(self, commander: Commander) -> None:
        from waldo_commander.services.programs import is_any_program_running

        directory = (
            Path(
                os.environ.get("WALDO_RECORDING_DIR")
                or Path.home() / ".waldo-commander" / "recordings"
            )
            .expanduser()
            .resolve()
        )

        def path() -> Path:
            return directory / f"{validate_name(name.value)}.json"

        def names() -> list[str]:
            return sorted(p.stem for p in directory.glob("*.json"))

        syncing_span = False
        span_error: str | None = None

        def update_span() -> None:
            nonlocal syncing_span, span_error
            if syncing_span or self.recording is None:
                return
            first, last = first_seconds.value, last_seconds.value
            if (
                first is None
                or last is None
                or not all(math.isfinite(v) for v in (first, last))
                or first > last
            ):
                span_error = "Enter a valid time range."
                summary.set_text(span_error)
                return
            span_error = None
            times = [
                (sample.observed_ns - self.recording.samples[0].observed_ns) / 1e9
                for sample in self.recording.samples
            ]
            syncing_span = True
            start.set_value(bisect_left(times, first))
            end.set_value(bisect_right(times, last))
            syncing_span = False
            refresh_plot()

        def selected() -> Demonstration:
            if span_error:
                raise ValueError(span_error)
            if self.recording is None:
                raise ValueError("Record or load observations first")
            for value in (start.value, end.value):
                if (
                    not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or int(value) != value
                ):
                    raise ValueError("Select whole-number observation indices")
            return self.recording.select(int(start.value), int(end.value))

        def update_indices() -> None:
            nonlocal span_error
            span_error = None
            refresh_plot()

        def refresh_plot() -> None:
            nonlocal syncing_span
            if syncing_span:
                return
            try:
                recording = selected()
            except (ValueError, TypeError) as error:
                summary.set_text(str(error))
                return
            gaps = {gap.sample_index for gap in recording.gaps}
            rows = recording.samples[:5000]
            origin = self.recording.samples[0].observed_ns if self.recording else 0
            syncing_span = True
            first_seconds.set_value((recording.samples[0].observed_ns - origin) / 1e9)
            last_seconds.set_value((recording.samples[-1].observed_ns - origin) / 1e9)
            syncing_span = False
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
            chart.options["series"] = series
            chart.options["legend"]["data"] = [row["name"] for row in series]
            chart.update()
            rate = recording.observed_rate_hz
            summary.set_text(
                f"{len(recording.samples)} observations · {recording.duration_s:.2f} s · "
                f"{len(gaps)} gaps"
                + (
                    " · chart shows first 5,000 observations"
                    if len(recording.samples) > 5000
                    else ""
                )
            )
            capture_details.set_text(
                f"{rate or 0:.1f} Hz observed / {recording.requested_rate_hz:g} Hz requested · Ended: {recording.ended.replace(chr(95), chr(32))}"
            )
            gap_table.rows = [
                {
                    "sample": int(start.value) + g.sample_index,
                    "missing": g.missing_publications,
                    "seconds": round(g.elapsed_s, 4),
                }
                for g in recording.gaps
            ]
            gap_table.update()
            gap_table.set_visibility(bool(gaps))

        def adopt(recording: Demonstration) -> None:
            nonlocal syncing_span, span_error
            self.recording = recording
            span_error = None
            syncing_span = True
            start.set_value(0)
            end.set_value(len(recording.samples))
            syncing_span = False
            refresh_plot()

        async def capture() -> None:
            if self._capture is not None and not self._capture.done():
                return
            self._stop = asyncio.Event()
            self._samples = 0

            async def collect() -> None:
                try:
                    self.recording = await record_demonstration(
                        commander.client,
                        duration_s=float(duration.value),
                        stop=self._stop,
                        on_sample=count,
                    )
                    self._message = "Capture ended; save to keep these observations"
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    RuntimeError,
                    StopAsyncIteration,
                ) as error:
                    self._message = f"Capture failed: {error}"

            def count(sample) -> None:
                self._samples += 1

            self._message = "Capturing controller observations"
            self._capture = asyncio.create_task(collect())

        def load() -> None:
            try:
                adopt(load_demonstration(path()))
                self._message = "Loaded observations"
            except (OSError, ValueError) as error:
                self._message = str(error)

        def save() -> None:
            try:
                destination = path()
                recording = selected()
                directory.mkdir(parents=True, exist_ok=True)
                save_demonstration(destination, recording)
                saved.set_options(names(), value=name.value)
                self._message = "Saved selected span with original timestamps"
            except (OSError, ValueError) as error:
                self._message = str(error)

        def download() -> None:
            try:
                recording = selected()
                import json
                from dataclasses import asdict

                ui.download(
                    json.dumps(
                        {"schema": 1, **asdict(recording)}, allow_nan=False
                    ).encode(),
                    f"{validate_name(name.value)}.json",
                )
            except (ValueError, TypeError) as error:
                self._message = str(error)

        def convert() -> None:
            from waldo_commander.state import ui_state

            try:
                if is_any_program_running():
                    raise ValueError("Stop the running program before converting")
                recording = selected()
                destination = path()
                saved_span = (
                    load_demonstration(destination) if destination.is_file() else None
                )
                conversion = to_program(
                    recording,
                    ui_state.active_robot,
                    name=validate_name(name.value),
                    # Only a saved span can be replayed by the program it
                    # falls back to, so the path is offered when it holds
                    # this span and withheld when it does not.
                    source_path=(destination if saved_span == recording else None),
                )
                program = commander.programs.new(
                    source=conversion.source,
                    filename=f"{validate_name(name.value)}.py",
                )
                commander.programs.switch(program.id)
                if ui_state._program_tab is not None:
                    ui_state._program_tab.parent_slot.parent.set_value("program")
                self._message = f"Converted: {conversion.summary()}"
            except (OSError, ValueError, NotImplementedError) as error:
                self._message = str(error)

        def insert() -> None:
            from waldo_commander.services.motion_recorder import motion_recorder

            try:
                if commander.programs.active is None or is_any_program_running():
                    raise ValueError("Open a stopped program before inserting replay")
                recording = selected()
                recording.require_continuous()
                destination = path()
                if load_demonstration(destination) != recording:
                    raise ValueError(
                        "Save the selected span before inserting its replay call"
                    )
                motion_recorder.insert_skill_call(
                    "from waldo_commander.demonstrations import load_demonstration\n"
                    "from waldo_commander.skills import replay_demonstration\n"
                    f"recording = load_demonstration({str(destination)!r})\n"
                    "replay_demonstration(rbt, recording, "
                    f"replay_gripper={bool(gripper.value)!r})"
                )
                self._message = "Inserted replay; move to its start and inspect the program preview before running"
            except (OSError, ValueError) as error:
                self._message = str(error)

        with ui.column().classes("w-full h-full min-h-0 flex-nowrap gap-2"):
            ui.label("Demonstrations").classes("panel-heading")
            ui.label(
                "Capture joint and tool observations while jogging, hand guiding, or running a program."
            ).classes("text-caption")
            with ui.row().classes("items-center"):
                duration = (
                    ui.number("Max capture (s)", value=30, min=0.1, max=3600)
                    .props("dense")
                    .classes("w-32")
                    .mark("demo-duration")
                )
                record_button = (
                    ui.button("Capture", on_click=capture)
                    .props("dense")
                    .mark("demo-capture")
                )
                stop_button = (
                    ui.button("End capture", on_click=lambda: self._stop.set())
                    .props("dense flat")
                    .mark("demo-stop")
                )
            message = ui.label().classes("text-caption").mark("demo-message")
            with ui.column().classes(
                "w-full flex-1 min-h-0 overflow-y-auto flex-nowrap"
            ):
                with ui.row().classes("w-full"):
                    name = (
                        ui.input("Recording name", value="demonstration")
                        .props("dense")
                        .classes("flex-1 min-w-0")
                        .mark("demo-name")
                    )
                    saved = (
                        ui.select(
                            names(),
                            label="Saved",
                            on_change=lambda e: (
                                name.set_value(e.value) if e.value else None
                            ),
                        )
                        .props("dense")
                        .classes("flex-1 min-w-0")
                        .mark("demo-saved")
                    )
                with ui.row():
                    ui.button("Load", on_click=load).props("dense flat").mark(
                        "demo-load"
                    )
                    ui.button("Save span", on_click=save).props("dense").mark(
                        "demo-save"
                    )
                    ui.button("Export", on_click=download).props("dense flat").mark(
                        "demo-export"
                    )
                with ui.row().classes("w-full gap-2"):
                    first_seconds = (
                        ui.number(
                            "From (s)",
                            value=0,
                            min=0,
                            format="%.3f",
                            on_change=update_span,
                        )
                        .props("dense")
                        .classes("flex-1 min-w-0")
                        .mark("demo-from-seconds")
                    )
                    last_seconds = (
                        ui.number(
                            "To (s)",
                            value=0,
                            min=0,
                            format="%.3f",
                            on_change=update_span,
                        )
                        .props("dense")
                        .classes("flex-1 min-w-0")
                        .mark("demo-to-seconds")
                    )
                with (
                    ui.expansion("Sample details", icon="tune")
                    .classes("w-full")
                    .mark("demo-sample-details")
                ):
                    with ui.row():
                        start = (
                            ui.number(
                                "First sample (0-based)",
                                value=0,
                                min=0,
                                step=1,
                                precision=0,
                                on_change=update_indices,
                            )
                            .props("dense")
                            .classes("w-44")
                            .mark("demo-first")
                        )
                        end = (
                            ui.number(
                                "End (exclusive)",
                                value=1,
                                min=1,
                                step=1,
                                precision=0,
                                on_change=update_indices,
                            )
                            .props("dense")
                            .classes("w-40")
                            .mark("demo-end")
                        )
                    capture_details = ui.label().classes("panel-note")
                summary = (
                    ui.label("No observations loaded")
                    .classes("text-caption")
                    .mark("demo-summary")
                )
                chart = (
                    ui.echart(
                        chart_options(
                            x_name="Time (s)", y_name="Angle (°)", x_type="value"
                        ),
                        renderer="svg",
                    )
                    .classes("w-full shrink-0")
                    .style("height: 260px")
                    .mark("demo-chart")
                )
                expand_chart_button(chart, "Recorded joint angles (°)").mark(
                    "demo-expand-chart"
                )
                gap_table = (
                    ui.table(
                        columns=[
                            {
                                "name": "sample",
                                "label": "Gap before",
                                "field": "sample",
                            },
                            {
                                "name": "missing",
                                "label": "Missed publications",
                                "field": "missing",
                            },
                            {"name": "seconds", "label": "Seconds", "field": "seconds"},
                        ],
                        rows=[],
                        row_key="sample",
                    )
                    .props("dense")
                    .classes("w-full")
                )
                gap_table.set_visibility(False)
                gripper = ui.checkbox(
                    "Replay observed gripper positions", value=False
                ).mark("demo-gripper")
                with ui.expansion("Replay behavior", icon="info_outline").classes(
                    "w-full"
                ):
                    ui.label(
                        "Replay stops at every waypoint and may be much slower. Gripper changes run sequentially at waypoint boundaries; recorded grasp signals are not replayed. Gaps must be excluded by selecting a continuous span."
                    ).classes("text-caption")
            with ui.row().classes("w-full gap-2"):
                ui.button("Convert to program", on_click=convert).props("dense").mark(
                    "demo-convert"
                )
                ui.button("Insert replay call", on_click=insert).props(
                    "dense flat"
                ).mark("demo-insert")

        shown = self.recording
        if shown is not None:
            adopt(shown)

        def tick() -> None:
            nonlocal shown
            active = self._capture is not None and not self._capture.done()
            record_button.set_enabled(not active)
            stop_button.set_enabled(active)
            message.set_text(
                self._message + (f" · {self._samples} observations" if active else "")
            )
            if self.recording is not shown:
                shown = self.recording
                if shown is not None:
                    adopt(shown)

        ui.timer(0.2, tick)
