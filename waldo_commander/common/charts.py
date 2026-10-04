"""Readable chart styling, live pushes that send only new samples, and an
expanded view of the same observations."""

import weakref
from bisect import bisect_right
from collections.abc import Sequence

from nicegui import json, ui

from waldo_commander.common.panel_theme import chart_grid, chart_text, joint_colors
from waldo_commander.common.theme import hex_of

#: Expanded views of a live chart, which receive its pushes too.
_mirrors: weakref.WeakKeyDictionary[ui.echart, weakref.WeakSet[ui.echart]] = (
    weakref.WeakKeyDictionary()
)


def chart_options(
    *, x_name: str = "Time", y_name: str = "", x_type: str = "time"
) -> dict:
    return {
        "animation": False,
        "color": joint_colors(),
        "tooltip": {
            "trigger": "axis",
            "confine": True,
            "backgroundColor": hex_of("surface"),
            "textStyle": {"color": hex_of("text")},
            "borderColor": hex_of("control"),
        },
        "legend": {"top": 0, "textStyle": {"color": chart_text(), "fontSize": 12}},
        "grid": {"left": 54, "right": 20, "top": 46, "bottom": 48},
        "xAxis": {
            "type": x_type,
            "name": x_name,
            "nameLocation": "middle",
            "nameGap": 28,
            "axisLabel": {"color": chart_text(), "fontSize": 12},
            "nameTextStyle": {"color": chart_text(), "fontSize": 12},
            "splitLine": {"show": False},
        },
        "yAxis": {
            "type": "value",
            "name": y_name,
            "scale": True,
            "axisLabel": {"color": chart_text(), "fontSize": 12},
            "nameTextStyle": {"color": chart_text(), "fontSize": 12},
            "splitLine": {"lineStyle": {"color": chart_grid()}},
        },
        "series": [],
    }


def fresh_since(timestamps: Sequence[float], after: float) -> int:
    """Index of the first sample in ``timestamps`` (ascending) newer than ``after``."""
    return bisect_right(timestamps, after)


def push_live(chart: ui.echart, rows: list[list[float | None]], keep: int) -> None:
    """Append ``rows`` to the chart's series and keep its newest ``keep`` points.

    A row is ``[time, value for series 0, value for series 1, ...]``; ``None``
    adds nothing to that series. The browser receives the rows alone, not the
    history, and so does any expanded view of the chart. While an expanded view
    covers the chart, only the view is drawn.
    """
    if not rows:
        return
    mirrors = list(_mirrors.get(chart, ()))
    for target in [chart, *mirrors]:
        # The props keep the history for a remount, without sending it.
        with target.props.suspend_updates():
            for index, series in enumerate(target.options["series"]):
                data = series["data"]
                for row in rows:
                    if index + 1 < len(row) and row[index + 1] is not None:
                        data.append([row[0], row[index + 1]])
                if len(data) > keep:
                    del data[: len(data) - keep]
    for target in mirrors or [chart]:
        target.client.run_javascript(
            f"liveChartAppend({target.id}, {json.dumps(rows)}, {keep})"
        )


def expand_chart_button(chart: ui.echart, title: str) -> ui.button:
    """A button that opens ``chart`` large in a dialog; a chart fed by
    :func:`push_live` keeps streaming into it."""

    def expand() -> None:
        with (
            ui.dialog() as dialog,
            ui.card().classes("task-dialog w-[1100px] max-w-full"),
        ):
            with ui.row().classes("w-full items-center"):
                ui.label(title).classes("panel-heading")
                ui.space()
                ui.button(icon="close", on_click=dialog.close).props(
                    "flat round dense"
                ).mark("expanded-chart-close").tooltip("Close expanded chart")
            expanded = (
                # NiceGUI's observable containers retain their parent on
                # deepcopy. A JSON copy gives this view independent observers.
                ui.echart(json.loads(json.dumps(chart.options)))
                .classes("w-full")
                .style("height: min(65vh, 650px)")
            )

            _mirrors.setdefault(chart, weakref.WeakSet()).add(expanded)

        def close() -> None:
            mirrors = _mirrors.get(chart, weakref.WeakSet())
            mirrors.discard(expanded)
            if not mirrors:
                # The chart was covered and not drawn; bring it up to date.
                history = [series["data"] for series in chart.options["series"]]
                chart.client.run_javascript(
                    f"liveChartLoad({chart.id}, {json.dumps(history)})"
                )
            dialog.delete()

        dialog.on("hide", close)
        dialog.open()

    return ui.button("Expand chart", icon="open_in_full", on_click=expand).props(
        "flat dense"
    )
