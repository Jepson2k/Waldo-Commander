"""Top-left readout panel component for robot pose and IO status display."""

import html as html_mod
import logging
import random
from enum import Enum
from pathlib import Path

from nicegui import ui

import waldoctl
from waldoctl import ActionStatus

from waldo_commander.common.theme import IO_COLOR_OFF, IO_COLOR_ON
from waldo_commander.state import robot_state, ui_state

logger = logging.getLogger(__name__)


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
_FACE_TOOLTIPS = {
    RobotFace.HAPPY: "Connected",
    RobotFace.NEUTRAL: "Simulator",
    RobotFace.SAD: "Disconnected",
}
# Chip background — darker hue of the face icon color
_CHIP_COLORS = {
    RobotFace.HAPPY: "var(--color-emerald-400)",
    RobotFace.NEUTRAL: "var(--color-gray-400)",
    RobotFace.SAD: "var(--color-red-400)",
}


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
        # Robot face + IO elements
        self._robot_face_html: ui.html | None = None
        self._robot_face_container: ui.element | None = None
        self._robot_face_tooltip: ui.tooltip | None = None
        self._robot_chip: ui.chip | None = None
        self._backend_label: ui.label | None = None
        self._tool_chip: ui.chip | None = None
        self._tool_label: ui.label | None = None
        self._tool_separator: ui.label | None = None
        self._io_chips: list[ui.chip] = []

        # Action log elements
        self._action_scroll_area: ui.scroll_area | None = None
        self._action_log_html: ui.html | None = None
        self._action_log_expanded: bool = False

        # Dirty checking state
        self._last_face_state: RobotFace | None = None
        self._last_tool_key: str | None = None
        self._last_io_inputs: list[int] | None = None
        self._last_io_outputs: list[int] | None = None

        # commander.status.action change-listener for the action log. Bound at
        # build time (when the scroll-area widget exists); kept here so cleanup
        # can remove it on shutdown.
        self._action_log_listener_bound: bool = False

    def update_conn_io(self) -> None:
        """Update connection face and IO status. Called from status consumer."""
        # Robot face — determine state from connection
        if self._robot_face_html and self._robot_face_container:
            sim_active = robot_state.simulator_active
            connected = robot_state.connected
            if sim_active:
                face = RobotFace.NEUTRAL
            elif connected:
                face = RobotFace.HAPPY
            else:
                face = RobotFace.SAD
            if face != self._last_face_state:
                self._last_face_state = face
                self._robot_face_html.set_content(FACE_SVGS[face])
                # Swap CSS class for breathing animation
                remove = " ".join(
                    f"robot-face-{s.value}" for s in RobotFace if s != face
                )
                self._robot_face_container.classes(
                    add=f"robot-face-{face.value}", remove=remove
                )
                self._robot_face_container.update()
                # Restart JS face animations for the new state
                ui.run_javascript(
                    "window.stopRobotFace();"
                    " window.initRobotFace('" + face.value + "');"
                )
                if self._robot_face_tooltip:
                    self._robot_face_tooltip.text = _FACE_TOOLTIPS[face]
                    self._robot_face_tooltip.update()
                if self._robot_chip:
                    self._robot_chip.style(
                        f"background-color: {_CHIP_COLORS[face]} !important;"
                        " margin: 0;"
                        " padding: 20px 12px !important;"
                        " box-shadow: none;"
                        " border-radius: 10px;"
                    )
                    self._robot_chip.update()

        # Tool chip — show "w/ <tool>" when a tool is active
        tool_key = robot_state.tool_key
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

        # IO chips — update colors when values change
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

        Called once from build time (after the scroll-area widget exists and
        after ``commander`` is registered). Idempotent.
        """
        if self._action_log_listener_bound:
            return
        waldoctl.commander.status.action.add_change_listener(self.update_action_log)
        self._action_log_listener_bound = True

    def _toggle_action_log(self) -> None:
        """Toggle action log between collapsed and expanded."""
        self._action_log_expanded = not self._action_log_expanded
        if self._action_scroll_area:
            if self._action_log_expanded:
                self._action_scroll_area.classes(add="action-log-expanded")
            else:
                self._action_scroll_area.classes(remove="action-log-expanded")

    def build(self, anchor: str = "tl") -> None:
        """Render the top-left readout panel as an overlay card."""
        with ui.card().classes(f"overlay-panel overlay-card overlay-{anchor}"):
            with ui.column().classes("gap-1"):
                # Connectivity + IO row — merged into panel corner
                with (
                    ui.row()
                    .classes("items-center w-full no-wrap gap-2")
                    .style("margin: -10px 0 0 -10px; width: calc(100% + 12px);")
                ):
                    _init_face = (
                        RobotFace.NEUTRAL
                        if robot_state.simulator_active
                        else RobotFace.HAPPY
                        if robot_state.connected
                        else RobotFace.SAD
                    )
                    self._last_face_state = _init_face
                    self._robot_chip = ui.chip().style(
                        f"background-color: {_CHIP_COLORS[_init_face]} !important;"
                        " margin: 0;"
                        " padding: 20px 12px !important;"
                        " box-shadow: none;"
                        " border-radius: 10px;"
                    )
                    with self._robot_chip:
                        self._robot_face_container = (
                            ui.element("div")
                            .classes(f"robot-face robot-face-{_init_face.value}")
                            .style(
                                "width: 36px; height: 36px;"
                                " margin-top: 4px;"
                                " filter: drop-shadow(0 1px 1px rgba(0,0,0,0.4));"
                            )
                            .mark("readout-robot-face")
                        )
                        with self._robot_face_container:
                            self._robot_face_html = ui.html(
                                FACE_SVGS[_init_face], sanitize=False
                            ).style("width: 36px; height: 36px")
                            self._robot_face_tooltip = ui.tooltip(
                                _FACE_TOOLTIPS[_init_face]
                            )
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
                    # Tool name chip (hidden when no tool)
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
                    # IO chips — single row
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

                # X/Y/Z row - larger text with mm units
                with ui.row().classes("items-center justify-between w-full no-wrap"):
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label("X:").classes("text-sm tcp-x")
                        (
                            ui.label("-")
                            .bind_text_from(robot_state, "x", backward=_fmt_1f)
                            .classes("text-3xl tcp-x")
                            .style("min-width: 5rem; text-align: right;")
                            .mark("readout-x")
                        )
                        ui.label("mm").classes("text-xs tcp-x")

                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label("Y:").classes("text-sm tcp-y")
                        (
                            ui.label("-")
                            .bind_text_from(robot_state, "y", backward=_fmt_1f)
                            .classes("text-3xl tcp-y")
                            .style("min-width: 5rem; text-align: right;")
                            .mark("readout-y")
                        )
                        ui.label("mm").classes("text-xs tcp-y")

                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.label("Z:").classes("text-sm tcp-z")
                        (
                            ui.label("-")
                            .bind_text_from(robot_state, "z", backward=_fmt_1f)
                            .classes("text-3xl tcp-z")
                            .style("min-width: 5rem; text-align: right;")
                            .mark("readout-z")
                        )
                        ui.label("mm").classes("text-xs tcp-z")

                # Rx/Ry/Rz + speed row
                with ui.row().classes("items-center w-full no-wrap"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Rx:").classes("text-xs tcp-rx")
                        (
                            ui.label("-")
                            .bind_text_from(robot_state, "rx", backward=_fmt_1f)
                            .classes("text-base tcp-rx")
                            .style("min-width: 3.5rem; text-align: right;")
                            .mark("readout-rx")
                        )
                        ui.label("°").classes("text-xs tcp-rx")

                    with ui.row().classes("items-center gap-1"):
                        ui.label("Ry:").classes("text-xs tcp-ry")
                        (
                            ui.label("-")
                            .bind_text_from(robot_state, "ry", backward=_fmt_1f)
                            .classes("text-base tcp-ry")
                            .style("min-width: 3.5rem; text-align: right;")
                            .mark("readout-ry")
                        )
                        ui.label("°").classes("text-xs tcp-ry")

                    with ui.row().classes("items-center gap-1"):
                        ui.label("Rz:").classes("text-xs tcp-rz")
                        (
                            ui.label("-")
                            .bind_text_from(robot_state, "rz", backward=_fmt_1f)
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
                                robot_state,
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

                # Subscribe to action-log updates and seed the initial state
                # (conn_io is synced after URDF init in _init()).
                self._bind_action_log_listener()
                self.update_action_log()
