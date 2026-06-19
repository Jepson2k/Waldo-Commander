"""Integration tests for global keybinding actions.

These tests verify keybinding action callbacks directly rather than going
through real Selenium key events. Selenium key delivery is brittle when
no element holds focus, and the bug we're regression-covering lives in
the action callback's behavior — not in the JS focus detection or the
websocket dispatch path. Direct invocation is deterministic and exercises
exactly the code that broke.
"""

from __future__ import annotations

import pytest
import waldoctl
from nicegui import app
from nicegui.testing import User

from tests.helpers.wait import wait_for_app_ready


@pytest.mark.integration
async def test_jog_speed_keybinding_syncs_rating_widget(user: User) -> None:
    """`]` and `[` must update the rating widget, commander.settings.jog.speed,
    storage, icon color, and tooltip in lockstep.

    Regression for the bug where the keybinding only mutated
    ``waldoctl.commander.settings.jog.speed`` so the underlying jog actions used the new
    value but the rating widget visible to the user never moved — making
    it look like the keystroke had no effect. The fix routes both the
    click handler and the keybinding through
    ``ControlPanel._set_rating_step``.

    Verifies the bug at two layers:
    1. The keybinding for `]` / `[` is registered with the right action
    2. Invoking that action updates all five dependent visuals
    """
    from waldo_commander.services.keybindings import keybindings_manager
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    cp = ui_state.control_panel
    refs = cp._rating_widgets["jog_speed"]
    rating = refs["rating"]
    icon = refs["icon"]
    tooltip = refs["tooltip"]
    colors = refs["colors"]

    # Both keybindings must be registered. If anyone removes the entries
    # in services/keybindings.py, this lookup raises KeyError.
    inc_binding = keybindings_manager._bindings["]"]
    dec_binding = keybindings_manager._bindings["["]

    # Seed deterministically — earlier runs may have persisted a different
    # value to app.storage.general["jog_speed"].
    cp.adjust_rating("jog_speed", 50 - waldoctl.commander.settings.jog.speed)
    try:
        assert waldoctl.commander.settings.jog.speed == 50
        assert rating.value == 5
        assert app.storage.general["jog_speed"] == 50
        assert icon.props.get("color") == colors[4]
        assert "50%" in tooltip.text

        # `]` action — should advance by one step.
        inc_binding.action()
        assert waldoctl.commander.settings.jog.speed == 60, (
            "jog speed should advance to 60"
        )
        assert rating.value == 6, "rating widget should reflect new step"
        assert app.storage.general["jog_speed"] == 60, "storage should persist"
        assert icon.props.get("color") == colors[5], (
            "icon color should advance to the 6th palette entry"
        )
        assert "60%" in tooltip.text, (
            f"tooltip should reflect 60%, got {tooltip.text!r}"
        )

        # `[` action — should retreat by one step.
        dec_binding.action()
        assert waldoctl.commander.settings.jog.speed == 50
        assert rating.value == 5
        assert app.storage.general["jog_speed"] == 50
        assert icon.props.get("color") == colors[4]
        assert "50%" in tooltip.text

        # Lower-bound clamp: pressing `[` repeatedly must not go below
        # rating step 1 (= 10%).
        for _ in range(20):
            dec_binding.action()
        assert waldoctl.commander.settings.jog.speed == 10
        assert rating.value == 1
        assert icon.props.get("color") == colors[0]

        # Upper-bound clamp: pressing `]` repeatedly must not exceed
        # rating step 10 (= 100%).
        for _ in range(20):
            inc_binding.action()
        assert waldoctl.commander.settings.jog.speed == 100
        assert rating.value == 10
        assert icon.props.get("color") == colors[9]
    finally:
        cp.adjust_rating("jog_speed", 50 - waldoctl.commander.settings.jog.speed)
