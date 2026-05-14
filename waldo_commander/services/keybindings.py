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
from waldo_commander.state import simulation_state, ui_state

logger = logging.getLogger(__name__)


@dataclass
class Keybinding:
    """Definition of a keyboard shortcut."""

    key: str  # Key identifier (e.g., "h", " ", "Escape")
    display: str  # Display string for UI (e.g., "H", "Space", "Esc")
    description: str  # Human-readable description
    action: Callable  # Function to call when triggered
    category: str  # Category for help menu grouping
    requires_shift: bool = False
    requires_ctrl: bool = False
    requires_alt: bool = False
    holdable: bool = False  # If True, supports click vs hold behavior
    on_release: Callable | None = None  # Called on keyup for holdable keys
    enabled_check: Callable[[], bool] | None = None  # Dynamic enable check
    _accepts_press_kwargs: bool = field(
        default=False, repr=False
    )  # Set at registration


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
        self._keys_down: set[str] = set()  # Track currently pressed keys

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
        # NiceGUI keyboard events use specific key names
        key = key.lower()
        # Space is reported as " " in some cases
        if key == " ":
            return " "
        return key

    def handle_key(self, e: Any) -> None:
        """Handle keyboard event from ui.keyboard."""
        if not self._enabled:
            return

        # Check if editor/input is focused
        if self._editor_focused:
            return

        key = self._normalize_key(e.key.name)
        is_keydown = e.action.keydown
        is_keyup = e.action.keyup

        # Build key ID with modifiers
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
            # Ignore repeat keydown events
            if key_id in self._keys_down:
                return
            self._keys_down.add(key_id)

            # Cancel any existing timer
            old_timer = self._hold_timers.pop(key_id, None)
            if old_timer:
                old_timer.active = False

            # Record start time
            self._hold_start_times[key_id] = time.time()

            # Start timer for hold detection
            def start_hold():
                self._holding_active.add(key_id)
                self._hold_timers.pop(key_id, None)
                # Execute action for continuous jog start
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
                # If no client context, fall back to simple execution
                self._execute_action(
                    binding.action,
                    is_press=True,
                    accepts_kwargs=binding._accepts_press_kwargs,
                )

        elif is_keyup:
            self._keys_down.discard(key_id)

            # Cancel timer if still running
            timer = self._hold_timers.pop(key_id, None)
            was_holding = key_id in self._holding_active
            self._holding_active.discard(key_id)
            self._hold_start_times.pop(key_id, None)

            if timer and timer.active:
                timer.active = False
                # Was a click (quick press) - execute single step action
                self._execute_action(
                    binding.action,
                    is_press=False,
                    is_click=True,
                    accepts_kwargs=binding._accepts_press_kwargs,
                )
            elif was_holding and binding.on_release:
                # Was a hold - execute release action
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
    # Add global keyboard handler
    ui.keyboard(on_key=keybindings_manager.handle_key)

    # Set up JavaScript callback for focus detection
    def on_focus_change(focused: bool) -> None:
        keybindings_manager.set_editor_focused(focused)

    # Expose the callback to JavaScript and initialize the focus detector
    ui.run_javascript(
        """
        if (window.KeybindingsFocusDetector) {
            window.KeybindingsFocusDetector.init(function(focused) {
                // Send focus state to Python
                emitEvent('keybindings_focus_change', { focused: focused });
            });
        }
        """
    )

    # Listen for focus change events from JavaScript
    ui.on(
        "keybindings_focus_change",
        lambda e: on_focus_change(e.args.get("focused", False)),
    )

    # Register all keybindings
    _register_default_keybindings()

    # Set up first-time tutorial dialog
    ui_client = ui.context.client

    async def check_first_visit():
        with ui_client:
            help_menu.check_first_visit()

    asyncio.create_task(check_first_visit())


def _register_default_keybindings() -> None:
    """Register all default keybindings."""
    cp = ui_state.control_panel
    ep = ui_state.editor_panel

    # Robot Control
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

    # Playback Controls
    keybindings_manager.register(
        Keybinding(
            key=" ",
            display="Space",
            description="Play/Pause",
            action=lambda: asyncio.create_task(ep.playback.toggle_play()),
            category="Playback",
        )
    )

    keybindings_manager.register(
        Keybinding(
            key="s",
            display="S",
            description="Step forward",
            action=lambda: ep.playback.step_forward(),
            category="Playback",
            enabled_check=lambda: simulation_state.script_running
            or simulation_state.total_steps > 0,
        )
    )

    # Cartesian Jog - WASD + Q/E
    # These are holdable: click = single step, hold = continuous jog
    _register_cartesian_jog_keybindings(cp, ep)

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

    # Target insertion
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


def _register_cartesian_jog_keybindings(cp: Any, ep: Any) -> None:
    """Register WASD + Q/E keybindings for cartesian jogging."""
    # Map keys to axes: W/S = Y, A/D = X, Q/E = Z
    jog_key_map = {
        "w": "Y+",
        "s": "Y-",
        "a": "X-",
        "d": "X+",
        "q": "Z-",
        "e": "Z+",
    }

    for key, axis in jog_key_map.items():
        # S key is context-aware: jog when not running, step when running
        enabled_check = (
            (lambda: not simulation_state.script_running) if key == "s" else None
        )

        keybindings_manager.register(
            Keybinding(
                key=key,
                display=key.upper(),
                description=f"Jog {axis}",
                action=_make_jog_action(cp, axis),
                on_release=_make_jog_release(cp, axis),
                category="Cartesian Jog",
                holdable=True,
                enabled_check=enabled_check,
            )
        )


def _make_jog_action(cp: Any, axis: str) -> Callable:
    """Create a jog action callback for the given axis."""

    def action(is_press: bool = True, is_click: bool = False) -> None:
        _handle_jog_key(cp, axis, is_press, is_click)

    return action


def _make_jog_release(cp: Any, axis: str) -> Callable:
    """Create a jog release callback for the given axis."""

    def release() -> None:
        asyncio.create_task(cp.set_axis_pressed(axis, False))

    return release


def _handle_jog_key(
    cp: Any, axis: str, is_press: bool = True, is_click: bool = False
) -> None:
    """Handle jog key press/click for cartesian movement."""
    if is_click:
        # Single step movement
        asyncio.create_task(cp.set_axis_pressed(axis, True))

        # Small delay then release
        async def release():
            await asyncio.sleep(0.05)
            await cp.set_axis_pressed(axis, False)

        asyncio.create_task(release())
    elif is_press:
        # Start continuous jog
        asyncio.create_task(cp.set_axis_pressed(axis, True))
