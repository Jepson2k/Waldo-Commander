"""Flash a tab to say something landed in a panel nobody is looking at.

Kept in Python and driven off the tab element itself. The earlier version
lived in a JS snippet that found its tab by matching the icon's text
content, which only ever worked for the one tab whose icon was unique and
broke silently the moment two tabs shared a glyph.
"""

from __future__ import annotations

from nicegui import ui

#: Matches the ``tab-flash`` keyframes' duration × iteration count.
_FLASH_S = 2.0


def flash_tab(tab: ui.tab | None) -> None:
    """Pulse *tab* once, unless it is already pulsing.

    Re-adding the class mid-animation does not restart it, so a second call
    while the first is still running is dropped rather than producing a
    flash that looks stuck.
    """
    if tab is None or "tab-flash" in tab.classes:
        return
    tab.classes(add="tab-flash")
    # Inside the tab's own slot: callers reach here from the status loop and
    # from background tasks, where there is no current slot and a bare
    # ui.timer() raises rather than scheduling.
    with tab:
        ui.timer(_FLASH_S, lambda: tab.classes(remove="tab-flash"), once=True)
