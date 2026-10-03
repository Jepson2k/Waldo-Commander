"""Status footer: mode, robot, tool, I/O, pose, last action and the event counts."""

import html as html_mod
import random
import time

import waldoctl
from nicegui import binding, ui
from nicegui.events import ValueChangeEventArguments
from waldoctl import ActionStatus

from waldo_commander.components.robot_buddy import Light, Mood, Reaction, RobotBuddy
from waldo_commander.services.control_lease import MCP, control_lease
from waldo_commander.services.programs import (
    is_any_program_recording,
    is_any_program_running,
)
from waldo_commander.state import robot_events, robot_state, ui_state

_MOOD_WORDS = {
    Mood.HAPPY: "Connected",
    Mood.NEUTRAL: "Simulator",
    Mood.SAD: "Disconnected",
    Mood.ALARMED: "E-STOP",
}
_LIGHT_WORDS = {
    Light.RECORDING: "Recording",
    Light.AGENT: "AI agent in control",
}
#: Status chip (fill, text) per mood; the simulator is the app's amber mode
#: colour. The buddy is drawn in the chip's text colour.
CHIP_COLORS = {
    Mood.HAPPY: ("wc-positive-soft", "wc-positive"),
    Mood.NEUTRAL: ("wc-mode-sim", "wc-on-bright"),
    Mood.SAD: ("wc-error-soft", "wc-error"),
    Mood.ALARMED: ("wc-error-soft", "wc-error"),
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
    text = _MOOD_WORDS[mood]
    return f"{text} · {_LIGHT_WORDS[light]}" if light else text


def _fmt_1f(v: float) -> str:
    return f"{v:.1f}"


# ---------------------------------------------------------------------------
# Action log HTML rendering
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    ActionStatus.EXECUTING: (
        '<span style="color:var(--wc-info)" '
        'class="material-icons q-spinner-mat action-icon">sync</span>'
    ),
    ActionStatus.COMPLETED: (
        '<span style="color:var(--wc-positive)" class="action-icon">✓</span>'
    ),
    ActionStatus.FAILED: (
        '<span style="color:var(--wc-error)" class="action-icon">✗</span>'
    ),
}


_TIPS = [
    "Press H to home the robot",
    "Press Esc for emergency stop",
    "Use [ and ] to adjust jog speed",
    "Click the last action for the history",
    "Press Space to play/pause the script",
    "Use WASD + Q/E to jog in cartesian space",
    "Set TCP offset in Settings for tool tips",
    "Press T to add a target at the current position",
    "Hold a jog key for continuous movement",
]
_TIP_TEXT = random.choice(_TIPS)
_TIP_HTML = (
    '<span style="color:var(--wc-warning)" class="material-icons action-icon">'
    "tips_and_updates</span>"
    f'<span style="color:var(--wc-text-muted)">{_TIP_TEXT}</span>'
)


def _entry_html(entry) -> str:
    icon = _STATUS_ICONS.get(entry.status, "")
    count = (
        f" <span style='color:var(--wc-text-muted)'>×{entry.count}</span>"
        if entry.count > 1
        else ""
    )
    params = ""
    if entry.params:
        params = (
            f' <span style="color:var(--wc-text-muted)">'
            f"{html_mod.escape(entry.params)}</span>"
        )
    return f"{icon}<b>{html_mod.escape(entry.command_name)}</b>{count}{params}"


def _build_log_entries_html() -> str:
    """The whole history, newest first, the tip of the day as the oldest entry."""
    parts = [
        f'<div class="action-log-entry">{_entry_html(entry)}</div>'
        for entry in reversed(waldoctl.commander.status.action.history)
    ]
    parts.append(f'<div class="action-log-entry">{_TIP_HTML}</div>')
    return "\n".join(parts)


class StatusFooter:
    """Live status in one desktop row or a wrapped footer below the controls."""

    def __init__(self) -> None:
        self._buddy: RobotBuddy | None = None
        self._buddy_tooltip: ui.tooltip | None = None
        self._robot_chip: ui.chip | None = None
        self._mode_word: ui.label | None = None
        self._tool_chip: ui.chip | None = None
        self._tool_label: ui.label | None = None
        self._io_dots: list[ui.element] = []
        self._io_container: ui.element | None = None

        self._action_line: ui.html | None = None
        self._history_menu: ui.menu | None = None
        self._history_html: ui.html | None = None
        self.events_button: ui.button | None = None

        self._moving_until: float = 0.0
        self._last_collision: bool = False
        self._last_homed: bool | None = None
        self._seen_jog_pos: list[bool] | None = None
        self._seen_jog_neg: list[bool] | None = None
        self._blocked_jogs: int = 0
        self._seen_events_version: int = 0
        self._last_tool_key: str | None = None
        self._last_io_inputs: list[int] | None = None
        self._last_io_outputs: list[int] | None = None
        self._unread_severity = ""
        binding.bind_from(self, "unread_severity", robot_events, "unread_severity")

    # ---- unread tint, bound from the event log ----

    @property
    def unread_severity(self) -> str:
        return self._unread_severity

    @unread_severity.setter
    def unread_severity(self, value: str) -> None:
        self._unread_severity = value
        if self.events_button is None:
            return
        self.events_button.classes(
            add=f"unread-{value}" if value else None,
            remove="unread-warning unread-error",
        )

    # ---- I/O ----

    def _build_io_dots(self) -> None:
        """One dot per digital line; rebuilt when the backend's line count changes."""
        if self._io_container is None:
            return
        self._io_container.clear()
        self._io_dots = []
        io = waldoctl.commander.status.io
        lines = [("Digital Input", i) for i in range(len(io.inputs))]
        lines += [("Digital Output", i) for i in range(len(io.outputs))]
        with self._io_container:
            for description, i in lines:
                if i == 0:
                    ui.label("IN" if description == "Digital Input" else "OUT").classes(
                        "footer-io-label wc-micro text-wc-text-muted"
                    )
                with ui.element("div").classes("io-dot") as dot:
                    ui.tooltip(f"{description} {i + 1}")
                self._io_dots.append(dot)

    def update_conn_io(self) -> None:
        """Update the mode chip and its buddy, the tool chip and the I/O dots.
        Called from the status consumer."""
        if self._buddy is not None:
            self._update_buddy()

        tool_key = waldoctl.commander.status.tool.key
        if tool_key != self._last_tool_key:
            self._last_tool_key = tool_key
            if self._tool_chip is not None and self._tool_label is not None:
                if tool_key and tool_key != "NONE":
                    try:
                        name = ui_state.active_robot.tools[
                            tool_key
                        ].display_name.replace("_", " ")
                    except KeyError:
                        name = tool_key.replace("_", " ")
                    self._tool_label.text = name
                    self._tool_label._props["title"] = tool_key
                    self._tool_chip.set_visibility(True)
                else:
                    self._tool_chip.set_visibility(False)

        if self._io_container is not None:
            io = waldoctl.commander.status.io
            inputs = io.inputs
            outputs = io.outputs
            # par6 takes both counts from its config, so the first frame may
            # not match what was on screen a moment ago.
            if len(inputs) + len(outputs) != len(self._io_dots):
                self._build_io_dots()
                self._last_io_inputs = None
                self._last_io_outputs = None
            if inputs != self._last_io_inputs or outputs != self._last_io_outputs:
                self._last_io_inputs = list(inputs)
                self._last_io_outputs = list(outputs)
                values = self._last_io_inputs + self._last_io_outputs
                for dot, on in zip(self._io_dots, values):
                    if on:
                        dot.classes(add="io-dot-on")
                    else:
                        dot.classes(remove="io-dot-on")

    # ---- buddy ----

    def _update_buddy(self) -> None:
        assert self._buddy is not None
        mood = _status_mood()
        light = _status_light()
        if mood != self._buddy.mood:
            self._buddy.set_mood(mood)
            self._buddy.set_sleep_after(_sleep_after(mood))
            if self._mode_word is not None:
                self._mode_word.text = _MOOD_WORDS[mood]
            if self._robot_chip is not None:
                fill, text = CHIP_COLORS[mood]
                self._robot_chip.props(f"color={fill} text-color={text}")
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

        # Only news startles the buddy: clearing the log bumps the version
        # too, and leaves it empty.
        if robot_events.version != self._seen_events_version:
            self._seen_events_version = robot_events.version
            if robot_events.entries:
                self._buddy.react(Reaction.STARTLE)

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
        # A held jog the panel ended at a limit stops short of where the
        # controller flags the direction, so it is counted where it ends.
        if robot_state.jog_limit_stops != self._seen_jog_limit_stops:
            self._seen_jog_limit_stops = robot_state.jog_limit_stops
            self._buddy.react(Reaction.SHRUG)

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

    def on_script_finished(self, completed: bool) -> None:
        """Cheer a program that ran to completion; wince at one that failed."""
        if self._buddy is not None:
            self._buddy.react(Reaction.CELEBRATE if completed else Reaction.OOPS)

    # ---- action line ----

    def update_action_log(self) -> None:
        """Redraw the last action, and the history while its menu is open."""
        if self._action_line is None:
            return
        latest = waldoctl.commander.status.action.latest
        self._action_line.set_content(_entry_html(latest) if latest else _TIP_HTML)
        if self._history_menu is not None and self._history_menu.value:
            self._draw_history()

    def _draw_history(self) -> None:
        if self._history_html is not None:
            self._history_html.set_content(_build_log_entries_html())

    def _on_history_toggle(self, e: ValueChangeEventArguments) -> None:
        # The whole history is tens of kilobytes; it is drawn for a reader.
        if e.value:
            self._draw_history()

    def _bind_action_log_listener(self) -> None:
        waldoctl.commander.status.action.add_change_listener(self.update_action_log)

    # ---- build ----

    @staticmethod
    def _open_bottom(tab: str) -> None:
        if ui_state.bottom_panel is not None:
            ui_state.bottom_panel.open(tab)

    @staticmethod
    def _open_settings() -> None:
        if ui_state.settings_content is not None:
            ui_state.settings_content.open()

    def _build_pose_well(self) -> None:
        pose = waldoctl.commander.status.pose
        with ui.element("div").classes("pose-well well"):
            for axis, unit, width, cell in (
                ("x", "mm", "3.6rem", "pose-cell"),
                ("y", "mm", "3.6rem", "pose-cell"),
                ("z", "mm", "3.6rem", "pose-cell"),
                ("rx", "°", "3.2rem", "pose-cell pose-cell-rot"),
                ("ry", "°", "3.2rem", "pose-cell pose-cell-rot"),
                ("rz", "°", "3.2rem", "pose-cell pose-cell-rot"),
            ):
                with ui.element("div").classes(f"{cell} footer-pose-{axis}"):
                    ui.label(axis.upper()).classes(f"wc-micro tcp-{axis}-text")
                    (
                        ui.label("-")
                        .bind_text_from(pose, axis, backward=_fmt_1f)
                        .classes(f"wc-caption pose-value tcp-{axis}-text")
                        .style(f"min-width: {width}")
                        .mark(f"readout-{axis}")
                    )
                    ui.label(unit).classes("wc-micro text-wc-text-muted")
            with ui.element("div").classes("pose-cell footer-speed"):
                ui.label("v").classes("wc-micro text-wc-text-muted")
                (
                    ui.label("-")
                    .bind_text_from(pose, "tcp_speed", backward=lambda v: f"{v:.0f}")
                    .classes("wc-caption pose-value")
                    .style("min-width: 2.2rem")
                    .mark("readout-tcp-speed")
                )
                ui.label("mm/s").classes("wc-micro text-wc-text-muted")

    def build(self) -> None:
        """Render the shared status elements for both workspace layouts."""
        # A component root: NiceGUI renders plain elements in their nearest
        # component's render, so pose updates on a bare div re-render the page.
        with (
            ui.element("q-card")
            .props("flat")
            .classes("status-footer")
            .mark("status-footer")
        ):
            mood = _status_mood()
            fill, text = CHIP_COLORS[mood]
            self._robot_chip = (
                ui.chip()
                .props(f"dense color={fill} text-color={text}")
                .classes("footer-mode")
                .mark("footer-mode")
            )
            with self._robot_chip:
                self._buddy = RobotBuddy(
                    mood,
                    size=20,
                    color="currentColor",
                    interactive=True,
                    sleep_after_s=_sleep_after(mood),
                ).mark("readout-robot-buddy")
                self._mode_word = ui.label(_MOOD_WORDS[mood]).classes("wc-micro")
                self._buddy_tooltip = ui.tooltip(_tooltip(mood, None))
            self._seen_events_version = robot_events.version
            self._seen_jog_limit_stops = robot_state.jog_limit_stops
            ui.label(ui_state.active_robot.name).classes("wc-label readout-robot-name")
            self._tool_chip = (
                ui.chip()
                .props("dense color=wc-control text-color=wc-text")
                .classes("footer-tool")
                .mark("footer-tool")
            )
            self._tool_chip.set_visibility(False)
            with self._tool_chip:
                self._tool_label = ui.label("").classes("wc-caption truncate")
            ui.label("No tool").classes(
                "footer-empty-tool wc-caption text-wc-text-muted"
            ).bind_visibility_from(
                waldoctl.commander.status.tool,
                "key",
                backward=lambda key: not key or key == "NONE",
            )
            ui.element("div").classes("footer-sep")
            self._io_container = ui.element("div").classes("io-dots").mark("io-dots")
            self._build_io_dots()
            ui.element("div").classes("footer-sep")
            self._build_pose_well()

            with (
                ui.element("div")
                .classes("footer-action wc-caption")
                .mark("readout-action-log")
            ):
                self._action_line = ui.html("", sanitize=False)
                with (
                    ui.menu()
                    .classes("footer-history")
                    .mark("footer-history")
                    .on_value_change(self._on_history_toggle) as self._history_menu
                ):
                    self._history_html = (
                        ui.html("", sanitize=False)
                        .classes("wc-caption")
                        .mark("footer-history-entries")
                    )

            self.events_button = (
                ui.button(on_click=lambda: self._open_bottom("diagnostics"))
                .props("flat dense no-caps color=wc-text")
                .classes("footer-btn")
                .mark("footer-events")
                .tooltip("Diagnostics")
            )
            with self.events_button:
                ui.icon("warning_amber")
                ui.label().bind_text_from(
                    robot_events, "warnings", backward=str
                ).classes("footer-count").mark("footer-warnings")
                ui.icon("error_outline")
                ui.label().bind_text_from(robot_events, "errors", backward=str).classes(
                    "footer-count"
                ).mark("footer-errors")
            self.unread_severity = robot_events.unread_severity

            (
                ui.button(icon="article", on_click=lambda: self._open_bottom("log"))
                .props("flat dense color=wc-text")
                .classes("footer-btn footer-log")
                .mark("footer-log")
                .tooltip("Log")
            )
            # The rail and its gear are hidden on a phone.
            (
                ui.button(icon="tune", on_click=self._open_settings)
                .props("flat dense color=wc-text")
                .classes("footer-btn footer-settings")
                .mark("footer-settings")
                .tooltip("Settings")
            )

        self._bind_action_log_listener()
        self.update_action_log()
