"""Top-left readout panel component for robot pose and IO status display."""

import html as html_mod
import logging
import random
from enum import Enum
from pathlib import Path

import waldoctl
from nicegui import ui
from waldoctl import ActionStatus

from waldo_commander.state import ui_state

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
# Chip (fill, text) per face state; simulator is the app's amber mode colour.
_CHIP_COLORS = {
    RobotFace.HAPPY: ("wc-positive-soft", "wc-positive"),
    RobotFace.NEUTRAL: ("wc-mode-sim", "wc-on-bright"),
    RobotFace.SAD: ("wc-fill-error", "wc-on-fill"),
}
# I/O chip (fill, text) by pin state.
_IO_CHIP = {True: ("wc-action", "wc-on-bright"), False: ("wc-control", "wc-text")}


def _fmt_1f(v: float) -> str:
    """Format float with 1 decimal place."""
    return f"{v:.1f}"


# ---------------------------------------------------------------------------
# Action log HTML rendering
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    ActionStatus.EXECUTING: (
        '<span style="color:var(--wc-info);font-size:11px" '
        'class="material-icons q-spinner-mat">sync</span>'
    ),
    ActionStatus.COMPLETED: (
        '<span style="color:var(--wc-positive);font-size:13px">\u2713</span>'
    ),
    ActionStatus.FAILED: (
        '<span style="color:var(--wc-error);font-size:13px">\u2717</span>'
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
            f" <span style='color:var(--wc-text-muted)'>\u00d7{entry.count}</span>"
            if entry.count > 1
            else ""
        )
        params = ""
        if entry.params:
            params = (
                f' <span style="color:var(--wc-text-muted)">'
                f"{html_mod.escape(entry.params)}</span>"
            )
        name = html_mod.escape(entry.command_name)
        parts.append(
            f'<div class="action-log-entry" style="font-size:12px;line-height:1.5">'
            f"{icon} <b>{name}</b>{count}{params}</div>"
        )
    # Tip of the day as the oldest entry
    tip_icon = (
        '<span style="color:var(--wc-warning);font-size:13px"'
        ' class="material-icons">tips_and_updates</span>'
    )
    parts.append(
        f'<div class="action-log-entry" style="font-size:12px;line-height:1.5">'
        f'{tip_icon} <span style="color:var(--wc-text-muted)">{_TIP_TEXT}</span></div>'
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
        self._io_container: ui.row | None = None

        # Action log elements
        self._action_scroll_area: ui.scroll_area | None = None
        self._action_log_html: ui.html | None = None
        self._action_log_expanded: bool = False

        # Dirty checking state
        self._last_face_state: RobotFace | None = None
        self._last_tool_key: str | None = None
        self._last_io_inputs: list[int] | None = None
        self._last_io_outputs: list[int] | None = None

    def _build_io_chips(self) -> None:
        """Fill the I/O strip with one chip per digital line.

        The strip is a bounded block at the header's right edge: parol6 has
        four lines, but a backend that takes its I/O from config can report
        many more, so the chips wrap into rows inside the block and get
        smaller as the count grows rather than widening the panel or taking
        a line of their own.
        """
        if self._io_container is None:
            return
        self._io_container.clear()
        self._io_chips = []
        io = waldoctl.commander.status.io
        lines = [("DI", "Digital Input", i) for i in range(len(io.inputs))]
        lines += [("DO", "Digital Output", i) for i in range(len(io.outputs))]
        # Past a handful the prefix drops to one letter; tooltips keep the
        # full name either way.
        terse = len(lines) > 8
        size = "xs" if terse else "sm"
        if len(lines) > 16:
            self._io_container.classes(add="io-chips-dense")
        else:
            self._io_container.classes(remove="io-chips-dense")
        with self._io_container:
            for prefix, description, i in lines:
                label = f"{prefix[-1] if terse else prefix}{i + 1}"
                self._io_chips.append(
                    ui.chip(label)
                    .props(f"dense size={size} color=wc-control text-color=wc-text")
                    .style("box-shadow: none;")
                    .tooltip(f"{description} {i + 1}")
                )

    def update_conn_io(self) -> None:
        """Update connection face and IO status. Called from status consumer."""
        if self._robot_face_html and self._robot_face_container:
            sim_active = waldoctl.commander.status.simulator_active
            connected = waldoctl.commander.status.connected
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
                # Restart JS animations so the new face state animates
                ui.run_javascript(
                    "window.stopRobotFace();"
                    " window.initRobotFace('" + face.value + "');"
                )
                if self._robot_face_tooltip:
                    self._robot_face_tooltip.text = _FACE_TOOLTIPS[face]
                    self._robot_face_tooltip.update()
                if self._robot_chip:
                    fill, text = _CHIP_COLORS[face]
                    self._robot_chip.props(f"color={fill} text-color={text}")
                    self._robot_chip.update()

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
                    if self._tool_separator is not None:
                        self._tool_separator.set_visibility(True)
                else:
                    self._tool_chip.set_visibility(False)
                    if self._tool_separator is not None:
                        self._tool_separator.set_visibility(False)

        if self._io_container is not None:
            io = waldoctl.commander.status.io
            inputs = io.inputs
            outputs = io.outputs
            # A backend can report a different line count than the one the
            # chips were built for — par6 takes both from its config, so the
            # first frame may not match what was on screen a moment ago.
            if len(inputs) + len(outputs) != len(self._io_chips):
                self._build_io_chips()
                self._last_io_inputs = None
                self._last_io_outputs = None
            if inputs != self._last_io_inputs or outputs != self._last_io_outputs:
                self._last_io_inputs = list(inputs)
                self._last_io_outputs = list(outputs)
                all_vals = self._last_io_inputs + self._last_io_outputs
                for i, chip in enumerate(self._io_chips):
                    if i < len(all_vals):
                        fill, text = _IO_CHIP[bool(all_vals[i])]
                        chip.props(f"color={fill} text-color={text}")

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

    def build(self, anchor: str = "tl") -> None:
        """Render the top-left readout panel as an overlay card."""
        with ui.card().classes(
            f"overlay-panel overlay-card readout-panel overlay-{anchor}"
        ):
            with ui.column().classes("gap-1"):
                with (
                    ui.row()
                    .classes("readout-header items-center w-full no-wrap gap-2")
                    .style("margin: -12px 0 0 -12px; width: calc(100% + 16px);")
                ):
                    _init_face = (
                        RobotFace.NEUTRAL
                        if waldoctl.commander.status.simulator_active
                        else RobotFace.HAPPY
                        if waldoctl.commander.status.connected
                        else RobotFace.SAD
                    )
                    self._last_face_state = _init_face
                    fill, text = _CHIP_COLORS[_init_face]
                    self._robot_chip = (
                        ui.chip()
                        .props(f"color={fill} text-color={text}")
                        .style(
                            "margin: 0;"
                            " padding: 20px 12px !important;"
                            " box-shadow: none;"
                            " border-radius: var(--wc-radius-md);"
                        )
                    )
                    with self._robot_chip:
                        self._robot_face_container = (
                            ui.element("div")
                            .classes(f"robot-face robot-face-{_init_face.value}")
                            .style("width: 36px; height: 36px; margin-top: 4px;")
                            .mark("readout-robot-face")
                        )
                        with self._robot_face_container:
                            self._robot_face_html = ui.html(
                                FACE_SVGS[_init_face], sanitize=False
                            ).style("width: 36px; height: 36px")
                            self._robot_face_tooltip = ui.tooltip(
                                _FACE_TOOLTIPS[_init_face]
                            )
                        self._backend_label = ui.label(
                            ui_state.active_robot.name
                        ).classes("text-lg font-medium ml-2 readout-robot-name")
                    self._tool_separator = ui.label("\u00b7").classes(
                        "text-2xl font-bold text-wc-text-muted"
                    )
                    self._tool_separator.set_visibility(False)
                    self._tool_chip = (
                        ui.chip()
                        .props("dense color=wc-control text-color=wc-text")
                        .classes("text-sm font-medium min-w-0")
                        .style("box-shadow: none; margin: 0; max-width: 170px;")
                    )
                    self._tool_chip.set_visibility(False)
                    self._tool_label: ui.label | None = None
                    with self._tool_chip:
                        self._tool_label = ui.label("").classes(
                            "text-sm font-medium truncate"
                        )
                    ui.space()
                    self._io_container = ui.row().classes("io-chips")
                    self._build_io_chips()

                with ui.column().classes("gap-0 py-1 w-full"):
                    with ui.row().classes(
                        "items-center justify-between w-full no-wrap"
                    ):
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.label("X:").classes("text-sm tcp-x-text")
                            (
                                ui.label("-")
                                .bind_text_from(
                                    waldoctl.commander.status.pose,
                                    "x",
                                    backward=_fmt_1f,
                                )
                                .classes("text-3xl tabular-nums tcp-x-text")
                                .style("min-width: 5rem; text-align: right;")
                                .mark("readout-x")
                            )
                            ui.label("mm").classes("text-xs tcp-x-text")

                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.label("Y:").classes("text-sm tcp-y-text")
                            (
                                ui.label("-")
                                .bind_text_from(
                                    waldoctl.commander.status.pose,
                                    "y",
                                    backward=_fmt_1f,
                                )
                                .classes("text-3xl tabular-nums tcp-y-text")
                                .style("min-width: 5rem; text-align: right;")
                                .mark("readout-y")
                            )
                            ui.label("mm").classes("text-xs tcp-y-text")

                        with ui.row().classes("items-center gap-1 no-wrap"):
                            ui.label("Z:").classes("text-sm tcp-z-text")
                            (
                                ui.label("-")
                                .bind_text_from(
                                    waldoctl.commander.status.pose,
                                    "z",
                                    backward=_fmt_1f,
                                )
                                .classes("text-3xl tabular-nums tcp-z-text")
                                .style("min-width: 5rem; text-align: right;")
                                .mark("readout-z")
                            )
                            ui.label("mm").classes("text-xs tcp-z-text")

                    with ui.row().classes("items-center w-full no-wrap"):
                        with ui.row().classes("items-center gap-1"):
                            ui.label("Rx:").classes("text-xs tcp-rx-text")
                            (
                                ui.label("-")
                                .bind_text_from(
                                    waldoctl.commander.status.pose,
                                    "rx",
                                    backward=_fmt_1f,
                                )
                                .classes("text-base tabular-nums tcp-rx-text")
                                .style("min-width: 3.5rem; text-align: right;")
                                .mark("readout-rx")
                            )
                            ui.label("°").classes("text-xs tcp-rx-text")

                        with ui.row().classes("items-center gap-1"):
                            ui.label("Ry:").classes("text-xs tcp-ry-text")
                            (
                                ui.label("-")
                                .bind_text_from(
                                    waldoctl.commander.status.pose,
                                    "ry",
                                    backward=_fmt_1f,
                                )
                                .classes("text-base tabular-nums tcp-ry-text")
                                .style("min-width: 3.5rem; text-align: right;")
                                .mark("readout-ry")
                            )
                            ui.label("°").classes("text-xs tcp-ry-text")

                        with ui.row().classes("items-center gap-1"):
                            ui.label("Rz:").classes("text-xs tcp-rz-text")
                            (
                                ui.label("-")
                                .bind_text_from(
                                    waldoctl.commander.status.pose,
                                    "rz",
                                    backward=_fmt_1f,
                                )
                                .classes("text-base tabular-nums tcp-rz-text")
                                .style("min-width: 3.5rem; text-align: right;")
                                .mark("readout-rz")
                            )
                            ui.label("°").classes("text-xs tcp-rz-text")

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

                # Subscribe to action-log updates and seed the initial state
                # (conn_io is synced after URDF init in _init()).
                self._bind_action_log_listener()
                self.update_action_log()
