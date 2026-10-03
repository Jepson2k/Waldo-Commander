"""Flash a tab or button to say something landed where nobody is looking.

Driven off the element itself, so it is addressed by identity rather than by
whatever glyph its icon happens to render.
"""

from __future__ import annotations

from nicegui import ui

#: Matches the ``tab-flash`` keyframes' duration × iteration count.
_FLASH_S = 2.0


def flash_tab(tab: ui.element | None) -> None:
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


def replay(element: ui.element, name: str) -> None:
    """Restart the one-shot animation keyed on the twin classes ``<name>-a``
    and ``<name>-b``: re-adding a class does not restart its animation, but
    swapping to an identical twin does."""
    a, b = name + "-a", name + "-b"
    if a in element.classes:
        element.classes(add=b, remove=a)
    else:
        element.classes(add=a, remove=b)
