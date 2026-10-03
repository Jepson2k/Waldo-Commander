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
from nicegui import Client, app
from nicegui.events import (
    KeyboardAction,
    KeyboardKey,
    KeyboardModifiers,
    KeyEventArguments,
)
from nicegui.testing import User

from tests.helpers.motion import settled, wait_moved
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    teleport_to_jog_pose,
    wait_for_app_ready,
    wait_for_motion_start,
)


@pytest.mark.integration
async def test_jog_speed_keys_and_popover_and_the_mode_shortcut(user: User) -> None:
    """`]` and `[` must update the rating widget, commander.settings.jog.speed,
    storage, and tooltip in lockstep, and so must a dot picked in the level
    chip's popover. Alt+M cycles the AI control mode on every keyboard layout.

    Regression for the bug where the keybinding only mutated
    ``waldoctl.commander.settings.jog.speed`` so the underlying jog actions used the new
    value but the rating widget visible to the user never moved — making
    it look like the keystroke had no effect. The fix routes both the
    click handler and the keybinding through
    ``ControlPanel._set_rating_step``.

    Alt+M arrives in two event shapes: Linux/Windows report ``key: "m"`` with
    altKey, but macOS Option *composes* a character (Option+M → ``key: "µ"``),
    so matching must fall back to the physical key code (``KeyM``).
    Regression for the shortcut being dead on Macs because the manager
    matched only ``e.key.name``.
    """
    from waldo_commander.services.control_lease import (
        ControlMode,
        control_mode,
        set_control_mode,
    )
    from waldo_commander.services.keybindings import keybindings_manager
    from waldo_commander.state import ui_state

    await user.open("/")
    await wait_for_app_ready()

    cp = ui_state.control_panel
    refs = cp._rating_widgets["jog_speed"]
    rating = refs["rating"]
    chip = refs["label"]
    tooltip = refs["tooltip"]

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
        assert chip.text == "50%"
        assert "50%" in tooltip.text

        # `]` action — should advance by one step.
        inc_binding.action()
        assert waldoctl.commander.settings.jog.speed == 60, (
            "jog speed should advance to 60"
        )
        assert rating.value == 6, "rating widget should reflect new step"
        assert app.storage.general["jog_speed"] == 60, "storage should persist"
        assert chip.text == "60%", f"chip should read 60%, got {chip.text!r}"
        assert "60%" in tooltip.text, (
            f"tooltip should reflect 60%, got {tooltip.text!r}"
        )

        # `[` action — should retreat by one step.
        dec_binding.action()
        assert waldoctl.commander.settings.jog.speed == 50
        assert rating.value == 5
        assert app.storage.general["jog_speed"] == 50
        assert chip.text == "50%"
        assert "50%" in tooltip.text

        # Lower-bound clamp: pressing `[` repeatedly must not go below
        # rating step 1 (= 10%).
        for _ in range(20):
            dec_binding.action()
        assert waldoctl.commander.settings.jog.speed == 10
        assert rating.value == 1

        # Upper-bound clamp: pressing `]` repeatedly must not exceed
        # rating step 10 (= 100%).
        for _ in range(20):
            inc_binding.action()
        assert waldoctl.commander.settings.jog.speed == 100
        assert rating.value == 10

        # The popover is Quasar's; picking the seventh dot in it is the
        # rating's own change event.
        user.find(marker="rating-jog-speed").trigger("update:modelValue", 7)
        assert waldoctl.commander.settings.jog.speed == 70
        assert app.storage.general["jog_speed"] == 70
        assert chip.text == "70%"
        assert "70%" in tooltip.text
    finally:
        cp.adjust_rating("jog_speed", 50 - waldoctl.commander.settings.jog.speed)

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
async def test_wasd_jog_keys_drive_the_pad_arrows(user: User) -> None:
    """Each jog key presses one arrow of the cartesian pad: D the right
    arrow, W the up arrow. With the default assignment those are X- and Y-,
    and the Invert X/Y Jog settings flip key and arrow together.
    (The arrow-button flip itself is covered in test_control_panel_jogging.)
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
    # step can be refused — and the homed standby pose itself is a wrist
    # singularity; start each direction check from the jog-safe pose.
    panel = ui_state.control_panel
    assert panel is not None
    await teleport_to_jog_pose(panel.client)

    def key_event(name: str, *, keydown: bool) -> KeyEventArguments:
        return KeyEventArguments(
            sender=ng_client.layout,
            client=ng_client,
            action=KeyboardAction(keydown=keydown, keyup=not keydown, repeat=False),
            key=KeyboardKey(name=name, code=f"Key{name.upper()}", location=0),
            modifiers=KeyboardModifiers(alt=False, ctrl=False, meta=False, shift=False),
        )

    async def tap_key(name: str, axis_attr: str, expected: float) -> None:
        """Tap a jog key (keydown+keyup = click step) and wait for the axis to
        land ``expected`` mm away."""

        def axis_value() -> float:
            return float(getattr(waldoctl.commander.status.pose, axis_attr))

        # The baseline must come from a settled pose: on slow runners the
        # status view can still be converging from the previous motion when
        # the action state already reads IDLE.
        initial = await settled(axis_value)
        with ng_client:
            # No await between the events, so the hold timer can never fire:
            # this is deterministically a click (single step).
            keybindings_manager.handle_key(key_event(name, keydown=True))
            keybindings_manager.handle_key(key_event(name, keydown=False))
        await wait_for_motion_start()
        # Completion cannot be gated on action state or pose stability alone:
        # IDLE flickers between the 5mm move_l's creep phase and its main
        # ramp, and the creep's sub-tolerance ticks read as "stable". Both
        # the landed displacement and IDLE together mark the end.
        await wait_moved(
            axis_value,
            initial,
            lambda d: abs(d - expected) <= 0.1,
            what=f"'{name}' moving {axis_attr.upper()} {expected:+.1f}mm",
            timeout_s=30.0,
        )

    waldoctl.commander.settings.jog.joint_step_deg = 5.0

    user.find(marker="tab-settings").click()
    invert_x = next(iter(user.find(marker="switch-invert-x").elements))
    invert_y = next(iter(user.find(marker="switch-invert-y").elements))
    # Inversion hydrates from app.storage.general, and a prior test's
    # debounced storage flush can race its teardown — a leaked True would
    # make the baseline tap command X- and fail with reversed motion.
    # Force a known baseline through the same funnel the switches use.
    invert_x.set_value(False)
    invert_y.set_value(False)
    await asyncio.sleep(0)

    def arrow_label(slot_id: str) -> str:
        return panel._cart_slot_meta[slot_id]["label"].text

    def assert_keys_match_arrows() -> None:
        for key, slot_id in (("d", "lr_pos"), ("w", "ud1_up")):
            assert (
                keybindings_manager._bindings[key].description
                == f"Jog {arrow_label(slot_id)}"
            ), f"'{key}' must drive the {slot_id} arrow"

    assert arrow_label("lr_pos") == "X-" and arrow_label("ud1_up") == "Y-"
    assert_keys_match_arrows()
    try:
        await tap_key("d", "x", -5.0)
        await tap_key("w", "y", -5.0)

        invert_x.set_value(True)
        invert_y.set_value(True)
        await asyncio.sleep(0)
        assert arrow_label("lr_pos") == "X+" and arrow_label("ud1_up") == "Y+"
        assert_keys_match_arrows()
        await tap_key("d", "x", 5.0)
        await tap_key("w", "y", 5.0)
    finally:
        invert_x.set_value(False)
        invert_y.set_value(False)
        await asyncio.sleep(0)
