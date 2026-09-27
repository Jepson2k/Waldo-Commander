"""Bottom panel: Diagnostics and the app log, opened from the status footer."""

from __future__ import annotations

import json
from typing import Any

from nicegui import ui

from waldo_commander.components.diagnostics import DiagnosticsPage
from waldo_commander.state import ui_state


class BottomPanel:
    """A card above the footer, hidden until a footer button opens it."""

    def __init__(self, client: Any, attention: ui.element | None) -> None:
        self.client = client
        self._attention = attention
        self.card: ui.element | None = None
        self.tabs: ui.tabs | None = None
        self.diagnostics: DiagnosticsPage | None = None

    @property
    def visible(self) -> bool:
        return self.card is not None and self.card.visible

    def _diagnostics_open(self) -> bool:
        return (
            self.visible and self.tabs is not None and self.tabs.value == "diagnostics"
        )

    def build(self) -> None:
        self.card = ui.element("div").classes("bottom-panel").mark("bottom-panel")
        self.card.set_visibility(False)
        with self.card:
            with ui.row().classes("items-center w-full no-wrap gap-0 px-2"):
                self.tabs = (
                    ui.tabs(value="diagnostics")
                    .props("dense no-caps align=left inline-label")
                    .classes("bottom-panel-tabs")
                )
                with self.tabs:
                    ui.tab(
                        "diagnostics", label="Diagnostics", icon="monitor_heart"
                    ).mark("bottom-tab-diagnostics")
                    ui.tab("log", label="Log", icon="article").mark("bottom-tab-log")
                self.tabs.on_value_change(self._remember)
                ui.space()
                ui.button(icon="close", on_click=self.close).props(
                    "flat round dense color=wc-text"
                ).mark("bottom-panel-close")
            with ui.tab_panels(self.tabs, value="diagnostics").classes("w-full"):
                with ui.tab_panel("diagnostics"):
                    self.diagnostics = DiagnosticsPage(
                        self.client,
                        is_open=self._diagnostics_open,
                        attention=self._attention,
                    )
                    self.diagnostics.build()
                with ui.tab_panel("log"):
                    ui_state.response_log = (
                        ui.log(max_lines=1000)
                        .classes("w-full h-full no-x-scroll well")
                        .mark("response-log")
                    )
        ui_state.diagnostics_page = self.diagnostics
        ui_state.bottom_panel = self

    def open(self, tab: str) -> None:
        assert self.card is not None and self.tabs is not None
        self.tabs.set_value(tab)
        self.card.set_visibility(True)
        self._remember()

    def close(self) -> None:
        assert self.card is not None
        self.card.set_visibility(False)
        self._remember()

    def _remember(self) -> None:
        """Save the open tab, or none, for the next page load to reopen."""
        tab = self.tabs.value if self.visible and self.tabs is not None else None
        ui.run_javascript(f"PanelResize.rememberTab('panel', {json.dumps(tab)})")
