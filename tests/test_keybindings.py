"""Integration tests for global keybinding actions.

These tests verify keybinding action callbacks directly rather than going
through real Selenium key events. Selenium key delivery is brittle when
no element holds focus, and the bug we're regression-covering lives in
the action callback's behavior — not in the JS focus detection or the
websocket dispatch path. Direct invocation is deterministic and exercises
exactly the code that broke.
"""

from __future__ import annotations

import asyncio

import pytest
import waldoctl
from waldoctl import ActionState
from nicegui import Client, app
from nicegui.events import (
    KeyboardAction,
    KeyboardKey,
    KeyboardModifiers,
    KeyEventArguments,
)
from nicegui.testing import User

from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
    wait_for_motion_start,
)


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


@pytest.mark.integration
async def test_alt_m_cycles_mode_on_all_keyboard_layouts(user: User) -> None:
    """Alt+M must cycle the AI control mode from both event shapes browsers
    send: Linux/Windows report ``key: "m"`` with altKey, but macOS Option
    *composes* a character (Option+M → ``key: "µ"``), so matching must fall
    back to the physical key code (``KeyM``). Regression for the shortcut
    being dead on Macs because the manager matched only ``e.key.name``."""
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_mode,
        set_control_mode,
    )
    from waldo_commander.services.keybindings import keybindings_manager
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    assert ui_state.active_client_id is not None
    ng_client = Client.instances[ui_state.active_client_id]

    def alt_m(name: str, *, keydown: bool) -> KeyEventArguments:
        return KeyEventArguments(
            sender=ng_client.layout,
            client=ng_client,
            action=KeyboardAction(keydown=keydown, keyup=not keydown, repeat=False),
            key=KeyboardKey(name=name, code="KeyM", location=0),
            modifiers=KeyboardModifiers(alt=True, ctrl=False, meta=False, shift=False),
        )

    set_control_mode(ControlMode.INSPECT)
    try:
        with ng_client:
            # macOS shape: Option composes "µ"; only the code says KeyM.
            keybindings_manager.handle_key(alt_m("µ", keydown=True))
            keybindings_manager.handle_key(alt_m("µ", keydown=False))
        assert control_mode() is ControlMode.AUTO_EDITS, (
            "macOS Option+M (key 'µ', code KeyM) must cycle the mode"
        )
        with ng_client:
            # Linux/Windows shape: plain "m" with altKey.
            keybindings_manager.handle_key(alt_m("m", keydown=True))
            keybindings_manager.handle_key(alt_m("m", keydown=False))
        assert control_mode() is ControlMode.AUTOPILOT, (
            "plain Alt+M (key 'm') must still cycle the mode"
        )
    finally:
        set_control_mode(ControlMode.INSPECT)


@pytest.mark.integration
async def test_wasd_jog_keys_follow_arrow_inversion(user: User) -> None:
    """The Invert X/Y Jog settings must flip the WASD jog keys through the
    same funnel as the arrow buttons: 'd' commands X+ by default and X- when
    invert-X is on; 'w' commands Y+ by default and Y- when invert-Y is on.
    (The matching arrow-button flip is covered in test_control_panel_jogging.)
    """
    from waldo_commander.services.keybindings import keybindings_manager
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()

    assert ui_state.active_client_id is not None
    ng_client = Client.instances[ui_state.active_client_id]

    # Earlier suite tests leave the arm at arbitrary poses where a WRF X/Y
    # step can be refused; start each direction check from the home pose.
    # Gate on the full joint vector: joint 0 is usually already at 90 from
    # prior tests, so checking it alone can pass before the teleport lands
    # and the jog then runs from a pose where the step is IK-refused.
    panel = ui_state.control_panel
    assert panel is not None
    home = [90.0, -90.0, 180.0, 0.0, 0.0, 180.0]
    await panel.client.teleport(home)
    for _ in range(50):
        angles = [float(a) for a in waldoctl.commander.status.joints.angles.deg]
        if all(abs(a - h) < 1.0 for a, h in zip(angles, home)):
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"teleport to home did not settle: {angles}")

    def key_event(name: str, *, keydown: bool) -> KeyEventArguments:
        return KeyEventArguments(
            sender=ng_client.layout,
            client=ng_client,
            action=KeyboardAction(keydown=keydown, keyup=not keydown, repeat=False),
            key=KeyboardKey(name=name, code=f"Key{name.upper()}", location=0),
            modifiers=KeyboardModifiers(alt=False, ctrl=False, meta=False, shift=False),
        )

    async def wait_idle() -> None:
        for _ in range(100):
            if waldoctl.commander.status.action.state == ActionState.IDLE:
                return
            await asyncio.sleep(0.1)

    async def tap_key(name: str, axis_attr: str) -> float:
        """Tap a jog key (keydown+keyup = click step) and return the axis delta."""
        # The 5mm move_l has a sub-tolerance creep phase before its main ramp,
        # so value-stability would trigger early — gate on action state.
        await wait_idle()
        initial = float(getattr(waldoctl.commander.status.pose, axis_attr))
        with ng_client:
            # No await between the events, so the hold timer can never fire:
            # this is deterministically a click (single step).
            keybindings_manager.handle_key(key_event(name, keydown=True))
            keybindings_manager.handle_key(key_event(name, keydown=False))
        await wait_for_motion_start()
        await wait_idle()
        return float(getattr(waldoctl.commander.status.pose, axis_attr)) - initial

    waldoctl.commander.settings.jog.joint_step_deg = 5.0

    invert_x = next(iter(user.find(marker="switch-invert-x").elements))
    invert_y = next(iter(user.find(marker="switch-invert-y").elements))
    try:
        delta = await tap_key("d", "x")
        assert 4.9 <= delta <= 5.1, f"d should command X+5mm, moved {delta:.2f}mm"

        invert_x.set_value(True)
        await asyncio.sleep(0)
        assert keybindings_manager._bindings["d"].description == "Jog X-", (
            "help-menu description must follow the inversion"
        )
        delta = await tap_key("d", "x")
        assert -5.1 <= delta <= -4.9, (
            f"d should command X-5mm with invert-X on, moved {delta:.2f}mm"
        )

        delta = await tap_key("w", "y")
        assert 4.9 <= delta <= 5.1, f"w should command Y+5mm, moved {delta:.2f}mm"

        invert_y.set_value(True)
        await asyncio.sleep(0)
        delta = await tap_key("w", "y")
        assert -5.1 <= delta <= -4.9, (
            f"w should command Y-5mm with invert-Y on, moved {delta:.2f}mm"
        )
    finally:
        invert_x.set_value(False)
        invert_y.set_value(False)
        await asyncio.sleep(0)
