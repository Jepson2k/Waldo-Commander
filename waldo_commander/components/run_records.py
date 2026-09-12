"""Review optional execution records and explicitly download diagnostics."""

import json
from datetime import UTC, datetime

from nicegui import ui

from waldo_commander.components.script_execution import script_exec
from waldo_commander.services.run_records import (
    debugging_export,
    load_record,
    record_directory,
)


def show_run_records() -> None:
    selected = {}

    def recent():
        paths = sorted(
            record_directory().glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:20]
        selected.clear()
        selected.update({p.stem: p for p in paths})
        return {
            p.stem: f"{datetime.fromtimestamp(p.stat().st_mtime, UTC).astimezone():%b %d %H:%M:%S} · {p.stem[:8]}"
            for p in paths
        }

    options = recent()
    with (
        ui.dialog() as dialog,
        ui.card().classes("task-dialog w-[850px] max-w-full flex-nowrap"),
    ):
        ui.label("Run records").classes("text-lg font-semibold")
        with ui.column().classes("panel-body gap-2"):
            ui.checkbox(
                "Record future program runs",
                value=script_exec.record_runs,
                on_change=lambda e: setattr(script_exec, "record_runs", bool(e.value)),
            ).mark("record-runs-enabled")
            with ui.expansion("What is recorded?", icon="info_outline").classes(
                "w-full"
            ):
                ui.label(
                    "Records stay local and include arguments, results, setup snapshots and sampled status. Source code and console output are excluded."
                ).classes("panel-note")
                ui.label(
                    "Debugging export retains numeric values and timing; free text, custom names and mapping keys are removed."
                ).classes("panel-note")
            choice = (
                ui.select(options, value=next(iter(options), None), label="Recent run")
                .classes("w-full")
                .mark("run-record-choice")
            )
            summary = ui.label("No recorded runs yet.").mark("run-record-summary")
            search = (
                ui.input("Filter events")
                .props("dense clearable")
                .classes("w-full")
                .mark("run-record-filter")
            )
            table = (
                ui.table(
                    columns=[
                        {"name": "time", "label": "Elapsed (s)", "field": "time"},
                        {"name": "event", "label": "Event", "field": "event"},
                        {
                            "name": "method",
                            "label": "Skill / command",
                            "field": "method",
                        },
                        {"name": "step", "label": "Step", "field": "step"},
                    ],
                    rows=[],
                    row_key="row",
                    pagination=6,
                )
                .classes("w-full shrink-0")
                .props("dense")
                .mark("run-record-events")
            )
            table.bind_filter_from(search, "value", backward=lambda value: value or "")

            detail = (
                ui.column().classes("w-full shrink-0 gap-2").mark("run-record-detail")
            )
            ui.label("Select an event to inspect its values.").classes("panel-note")

            def refresh():
                detail.clear()
                if choice.value not in selected:
                    table.rows = []
                    table.update()
                    summary.text = "No recorded runs yet."
                    return
                try:
                    events = load_record(selected[choice.value])
                    started = events[0].get("received_ns", 0) if events else 0
                    outcome = next(
                        (
                            e.get("outcome")
                            for e in reversed(events)
                            if e["event"] == "run_finished"
                        ),
                        "No terminal event",
                    )
                    loss = any(
                        e["event"] in {"events_lost", "record_truncated"}
                        for e in events
                    )
                    summary.text = f"{outcome} · {len(events)} entries" + (
                        " · incomplete capture" if loss else ""
                    )
                    table.rows = [
                        {
                            "row": i,
                            "time": round(
                                (e.get("received_ns", started) - started) / 1e9, 3
                            ),
                            "event": e["event"],
                            "method": e.get("method", ""),
                            "step": e.get("step", ""),
                        }
                        for i, e in enumerate(events)
                    ]
                    table.update()
                except (OSError, ValueError) as error:
                    summary.text = f"Record unavailable: {error}"

            def details(event):
                if choice.value not in selected:
                    return
                row = event.args.get("row", {})
                try:
                    record = load_record(selected[choice.value])[row["row"]]
                    detail.clear()
                    with detail:
                        ui.label("Local event values").classes("font-medium")
                        with ui.element("div").classes("event-detail-grid w-full"):
                            fields = (
                                "method",
                                "arguments",
                                "result",
                                "outcome",
                                "error",
                                "message",
                                "step",
                                "fraction",
                                "stop_confirmed",
                                "status",
                                "setup",
                            )
                            for key in fields:
                                value = record.get(key)
                                if value is None or value == "":
                                    continue
                                ui.label(key.replace("_", " ").capitalize()).classes(
                                    "panel-note"
                                )
                                if isinstance(value, (dict, list)):
                                    ui.label(
                                        json.dumps(value, ensure_ascii=False, indent=2)
                                    ).classes("text-sm font-mono whitespace-pre-wrap")
                                else:
                                    ui.label(str(value)).classes("text-sm")
                        with ui.expansion("Raw JSON", icon="code").classes("w-full"):
                            ui.code(
                                json.dumps(record, indent=2), language="json"
                            ).classes("w-full overflow-auto")
                except (OSError, ValueError, KeyError, IndexError) as error:
                    ui.notify(f"Event unavailable: {error}", color="warning")

            def export():
                if choice.value not in selected:
                    return
                try:
                    ui.download.content(
                        debugging_export(selected[choice.value]),
                        filename="waldo-debug.json",
                        media_type="application/json",
                    )
                except (OSError, ValueError) as error:
                    ui.notify(f"Export unavailable: {error}", color="warning")

            table.on("rowClick", details, js_handler="(_event, row) => emit({row})")
            choice.on_value_change(lambda _: refresh())
            refresh()

            def reload_choices():
                options = recent()
                choice.set_options(
                    options,
                    value=choice.value
                    if choice.value in options
                    else next(iter(options), None),
                )
                refresh()

        with ui.row().classes("panel-actions"):
            ui.button("Export debugging data", on_click=export).mark(
                "run-record-export"
            )
            ui.button("Refresh", on_click=reload_choices).props("flat").mark(
                "run-record-refresh"
            )
            ui.space()
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.on("hide", dialog.delete)
    dialog.open()
