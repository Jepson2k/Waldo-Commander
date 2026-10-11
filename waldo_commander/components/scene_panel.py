"""The scene panel: the 3D view's objects as a list, with the selection's
properties under it, docked over the settings gear so it stays open beside
the program editor.

This builds its tab and frame; the page's scene fills the toolbar and body.
"""

from collections.abc import Callable

from nicegui import ui


class ScenePanel:
    def __init__(self) -> None:
        self.tabs: ui.tabs | None = None
        self.tools: ui.row
        self.body: ui.column

    def build_tab(self, tabs: ui.tabs) -> None:
        """The panel's tab in the bottom rail; call inside *tabs*."""
        self.tabs = tabs
        ui.tab(name="scene", label="", icon="view_in_ar").tooltip("Scene").mark(
            "tab-scene"
        )

    def build(self, close: Callable[[], None]) -> None:
        """The panel; call inside the bottom rail's tab panels."""
        with ui.tab_panel("scene").classes(
            "overlay-card task-panel scene-panel resizable-panel p-0"
        ):
            with ui.row().classes("scene-panel-head w-full items-center no-wrap"):
                ui.label("Scene").classes("panel-heading flex-1")
                self.tools = ui.row().classes("items-center no-wrap gap-0")
                ui.button(icon="close", on_click=close).props(
                    "flat round dense color=wc-text"
                ).tooltip("Close").mark("scene-close")
            self.body = ui.column().classes("scene-panel-body w-full")
            for edge in ("top", "right", "corner"):
                ui.element("div").classes(f"resize-handle-{edge}")

    @property
    def is_open(self) -> bool:
        return self.tabs is not None and self.tabs.value == "scene"

    def open(self) -> None:
        if self.tabs is not None and not self.is_open:
            self.tabs.set_value("scene")
            ui.run_javascript("PanelResize.onTabChange('bottom', 'scene')")
