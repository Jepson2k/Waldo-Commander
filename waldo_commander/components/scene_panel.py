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
        self.tab: ui.tab
        self.tip: ui.tooltip
        self.tools: ui.row
        self.body: ui.column
        self.noted: tuple[int, bool] = (0, False)

    def build_tab(self, tabs: ui.tabs) -> None:
        """The panel's tab in the bottom rail; call inside *tabs*."""
        self.tabs = tabs
        with ui.tab(name="scene", label="", icon="view_in_ar").mark("tab-scene") as tab:
            self.tip = ui.tooltip("Scene")
        self.tab = tab

    def note(self, hidden: int, isolated: bool) -> None:
        """Mark the tab while objects are hidden or isolated, so the way back
        to them shows with the panel closed."""
        if (hidden, isolated) == self.noted:
            return
        self.noted = (hidden, isolated)
        parts = ["Scene"]
        if hidden:
            parts.append(f"{hidden} hidden")
        if isolated:
            parts.append("isolated")
        self.tip.set_text(" · ".join(parts))
        if hidden or isolated:
            self.tab.props("alert=wc-action")
        else:
            self.tab.props(remove="alert")

    def build(self, close: Callable[[], None]) -> None:
        """The panel; call inside the bottom rail's tab panels."""
        with ui.tab_panel("scene").classes(
            "overlay-card task-panel scene-panel resizable-panel p-0"
        ):
            with ui.row().classes("scene-panel-head w-full items-center no-wrap gap-0"):
                ui.label("Scene").classes("panel-heading flex-1 min-w-0 truncate")
                self.tools = ui.row().classes(
                    "scene-panel-tools items-center no-wrap gap-0"
                )
                ui.button(icon="close", on_click=close).classes("shrink-0").props(
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
            # Called from an undo's task too, which has no page of its own.
            self.tabs.client.run_javascript(
                "PanelResize.onTabChange('bottom', 'scene')"
            )
