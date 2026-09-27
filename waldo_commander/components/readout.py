"""Top-left readout panel component for robot pose and IO status display."""

import html as html_mod
import logging
import random
import time

from nicegui import ui

import waldoctl
from waldoctl import ActionStatus

from waldo_commander.common.theme import IO_COLOR_OFF, IO_COLOR_ON
from waldo_commander.components.robot_buddy import Light, Mood, Reaction, RobotBuddy
from waldo_commander.services.control_lease import MCP, control_lease
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.state import robot_events, robot_state, ui_state

logger = logging.getLogger(__name__)


_MOOD_TOOLTIPS = {
    Mood.HAPPY: "Connected",
    Mood.NEUTRAL: "Simulator",
    Mood.SAD: "Disconnected",
    Mood.ALARMED: "E-STOP active",
}
_LIGHT_TOOLTIPS = {
    Light.RECORDING: "Recording",
    Light.AGENT: "AI agent in control",
}
# Chip background — darker hue of the buddy's colour
_CHIP_COLORS = {
    Mood.HAPPY: "var(--color-emerald-400)",
    Mood.NEUTRAL: "var(--color-gray-400)",
    Mood.SAD: "var(--color-red-400)",
    Mood.ALARMED: "var(--color-red-400)",
}
# The buddy watches the arm while it moves; hold that a beat past each stop
# so a train of step jogs reads as one stretch of work, not a flicker.
_MOVING_DEG_S = 0.5
_MOVING_HOLD_S = 1.0
# Only the simulator buddy dozes: on a live or lost hardware connection a
# sleeping robot would read as "robot idle / offline" at a glance.
_BUDDY_SLEEP_AFTER_S = 180.0


def _sleep_after(mood: Mood) -> float:
    return _BUDDY_SLEEP_AFTER_S if mood == Mood.NEUTRAL else 0.0


def _chip_style(mood: Mood) -> str:
    return (
        f"background-color: {_CHIP_COLORS[mood]} !important;"
        " margin: 0;"
        " padding: 20px 12px !important;"
        " box-shadow: none;"
        " border-radius: 10px;"
    )


def _status_mood() -> Mood:
    """The chip buddy's mood from connection and E-STOP state."""
    status = waldoctl.commander.status
    control_panel = ui_state._control_panel
    estop = control_panel.estop if control_panel is not None else None
    if status.io.estop == 0 or (estop is not None and estop.active):
        return Mood.ALARMED
    if status.simulator_active:
        return Mood.NEUTRAL
    if status.connected:
        return Mood.HAPPY
    return Mood.SAD


def _status_light() -> Light | None:
    """The standing condition the chip buddy's antennae show, if any."""
    holder = control_lease.holder()
    if holder is not None and holder.channel == MCP:
        return Light.AGENT
    if is_any_program_recording():
        return Light.RECORDING
    return None


def _tooltip(mood: Mood, light: Light | None) -> str:
    text = _MOOD_TOOLTIPS[mood]
    return f"{text} · {_LIGHT_TOOLTIPS[light]}" if light else text


def _fmt_1f(v: float) -> str:
    """Format float with 1 decimal place."""
    return f"{v:.1f}"


# ---------------------------------------------------------------------------
# Action log HTML rendering
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    ActionStatus.EXECUTING: (
        '<span style="color:var(--color-sky-500);font-size:11px" '
        'class="material-icons q-spinner-mat">sync</span>'
    ),
    ActionStatus.COMPLETED: (
        '<span style="color:var(--color-emerald-500);font-size:13px">\u2713</span>'
    ),
    ActionStatus.FAILED: (
        '<span style="color:var(--color-red-500);font-size:13px">\u2717</span>'
    ),
}


_TIPS = [
    "Press H to home the robot",
    "Press Esc for emergency stop",
    "Use [ and ] to adjust jog speed",
    "Click the action log to expand it",
    "Press Space to play/pause the script",
    "Use WASD + Q/E to jog in cartesian space",
    "Set TCP offset in Settings for tool tips",
    "Press T to add a target at the current position",
    "Hold a jog key for continuous movement",
]
_TIP_TEXT = random.choice(_TIPS)


_EVENT_ICONS = {
    "error": '<span style="color:var(--color-red-500);font-size:13px">\u2717</span>',
    "warning": (
        '<span style="color:var(--color-amber-400);font-size:13px"'
        ' class="material-icons">warning</span>'
    ),
}


def _build_event_log_html() -> str:
    """Build HTML for the warnings/errors log entries."""
    parts: list[str] = []
    for ts, severity, message, detail in reversed(robot_events.entries):
        icon = _EVENT_ICONS.get(severity, _EVENT_ICONS["warning"])
        tail = (
            f' <span style="color:var(--ctk-muted)">{html_mod.escape(detail)}</span>'
            if detail
            else ""
        )
        parts.append(
            f'<div class="action-log-entry" style="font-size:12px;line-height:1.5">'
            f'{icon} <span style="color:var(--ctk-muted)">{ts}</span> '
            f"<b>{html_mod.escape(message)}</b>{tail}</div>"
        )
    return "".join(parts)


def _build_log_entries_html() -> str:
    """Build HTML for all action log entries."""
    parts: list[str] = []
    for entry in reversed(waldoctl.commander.status.action.history):
        icon = _STATUS_ICONS.get(entry.status, "")
        count = (
            f" <span style='color:var(--ctk-muted)'>\u00d7{entry.count}</span>"
            if entry.count > 1
            else ""
        )
        params = ""
        if entry.params:
            params = (
                f' <span style="color:var(--ctk-muted)">'
                f"{html_mod.escape(entry.params)}</span>"
            )
        name = html_mod.escape(entry.command_name)
        parts.append(
            f'<div class="action-log-entry" style="font-size:12px;line-height:1.5">'
            f"{icon} <b>{name}</b>{count}{params}</div>"
        )
    # Tip of the day as the oldest entry
    tip_icon = (
        '<span style="color:var(--color-amber-400);font-size:13px"'
        ' class="material-icons">tips_and_updates</span>'
    )
    parts.append(
        f'<div class="action-log-entry" style="font-size:12px;line-height:1.5">'
        f'{tip_icon} <span style="color:var(--ctk-muted)">{_TIP_TEXT}</span></div>'
    )
    return "\n".join(parts)


class ReadoutPanel:
    """Top-left readout panel displaying cartesian pose, rotational pose, and IO status."""

    def __init__(self) -> None:
        """Initialize readout panel with UI element references."""
        # Robot buddy + IO elements
        self._buddy: RobotBuddy | None = None
        self._buddy_tooltip: ui.tooltip | None = None
        self._robot_chip: ui.chip | None = None
        self._backend_label: ui.label | None = None
        self._tool_chip: ui.chip | None = None
        self._tool_label: ui.label | None = None
        self._tool_separator: ui.label | None = None
        self._io_chips: list[ui.chip] = []

        # Warnings/errors log elements
        self._event_log_row: ui.row | None = None
        self._event_scroll_area: ui.scroll_area | None = None
        self._event_log_html: ui.html | None = None
        self._event_log_expanded: bool = False
        self._event_log_version: int = -1

        # Action log elements
        self._action_scroll_area: ui.scroll_area | None = None
        self._action_log_html: ui.html | None = None
        self._action_log_expanded: bool = False

        # Dirty checking state
        self._moving_until: float = 0.0
        self._last_collision: bool = False
        self._last_homed: bool | None = None
        self._seen_jog_pos: list[bool] | None = None
        self._seen_jog_neg: list[bool] | None = None
        self._blocked_jogs: int = 0
        self._last_tool_key: str | None = None
        self._last_io_inputs: list[int] | None = None
        self._last_io_outputs: list[int] | None = None

    def update_conn_io(self) -> None:
        """Update the buddy and IO status. Called from status consumer."""
        if self._buddy is not None:
            self._update_buddy()

        tool_key = waldoctl.commander.status.tool.key
        if tool_key != self._last_tool_key:
            self._last_tool_key = tool_key
            if self._tool_chip is not None and self._tool_label is not None:
                if tool_key and tool_key != "NONE":
                    self._tool_label.text = tool_key
                    self._tool_chip.set_visibility(True)
                    if self._tool_separator is not None:
                        self._tool_separator.set_visibility(True)
                else:
                    self._tool_chip.set_visibility(False)
                    if self._tool_separator is not None:
                        self._tool_separator.set_visibility(False)

        if self._io_chips:
            io = waldoctl.commander.status.io
            inputs = io.inputs
            outputs = io.outputs
            if inputs != self._last_io_inputs or outputs != self._last_io_outputs:
                self._last_io_inputs = list(inputs)
                self._last_io_outputs = list(outputs)
                all_vals = self._last_io_inputs + self._last_io_outputs
                for i, chip in enumerate(self._io_chips):
                    if i < len(all_vals):
                        color = IO_COLOR_ON if all_vals[i] else IO_COLOR_OFF
                        chip.props(f"color={color}")

    def _update_buddy(self) -> None:
        assert self._buddy is not None
        mood = _status_mood()
        light = _status_light()
        if mood != self._buddy.mood:
            self._buddy.set_mood(mood)
            self._buddy.set_sleep_after(_sleep_after(mood))
            if self._robot_chip is not None:
                self._robot_chip.style(_chip_style(mood))
                self._robot_chip.update()
        self._buddy.set_light(light)
        if self._buddy_tooltip is not None:
            text = _tooltip(mood, light)
            if self._buddy_tooltip.text != text:
                self._buddy_tooltip.text = text
                self._buddy_tooltip.update()

        now = time.monotonic()
        speeds = robot_state.speeds
        if speeds.size and (
            speeds.max() > _MOVING_DEG_S or speeds.min() < -_MOVING_DEG_S
        ):
            self._moving_until = now + _MOVING_HOLD_S
        moving = now < self._moving_until
        self._buddy.set_busy(is_any_program_running() or moving)

        collision = waldoctl.commander.status.collision.active
        if collision and not self._last_collision:
            self._buddy.react(Reaction.STARTLE)
        self._last_collision = collision

        # The status loop replaces these lists only when a limit changes, so
        # an identity check keeps the per-tick cost at two comparisons.
        joints = waldoctl.commander.status.joints
        pos, neg = joints.can_jog_pos, joints.can_jog_neg
        if pos is not self._seen_jog_pos or neg is not self._seen_jog_neg:
            blocked = pos.count(False) + neg.count(False)
            if blocked > self._blocked_jogs and moving:
                self._buddy.react(Reaction.SHRUG)
            self._blocked_jogs = blocked
            self._seen_jog_pos, self._seen_jog_neg = pos, neg

        homed = robot_state.homed
        if homed and self._last_homed is False:
            self._buddy.react(Reaction.NOD)
        self._last_homed = homed

    def set_buddy_calm(self, calm: bool) -> None:
        if self._buddy is not None:
            self._buddy.set_calm(calm)

    def greet(self) -> None:
        """Wave hello once the page has finished loading."""
        if self._buddy is not None:
            self._buddy.react(Reaction.GREET)

    def on_script_finished(self, exit_code: int) -> None:
        """Cheer a program that ran to completion; wince at one that crashed."""
        if self._buddy is not None:
            self._buddy.react(Reaction.CELEBRATE if exit_code == 0 else Reaction.OOPS)

    def update_action_log(self) -> None:
        """Rebuild the action log scroll area from ``commander.status.action``.

        Bound as a change-listener on ``commander.status.action`` once the
        scroll-area widget is built; fires only when the log actually mutates.
        """
        if not self._action_scroll_area or not self._action_log_html:
            return
        self._action_log_html.set_content(_build_log_entries_html())
        self._action_scroll_area.scroll_to(percent=0.0)

    def _bind_action_log_listener(self) -> None:
        """Subscribe to ``commander.status.action`` for incremental rebuilds.

        Called from build time (after the scroll-area widget exists and after
        ``commander`` is registered). ``add_change_listener`` dedups by
        ``(instance, func)``, so this is idempotent per Action instance.
        """
        waldoctl.commander.status.action.add_change_listener(self.update_action_log)

    def _toggle_action_log(self) -> None:
        """Toggle action log between collapsed and expanded."""
        self._action_log_expanded = not self._action_log_expanded
        if self._action_scroll_area:
            if self._action_log_expanded:
                self._action_scroll_area.classes(add="action-log-expanded")
            else:
                self._action_scroll_area.classes(remove="action-log-expanded")

    def _toggle_event_log(self) -> None:
        """Toggle warnings/errors log between collapsed and expanded."""
        self._event_log_expanded = not self._event_log_expanded
        if self._event_scroll_area:
            if self._event_log_expanded:
                self._event_scroll_area.classes(add="action-log-expanded")
            else:
                self._event_scroll_area.classes(remove="action-log-expanded")
            self._apply_event_log_height()

    def _apply_event_log_height(self) -> None:
        """Size the log to its content — the scroll area's stock height
        would otherwise pin the expanded log at the 200px cap and leave a
        block of dead space under a short history."""
        if self._event_scroll_area is None:
            return
        if self._event_log_expanded:
            height = min(8 + 19 * max(len(robot_events.entries), 1), 200)
        else:
            height = 20
        self._event_scroll_area.style(f"height: {height}px")

    def update_event_log(self) -> None:
        """Render the warnings/errors log; the row appears with its first entry."""
        if self._event_log_row is None or self._event_log_html is None:
            return
        if robot_events.version == self._event_log_version:
            return
        # Only news startles the buddy, not the history a fresh page renders.
        if self._event_log_version >= 0 and self._buddy is not None:
            self._buddy.react(Reaction.STARTLE)
        self._event_log_version = robot_events.version
        self._event_log_row.set_visibility(bool(robot_events.entries))
        self._event_log_html.set_content(_build_event_log_html())
        self._apply_event_log_height()

    def build(self, anchor: str = "tl") -> None:
        """Render the top-left readout panel as an overlay card."""
        self._event_log_version = -1
        with ui.card().classes(f"overlay-panel overlay-card overlay-{anchor}"):
            with ui.column().classes("gap-1"):
                with (
                    ui.row()
                    .classes("items-center w-full no-wrap gap-2")
                    .style("margin: -10px 0 0 -10px; width: calc(100% + 12px);")
                ):
                    mood = _status_mood()
                    self._robot_chip = ui.chip().style(_chip_style(mood))
                    with self._robot_chip:
                        self._buddy = (
                            RobotBuddy(
                                mood,
                                interactive=True,
                                sleep_after_s=_sleep_after(mood),
                            )
                            .style(
                                "margin-top: 4px;"
                                " filter: drop-shadow(0 1px 1px rgba(0,0,0,0.4));"
                            )
                            .mark("readout-robot-buddy")
                        )
                        with self._buddy:
                            self._buddy_tooltip = ui.tooltip(_tooltip(mood, None))
                        self._backend_label = (
                            ui.label(ui_state.active_robot.name)
                            .classes("text-lg font-medium ml-2")
                            .style("text-shadow: 0 1px 1px rgba(0,0,0,0.4);")
                        )
                    self._tool_separator = (
                        ui.label("\u00b7")
                        .classes("text-2xl font-bold")
                        .style("color: var(--ctk-muted);")
                    )
                    self._tool_separator.set_visibility(False)
                    self._tool_chip = (
                        ui.chip()
                        .props("dense")
                        .classes("text-lg font-medium")
                        .style("box-shadow: none; margin: 0;")
                    )
                    self._tool_chip.set_visibility(False)
                    self._tool_label: ui.label | None = None
                    with self._tool_chip:
                        self._tool_label = ui.label("").classes("text-lg font-medium")
                    ui.space()
                    with ui.row().classes("gap-0 no-wrap"):
                        self._io_chips = []
                        _io_init = waldoctl.commander.status.io
                        for i in range(len(_io_init.inputs)):
                            chip = (
                                ui.chip(f"DI{i + 1}", color=IO_COLOR_OFF)
                                .props("dense size=sm")
                                .classes("text-xs")
                                .style("box-shadow: none;")
                                .tooltip(f"Digital Input {i + 1}")
                            )
                            self._io_chips.append(chip)
                        for i in range(len(_io_init.outputs)):
                            chip = (
                                ui.chip(f"DO{i + 1}", color=IO_COLOR_OFF)
                                .props("dense size=sm")
                                .classes("text-xs")
                                .style("box-shadow: none;")
                                .tooltip(f"Digital Output {i + 1}")
                            )
                            self._io_chips.append(chip)

                with ui.row().classes("items-center justify-between w-full no-wrap"):
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label("X:").classes("text-sm tcp-x")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose, "x", backward=_fmt_1f
                            )
                            .classes("text-3xl tcp-x")
                            .style("min-width: 5rem; text-align: right;")
                            .mark("readout-x")
                        )
                        ui.label("mm").classes("text-xs tcp-x")

                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label("Y:").classes("text-sm tcp-y")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose, "y", backward=_fmt_1f
                            )
                            .classes("text-3xl tcp-y")
                            .style("min-width: 5rem; text-align: right;")
                            .mark("readout-y")
                        )
                        ui.label("mm").classes("text-xs tcp-y")

                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label("Z:").classes("text-sm tcp-z")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose, "z", backward=_fmt_1f
                            )
                            .classes("text-3xl tcp-z")
                            .style("min-width: 5rem; text-align: right;")
                            .mark("readout-z")
                        )
                        ui.label("mm").classes("text-xs tcp-z")

                with ui.row().classes("items-center w-full no-wrap"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Rx:").classes("text-xs tcp-rx")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose, "rx", backward=_fmt_1f
                            )
                            .classes("text-base tcp-rx")
                            .style("min-width: 3.5rem; text-align: right;")
                            .mark("readout-rx")
                        )
                        ui.label("°").classes("text-xs tcp-rx")

                    with ui.row().classes("items-center gap-1"):
                        ui.label("Ry:").classes("text-xs tcp-ry")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose, "ry", backward=_fmt_1f
                            )
                            .classes("text-base tcp-ry")
                            .style("min-width: 3.5rem; text-align: right;")
                            .mark("readout-ry")
                        )
                        ui.label("°").classes("text-xs tcp-ry")

                    with ui.row().classes("items-center gap-1"):
                        ui.label("Rz:").classes("text-xs tcp-rz")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose, "rz", backward=_fmt_1f
                            )
                            .classes("text-base tcp-rz")
                            .style("min-width: 3.5rem; text-align: right;")
                            .mark("readout-rz")
                        )
                        ui.label("°").classes("text-xs tcp-rz")

                    ui.space()

                    with ui.row().classes("items-center gap-1"):
                        ui.label("v:").classes("text-xs")
                        (
                            ui.label("-")
                            .bind_text_from(
                                waldoctl.commander.status.pose,
                                "tcp_speed",
                                backward=lambda v: f"{v:.0f}",
                            )
                            .classes("text-base")
                            .style("min-width: 2.5rem; text-align: right;")
                            .mark("readout-tcp-speed")
                        )
                        ui.label("mm/s").classes("text-xs")

                # Collapsible action log
                with (
                    ui.row()
                    .classes("items-center w-full no-wrap gap-0")
                    .mark("readout-action-log")
                ):
                    self._action_scroll_area = (
                        ui.scroll_area()
                        .classes("action-log flex-1")
                        .on("click", self._toggle_action_log)
                    )
                    with self._action_scroll_area:
                        self._action_log_html = ui.html("", sanitize=False).classes(
                            "w-full"
                        )

                # Collapsible warnings/errors log, hidden until something
                # actually lands in it.
                with (
                    ui.row()
                    .classes("items-center w-full no-wrap gap-0")
                    .mark("readout-event-log")
                ) as event_row:
                    self._event_log_row = event_row
                    self._event_scroll_area = (
                        ui.scroll_area()
                        .classes("action-log flex-1")
                        .on("click", self._toggle_event_log)
                    )
                    with self._event_scroll_area:
                        self._event_log_html = ui.html("", sanitize=False).classes(
                            "w-full"
                        )
                event_row.set_visibility(False)

                # Subscribe to action-log updates and seed the initial state
                # (conn_io is synced after URDF init in _init()).
                self._bind_action_log_listener()
                self.update_action_log()
                self.update_event_log()
