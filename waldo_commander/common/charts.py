"""Readable chart styling and an expanded view of the same observations."""

from nicegui import json, ui

from waldo_commander.common.panel_theme import chart_grid, chart_text, joint_colors
from waldo_commander.common.theme import hex_of


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


def expand_chart_button(chart: ui.echart, title: str) -> ui.button:
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

            # Only replace data, so focusing a series in this view remains stable.
            def refresh() -> None:
                expanded.run_chart_method(
                    "setOption", {"series": chart.options.get("series", [])}
                )

            timer = ui.timer(0.3, refresh)

        def close() -> None:
            timer.cancel()
            dialog.delete()

        dialog.on("hide", close)
        dialog.open()

    return ui.button("Expand chart", icon="open_in_full", on_click=expand).props(
        "flat dense"
    )
