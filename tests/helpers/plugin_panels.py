"""Mount test-defined panels through real ``waldoctl.panels`` discovery."""

from __future__ import annotations

import importlib.metadata

import pytest
from waldoctl import Panel

from waldo_commander.state import ui_state


def install_plugin_panels(
    monkeypatch: pytest.MonkeyPatch, *panels: type[Panel]
) -> None:
    """Make the ``waldoctl.panels`` entry points list ``panels`` and nothing
    else, as an installed plugin package would, and drop any panels already
    discovered so the next page build finds these."""
    real = importlib.metadata.entry_points
    fake = [
        importlib.metadata.EntryPoint(
            name=p.id,
            value=f"{p.__module__}:{p.__qualname__}",
            group="waldoctl.panels",
        )
        for p in panels
    ]

    def entry_points(*, group: str = "") -> object:
        if group == "waldoctl.panels":
            return fake
        return real(group=group) if group else real()

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
