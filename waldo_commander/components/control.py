"""Bottom-left control panel component for jog speed, step size, and robot control buttons."""

import asyncio
import dataclasses
import logging
import time
import re
import math
from functools import partial
from typing import Any, Callable
import importlib.resources as pkg_resources

import numpy as np

from nicegui import ui, app, Client
from waldoctl import ElectricGripperTool, GripperTool, RobotClient, ToggleMode, ToolSpec
from waldoctl.types import Axis

from waldo_commander.constants import config, DEFAULT_CAMERA, CLICK_HOLD_THRESHOLD_S
from waldo_commander.state import (
    robot_state,
    ui_state,
    global_phase_timer,
)
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.components.settings import SettingsContent

logger = logging.getLogger(__name__)

# Module-level constants (avoid recreation every frame)
_AXIS_ORDER = (
    "X+",
    "X-",
    "Y+",
    "Y-",
    "Z+",
    "Z-",
    "RX+",
    "RX-",
    "RY+",
    "RY-",
    "RZ+",
    "RZ-",
)
_AXIS_MAP = {"X": 0, "Y": 1, "Z": 2, "RX": 3, "RY": 4, "RZ": 5}
_DEFAULT_CART_EN = np.ones(12, dtype=np.int32)
_DEFAULT_CART_EN.flags.writeable = False

# SVG icon transform lookup: (vb_width, vb_height) -> default transform
_ICON_TRANSFORMS: dict[tuple[int, int], str] = {
    (32, 32): "translate(-2,-2) scale(0.85)",
    (24, 24): "translate(-5,-5) scale(1.4)",
}
# Per-slot transform overrides (takes precedence over dimension-based lookup)
_SLOT_TRANSFORM_OVERRIDES: dict[str, str] = {
    "lr_neg": "translate(-2,-5) scale(1.4)",
}
# Slots that use overflow:visible style on the outer SVG
# Regex patterns compiled once
_RE_VIEWBOX = re.compile(r'viewBox="\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*"')
_RE_SVG_INNER = re.compile(r"<svg[^>]*>([\s\S]*?)</svg>")
_RE_TEXT_LABEL = re.compile(r"(<text\b[^>]*>)(.*?)(</text>)", re.DOTALL)
_RE_WHITESPACE = re.compile(r"\s+")


@dataclasses.dataclass
class _CadenceTracker:
    """Tracks timer cadence and warns on drift."""

    last_ts: float = 0.0
    accum: float = 0.0
    count: int = 0

    def tick(
        self, now: float, target_dt: float, window: int, tolerance: float, label: str
    ) -> None:
        if self.last_ts > 0.0:
            dt = now - self.last_ts
            self.accum += dt
            self.count += 1
            if self.count >= window:
                avg = self.accum / self.count
                if abs(avg - target_dt) > tolerance:
                    logger.warning(
                        "[CADENCE] %s avg dt=%.4f s (target=%.4f s, tol=%.4f s)",
                        label,
                        avg,
                        target_dt,
                        tolerance,
                    )
                self.accum = 0.0
                self.count = 0
        self.last_ts = now

    def reset(self) -> None:
        self.last_ts = self.accum = 0.0
        self.count = 0


class _EStopManager:
    """Manages E-STOP dialog state and physical/digital E-STOP transitions."""

    def __init__(self, client: "RobotClient", ui_client_fn: Callable[[], Any]) -> None:
        self._client = client
        self._ui_client_fn = ui_client_fn
        self._dialog: ui.dialog | None = None
        self._is_physical: bool = False
        self._last_io_state: int = 1
        self._digital_active: bool = False

    def show(self, is_physical: bool) -> None:
        """Show E-STOP dialog with Lottie animation."""
        ui_client = self._ui_client_fn()
        if not ui_client:
            return

        with ui_client:
            if is_physical and self._dialog and not self._is_physical:
                self._digital_active = True

            if self._dialog:
                self._dialog.close()
                self._dialog = None

            self._dialog = ui.dialog()
            self._is_physical = is_physical
            self._dialog.props("persistent")

            with (
                self._dialog,
                ui.card()
                .classes("overlay-card gap-4 items-center")
                .mark("estop-dialog"),
            ):
                ui.html(
                    """<lottie-player src="https://lottie.host/b9d2fa51-2204-454e-a882-7647c6712b03/d7w0e81TRh.json" autoplay loop />""",
                    sanitize=False,
                ).classes("w-96")

                if is_physical:
                    ui.label("Physical E-STOP Active").classes(
                        "text-xl font-bold text-negative text-center"
                    )
                    ui.label("The physical E-STOP button was pressed.").classes(
                        "text-center"
                    )
                    ui.label("To continue, unset the E-STOP button.").classes(
                        "text-center"
                    )
                else:
                    ui.label("Digital E-STOP Active").classes(
                        "text-xl font-bold text-warning text-center"
                    )
                    ui.label("Robot motion has been stopped.").classes("text-center")

                    async def resume():
                        try:
                            await self._client.resume()
                            self._digital_active = False
                            if self._dialog:
                                self._dialog.close()
                                self._dialog = None
                        except Exception as e:
                            logger.error("Resume after digital E-STOP failed: %s", e)

                    with ui.row().classes("gap-2 justify-center w-full mt-4"):
                        ui.button("Resume", on_click=resume).props(
                            "color=positive size=lg"
                        ).mark("btn-estop-resume")

            self._dialog.open()

    def close(self) -> None:
        """Close the E-STOP dialog if open."""
        ui_client = self._ui_client_fn()
        if not ui_client:
            return
        with ui_client:
            if self._dialog:
                self._dialog.close()
                self._dialog = None
                self._is_physical = False

    def check_state_change(self) -> None:
        """Monitor robot_state.io_estop and show/hide dialog on transitions."""
        current = robot_state.io_estop

        if self._last_io_state == 1 and current == 0:
            logger.warning("Physical E-STOP detected (io_estop 1->0)")
            self.show(is_physical=True)
        elif self._last_io_state == 0 and current == 1:
            logger.info("Physical E-STOP released (io_estop 0->1)")
            if self._dialog and self._is_physical:
                self.close()
                if self._digital_active:
                    self.show(is_physical=False)

        self._last_io_state = current


class _ToolQuickActions:
    """Tool action buttons (L/R) and adjust buttons with visual updates."""

    def __init__(
        self, client: "RobotClient", movement_allowed_fn: Callable[..., bool]
    ) -> None:
        self._client = client
        self._movement_allowed = movement_allowed_fn
        self._action_l_btn: ui.button | None = None
        self._action_r_btn: ui.button | None = None
        self._adjust_minus_btn: ui.button | None = None
        self._adjust_plus_btn: ui.button | None = None
        self._action_l_tooltip: ui.tooltip | None = None
        self._action_r_tooltip: ui.tooltip | None = None
        self._adjust_minus_tooltip: ui.tooltip | None = None
        self._adjust_plus_tooltip: ui.tooltip | None = None
        self._last_visual: tuple = ()

    def _get_active_tool(self) -> "ToolSpec | None":
        try:
            return self._client.tool
        except (RuntimeError, KeyError, NotImplementedError):
            return None

    def build(self) -> None:
        """Build the tool quick-action box (L/R action + adjust)."""
        with (
            ui.column()
            .classes("rounded-lg shadow-sm p-2 gap-1")
            .style("border: 1px solid rgba(255,255,255,0.1);")
            .bind_visibility_from(
                robot_state, "tool_key", backward=lambda k: k != "NONE"
            )
            .mark("tool-quick-actions")
        ):
            ui.label().bind_text_from(robot_state, "tool_key").classes(
                "text-xs text-center w-full opacity-60"
            )

            with ui.row().classes("items-center gap-2 justify-center"):
                self._action_l_btn = (
                    ui.button(icon="close_fullscreen", on_click=self._on_action_l)
                    .props("round dense unelevated size=md color=grey-7")
                    .mark("btn-tool-action-l")
                )
                self._action_r_btn = (
                    ui.button(
                        icon="build",
                        on_click=lambda: _safe_task(self._on_action_r()),
                    )
                    .props("round dense unelevated size=md color=grey-7")
                    .classes("cp-disabled-strong")
                    .mark("btn-tool-action-r")
                )
                with ui.button_group().props("rounded unelevated dense"):
                    self._adjust_minus_btn = (
                        ui.button(
                            icon="remove",
                            on_click=lambda: _safe_task(self._on_adjust(-1)),
                        )
                        .props("round dense unelevated size=md color=grey-7")
                        .mark("btn-tool-adjust-minus")
                    )
                    self._adjust_plus_btn = (
                        ui.button(
                            icon="add", on_click=lambda: _safe_task(self._on_adjust(1))
                        )
                        .props("round dense unelevated size=md color=grey-7")
                        .mark("btn-tool-adjust-plus")
                    )

    def update_visual(self) -> None:
        """Update action button icons and colors from current tool state."""
        if self._action_l_btn is None:
            return
        tool = self._get_active_tool()
        if tool is None:
            return

        visual_key = (
            robot_state.tool_key,
            robot_state.tool_position,
            robot_state.tool_engaged,
            ui_state.gripper_current,
        )
        if visual_key == self._last_visual:
            return
        self._last_visual = visual_key

        # Left action button
        if tool.action_l_icons:
            if isinstance(tool, GripperTool):
                is_open = tool.is_open(robot_state.tool_position)
                off_icon, on_icon = tool.action_l_icons
                off_label, on_label = tool.action_l_labels or ("Close", "Open")
                icon = off_icon if is_open else on_icon
                color = "light-blue-7" if is_open else "teal-7"
                tooltip_text = off_label if is_open else on_label
            else:
                off_icon, on_icon = tool.action_l_icons
                off_label, on_label = tool.action_l_labels or ("Off", "On")
                engaged = robot_state.tool_engaged
                if tool.action_l_mode == ToggleMode.TRIGGER:
                    icon = off_icon
                    color = "cyan-8"
                    tooltip_text = off_label
                else:
                    icon = off_icon if engaged else on_icon
                    color = "teal-7" if engaged else "light-blue-7"
                    tooltip_text = off_label if engaged else on_label

            self._action_l_btn._props["icon"] = icon
            self._action_l_btn.props(f"color={color}")
            if self._action_l_tooltip is None:
                with self._action_l_btn:
                    self._action_l_tooltip = ui.tooltip(tooltip_text)
            else:
                self._action_l_tooltip.text = tooltip_text
            self._action_l_btn.update()

        # Right action button
        if self._action_r_btn is not None:
            if tool.action_r_labels is None:
                self._action_r_btn.classes(add="cp-disabled-strong")
            else:
                self._action_r_btn.classes(remove="cp-disabled-strong")
                off_icon_r, on_icon_r = tool.action_r_icons or ("build", "build")
                off_label_r, on_label_r = tool.action_r_labels
                if tool.action_r_mode == ToggleMode.TRIGGER:
                    self._action_r_btn._props["icon"] = off_icon_r
                    self._action_r_btn.props("color=cyan-8")
                    r_tooltip = off_label_r
                else:
                    engaged_r = robot_state.tool_engaged
                    self._action_r_btn._props["icon"] = (
                        off_icon_r if engaged_r else on_icon_r
                    )
                    self._action_r_btn.props(
                        f"color={'teal-7' if engaged_r else 'light-blue-7'}"
                    )
                    r_tooltip = off_label_r if engaged_r else on_label_r
                if self._action_r_tooltip is None:
                    with self._action_r_btn:
                        self._action_r_tooltip = ui.tooltip(r_tooltip)
                else:
                    self._action_r_tooltip.text = r_tooltip
            self._action_r_btn.update()

        has_adjust = tool.adjust_step is not None
        if self._adjust_minus_btn is not None:
            assert self._adjust_plus_btn is not None
            if not has_adjust:
                self._adjust_minus_btn.classes(add="cp-disabled-strong")
                self._adjust_plus_btn.classes(add="cp-disabled-strong")
            else:
                cur = ui_state.gripper_current
                step = tool.adjust_step
                # Disable at limits
                if isinstance(tool, ElectricGripperTool):
                    lo, hi = tool.current_range
                    at_lo = cur <= lo
                    at_hi = cur >= hi
                else:
                    at_lo = at_hi = False
                if at_lo:
                    self._adjust_minus_btn.classes(add="cp-disabled-strong")
                else:
                    self._adjust_minus_btn.classes(remove="cp-disabled-strong")
                if at_hi:
                    self._adjust_plus_btn.classes(add="cp-disabled-strong")
                else:
                    self._adjust_plus_btn.classes(remove="cp-disabled-strong")
                # Tooltips
                dec_label, inc_label = tool.adjust_labels or ("Decrease", "Increase")
                if self._adjust_minus_tooltip is None:
                    with self._adjust_minus_btn:
                        self._adjust_minus_tooltip = ui.tooltip("")
                    with self._adjust_plus_btn:
                        self._adjust_plus_tooltip = ui.tooltip("")
                assert self._adjust_minus_tooltip is not None
                assert self._adjust_plus_tooltip is not None
                self._adjust_minus_tooltip.text = (
                    f"{dec_label}: {cur} mA (\u2212{step})"
                )
                self._adjust_plus_tooltip.text = f"{inc_label}: {cur} mA (+{step})"

    async def _on_action_l(self) -> None:
        if not self._movement_allowed():
            return
        tool = self._get_active_tool()
        if tool is None or tool.action_l_labels is None:
            return
        try:
            if isinstance(tool, GripperTool):
                spd_kwargs: dict = {}
                if isinstance(tool, ElectricGripperTool):
                    spd_kwargs["speed"] = ui_state.jog_speed / 100.0
                    spd_kwargs["current"] = ui_state.gripper_current
                if tool.is_open(robot_state.tool_position):
                    target = 1.0  # close
                else:
                    target = 0.0  # open
                if ui_state.gripper_page is not None:
                    ui_state.gripper_page.set_target_position(target)
                else:
                    ui_state.tool_target_position = target
                await tool.set_position(target, **spd_kwargs)
                motion_recorder.record_action("gripper", position=target, **spd_kwargs)
            else:
                await tool.action_l(not robot_state.tool_engaged)
        except Exception as e:
            logger.error("Tool action_l failed: %s", e)
            ui.notify(f"Action failed: {e}", color="negative")

    async def _on_action_r(self) -> None:
        if not self._movement_allowed():
            return
        tool = self._get_active_tool()
        if tool is None or tool.action_r_labels is None:
            return
        try:
            await tool.action_r(not robot_state.tool_engaged)
        except Exception as e:
            logger.error("Tool action_r failed: %s", e)
            ui.notify(f"Action failed: {e}", color="negative")

    async def _on_adjust(self, direction: int) -> None:
        if not self._movement_allowed():
            return
        tool = self._get_active_tool()
        if tool is None or tool.adjust_step is None:
            return
        if not isinstance(tool, ElectricGripperTool):
            return
        step = tool.adjust_step * direction
        lo, hi = tool.current_range
        new_cur = max(lo, min(hi, ui_state.gripper_current + step))
        if ui_state.gripper_page is not None:
            ui_state.gripper_page.set_target_current(new_cur)
        else:
            ui_state.gripper_current = new_cur
        try:
            pos = ui_state.tool_target_position
            await tool.set_position(pos, current=new_cur)
            motion_recorder.record_action("gripper", position=pos, current=new_cur)
        except Exception as e:
            logger.error("Adjust failed: %s", e)
            ui.notify(f"Adjust failed: {e}", color="negative")


class _ClickHoldHandler:
    """Generic click-vs-hold detection for jog buttons.

    Manages hold timers and tracks which keys are actively being held.
    Domain-specific behavior is injected via callbacks to on_change().
    """

    def __init__(self, threshold_s: float, ui_client_fn: Callable[[], Any]) -> None:
        self._threshold_s = threshold_s
        self._ui_client_fn = ui_client_fn
        self._hold_timers: dict[Any, ui.timer] = {}
        self._holding_active: set[Any] = set()

    def is_holding(self, key: Any) -> bool:
        return key in self._holding_active

    @property
    def any_active(self) -> bool:
        return bool(self._holding_active)

    def cancel_key(self, key: Any) -> None:
        """Cancel any pending timer and clear hold state for a key."""
        tm = self._hold_timers.pop(key, None)
        if tm:
            tm.active = False
        self._holding_active.discard(key)

    async def on_change(
        self,
        key: Any,
        is_pressed: bool,
        *,
        on_click: Callable[[], Any],
        on_hold_start: Callable[[], None],
        on_release: Callable[[bool], None],
    ) -> None:
        """Handle press/release for a key.

        Args:
            key: Unique identifier for the button/axis
            is_pressed: True on press, False on release
            on_click: Called (awaited if coroutine) when a quick click is detected
            on_hold_start: Called when hold threshold is reached (start streaming)
            on_release: Called on release with was_holding=True/False for cleanup
        """
        if is_pressed:
            # Cancel any existing timer for this key
            tm_prev = self._hold_timers.pop(key, None)
            if tm_prev:
                tm_prev.active = False

            def _start_hold():
                self._holding_active.add(key)
                on_hold_start()
                tm = self._hold_timers.pop(key, None)
                if tm:
                    tm.active = False

            ui_client = self._ui_client_fn()
            if ui_client:
                with ui_client:
                    self._hold_timers[key] = ui.timer(
                        self._threshold_s, _start_hold, once=True
                    )
            return

        # Release path
        tm = self._hold_timers.pop(key, None)
        was_holding = key in self._holding_active

        if tm and tm.active:
            # Timer still running → this was a quick click
            tm.active = False
            result = on_click()
            if asyncio.iscoroutine(result):
                await result
            self._holding_active.discard(key)
            on_release(False)
            return

        if was_holding:
            self._holding_active.discard(key)
            on_release(True)

    def cleanup(self) -> None:
        for tm in self._hold_timers.values():
            tm.cancel()
        self._hold_timers.clear()
        self._holding_active.clear()


def _safe_task(coro: Any) -> asyncio.Task:
    """Create an asyncio task that logs exceptions instead of silently swallowing them."""
    task = asyncio.create_task(coro)
    task.add_done_callback(
        lambda t: logger.error("Unhandled error in task", exc_info=t.exception())
        if not t.cancelled() and t.exception()
        else None
    )
    return task


def _norm_speed() -> float:
    """Normalize jog_speed (0-100 slider) to 0.01..1.0 range."""
    return max(0.01, min(1.0, ui_state.jog_speed / 100.0))


def _norm_accel() -> float:
    """Normalize jog_accel (0-100 slider) to 0.0..1.0 range."""
    return ui_state.jog_accel / 100.0


class ControlPanel:
    """Bottom-left control panel for jog settings and robot control."""

    def __init__(self, client: RobotClient) -> None:
        """Initialize control panel with jog state and required robot client."""
        self.client = client
        self._ui_client: Any = None  # NiceGUI client for background task UI ops

        # Jog UI references
        self._joint_left_btns: dict[int, ui.button] = {}
        self._joint_right_btns: dict[int, ui.button] = {}
        self._joint_limit_btns: dict[
            tuple[int, str], ui.button
        ] = {}  # (joint_idx, "min"/"max") -> button
        self._cart_axis_imgs: dict[str, ui.element] = {}

        # Jog state tracking
        self._n_joints = ui_state.active_robot.joints.count
        self._jog_pressed_pos: list[bool] = [False] * self._n_joints
        self._jog_pressed_neg: list[bool] = [False] * self._n_joints
        self._cart_pressed_axes: dict[str, bool] = {ax: False for ax in _AXIS_ORDER}

        # Cartesian button slots/elements and assignment (layout fixed; labels/colors/actions dynamic)
        self._cart_slot_elems: dict[str, ui.element] = {}
        self._cart_slot_meta: dict[str, dict] = {}
        # Assignment of axes to fixed slots: 'ud1' (first up/down column), 'lr' (left/right row), 'ud2' (second up/down column)
        self._cart_assignment: dict[str, str] = {"ud1": "Y", "lr": "X", "ud2": "Z"}
        self._axis_classes = {
            "x": "tcp-x",
            "y": "tcp-y",
            "z": "tcp-z",
            "rx": "tcp-rx",
            "ry": "tcp-ry",
            "rz": "tcp-rz",
        }

        # Click/hold handlers (initialized with ui_client in build())
        self.CLICK_HOLD_THRESHOLD_S: float = CLICK_HOLD_THRESHOLD_S
        self._joint_click_hold: _ClickHoldHandler | None = None
        self._cart_click_hold: _ClickHoldHandler | None = None

        # Settings content for cleanup
        self._settings_content: "SettingsContent | None" = None

        # Tool quick-actions (initialized in build())
        self.tool_actions: _ToolQuickActions | None = None

        # Cartesian axis lookup (lazily built from robot's frame names)
        self._cart_axis_lookup: dict[str, tuple[Axis, float, str]] | None = None

        # Jog cadence constants
        self.JOG_TICK_S: float = config.webapp_control_interval_s
        self.CADENCE_WARN_WINDOW: int = max(1, int(config.webapp_control_rate_hz))
        self.CADENCE_TOLERANCE: float = 0.015  # 15mm
        self.STREAM_TIMEOUT_S: float = 0.1

        # Cadence tracking
        self._joint_cadence = _CadenceTracker()
        self._cart_cadence = _CadenceTracker()

        # Robot/Sim toggle button reference
        self._robot_btn: ui.button | None = None

        # E-STOP manager (initialized with ui_client in build())
        self.estop: _EStopManager | None = None

        # TCP TransformControls drag state
        self._tcp_latest_pose: list[float] | None = None
        self._tcp_last_sent_pose: list[float] | None = (
            None  # Track last sent to avoid duplicates
        )
        self._tcp_drag_active: bool = False

        # Step input widget reference for dynamic suffix/tooltip
        self._step_input: ui.number | None = None
        self._step_input_tooltip: ui.tooltip | None = None
        self._jog_mode_tabs: Any = None

        # Rating-row widget refs (populated by _build_rating_row).
        # Keyed by ui_attr ("jog_speed", "jog_accel"). Lets keybindings
        # adjust the underlying value AND keep the visible rating, icon
        # color, and tooltip in sync.
        self._rating_widgets: dict[str, dict[str, Any]] = {}

        # Dirty checking caches for button enablement (avoid redundant CSS updates)
        self._last_joint_en_tuple: tuple[int, ...] | None = None
        self._last_cart_en_tuple: tuple[tuple, ...] | None = None
        self._last_editing_mode: bool | None = None
        self._gizmo_auto_hidden: bool = (
            False  # True when gizmo hidden due to jog unavailable
        )

        # Pending jog end wait task (to prevent spawning multiple concurrent wait tasks)
        self._jog_end_wait_task: asyncio.Task | None = None

    # ---- Helper methods ----

    def _get_cart_axis_lookup(self) -> dict[str, tuple[Axis, float, str]]:
        """Build cartesian axis lookup from the active robot's frame names.

        Translation axes (X, Y, Z) use the first frame (world),
        rotation axes (RX, RY, RZ) use the second frame (tool).
        """
        if self._cart_axis_lookup is not None:
            return self._cart_axis_lookup
        frames = ui_state.active_robot.cartesian_frames
        wrf, trf = frames[0], frames[1]
        self._cart_axis_lookup = {
            "X+": ("X", 1.0, wrf),
            "X-": ("X", -1.0, wrf),
            "Y+": ("Y", 1.0, wrf),
            "Y-": ("Y", -1.0, wrf),
            "Z+": ("Z", 1.0, wrf),
            "Z-": ("Z", -1.0, wrf),
            "RX+": ("RX", 1.0, trf),
            "RX-": ("RX", -1.0, trf),
            "RY+": ("RY", 1.0, trf),
            "RY-": ("RY", -1.0, trf),
            "RZ+": ("RZ", 1.0, trf),
            "RZ-": ("RZ", -1.0, trf),
        }
        return self._cart_axis_lookup

    def _apply_pressed_style(self, widget: ui.element | None, pressed: bool) -> None:
        if not widget:
            return
        if pressed:
            widget.classes(add="is-pressed")
        else:
            widget.classes(remove="is-pressed")

    def _get_first_pressed_joint(self) -> tuple[int, str] | None:
        """Return (index, 'pos'|'neg') for the first pressed joint, else None."""
        for j in range(len(self._jog_pressed_pos)):
            if self._jog_pressed_pos[j]:
                return (j, "pos")
            if self._jog_pressed_neg[j]:
                return (j, "neg")
        return None

    def _get_first_pressed_axis(self) -> str | None:
        """Return the first pressed cartesian axis key like 'X+' if any."""
        for k, pressed in self._cart_pressed_axes.items():
            if pressed:
                return k
        return None

    def _get_joint_limits(self, i: int) -> tuple[float, float]:
        """Return (lo, hi) for joint i with safe defaults."""
        try:
            pos_deg = ui_state.active_robot.joints.limits.position.deg
            if i < pos_deg.shape[0]:
                return float(pos_deg[i, 0]), float(pos_deg[i, 1])
            return (-360.0, 360.0)
        except (AttributeError, IndexError, AssertionError):
            return (-360.0, 360.0)

    # ---- Cartesian helpers (icons, orientation, refresh) ----

    def _axis_color_class_for(self, letter: str, rotation: bool = False) -> str:
        """Return CSS class for axis letter using theme tokens."""
        k = ("r" if rotation else "") + letter.lower()
        return self._axis_classes.get(k, "tcp-x")

    def _axis_string_for(
        self, assign_key: str, sign: str, rotation: bool = False
    ) -> str:
        """Compose axis string like 'X+' or 'RX-' for a given slot assignment."""
        letter = self._cart_assignment.get(assign_key, "X").upper()
        return f"R{letter}{sign}" if rotation else f"{letter}{sign}"

    @staticmethod
    def _read_icon_svg(svg_filename: str) -> tuple[str, list[int]]:
        """Load SVG text via package resources and extract viewBox size."""
        raw = (
            pkg_resources.files("waldo_commander.static.icons") / svg_filename
        ).read_text(encoding="utf-8")
        m = _RE_VIEWBOX.search(raw)
        vb = [int(m.group(i)) if m else 24 for i in range(1, 5)]
        return raw, vb

    @staticmethod
    def _prepare_icon_markup(
        raw_svg: str, viewbox_wh: list[int], label: str, slot_id: str = ""
    ) -> str:
        """Return wrapped SVG markup with enlarged glyph and updated label."""
        inner_match = _RE_SVG_INNER.search(raw_svg)
        inner = inner_match.group(1) if inner_match else raw_svg

        inner = _RE_TEXT_LABEL.sub(r"\1" + label + r"\3", inner)
        raw_svg_processed = _RE_TEXT_LABEL.sub(r"\1" + label + r"\3", raw_svg)

        vb_min_x, vb_min_y, vb_width, vb_height = viewbox_wh

        # Determine transform: slot override > dimension lookup > fallback
        if slot_id in _SLOT_TRANSFORM_OVERRIDES:
            transform = _SLOT_TRANSFORM_OVERRIDES[slot_id]
        elif (vb_width, vb_height) in _ICON_TRANSFORMS:
            transform = _ICON_TRANSFORMS[(vb_width, vb_height)]
        elif vb_min_y == 17:
            transform = "translate(-5,12)"
        else:
            transform = "translate(-5,-12)"

        # Cropped icons (height == 7) use the full SVG with overflow:visible
        if vb_height == 7:
            content = raw_svg_processed
            style = ' style="overflow:visible"'
        else:
            content = inner
            style = ""

        svg = (
            f'<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"'
            f' preserveAspectRatio="xMidYMid meet"{style}>'
            f'<g transform="{transform}" fill="currentColor" stroke="currentColor">'
            f"{content}</g></svg>"
        )
        return _RE_WHITESPACE.sub(" ", svg).strip()

    async def _on_slot_press(self, slot_id: str, is_pressed: bool) -> None:
        """Event bridge: map fixed slot to current axis string and call set_axis_pressed."""
        meta = self._cart_slot_meta.get(slot_id) or {}
        assign_key = meta.get("assign_key", "ud1")
        sign = meta.get("sign", "+")
        rotation = bool(meta.get("rotation", False))
        axis_str = self._axis_string_for(assign_key, sign, rotation)
        await self.set_axis_pressed(axis_str, bool(is_pressed))

    def set_axis_orientation(self, ud1: str, lr: str, ud2: str) -> None:
        """Update axis assignment for fixed slots and refresh labels/colors/actions."""
        self._cart_assignment["ud1"] = (ud1 or "X").upper()
        self._cart_assignment["lr"] = (lr or "Y").upper()
        self._cart_assignment["ud2"] = (ud2 or "Z").upper()
        self._refresh_cartesian_icons()

    def _refresh_cartesian_icons(self) -> None:
        """Rebuild icon SVGs and color classes for all slots; update axis->element mapping."""
        # Reset axis mapping for pressed visuals
        self._cart_axis_imgs.clear()
        # Known classes to remove
        remove_classes = "tcp-x tcp-y tcp-z tcp-rx tcp-ry tcp-rz"
        for slot_id, elem in self._cart_slot_elems.items():
            meta = self._cart_slot_meta.get(slot_id) or {}
            assign_key = meta.get("assign_key", "ud1")
            sign = meta.get("sign", "+")
            rotation = bool(meta.get("rotation", False))
            raw = meta.get("raw", "")
            vb = meta.get("viewbox", (24, 24))
            axis_str = self._axis_string_for(assign_key, sign, rotation)
            label = axis_str
            markup = self._prepare_icon_markup(raw, vb, label, slot_id)
            # Update HTML element content (ui.html elements, not ui.icon)
            new_html = f"""
            <svg viewBox="0 0 24 24" width="100" height="72" style="cursor:pointer;">
            <g style="pointer-events:visiblePainted;" fill="currentColor" stroke="currentColor">
                {markup}
            </g>
            </svg>
            """
            elem._props["content"] = new_html
            elem.update()
            # Update color classes
            elem.classes(remove=remove_classes)
            letter = self._cart_assignment.get(assign_key, "X").upper()
            elem.classes(add=self._axis_color_class_for(letter, rotation=rotation))
            # Update axis->element map for pressed visuals
            self._cart_axis_imgs[axis_str] = elem

    # ---- Enablement and visuals ----

    def _set_strong_disabled(self, elem: ui.element | None, disabled: bool) -> None:
        if not elem:
            return
        if disabled:
            elem.classes(add="cp-disabled-strong")
        else:
            elem.classes(remove="cp-disabled-strong")

    def refresh_joint_enablement(self) -> None:
        """Apply stronger disabled visuals to joint +/- buttons using robot_state.joint_en."""
        # Get current state for dirty checking
        editing_mode = robot_state.editing_mode
        en = robot_state.joint_en
        current_tuple = tuple(en) if len(en) == 2 * self._n_joints else None

        # Skip if state unchanged (18x faster when idle)
        if (
            editing_mode == self._last_editing_mode
            and current_tuple == self._last_joint_en_tuple
        ):
            return

        # If in editing mode, disable all buttons regardless of normal enablement
        if editing_mode:
            for btn in self._joint_left_btns.values():
                self._set_strong_disabled(btn, True)
            for btn in self._joint_right_btns.values():
                self._set_strong_disabled(btn, True)
            self._last_editing_mode = editing_mode
            self._last_joint_en_tuple = current_tuple
            return

        if current_tuple is None:
            return

        n_joints = ui_state.active_robot.joints.count
        for j in range(n_joints):
            plus_allowed = bool(en[2 * j])
            minus_allowed = bool(en[2 * j + 1])
            self._set_strong_disabled(self._joint_right_btns.get(j), not plus_allowed)
            self._set_strong_disabled(self._joint_left_btns.get(j), not minus_allowed)

        self._last_editing_mode = editing_mode
        self._last_joint_en_tuple = current_tuple

    def sync_cartesian_button_states(self) -> None:
        """Apply stronger disabled visuals to axis icons and mirror to 3D gizmo.

        Translation axes use WRF enablement, rotation axes use TRF enablement
        (matching the actual jog frame convention).
        """
        editing_mode = robot_state.editing_mode
        frames = ui_state.active_robot.cartesian_frames
        wrf, trf = frames[0], frames[1]
        en_wrf = robot_state.cart_en.get(wrf, _DEFAULT_CART_EN)
        en_trf = robot_state.cart_en.get(trf, _DEFAULT_CART_EN)
        current_tuple = (tuple(en_wrf), tuple(en_trf))

        # Skip if state unchanged
        if (
            editing_mode == self._last_editing_mode
            and current_tuple == self._last_cart_en_tuple
        ):
            return

        # If in editing mode, disable all cartesian buttons regardless of normal enablement
        if editing_mode:
            for ax in _AXIS_ORDER:
                elem = self._cart_axis_imgs.get(ax)
                self._set_strong_disabled(elem, True)
            for elem in self._cart_slot_elems.values():
                self._set_strong_disabled(elem, True)
            self._last_editing_mode = editing_mode
            self._last_cart_en_tuple = current_tuple
            return

        # 2D icons: translation (0-5) from WRF, rotation (6-11) from TRF
        for i, ax in enumerate(_AXIS_ORDER):
            en = en_trf if i >= 6 else en_wrf
            elem = self._cart_axis_imgs.get(ax)
            self._set_strong_disabled(elem, not bool(en[i]))

        self._last_editing_mode = editing_mode
        self._last_cart_en_tuple = current_tuple

    def sync_gizmo_for_jog_state(self) -> None:
        """Auto-hide TCP gizmo when live jogging is unavailable, restore when available."""
        jog_possible = (
            not robot_state.editing_mode
            and (robot_state.simulator_active or robot_state.connected)
            and not ui_state.editor_panel.script_running
        )
        if not jog_possible and not self._gizmo_auto_hidden:
            if ui_state.urdf_scene and ui_state.gizmo_visible:
                ui_state.urdf_scene.set_gizmo_visible(False)
                self._gizmo_auto_hidden = True
        elif jog_possible and self._gizmo_auto_hidden:
            if ui_state.urdf_scene and ui_state.gizmo_visible:
                ui_state.urdf_scene.set_gizmo_visible(True)
            self._gizmo_auto_hidden = False

    # ---- Movement permission check ----

    @staticmethod
    def _movement_allowed(notify: bool = True) -> bool:
        """Return True if robot movement is permitted (simulator active or hardware connected, no script running)."""
        editor = ui_state.editor_panel
        if editor and editor.script_running:
            if notify:
                ui.notify("Script is running — jog disabled", color="warning")
            return False
        if robot_state.simulator_active or robot_state.connected:
            return True
        if notify:
            ui.notify(
                "Robot mode requires a hardware connection. Connect robot or switch to Simulator mode.",
                color="negative",
                icon="error",
            )
        return False

    # ---- Joint jog methods ----

    async def set_joint_pressed(self, j: int, direction: str, is_pressed: bool) -> None:
        """Hybrid click/hold: quick click => single step, press-and-hold => stream until release."""
        if robot_state.editing_mode:
            return
        if not self._movement_allowed(notify=is_pressed):
            return
        assert self._joint_click_hold is not None

        sign = "+" if direction == "pos" else "-"
        axis_info = f"J{j + 1}{sign}"
        if is_pressed:
            motion_recorder.on_jog_start("joint", axis_info)
        else:
            self._schedule_jog_end_wait()

        target_btn = (
            self._joint_right_btns.get(j)
            if direction == "pos"
            else self._joint_left_btns.get(j)
        )
        self._apply_pressed_style(target_btn, bool(is_pressed))

        key = (j, direction)

        # Enforce mutual exclusivity: cancel opposite direction
        if is_pressed:
            other_dir = "neg" if direction == "pos" else "pos"
            other_key = (j, other_dir)
            self._joint_click_hold.cancel_key(other_key)
            if other_dir == "pos":
                self._jog_pressed_pos[j] = False
                self._apply_pressed_style(self._joint_right_btns.get(j), False)
            else:
                self._jog_pressed_neg[j] = False
                self._apply_pressed_style(self._joint_left_btns.get(j), False)

        def _set_pressed(val: bool):
            if direction == "pos":
                self._jog_pressed_pos[j] = val
            else:
                self._jog_pressed_neg[j] = val

        def _sync_timer():
            any_pressed = any(self._jog_pressed_pos) or any(self._jog_pressed_neg)
            if any_pressed and not ui_state.joint_jog_timer.active:
                self._joint_cadence.reset()
            ui_state.joint_jog_timer.active = bool(any_pressed)

        async def on_click():
            speed = _norm_speed()
            accel = _norm_accel()
            step = abs(float(ui_state.joint_step_deg))
            try:
                angles = list(robot_state.angles.deg)
                if len(angles) >= self._n_joints:
                    target_angles = angles[: self._n_joints]
                    lo, hi = self._get_joint_limits(j)
                    if direction == "pos":
                        target_angles[j] = min(hi, target_angles[j] + step)
                    else:
                        target_angles[j] = max(lo, target_angles[j] - step)
                    await self.client.move_j(target_angles, speed=speed, accel=accel)
            except Exception as e:
                logger.error("Incremental joint move failed: %s", e)

        def on_hold_start():
            _set_pressed(True)
            if not ui_state.joint_jog_timer.active:
                self._joint_cadence.reset()
            ui_state.joint_jog_timer.active = True

        def on_release(was_holding: bool):
            _set_pressed(False)
            _sync_timer()

        await self._joint_click_hold.on_change(
            key,
            is_pressed,
            on_click=on_click,
            on_hold_start=on_hold_start,
            on_release=on_release,
        )

    async def jog_tick(self) -> None:
        """Timer callback: send/update joint streaming jog if any button is pressed."""
        with global_phase_timer.phase("jog"):
            if not self._movement_allowed(notify=False):
                return

            speed = _norm_speed()
            intent = self._get_first_pressed_joint()
            if intent is not None:
                j, d = intent
                signed_speed = speed if d == "pos" else -speed
                await self.client.jog_j(
                    j,
                    speed=signed_speed,
                    duration=self.STREAM_TIMEOUT_S,
                    accel=_norm_accel(),
                )
            self._joint_cadence.tick(
                time.time(),
                self.JOG_TICK_S,
                self.CADENCE_WARN_WINDOW,
                self.CADENCE_TOLERANCE,
                "joint",
            )

    # ---- Cartesian jog methods ----

    async def set_axis_pressed(self, axis: str, is_pressed: bool) -> None:
        """Hybrid click/hold for cartesian axes: click => single step, hold => stream."""
        if robot_state.editing_mode:
            return
        if not self._movement_allowed(notify=is_pressed):
            return
        assert self._cart_click_hold is not None

        if is_pressed:
            motion_recorder.on_jog_start("cartesian", axis)
        else:
            self._schedule_jog_end_wait()

        # Check enablement: translation uses WRF, rotation uses TRF
        frames = ui_state.active_robot.cartesian_frames
        allowed = True
        if axis in _AXIS_ORDER:
            idx = _AXIS_ORDER.index(axis)
            frame = frames[1] if idx >= 6 else frames[0]
            en_list = robot_state.cart_en.get(frame, _DEFAULT_CART_EN)
            if len(en_list) == 12:
                allowed = bool(int(en_list[idx]))
        self._set_strong_disabled(self._cart_axis_imgs.get(axis), not allowed)
        if is_pressed and not allowed:
            return

        self._apply_pressed_style(self._cart_axis_imgs.get(axis), bool(is_pressed))

        def _sync_timer():
            t = ui_state.cart_jog_timer
            if t:
                any_pressed = any(bool(v) for v in self._cart_pressed_axes.values())
                if any_pressed and not t.active:
                    self._cart_cadence.reset()
                t.active = bool(any_pressed)

        async def on_click():
            speed = _norm_speed()
            step = max(0.1, min(100.0, float(ui_state.joint_step_deg)))
            try:
                axis_letter = axis.rstrip("+-")
                direction = 1.0 if axis.endswith("+") else -1.0
                is_rotation = axis_letter.startswith("R")
                # Use relative move: translation in WRF, rotation in TRF
                # (matches jog_l hold behavior)
                rel_pose = [0.0] * 6
                if axis_letter in _AXIS_MAP:
                    rel_pose[_AXIS_MAP[axis_letter]] = direction * step
                    frame = "TRF" if is_rotation else "WRF"
                    await self.client.move_l(
                        rel_pose,
                        frame=frame,
                        speed=speed,
                        accel=_norm_accel(),
                        rel=True,
                    )
            except Exception as e:
                logger.error("Incremental cart move failed: %s", e)

        def on_hold_start():
            self._cart_pressed_axes[axis] = True
            t = ui_state.cart_jog_timer
            if t:
                if not t.active:
                    self._cart_cadence.reset()
                t.active = True

        def on_release(was_holding: bool):
            self._cart_pressed_axes[axis] = False
            _sync_timer()

        await self._cart_click_hold.on_change(
            axis,
            is_pressed,
            on_click=on_click,
            on_hold_start=on_hold_start,
            on_release=on_release,
        )

    async def cart_jog_tick(self) -> None:
        """Timer callback: unified movement timer for TransformControls drag or cartesian jog."""
        with global_phase_timer.phase("jog"):
            if not self._movement_allowed(notify=False):
                return

            speed = _norm_speed()

            # Priority 1: TransformControls drag actively providing absolute poses
            if self._tcp_drag_active and self._tcp_latest_pose:
                # Only send if pose has changed (avoid flooding with duplicates)
                if self._tcp_last_sent_pose is not None:
                    # Compare with small epsilon for floating-point tolerance
                    epsilon = 0.01  # 0.01mm position / 0.01deg rotation tolerance
                    pose_changed = False
                    for i in range(6):
                        if (
                            abs(self._tcp_latest_pose[i] - self._tcp_last_sent_pose[i])
                            > epsilon
                        ):
                            pose_changed = True
                            break
                    if not pose_changed:
                        # Pose hasn't changed, skip sending
                        self._cart_cadence.tick(
                            time.time(),
                            self.JOG_TICK_S,
                            self.CADENCE_WARN_WINDOW,
                            self.CADENCE_TOLERANCE,
                            "cart",
                        )
                        return
                else:
                    logger.debug("TCP Drag: First move (no last sent pose)")

                try:
                    # Use speed for stream blending. The server enforces a
                    # minimum 200ms duration to keep commands alive long enough for
                    # subsequent updates to blend in, creating a "mouse trail" effect.
                    await self.client.servo_l(
                        list(self._tcp_latest_pose[:6]),
                        speed=float(speed),
                        accel=_norm_accel(),
                    )
                    # Track what we sent to avoid duplicates
                    self._tcp_last_sent_pose = list(self._tcp_latest_pose[:6])
                except Exception as e:
                    logger.debug("TCP Cartesian move (timer) failed: %s", e)
                self._cart_cadence.tick(
                    time.time(),
                    self.JOG_TICK_S,
                    self.CADENCE_WARN_WINDOW,
                    self.CADENCE_TOLERANCE,
                    "cart",
                )
                return

            # Priority 2: legacy cart jog buttons (streamed)
            axis = self._get_first_pressed_axis()
            if axis is not None:
                axis_name, direction, frame = self._get_cart_axis_lookup()[axis]
                await self.client.jog_l(
                    frame,
                    axis_name,
                    speed * direction,
                    self.STREAM_TIMEOUT_S,
                    accel=_norm_accel(),
                )
            self._cart_cadence.tick(
                time.time(),
                self.JOG_TICK_S,
                self.CADENCE_WARN_WINDOW,
                self.CADENCE_TOLERANCE,
                "cart",
            )

    def _handle_tcp_cartesian_move_start(self) -> None:
        """Handle start of a TCP TransformControls drag.

        Ensures drag state is reset so that even small initial movements are registered.
        """
        logger.debug("TCP Drag: START event received")
        if not self._movement_allowed(notify=False):
            return

        # Force a fresh start for the drag session
        self._tcp_last_sent_pose = None

        # Start drag session and recorder if not already active
        if not self._tcp_drag_active:
            self._tcp_drag_active = True
            motion_recorder.on_jog_start("cartesian", "TCP")

        # Ensure movement timer is active
        t = ui_state.cart_jog_timer
        if t and not t.active:
            self._cart_cadence.reset()
            t.active = True

    def _handle_tcp_cartesian_move(self, pose: list[float]) -> None:
        """Handle TCP Cartesian move events from TransformControls drag operations.

        Drag-start setup (arming _tcp_drag_active and the recorder) lives in
        _handle_tcp_cartesian_move_start, which is fired by transform_start.
        Stale pose updates that arrive after drag-end are ignored — the timer
        is already deactivated and the cached pose isn't streamed.
        """
        if not self._movement_allowed(notify=False):
            return

        if len(pose) < 6:
            logger.warning("Invalid pose length for Cartesian move: %d", len(pose))
            return

        if not self._tcp_drag_active:
            return

        self._tcp_latest_pose = list(pose[:6])

        t = ui_state.cart_jog_timer
        if t and not t.active:
            self._cart_cadence.reset()
            t.active = True

    def _handle_tcp_cartesian_move_end(self) -> None:
        """End of a TCP TransformControls drag: wait for motion to stop, then record."""
        logger.debug("TCP Drag: END event received")
        if self._tcp_drag_active:
            self._tcp_drag_active = False
            self._schedule_jog_end_wait()
        # Clear last sent pose so next drag starts fresh
        self._tcp_last_sent_pose = None
        # If no cart axis buttons are pressed, allow timer to stop
        t = ui_state.cart_jog_timer
        if t:
            any_pressed = any(bool(v) for v in self._cart_pressed_axes.values())
            t.active = bool(any_pressed)

    def _schedule_jog_end_wait(self) -> None:
        """Schedule a jog end wait task, cancelling any stale one."""
        if self._jog_end_wait_task is not None and not self._jog_end_wait_task.done():
            self._jog_end_wait_task.cancel()
        self._jog_end_wait_task = asyncio.create_task(self._wait_and_record_jog_end())

    async def _wait_and_record_jog_end(self) -> None:
        """Wait for robot motion to stop, then record the jog end position."""
        try:
            settled = await self.client.wait_motion(timeout=30.0, settle_window=0.5)
            if not settled:
                logger.warning("Jog: wait timed out, recording current position")
        except asyncio.CancelledError:
            logger.debug("Jog: wait task cancelled (superseded by new jog)")
            return
        except Exception as e:
            logger.warning("Jog: wait_motion failed: %s", e)
        finally:
            self._jog_end_wait_task = None
        # asyncio.create_task spawns this without a slot stack; on_jog_end touches
        # the editor via motion_recorder (`flash_editor_lines` → `ui.timer`), so
        # re-enter the panel's client context before any UI access.
        if self._ui_client is not None:
            with self._ui_client:
                motion_recorder.on_jog_end()
        else:
            motion_recorder.on_jog_end()

    async def move_joint_to_angle(self, joint_index: int, target_deg: float) -> None:
        """Move a single joint to the specified angle (deg) while holding others."""
        if not self._movement_allowed():
            return

        try:
            angles = list(robot_state.angles.deg)
            lo, hi = self._get_joint_limits(joint_index)
            tgt = max(lo, min(hi, float(target_deg)))
            pose = angles[: self._n_joints]
            pose[joint_index] = tgt
            spd = _norm_speed()

            await self.client.move_j(pose, speed=spd)
        except Exception as e:
            logger.error("Go to joint angle failed: %s", e)

    async def go_to_joint_limit(self, joint_index: int, which: str) -> None:
        """Move to min or max joint limit for a specific joint while holding others."""
        # Skip if in editing mode (target editor controls robot)
        if robot_state.editing_mode:
            return

        if not self._movement_allowed():
            return

        try:
            angles = list(robot_state.angles.deg)
            lo, hi = self._get_joint_limits(joint_index)

            target = angles[: self._n_joints]
            target[joint_index] = float(lo if which == "min" else hi)
            spd = _norm_speed()

            await self.client.move_j(target, speed=spd)
        except Exception as e:
            logger.error("Go to joint limit failed: %s", e)
            ui.notify(f"Failed joint move: {e}", color="negative")

    # ---- Gizmo control methods ----

    def sync_gizmo_to_urdf(self) -> None:
        """Sync gizmo state to URDF scene after it's initialized (called once after scene is ready)."""
        if ui_state.urdf_scene:
            # Apply current gizmo visibility
            ui_state.urdf_scene.set_gizmo_visible(ui_state.gizmo_visible)
            # Apply current gizmo mode (default is Move/TRANSLATE)
            ui_state.urdf_scene.set_gizmo_display_mode("TRANSLATE")
            # Fixed WRF orientation for cartesian UI layout
            # WRF: X (red) vertical (ud1), Y (green) horizontal (lr), Z (blue) vertical (ud2)
            self.set_axis_orientation("Y", "X", "Z")
            # Apply enablement visuals
            self.sync_cartesian_button_states()

            # Register Cartesian move callback for direct TCP position moves
            ui_state.urdf_scene.on_tcp_cartesian_move(self._handle_tcp_cartesian_move)
            # Register drag start/end to manage state
            ui_state.urdf_scene.on_tcp_cartesian_move_start(
                self._handle_tcp_cartesian_move_start
            )
            ui_state.urdf_scene.on_tcp_cartesian_move_end(
                self._handle_tcp_cartesian_move_end
            )
            # Note: set_gizmo_visible() already enables TransformControls when visible,
            # so no need for an additional enable_tcp_transform_controls() call here.

    def on_gizmo_mode_changed(self, mode: str) -> None:
        """Switch gizmo display mode between Move (translation) and Rotate."""
        if ui_state.urdf_scene is None:
            logger.warning("Cannot change gizmo mode: URDF scene not initialized")
            return
        # Map UI values to internal mode values
        internal_mode = "TRANSLATE" if mode == "Move" else "ROTATE"
        ui_state.urdf_scene.set_gizmo_display_mode(internal_mode)
        # Update TCP TransformControls mode (translate or rotate)
        tcp_mode = "translate" if mode == "Move" else "rotate"
        ui_state.urdf_scene.set_tcp_transform_mode(tcp_mode)

    async def on_gizmo_toggle(self, visible: bool) -> None:
        """Toggle gizmo visibility and TCP TransformControls."""
        ui_state.gizmo_visible = bool(visible)
        if ui_state.urdf_scene is None:
            logger.warning("Cannot toggle gizmo: URDF scene not initialized")
            return
        ui_state.urdf_scene.set_gizmo_visible(bool(visible))
        # Enable/disable TCP TransformControls based on visibility
        if visible:
            # Re-enable with current mode
            # Determine mode from current transform mode (lowercase: "translate" or "rotate")
            mode = ui_state.urdf_scene.tcp_transform_mode or "translate"
            ui_state.urdf_scene.enable_tcp_transform_controls(mode)
        else:
            ui_state.urdf_scene.disable_tcp_transform_controls()

    # ---- Robot action methods ----

    async def send_home(self) -> None:
        # In editing mode, move the editing robot to home position
        if robot_state.editing_mode:
            if ui_state.urdf_scene:
                ui_state.urdf_scene.apply_editing_home()
                logger.info("HOME sent to editing robot")
            return

        if not self._movement_allowed():
            return

        try:
            _ = await self.client.home()
            logger.info("HOME sent")

            # Record the home action if recording is active
            motion_recorder.record_action("home")
        except Exception as e:
            logger.error("HOME failed: %s", e)

    def _is_urdf_scene_valid(self) -> bool:
        """Check if urdf_scene exists and its client is still valid."""
        if not ui_state.urdf_scene:
            return False
        scene = ui_state.urdf_scene.scene
        if not scene:
            return False
        try:
            scene_client = scene._client()
            if scene_client is None or scene_client.id not in Client.instances:
                return False
        except (RuntimeError, AttributeError):
            return False
        return True

    def update_robot_btn_visual(self) -> None:
        """Update Robot/Simulator toggle button appearance."""
        if self._robot_btn is None:
            return
        if robot_state.simulator_active:
            self._robot_btn.props("color=amber-8")
            self._robot_btn.classes(add="glass-btn glass-amber")
        else:
            self._robot_btn.props("color=grey-7")
            self._robot_btn.classes(add="glass-btn", remove="glass-amber")

    async def on_toggle_sim(self) -> None:
        """Toggle between robot and simulator modes and update URDF appearance."""
        try:
            # Stop any running user script before mode switch (safety)
            editor_panel = ui_state.editor_panel
            if editor_panel and editor_panel.script_running:
                logger.info("Stopping running script before mode switch")
                try:
                    await editor_panel._stop_script_process()
                except Exception as e:
                    logger.warning("Failed to stop script before mode switch: %s", e)

            # Toggle simulator mode and enable
            if not robot_state.simulator_active:
                await self.client.simulator(True)
                robot_state.simulator_active = True
                # Apply simulator visual appearance to URDF scene (amber ghosting)
                if self._is_urdf_scene_valid() and ui_state.urdf_scene:
                    ui_state.urdf_scene.set_simulator_appearance(True)
                # Enable after switching to simulator
                # (no delay needed - controller waits for first frame before responding OK)
                try:
                    await self.client.resume()
                except Exception as e:
                    logger.warning("Resume after simulator on failed: %s", e)
            else:
                await self.client.simulator(False)
                robot_state.simulator_active = False
                # Restore default URDF appearance (remove simulator ghosting)
                if self._is_urdf_scene_valid() and ui_state.urdf_scene:
                    ui_state.urdf_scene.set_simulator_appearance(False)
                # Resume after switching back to robot mode
                try:
                    await self.client.resume()
                except Exception as e:
                    logger.warning("Resume after simulator off failed: %s", e)

        except Exception as ex:
            ui.notify(f"Simulator toggle failed: {ex}", color="negative")
            logger.error("Simulator toggle failed: %s", ex)
        finally:
            self.update_robot_btn_visual()
            if ui_state._readout_panel is not None:
                ui_state.readout_panel.update_conn_io()
            if ui_state._playback is not None:
                ui_state.playback.sync_mode()

    async def on_estop_click(self) -> None:
        """Trigger digital E-STOP (STOP command) and show dialog."""
        if robot_state.io_estop == 0:
            ui.notify("Physical E-STOP is active - release it first", color="warning")
            return

        await self.client.halt()
        if self.estop:
            self.estop._digital_active = True
            self.estop.show(is_physical=False)

    def render_jog_content(self) -> None:
        """Render jog controls (tabs + grids) and settings."""
        with ui.tabs().props("dense").classes("cp-jog-tabs") as jog_mode_tabs:
            joint_tab = ui.tab("Joint Jog").mark("tab-joint")
            cart_tab = ui.tab("Cartesian Jog").mark("tab-cartesian")
            settings_tab = ui.tab("Settings").mark("tab-settings")
        jog_mode_tabs.value = joint_tab
        self._jog_mode_tabs = jog_mode_tabs

        # Tab change handler to update step input suffix/tooltip
        _joint_tab_ref = joint_tab
        _cart_tab_ref = cart_tab

        def _on_tab_change(e):
            if self._step_input is None:
                return
            v = e.value if hasattr(e, "value") else e.args
            if v is _joint_tab_ref or v == "Joint Jog":
                self._step_input.props('suffix="°"')
                self._step_input.classes(remove="step-suffix-small")
                if self._step_input_tooltip:
                    self._step_input_tooltip.text = "Step size in degrees"
            elif v is _cart_tab_ref or v == "Cartesian Jog":
                self._step_input.props('suffix="mm"')
                self._step_input.classes(add="step-suffix-small")
                if self._step_input_tooltip:
                    self._step_input_tooltip.text = "Step size in mm"
            self._step_input.update()

        jog_mode_tabs.on("update:model-value", _on_tab_change)

        with (
            ui.tab_panels(jog_mode_tabs, value=joint_tab)
            .classes("cp-jog-panels")
            .style("width: 400px; height: 225px")
        ):
            # Joint jog panel
            with ui.tab_panel(joint_tab).classes("gap-1"):
                joint_names = list(ui_state.active_robot.joints.names)

                def make_joint_row(idx: int, name: str):
                    with ui.grid(rows="auto", columns="60px auto 80px").classes(
                        "items-center gap-3 w-full"
                    ):
                        ui.label(name).classes("text-right")
                        with ui.row().classes("w-full relative-position"):
                            lo, hi = self._get_joint_limits(idx)
                            bar = (
                                ui.linear_progress(value=0, show_value=False)
                                .props("rounded instant-feedback")
                                .classes("w-full joint-bar")
                            )

                            def _bar_backward(a, i=idx, lo=lo, hi=hi) -> float:
                                if hi <= lo or len(a) <= i or not math.isfinite(a[i]):
                                    return 0.0
                                return max(0.0, min(1.0, (a[i] - lo) / (hi - lo)))

                            bar.bind_value_from(
                                robot_state,
                                "angles",
                                backward=_bar_backward,
                            )

                            # Centered position + speed overlay
                            with (
                                ui.row()
                                .classes("items-center gap-1 no-wrap")
                                .style(
                                    "position:absolute; left:50%; top:50%;"
                                    " transform:translate(-50%,-50%);"
                                )
                            ):
                                num = (
                                    ui.number(
                                        value=0.0,
                                        min=lo,
                                        max=hi,
                                        step=0.1,
                                        format="%.1f",
                                        suffix="°",
                                    )
                                    .props(
                                        'dense borderless input-style="text-align:right;font-weight:bold"'
                                    )
                                    .classes("joint-readout-input")
                                    .style("width:55px;")
                                )
                                spd_lbl = (
                                    ui.label("0°/s")
                                    .classes("text-xs opacity-60")
                                    .style("min-width: 3rem; text-align: right;")
                                )

                            _num_ref: dict[str, Any] = {"focused": False, "el": num}

                            def _num_backward(a, i=idx, r=_num_ref) -> float | None:
                                if r["focused"]:
                                    return r[
                                        "el"
                                    ].value  # Keep current value while editing
                                if len(a) <= i or not math.isfinite(a[i]):
                                    return None
                                return float(a[i])

                            num.on(
                                "focus",
                                lambda _e, r=_num_ref: r.__setitem__("focused", True),
                            )
                            num.on(
                                "blur",
                                lambda _e, r=_num_ref: r.__setitem__("focused", False),
                            )

                            num.bind_value_from(
                                robot_state,
                                "angles",
                                backward=_num_backward,
                            )

                            def _spd_backward(s, i=idx) -> str:
                                if len(s) <= i:
                                    return "0°/s"
                                v = abs(s[i])
                                return f"{v:.0f}°/s"

                            spd_lbl.bind_text_from(
                                robot_state,
                                "speeds",
                                backward=_spd_backward,
                            )

                            def _submit_exact(e=None, i=idx, n=num):
                                try:
                                    val = (
                                        float(n.value) if n.value is not None else None
                                    )
                                except (ValueError, TypeError):
                                    val = None
                                if val is not None:
                                    _safe_task(self.move_joint_to_angle(i, val))

                            num.on("blur", _submit_exact)
                            num.on("keydown.enter", _submit_exact)

                            # Left minus pill
                            left_btn = (
                                ui.button(icon="remove")
                                .props("round flat dense no-caps text-color=white")
                                .classes("absolute left-1 joint-cap")
                            )
                            left_btn.mark(f"btn-j{idx + 1}-minus")

                            def check_lower_limit(a, i=idx, lo=lo):
                                if len(a) <= i:
                                    return False
                                step = ui_state.joint_step_deg
                                return a[i] - step >= lo

                            left_btn.bind_enabled_from(
                                robot_state, "angles", backward=check_lower_limit
                            )
                            left_btn.on(
                                "mousedown",
                                partial(self.set_joint_pressed, idx, "neg", True),
                            )
                            left_btn.on(
                                "mouseup",
                                partial(self.set_joint_pressed, idx, "neg", False),
                            )
                            left_btn.on(
                                "mouseleave",
                                partial(self.set_joint_pressed, idx, "neg", False),
                            )

                            # Right plus pill
                            right_btn = (
                                ui.button(icon="add")
                                .props("round flat dense no-caps text-color=white")
                                .classes("absolute right-1 joint-cap")
                            )
                            right_btn.mark(f"btn-j{idx + 1}-plus")

                            def check_upper_limit(a, i=idx, hi=hi):
                                if len(a) <= i:
                                    return False
                                step = ui_state.joint_step_deg
                                return a[i] + step <= hi

                            right_btn.bind_enabled_from(
                                robot_state, "angles", backward=check_upper_limit
                            )
                            right_btn.on(
                                "mousedown",
                                partial(self.set_joint_pressed, idx, "pos", True),
                            )
                            right_btn.on(
                                "mouseup",
                                partial(self.set_joint_pressed, idx, "pos", False),
                            )
                            right_btn.on(
                                "mouseleave",
                                partial(self.set_joint_pressed, idx, "pos", False),
                            )

                            self._joint_left_btns[idx] = left_btn
                            self._joint_right_btns[idx] = right_btn
                        with ui.row().classes("justify-end gap-1"):
                            min_btn = (
                                ui.button(
                                    icon="first_page",
                                    on_click=lambda e, i=idx: _safe_task(
                                        self.go_to_joint_limit(i, "min")
                                    ),
                                )
                                .props("round dense unelevated")
                                .tooltip("Move to minimum joint limit")
                                .mark(f"btn-j{idx + 1}-min-limit")
                            )
                            max_btn = (
                                ui.button(
                                    icon="last_page",
                                    on_click=lambda e, i=idx: _safe_task(
                                        self.go_to_joint_limit(i, "max")
                                    ),
                                )
                                .props("round dense unelevated")
                                .tooltip("Move to maximum joint limit")
                                .mark(f"btn-j{idx + 1}-max-limit")
                            )
                            self._joint_limit_btns[(idx, "min")] = min_btn
                            self._joint_limit_btns[(idx, "max")] = max_btn

                for i, n in enumerate(joint_names):
                    make_joint_row(i, n)

            # Cartesian jog panel
            with ui.tab_panel(cart_tab).classes("flex items-center justify-center"):
                # Icons inline with dynamic labels/colors/actions; layout remains fixed
                def _add_slot(
                    slot_id: str,
                    svg_filename: str,
                    assign_key: str,
                    sign: str,
                    rotation: bool,
                ) -> None:
                    raw, vb = self._read_icon_svg(svg_filename)
                    axis_str = self._axis_string_for(assign_key, sign, rotation)
                    label = axis_str
                    markup = self._prepare_icon_markup(raw, vb, label, slot_id)
                    cont = ui.html(
                        f"""
                        <svg viewBox="0 0 24 24" width="100" height="72"
                            style="cursor:pointer;">
                        <g style="pointer-events:visiblePainted;" fill="currentColor" stroke="currentColor">
                            {markup}
                        </g>
                        </svg>
                        """,
                        sanitize=False,
                    )
                    letter = self._cart_assignment.get(assign_key, "X").upper()
                    cont.classes(self._axis_color_class_for(letter, rotation=rotation))
                    marker = f"axis-{axis_str.replace('+', 'plus').replace('-', 'minus').lower()}"
                    cont.mark(marker)
                    # Events: press/hold streaming behavior (don't change)
                    cont.on("mousedown", partial(self._on_slot_press, slot_id, True))
                    cont.on("mouseup", partial(self._on_slot_press, slot_id, False))
                    cont.on("mouseleave", partial(self._on_slot_press, slot_id, False))
                    # Store for refresh
                    self._cart_slot_elems[slot_id] = cont
                    self._cart_slot_meta[slot_id] = {
                        "assign_key": assign_key,
                        "sign": sign,
                        "rotation": rotation,
                        "svg_filename": svg_filename,
                        "raw": raw,
                        "viewbox": vb,
                    }

                # Translation grid (original shape): XY cross with Z column on the right
                with (
                    ui.grid(
                        rows="72px 30px 72px",
                        columns="90px 30px 72px 42px 72px 30px 72px",
                    )
                    .classes("gap-0")
                    .style("place-items: center")
                ):
                    # Row 1:    [UD2+, UD1-, empty, RUD2+, empty, RUD1+, empty]
                    _add_slot("ud2_up", "arrow-small-up-cropped.svg", "ud2", "+", False)
                    _add_slot("ud1_up", "arrow-small-up.svg", "ud1", "-", False)
                    ui.element("div").style("width:30px;height:30px")  # empty
                    _add_slot("r_ud2_plus", "curved-arrow-down.svg", "ud2", "+", True)
                    ui.element("div").style("width:30px;height:30px")  # empty
                    _add_slot("r_ud1_plus", "curved-arrow-down.svg", "ud1", "+", True)
                    ui.element("div").style("width:30px;height:30px")  # empty

                    # Row 2:    [LR+, empty, LR-, empty, empty, RLR+, empty, RLR-]
                    _add_slot("lr_neg", "arrow-small-left.svg", "lr", "+", False)
                    ui.element("div").style("width:30px;height:30px")  # center empty
                    _add_slot("lr_pos", "arrow-small-right.svg", "lr", "-", False)
                    ui.element("div").style("width:30px;height:30px")  # empty
                    _add_slot("r_lr_plus", "curved-arrow-right.svg", "lr", "+", True)
                    ui.element("div").style("width:30px;height:30px")  # empty
                    _add_slot("r_lr_minus", "curved-arrow-left.svg", "lr", "-", True)

                    # Row 3:    [UD2-, UD1+, empty, RUD2-, empty, RUD1-, empty]
                    _add_slot(
                        "ud2_down", "arrow-small-down-cropped.svg", "ud2", "-", False
                    )
                    _add_slot("ud1_down", "arrow-small-down.svg", "ud1", "+", False)
                    ui.element("div").style("width:30px;height:30px")  # empty
                    _add_slot("r_ud2_minus", "curved-arrow-up.svg", "ud2", "-", True)
                    ui.element("div").style("width:30px;height:30px")  # empty
                    _add_slot("r_ud1_minus", "curved-arrow-up.svg", "ud1", "-", True)
                    ui.element("div").style("width:30px;height:30px")  # empty

                # Initialize axis->element mapping and ensure visuals reflect current assignment
                self._refresh_cartesian_icons()

            # Settings panel
            with ui.tab_panel(settings_tab).classes("gap-0 p-0"):
                with ui.scroll_area().classes("w-full h-full p-0"):
                    self._settings_content = SettingsContent(self.client)
                    self._settings_content.build_embedded()

    def _build_rating_row(
        self,
        *,
        icon_name: str,
        storage_key: str,
        ui_attr: str,
        default_color: str,
        colors: list[str],
        format_tooltip: Callable[[float], str],
    ) -> None:
        """Build a 10-step rating row (speed or acceleration) with persistence."""
        with ui.row().classes("items-center gap-2 w-full"):
            icon = ui.icon(icon_name, size="md", color=default_color)
            with icon:
                tooltip = ui.tooltip(storage_key.replace("_", " ").title())
            stored = app.storage.general.get(storage_key, getattr(ui_state, ui_attr))
            setattr(ui_state, ui_attr, stored)
            v_init = max(1, min(10, round(int(stored) / self._RATING_UNIT)))

            rating = ui.rating(max=10, icon="circle", size="16px", value=v_init).props(
                f':color="{colors}"'
            )
            self._rating_widgets[ui_attr] = {
                "rating": rating,
                "icon": icon,
                "tooltip": tooltip,
                "colors": colors,
                "format_tooltip": format_tooltip,
                "storage_key": storage_key,
            }

            # Click on the rating dispatches the new step value (1..10) as
            # e.args; route both UI clicks and keybindings through the same
            # _set_rating_step path so dependent visuals stay in sync.
            rating.on(
                "update:model-value",
                lambda e, _attr=ui_attr: self._set_rating_step(
                    _attr, int(e.args) if e.args else 1, sync_widget=False
                ),
            )
            if v_init > 0:
                self._set_rating_step(ui_attr, v_init, sync_widget=False)

    _RATING_UNIT = 10

    def _set_rating_step(
        self, ui_attr: str, step: int, *, sync_widget: bool = True
    ) -> None:
        """Apply a 1..10 rating step to the row's value, storage, icon color
        and tooltip text. Set sync_widget=False when invoked from the rating's
        own change event (the widget already holds the new value)."""
        refs = self._rating_widgets.get(ui_attr)
        if refs is None:
            return
        step = max(1, min(10, step))
        new_value = step * self._RATING_UNIT
        setattr(ui_state, ui_attr, new_value)
        app.storage.general[refs["storage_key"]] = new_value
        if sync_widget:
            refs["rating"].value = step
        refs["icon"].props(f"color={refs['colors'][step - 1]}")
        refs["tooltip"].text = refs["format_tooltip"](step / 10.0)

    def adjust_rating(self, ui_attr: str, delta: int) -> None:
        """Adjust a rating-row backed value (jog_speed/jog_accel) by delta
        and sync the widget. Used by the [/] keybindings so keyboard
        adjustments behave the same as clicking the rating."""
        current = int(getattr(ui_state, ui_attr, 0))
        step = round((current + delta) / self._RATING_UNIT)
        self._set_rating_step(ui_attr, step)

    def build(self, anchor: str = "bl") -> None:
        """Render the bottom-left control panel (overlay-bl).

        Args:
            anchor: Position anchor for the panel (e.g., "bl" for bottom-left)
        """
        # Capture UI client for background task operations
        self._ui_client = ui.context.client
        self.estop = _EStopManager(self.client, lambda: self._ui_client)

        def ui_client_fn() -> object:
            return self._ui_client

        self._joint_click_hold = _ClickHoldHandler(
            self.CLICK_HOLD_THRESHOLD_S, ui_client_fn
        )
        self._cart_click_hold = _ClickHoldHandler(
            self.CLICK_HOLD_THRESHOLD_S, ui_client_fn
        )

        with ui.card().classes(f"overlay-panel overlay-card overlay-{anchor} gap-1"):
            with ui.column().classes("gap-2 w-full"):
                with ui.row().classes("items-center w-full"):
                    with ui.column().classes("gap-1 flex-grow"):
                        self._build_speed_accel_rows()

                    # Tool quick-action box
                    self.tool_actions = _ToolQuickActions(
                        self.client, self._movement_allowed
                    )
                    self.tool_actions.build()

                self._build_action_row()

            # Jog controls (tabs + grids)
            self.render_jog_content()

    def _build_speed_accel_rows(self) -> None:
        """Build speed and acceleration rating rows."""

        def _format_speed_tooltip(fraction: float) -> str:
            pct = int(fraction * 100)
            try:
                robot = ui_state.active_robot
                n = robot.joints.count
                jog_vel_deg = np.rad2deg(robot.joints.limits.jog.velocity) * fraction
                cart = robot.cartesian_limits
                lin_mm_s = cart.velocity.linear * 1000 * fraction
                ang_deg_s = np.degrees(cart.velocity.angular) * fraction
                joints_str = ", ".join(f"{v:.0f}" for v in jog_vel_deg)
                return (
                    f"Jog Speed: {pct}%"
                    f" | L: {lin_mm_s:.0f} mm/s, {ang_deg_s:.0f} °/s"
                    f" | J1-{n}: {joints_str} °/s"
                )
            except (AttributeError, TypeError):
                return f"Jog Speed: {pct}%"

        def _format_accel_tooltip(fraction: float) -> str:
            pct = int(fraction * 100)
            try:
                robot = ui_state.active_robot
                n = robot.joints.count
                jog_acc_deg = (
                    np.rad2deg(robot.joints.limits.jog.acceleration) * fraction
                )
                cart = robot.cartesian_limits
                lin_mm_s2 = cart.acceleration.linear * 1000 * fraction
                ang_deg_s2 = np.degrees(cart.acceleration.angular) * fraction
                joints_str = ", ".join(f"{v:.0f}" for v in jog_acc_deg)
                return (
                    f"Jog Accel: {pct}%"
                    f" | L: {lin_mm_s2:.0f} mm/s², {ang_deg_s2:.0f} °/s²"
                    f" | J1-{n}: {joints_str} °/s²"
                )
            except (AttributeError, TypeError):
                return f"Jog Accel: {pct}%"

        self._build_rating_row(
            icon_name="speed",
            storage_key="jog_speed",
            ui_attr="jog_speed",
            default_color="amber-6",
            colors=[
                "yellow-3",
                "yellow-6",
                "amber-4",
                "amber-7",
                "orange-5",
                "orange-8",
                "deep-orange-5",
                "deep-orange-8",
                "red-7",
                "red-9",
            ],
            format_tooltip=_format_speed_tooltip,
        )
        self._build_rating_row(
            icon_name="bolt",
            storage_key="jog_accel",
            ui_attr="jog_accel",
            default_color="cyan-6",
            colors=[
                "lime-3",
                "lime-6",
                "light-green-4",
                "light-green-7",
                "green-5",
                "green-8",
                "teal-6",
                "teal-8",
                "cyan-7",
                "cyan-9",
            ],
            format_tooltip=_format_accel_tooltip,
        )

    def _build_action_row(self) -> None:
        """Build the action row: Home, Robot/Sim toggle, gizmo controls, camera reset, step input."""
        with ui.row().classes("gap-2 items-center"):
            ui.button(icon="home", on_click=self.send_home).props(
                "dense round unelevated color=teal-6"
            ).tooltip("Home (H)").mark("btn-home")

            # Single-button Robot/Simulator toggle
            robot_btn = (
                ui.button(
                    icon="precision_manufacturing",
                    on_click=self.on_toggle_sim,
                )
                .props("round unelevated dense")
                .tooltip("Robot/Simulator")
            )
            robot_btn.mark("btn-robot-toggle")
            self._robot_btn = robot_btn

            # Gizmo mode button group
            selected = {"value": "Move"}
            buttons: dict[str, ui.button] = {}

            def set_gizmo_mode(mode: str):
                if mode == "Hidden":
                    _safe_task(self.on_gizmo_toggle(False))
                else:
                    _safe_task(self.on_gizmo_toggle(True))
                    self.on_gizmo_mode_changed(mode)
                selected["value"] = mode
                for m, btn in buttons.items():
                    btn.props("color=primary" if m == mode else "color=grey-7")

            with ui.button_group().props("rounded unelevated dense"):
                buttons["Move"] = (
                    ui.button(
                        icon="open_with",
                        on_click=lambda e, m="Move": set_gizmo_mode(m),
                    )
                    .props("round unelevated dense")
                    .tooltip("Translate gizmo mode")
                )
                buttons["Rotate"] = (
                    ui.button(
                        icon="sync",
                        on_click=lambda e, m="Rotate": set_gizmo_mode(m),
                    )
                    .props("round unelevated dense")
                    .tooltip("Rotate gizmo mode")
                )
                buttons["Hidden"] = (
                    ui.button(
                        icon="visibility_off",
                        on_click=lambda e, m="Hidden": set_gizmo_mode(m),
                    )
                    .props("round unelevated dense")
                    .tooltip("Hide gizmo")
                )
                buttons["Move"].props("color=primary")
                buttons["Rotate"].props("color=grey-7")
                buttons["Hidden"].props("color=grey-7")

            # Reset camera button
            def _reset_cam():
                try:
                    if ui_state.urdf_scene and ui_state.urdf_scene.scene:
                        ui_state.urdf_scene.scene.move_camera(
                            **DEFAULT_CAMERA, duration=0.0
                        )
                except Exception as e:
                    logger.error("Reset camera failed: %s", e)

            ui.button(icon="view_in_ar", on_click=_reset_cam).props(
                "round unelevated dense color=light-blue-6"
            ).tooltip("Reset camera")
            with ui.row(align_items="center").classes("gap-1"):
                ui.label("Step:").classes("text-white")
                self._step_input = (
                    ui.number(
                        value=ui_state.joint_step_deg,
                        min=1,
                        max=100.0,
                        step=1,
                        format="%.1f",
                        suffix="°",
                    )
                    .props(
                        'dense borderless hide-bottom-space input-style="text-align:right"'
                    )
                    .classes("step-input")
                    .bind_value(ui_state, "joint_step_deg")
                )
                with self._step_input:
                    self._step_input_tooltip = ui.tooltip("Step size in degrees")

            with ui.element("div").style(
                "width: 0; height: 0; overflow: visible; position: relative;"
            ):
                ui.button(
                    icon="dangerous", color="negative", on_click=self.on_estop_click
                ).props("round unelevated").classes("glass-btn text-2xl").style(
                    "position: absolute; top: -18px; left: 10px;"
                ).tooltip("E-Stop (Esc)").mark("btn-estop")

    def cleanup(self) -> None:
        """Cancel background timers during shutdown."""
        if self._settings_content is not None:
            self._settings_content.cleanup()
        if self._joint_click_hold:
            self._joint_click_hold.cleanup()
        if self._cart_click_hold:
            self._cart_click_hold.cleanup()
