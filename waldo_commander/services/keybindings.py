"""Global keybindings manager for Waldo Commander.

Provides centralized keyboard shortcut handling with:
- Automatic disabling when editor/input is focused
- Click vs hold behavior for jog keys (matching button behavior)
- Dynamic tooltip suffix generation
- Keybinding registry for help menu display
- Default keybinding registration (setup_keybindings)
"""

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Any

from nicegui import ui


from waldo_commander.constants import CLICK_HOLD_THRESHOLD_S
from waldo_commander.services.programs import active_dry_run, is_any_program_running
from waldo_commander.state import ui_state

logger = logging.getLogger(__name__)


@dataclass
class Keybinding:
    """Definition of a keyboard shortcut."""

    key: str
    display: str
    description: str
    action: Callable
    category: str
    requires_shift: bool = False
    requires_ctrl: bool = False
    requires_alt: bool = False
    holdable: bool = False  # If True, supports click vs hold behavior
    on_release: Callable | None = None  # Called on keyup for holdable keys
    enabled_check: Callable[[], bool] | None = None
    _accepts_press_kwargs: bool = field(default=False, repr=False)


class KeybindingsManager:
    """Manages global keyboard shortcuts."""

    def __init__(self) -> None:
        self._bindings: dict[str, Keybinding] = {}
        self._enabled: bool = True
        self._editor_focused: bool = False

        # Hold state tracking for holdable keys
        self._hold_start_times: dict[str, float] = {}
        self._hold_timers: dict[str, ui.timer] = {}
        self._holding_active: set[str] = set()
        self._keys_down: set[str] = set()

    def register(self, binding: Keybinding) -> None:
        """Register a keybinding."""
        # Introspect action signature once to cache whether it accepts is_press/is_click
        try:
            params = inspect.signature(binding.action).parameters
            binding._accepts_press_kwargs = "is_press" in params or "is_click" in params
        except (ValueError, TypeError):
            binding._accepts_press_kwargs = False
        key_id = self._make_key_id(
            binding.key,
            binding.requires_shift,
            binding.requires_ctrl,
            binding.requires_alt,
        )
        self._bindings[key_id] = binding
        logger.debug("Registered keybinding: %s -> %s", key_id, binding.description)

    def unregister(
        self, key: str, shift: bool = False, ctrl: bool = False, alt: bool = False
    ) -> None:
        """Unregister a keybinding."""
        key_id = self._make_key_id(key, shift, ctrl, alt)
        self._bindings.pop(key_id, None)

    def _make_key_id(self, key: str, shift: bool, ctrl: bool, alt: bool) -> str:
        """Create unique identifier for key combination."""
        parts = []
        if ctrl:
            parts.append("Ctrl")
        if alt:
            parts.append("Alt")
        if shift:
            parts.append("Shift")
        parts.append(key.lower())
        return "+".join(parts)

    def _normalize_key(self, key: str) -> str:
        """Normalize key name for consistent matching."""
        key = key.lower()
        # Space is reported as " " in some cases.
        if key == " ":
            return " "
        return key

    def handle_key(self, e: Any) -> None:
        """Handle keyboard event from ui.keyboard."""
        if not self._enabled:
            return

        if self._editor_focused:
            return

        key = self._normalize_key(e.key.name)
        if e.modifiers.alt:
            # macOS Option composes characters (Option+M -> "µ"), so the key
            # *name* never matches an Alt+letter binding there. The physical
            # key *code* ("KeyM", "Digit3") is OS/layout-stable — use it.
            code = e.key.code or ""
            if code.startswith("Key"):
                key = code[len("Key") :].lower()
            elif code.startswith("Digit"):
                key = code[len("Digit") :]
        is_keydown = e.action.keydown
        is_keyup = e.action.keyup

        key_id = self._make_key_id(
            key,
            e.modifiers.shift,
            e.modifiers.ctrl,
            e.modifiers.alt,
        )

        binding = self._bindings.get(key_id)
        if not binding:
            return

        # Check dynamic enable condition
        if binding.enabled_check and not binding.enabled_check():
            return

        if binding.holdable:
            self._handle_holdable_key(key_id, binding, is_keydown, is_keyup)
        elif is_keydown:
            # Prevent repeat triggers for held keys
            if key_id in self._keys_down:
                return
            self._keys_down.add(key_id)
            self._execute_action(binding.action)
        elif is_keyup:
            self._keys_down.discard(key_id)

    def _handle_holdable_key(
        self, key_id: str, binding: Keybinding, is_keydown: bool, is_keyup: bool
    ) -> None:
        """Handle click vs hold behavior for holdable keys."""
        if is_keydown:
            if key_id in self._keys_down:
                return
            self._keys_down.add(key_id)

            old_timer = self._hold_timers.pop(key_id, None)
            if old_timer:
                old_timer.active = False

            self._hold_start_times[key_id] = time.time()

            def start_hold():
                self._holding_active.add(key_id)
                self._hold_timers.pop(key_id, None)
                # Hold threshold elapsed: start continuous jog.
                self._execute_action(
                    binding.action,
                    is_press=True,
                    accepts_kwargs=binding._accepts_press_kwargs,
                )

            try:
                with ui.context.client:
                    self._hold_timers[key_id] = ui.timer(
                        CLICK_HOLD_THRESHOLD_S, start_hold, once=True
                    )
            except Exception:
                # No client context: fall back to immediate execution.
                self._execute_action(
                    binding.action,
                    is_press=True,
                    accepts_kwargs=binding._accepts_press_kwargs,
                )

        elif is_keyup:
            self._keys_down.discard(key_id)

            timer = self._hold_timers.pop(key_id, None)
            was_holding = key_id in self._holding_active
            self._holding_active.discard(key_id)
            self._hold_start_times.pop(key_id, None)

            if timer and timer.active:
                timer.active = False
                # Released before threshold: a click, so do a single step.
                self._execute_action(
                    binding.action,
                    is_press=False,
                    is_click=True,
                    accepts_kwargs=binding._accepts_press_kwargs,
                )
            elif was_holding and binding.on_release:
                # Released after a hold: run the release action.
                self._execute_action(binding.on_release)

    def _execute_action(
        self,
        action: Callable,
        is_press: bool = True,
        is_click: bool = False,
        accepts_kwargs: bool = False,
    ) -> None:
        """Execute a keybinding action, handling async if needed."""
        try:
            if accepts_kwargs:
                result = action(is_press=is_press, is_click=is_click)
            else:
                result = action()

            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as ex:
            logger.error("Keybinding action failed: %s", ex)

    def set_editor_focused(self, focused: bool) -> None:
        """Called from JS when editor/input focus changes."""
        self._editor_focused = focused

    def get_all_bindings(self) -> dict[str, list[Keybinding]]:
        """Get all bindings grouped by category for help menu."""
        categories: dict[str, list[Keybinding]] = {}
        for binding in self._bindings.values():
            if binding.category not in categories:
                categories[binding.category] = []
            categories[binding.category].append(binding)
        return categories

    def get_tooltip_suffix(self, key: str, shift: bool = False) -> str:
        """Get tooltip suffix for a keybinding (e.g., ' (H)' for home)."""
        key_id = self._make_key_id(key, shift, False, False)
        binding = self._bindings.get(key_id)
        if binding:
            display = binding.display
            if shift:
                display = f"Shift+{display}"
            return f" ({display})"
        return ""

    def get_display_for_key(
        self, key: str, shift: bool = False, ctrl: bool = False, alt: bool = False
    ) -> str | None:
        """Get display string for a registered keybinding."""
        key_id = self._make_key_id(key, shift, ctrl, alt)
        binding = self._bindings.get(key_id)
        if binding:
            parts = []
            if ctrl:
                parts.append("Ctrl")
            if alt:
                parts.append("Alt")
            if shift:
                parts.append("Shift")
            parts.append(binding.display)
            return "+".join(parts)
        return None


# Singleton
keybindings_manager = KeybindingsManager()


# --------------- Default keybinding setup ---------------


def setup_keybindings(help_menu: Any) -> None:
    """Set up global keyboard handler, focus detection, register bindings,
    and trigger first-time tutorial check."""
    ui.keyboard(on_key=keybindings_manager.handle_key)

    def on_focus_change(focused: bool) -> None:
        keybindings_manager.set_editor_focused(focused)

    # Expose the callback to JS and initialize the focus detector.
    ui.run_javascript(
        """
        if (window.KeybindingsFocusDetector) {
            window.KeybindingsFocusDetector.init(function(focused) {
                // Send focus state to Python
                emitEvent('keybindings_focus_change', { focused: focused });
            });
        }
        // ui.keyboard ignores key events while a <button> has focus, so a
        // mouse click on any button would silence every global shortcut
        // until focus moves. Drop the lingering focus after mouse clicks;
        // keyboard (Tab) focus is unaffected.
        if (!window._wcButtonBlurInstalled) {
            window._wcButtonBlurInstalled = true;
            document.addEventListener('click', function(e) {
                const b = e.target.closest && e.target.closest('button');
                if (b && e.detail > 0) b.blur();
            });
        }
        """
    )

    ui.on(
        "keybindings_focus_change",
        lambda e: on_focus_change(e.args.get("focused", False)),
    )

    _register_default_keybindings()

    ui_client = ui.context.client

    async def check_first_visit():
        with ui_client:
            help_menu.check_first_visit()

    asyncio.create_task(check_first_visit())


def _register_default_keybindings() -> None:
    """Register all default keybindings."""
    # Local import: keybindings is in services/ and playback is in
    # components/, so a top-level import would invert the layered
    # dependency direction. Keep it lazy.
    from waldo_commander.components.playback import playback

    cp = ui_state.control_panel

    keybindings_manager.register(
        Keybinding(
            key="h",
            display="H",
            description="Home robot",
            action=lambda: asyncio.create_task(cp.send_home()),
            category="Robot Control",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key="Escape",
            display="Esc",
            description="Emergency Stop",
            action=lambda: asyncio.create_task(cp.on_estop_click()),
            category="Robot Control",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key="m",
            display="Alt+M",
            requires_alt=True,
            description="Cycle AI control mode",
            action=lambda: cp.cycle_mode(),
            category="Robot Control",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key=" ",
            display="Space",
            description="Play/Pause",
            action=lambda: asyncio.create_task(playback.toggle_play()),
            category="Playback",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key="n",
            display="N",
            description="Step forward",
            action=lambda: playback.step_forward(),
            category="Playback",
            enabled_check=lambda: is_any_program_running()
            or ((dr := active_dry_run()) is not None and dr.total_steps > 0),
        )
    )

    # Holdable jog keys: click = single step, hold = continuous jog.
    _register_cartesian_jog_keybindings(cp)

    # Speed Control — delegated to control panel so the rating widget,
    # icon color, tooltip, and persisted storage stay in sync.
    keybindings_manager.register(
        Keybinding(
            key="]",
            display="]",
            description="Increase jog speed",
            action=lambda: cp.adjust_rating("jog_speed", 10),
            category="Speed Control",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key="[",
            display="[",
            description="Decrease jog speed",
            action=lambda: cp.adjust_rating("jog_speed", -10),
            category="Speed Control",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key="t",
            display="T",
            description="Add target at current position",
            action=lambda: ui_state.urdf_scene._show_unified_target_editor(
                use_click_position=False
            )
            if ui_state.urdf_scene
            else None,
            category="Recording",
        )
    )


# Map keys to axes: W/S = Y, A/D = X, Q/E = Z
_JOG_KEY_MAP = {
    "w": "Y+",
    "s": "Y-",
    "a": "X-",
    "d": "X+",
    "q": "Z-",
    "e": "Z+",
}


def _register_cartesian_jog_keybindings(cp: Any) -> None:
    """Register WASD + Q/E keybindings for cartesian jogging."""
    for key, axis in _JOG_KEY_MAP.items():
        action, release = _make_jog_callbacks(cp, axis)
        keybindings_manager.register(
            Keybinding(
                key=key,
                display=key.upper(),
                description=f"Jog {axis}",
                action=action,
                on_release=release,
                category="Cartesian Jog",
                holdable=True,
            )
        )
    refresh_jog_key_descriptions(cp)


def refresh_jog_key_descriptions(cp: Any) -> None:
    """Sync the help-menu descriptions of the jog keys with the control
    panel's X/Y inversion, so help never advertises the wrong direction."""
    for key, axis in _JOG_KEY_MAP.items():
        binding = keybindings_manager._bindings.get(key)
        if binding is not None and binding.category == "Cartesian Jog":
            binding.description = f"Jog {cp.apply_jog_inversion(axis)}"


def _make_jog_callbacks(cp: Any, base_axis: str) -> tuple[Callable, Callable]:
    """Create press/release callbacks that apply the control panel's X/Y
    inversion at press time, matching the arrow buttons. The resolved axis
    is captured on press so a mid-hold settings change still releases the
    axis that is actually streaming."""
    resolved = base_axis

    def action(is_press: bool = True, is_click: bool = False) -> None:
        nonlocal resolved
        resolved = cp.apply_jog_inversion(base_axis)
        _handle_jog_key(cp, resolved, is_press, is_click)

    def release() -> None:
        asyncio.create_task(cp.set_axis_pressed(resolved, False))

    return action, release


def _handle_jog_key(
    cp: Any, axis: str, is_press: bool = True, is_click: bool = False
) -> None:
    """Handle jog key press/click for cartesian movement."""
    if is_click:
        # Click: press and auto-release in one task. A sleep between them
        # would race the click-vs-hold threshold under event-loop load,
        # turning the tap into a zero-tick hold that never moves.
        async def click():
            await cp.set_axis_pressed(axis, True)
            await cp.set_axis_pressed(axis, False)

        asyncio.create_task(click())
    elif is_press:
        # Hold: start continuous jog (released via on_release).
        asyncio.create_task(cp.set_axis_pressed(axis, True))
