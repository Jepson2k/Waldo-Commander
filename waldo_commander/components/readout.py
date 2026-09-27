"""Status footer: mode, robot, tool, I/O, pose, last action and the event counts."""

import html as html_mod
import random
from enum import Enum
from pathlib import Path

import waldoctl
from nicegui import binding, ui
from nicegui.events import ValueChangeEventArguments
from waldoctl import ActionStatus

from waldo_commander.state import robot_events, ui_state


class RobotFace(Enum):
    """Robot face states for the connection status indicator."""

    HAPPY = "happy"
    NEUTRAL = "neutral"
    SAD = "sad"


# Load robot face SVGs at module level for inline rendering (CSS hover needs DOM access)
_ICONS_DIR = Path(__file__).parent.parent / "static" / "icons"
FACE_SVGS = {
    RobotFace.HAPPY: (_ICONS_DIR / "robot_happy.svg").read_text(),
    RobotFace.NEUTRAL: (_ICONS_DIR / "robot_neutral.svg").read_text(),
    RobotFace.SAD: (_ICONS_DIR / "robot_sad.svg").read_text(),
}
_FACE_WORDS = {
    RobotFace.HAPPY: "Connected",
    RobotFace.NEUTRAL: "Simulator",
    RobotFace.SAD: "Disconnected",
}
# Chip (fill, text) per face state; simulator is the app's amber mode colour.
_CHIP_COLORS = {
    RobotFace.HAPPY: ("wc-positive-soft", "wc-positive"),
    RobotFace.NEUTRAL: ("wc-mode-sim", "wc-on-bright"),
    RobotFace.SAD: ("wc-error-soft", "wc-error"),
}


def _current_face() -> RobotFace:
    status = waldoctl.commander.status
    if status.simulator_active:
        return RobotFace.NEUTRAL
    return RobotFace.HAPPY if status.connected else RobotFace.SAD


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
    """One row along the bottom: mode chip, robot, tool, I/O dots, pose well,
    the last action, and the buttons that open Diagnostics and the Log."""

    def __init__(self) -> None:
        self._robot_face_html: ui.html | None = None
        self._robot_face_container: ui.element | None = None
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

        self._last_face_state: RobotFace | None = None
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
                with ui.element("div").classes("io-dot") as dot:
                    ui.tooltip(f"{description} {i + 1}")
                self._io_dots.append(dot)

    def update_conn_io(self) -> None:
        """Update the mode chip, tool chip and I/O dots. Called from the status consumer."""
        if self._robot_face_html and self._robot_face_container:
            face = _current_face()
            if face != self._last_face_state:
                self._last_face_state = face
                self._robot_face_html.set_content(FACE_SVGS[face])
                remove = " ".join(
                    f"robot-face-{s.value}" for s in RobotFace if s != face
                )
                self._robot_face_container.classes(
                    add=f"robot-face-{face.value}", remove=remove
                )
                ui.run_javascript(
                    "window.stopRobotFace();"
                    " window.initRobotFace('" + face.value + "');"
                )
                if self._mode_word is not None:
                    self._mode_word.text = _FACE_WORDS[face]
                if self._robot_chip:
                    fill, text = _CHIP_COLORS[face]
                    self._robot_chip.props(f"color={fill} text-color={text}")

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
                with ui.element("div").classes(cell):
                    ui.label(axis.upper()).classes(f"wc-micro tcp-{axis}-text")
                    (
                        ui.label("-")
                        .bind_text_from(pose, axis, backward=_fmt_1f)
                        .classes(f"wc-caption pose-value tcp-{axis}-text")
                        .style(f"min-width: {width}")
                        .mark(f"readout-{axis}")
                    )
                    ui.label(unit).classes("wc-micro text-wc-text-muted")
            with ui.element("div").classes("pose-cell"):
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
        """Render the footer as one absolute row along the bottom edge."""
        # A component root: NiceGUI renders plain elements in their nearest
        # component's render, so pose updates on a bare div re-render the page.
        with (
            ui.element("q-card")
            .props("flat")
            .classes("status-footer")
            .mark("status-footer")
        ):
            face = _current_face()
            self._last_face_state = face
            fill, text = _CHIP_COLORS[face]
            self._robot_chip = (
                ui.chip()
                .props(f"dense color={fill} text-color={text}")
                .classes("footer-mode")
                .mark("footer-mode")
            )
            with self._robot_chip:
                self._robot_face_container = (
                    ui.element("div")
                    .classes(f"robot-face robot-face-{face.value}")
                    .mark("readout-robot-face")
                )
                with self._robot_face_container:
                    self._robot_face_html = ui.html(FACE_SVGS[face], sanitize=False)
                self._mode_word = ui.label(_FACE_WORDS[face]).classes("wc-micro")
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
                .classes("footer-btn")
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
