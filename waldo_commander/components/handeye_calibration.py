"""Calibrate a camera on the tool or fixed in the workspace.

One flow of three steps: place the board, let the auto-capture run acquire
stationary robot poses paired with fresh ChArUco images, then save the named
setup data. The solve runs by itself whenever the views change and at least
four exist; a coverage ring shows where the views came from and where the
next one should.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import math
import time
from enum import Enum, auto
from typing import Literal, cast

import numpy as np
import waldoctl
from nicegui import Client, background_tasks, context, run, ui
from nicegui import app as ng_app
from scipy.spatial.transform import Rotation
from waldoctl import Commander, Panel, PanelSlot
from waldoctl.camera import CameraCalibration
from waldoctl.setup import Pose, SetupSnapshot, TcpCalibration

from waldo_commander.camera import CameraUnavailable
from waldo_commander.common.theme import SceneColors, hex_of
from waldo_commander.components.camera_calibration_data import CameraCalibrationData
from waldo_commander.services import handeye
from waldo_commander.services.camera_calibration import (
    CaptureBinding,
    calibration_from_result,
)
from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.services.camera_service import (
    camera_service,
    enumerate_video_devices,
)
from waldo_commander.services.control_lease import (
    BROWSER,
    control_lease,
    require_browser_control,
)
from waldo_commander.services.motion_guard import (
    CALIBRATION,
    MotionBusy,
    Reservation,
    motion_guard,
)
from waldo_commander.services.tcp_calibration import observe_tcp, read_applied_tcp
from waldo_commander.state import robot_state

logger = logging.getLogger(__name__)

STATIONARY_SPEED_DEG_S = 0.5
SOLVE_MIN_SAMPLES = handeye.MIN_SAMPLES
# The views must hold still this long before the automatic solve runs.
SOLVE_DEBOUNCE_S = 0.5
DETECT_INTERVAL_S = 0.2
# Consecutive undecodable frames before surfacing a camera-format hint.
DECODE_FAILURE_HINT = 15
SCENE_GROUP = "handeye-camera"
FRUSTUM_DEPTH_MM = 120.0

_QUALITY_RMS_PX = (1.0, 2.0)
_QUALITY_SPREAD_MM = (2.0, 5.0)

_SECTOR_PHRASE = {
    "up": "from above",
    "down": "from below",
    "left": "from the left",
    "right": "from the right",
    "up-left": "from the upper left",
    "up-right": "from the upper right",
    "down-left": "from the lower left",
    "down-right": "from the lower right",
}

# Auto-calibration pose set: joint deltas (deg) from the start pose. The
# board is fixed in the workspace, so large rotations are only possible about
# the camera's optical axis (J4/J6 rolls); J5 tilts swing the board toward
# the FOV edge and must stay small, and J1-J3 moves translate the camera to
# vary the viewing distance. With the MSG gripper attached, negative J5 folds
# its body toward the forearm and the controller rejects the move as a
# predicted self-collision, so J5 deltas stay non-negative and J2/J3 depth
# excursions within roughly -10..+6 deg of the standby pose. The order keeps
# large J4 swings at start-pose J2/J3 and walks depth in small steps so
# consecutive segments clear the collision margin structurally. A pose the
# controller still rejects (other tools, different start pose) is skipped,
# not fatal.
AUTO_VIEW_DELTAS_DEG: tuple[tuple[float, float, float, float, float, float], ...] = (
    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0, 0.0, 35.0),
    (0.0, 5.0, -7.0, 0.0, 0.0, -30.0),
    (0.0, 6.0, -8.0, 0.0, 2.0, 20.0),
    (0.0, -5.0, 7.0, 0.0, 7.0, 15.0),
    (0.0, 0.0, 0.0, 12.0, 7.0, -20.0),
    (0.0, 0.0, 0.0, 18.0, 7.0, 20.0),
    (0.0, 0.0, 0.0, -12.0, 5.0, -30.0),
    (0.0, 4.0, -5.0, -12.0, 5.0, 0.0),
    (4.0, -3.0, 4.0, 0.0, 6.0, 25.0),
    (0.0, -8.0, 11.0, 0.0, 5.0, 0.0),
    (0.0, -6.0, 8.0, 6.0, 6.0, 30.0),
    (-6.0, -6.0, 8.0, 0.0, 6.0, -25.0),
    (-5.0, -10.0, 14.0, 0.0, 8.0, 25.0),
    (0.0, -5.0, 7.0, 0.0, 6.0, -15.0),
)
# The one figure the panel counts views against: enough for a good fit, and
# what an auto-capture run attempts.
TARGET_VIEWS = len(AUTO_VIEW_DELTAS_DEG)

# Autonomous moves are deliberately slow: duration is sized so the fastest
# joint stays under AUTO_DEG_PER_S. Tests lower these for the simulator.
#: Largest printable square \[mm\]. The board is rendered at print
#: resolution, so square size drives the pixel count quadratically; past
#: this a "download" is a multi-megapixel encode nobody asked for.
MAX_SQUARE_MM = 100.0

AUTO_DEG_PER_S = 15.0
AUTO_MIN_MOVE_S = 1.5
AUTO_MOVE_TIMEOUT_MARGIN_S = 10.0
AUTO_WAIT_SLICE_S = 0.25
AUTO_STATIONARY_TIMEOUT_S = 4.0
# Post-motion settle before capturing, so the cached camera frame postdates
# the end of the move (one frame period + one capture-loop period, padded).
AUTO_SETTLE_S = 0.5
AUTO_CAPTURE_ATTEMPTS = 3
AUTO_CAPTURE_RETRY_S = 0.6
AUTO_MAX_CONSECUTIVE_REJECTS = 3
# Largest tool motion between the poses read before and after a capture.
# Rotation has its own limit: turning about the tool origin moves it by nothing.
STILL_TRANSLATION_MM = 0.05
STILL_ROTATION_DEG = 0.05


class _CaptureRefused(Exception):
    """A sample could not be taken. ``fatal`` marks conditions that
    invalidate the whole capture set (tool/resolution changed, robot
    unreachable) rather than just this attempt."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class _Move(Enum):
    """How one automatic move ended."""

    DONE = auto()
    REJECTED = auto()  # refused by the controller; nothing is moving
    HALTED = auto()  # the run was halted around it; the run stops the robot
    UNCONFIRMED = auto()  # no confirmed end; the robot was stopped


_PANEL_STOP = "stopped from the panel"


class _AutoRun:
    """One automatic calibration run's halt latch.

    The run halts on the panel's Stop, any stop Commander sends or sees, the
    page that started it disconnecting, or another client taking control.
    The first reason sticks, so a reconnect or a returned lease never
    resumes the sweep.
    """

    def __init__(self, page_client: Client) -> None:
        self.page_client = page_client
        self.reason: str | None = None
        self.wake = asyncio.Event()
        self._generation = motion_guard.stop_generation

    def halt(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.wake.set()

    def halted(self) -> bool:
        if self.reason is None:
            if motion_guard.stop_generation != self._generation:
                self.reason = "the robot was stopped"
            elif not self.page_client.has_socket_connection:
                self.reason = "the page disconnected"
            elif not control_lease.held_by(BROWSER, self.page_client.id):
                self.reason = "another client took control"
        return self.reason is not None

    async def sleep(self, seconds: float) -> None:
        """Sleep, returning early when a stop wakes the run."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self.wake.wait()


def _selected_tool_key() -> str:
    return ng_app.storage.general.get("selected_tool", "NONE")


def _verdict(result: handeye.HandEyeResult) -> tuple[str, str, str]:
    """(headline, step summary, tone class) for a solve. Colour appears only
    when the fit is not to be trusted."""
    rms = result.intrinsics.reproj_rms_px
    spread = result.target_spread_mm
    if rms < _QUALITY_RMS_PX[0] and spread < _QUALITY_SPREAD_MM[0]:
        return "Good fit", "good fit · ready to save", ""
    if rms < _QUALITY_RMS_PX[1] and spread < _QUALITY_SPREAD_MM[1]:
        return "Usable fit", "usable fit · ready to save", "text-wc-warning"
    return (
        "Poor fit; recapture the flagged views",
        "poor fit: recapture the flagged views",
        "text-wc-error",
    )


def _wedge(sector: int) -> str:
    """clip-path for one tilt sector: a 42° wedge from the centre past the
    rim (the circular container clips it), leaving a gap to its neighbours."""
    centre = sector * 45.0
    points = ["50% 50%"] + [
        f"{50 + 80 * math.cos(math.radians(a)):.1f}% "
        f"{50 + 80 * math.sin(math.radians(a)):.1f}%"
        for a in (centre - 21.0, centre, centre + 21.0)
    ]
    return "polygon(" + ", ".join(points) + ")"


class _CoverageRing:
    """Where the views came from: a rim filling toward the target count,
    eight tilt sectors around a 3x3 map of the frame, and the sector to fill
    next glowing."""

    def __init__(self) -> None:
        self._last: tuple | None = None
        self.element = ui.element("div").classes("handeye-coverage")
        self.element.mark("handeye-coverage")
        with self.element:
            with ui.element("div").classes("handeye-coverage-sectors"):
                self._sectors = [
                    ui.element("div")
                    .classes("handeye-coverage-sector cov-0")
                    .style(f"clip-path: {_wedge(k)}")
                    .mark(f"handeye-coverage-sector-{k}")
                    for k in range(len(handeye.SECTORS))
                ]
            with ui.element("div").classes("handeye-coverage-cells"):
                self._cells = [
                    ui.element("div")
                    .classes("handeye-coverage-cell cov-0")
                    .mark(f"handeye-coverage-cell-{i}")
                    for i in range(len(handeye.CELLS))
                ]

    def update(self, cov: handeye.Coverage, n: int, target: int) -> None:
        complete = n >= target
        state = (cov, n, complete)
        if state == self._last:
            return
        self._last = state
        self.element.style(f"--wc-coverage-progress: {min(n / target, 1.0):.3f}")
        if complete:
            self.element.classes(add="handeye-coverage-complete")
        else:
            self.element.classes(remove="handeye-coverage-complete")
        glow = (
            None
            if complete or cov.next_sector is None
            else handeye.SECTORS.index(cov.next_sector)
        )
        for k, sector in enumerate(self._sectors):
            sector.classes(
                replace=f"handeye-coverage-sector cov-{min(cov.sectors[k], 3)}"
                + (" handeye-coverage-next" if k == glow else "")
            )
        for i, cell in enumerate(self._cells):
            cell.classes(replace=f"handeye-coverage-cell cov-{min(cov.cells[i], 3)}")


class HandEyeCalibrationPanel(Panel):
    id = "handeye"
    display_name = "Hand-Eye Calibration"
    slot = PanelSlot.LEFT_TOP_TAB
    tab_icon = "center_focus_strong"
    tab_tooltip = "Hand-eye calibration"
    order = 50
    # Drag-resizable pane: as tall as its content until dragged, and the
    # chosen size persists. The live view scales to the pane, so a
    # high-resolution tool camera can't balloon the layout.
    min_width = 440
    min_height = 320
    default_width = 540
    resizable = True

    def __init__(self) -> None:
        stored = ng_app.storage.general.get("handeye/board")
        try:
            self._spec = (
                handeye.BoardSpec.from_dict(stored) if stored else handeye.BoardSpec()
            )
            # Storage can hold a board that make_detector would refuse.
            self._spec.validate()
        except (KeyError, TypeError, ValueError, handeye.CalibrationError):
            self._spec = handeye.BoardSpec()
        self._detector = handeye.make_detector(self._spec)
        self._samples: list[handeye.HandEyeSample] = []
        self._sample_tool_key: str | None = None
        self._sample_binding: CaptureBinding | None = None
        self._mount: Literal["tool", "fixed"] = "tool"
        self._sample_revision = 0
        self._solving = False
        self._solve_later: asyncio.TimerHandle | None = None
        self._solve_error: str | None = None
        self._method = "PARK"
        self._solved: handeye.HandEyeResult | None = None
        self._last_detection: handeye.Detection | None = None
        self._detect_busy = False
        self._decode_failures = 0
        self._camera_was_active = camera_service.active
        self._auto_task: asyncio.Task | None = None
        self._run: _AutoRun | None = None
        # The panel is shared by every page, so this covers them all.
        self._auto_confirming = False
        self._auto_progress_text: str | None = None
        self._step = "board"
        self._advanced = False
        self._saved = False
        self._reset_element_refs()

    @property
    def _result(self) -> handeye.HandEyeResult | None:
        return self._solved

    @_result.setter
    def _result(self, result: handeye.HandEyeResult | None) -> None:
        # Save stores one solve; any other result has not been saved.
        if result is not self._solved:
            self._saved = False
        self._solved = result

    def _reset_element_refs(self) -> None:
        self._image: ui.interactive_image | None = None
        self._camera_card: ui.element | None = None
        self._camera_hint: ui.row | None = None
        self._status_label: ui.label | None = None
        self._solve_btn: ui.button | None = None
        self._save_btn: ui.button | None = None
        self._sample_count: ui.label | None = None
        self._ring: _CoverageRing | None = None
        self._views_expand: ui.expansion | None = None
        self._views_grid: ui.element | None = None
        self._view_tiles: list[
            tuple[handeye.HandEyeSample, ui.element, ui.tooltip]
        ] = []
        self._diversity_label: ui.label | None = None
        self._step_headers: dict[str, ui.button] = {}
        self._step_icons: dict[str, ui.icon] = {}
        self._step_summaries: dict[str, ui.label] = {}
        self._step_bodies: dict[str, ui.column] = {}
        self._last_stage: tuple | None = None
        self._result_container: ui.column | None = None
        self._stored_container: ui.column | None = None
        self._scene_switch: ui.switch | None = None
        self._commander: Commander | None = None
        self._camera_hint_label: ui.label | None = None
        self._last_status_text: str | None = None
        self._last_overlay_content: str | None = None
        self._last_hint_text: str | None = None
        self._last_stored_tool: str | None = None
        self._auto_btn: ui.button | None = None
        self._clear_btn: ui.button | None = None
        self._auto_progress_label: ui.label | None = None
        self._last_auto_running: bool | None = None
        self._data_editor: CameraCalibrationData | None = None

    @property
    def _auto_running(self) -> bool:
        return self._auto_task is not None and not self._auto_task.done()

    # ------------------------------------------------------------------ build

    def build(self, commander: Commander) -> None:
        self._reset_element_refs()
        self._commander = commander

        with ui.column().classes(
            "camera-panel-scroll w-full h-full min-h-0 flex-nowrap overflow-y-auto overflow-x-hidden gap-1"
        ):
            with ui.row().classes("w-full items-center"):
                ui.label("Camera calibration").classes("panel-heading")
                ui.space()
                ui.label().bind_text_from(
                    ng_app.storage.general,
                    "selected_tool",
                    lambda t: f"Tool: {t}" if t and t != "NONE" else "No tool",
                ).classes("text-caption text-wc-text-muted")
            steps = (
                ("board", "Board", self._build_board_step),
                ("views", "Views", self._build_views_step),
                ("save", "Save", lambda: self._build_save_section(commander)),
            )
            for key, name, builder in steps:
                self._build_step_header(key, name)
                self._step_bodies[key] = ui.column().classes(
                    "handeye-step-body w-full gap-2"
                )
                with self._step_bodies[key]:
                    builder()

        ui.timer(DETECT_INTERVAL_S, self._detect_tick)
        self._advanced = bool(self._samples)
        self._open_step("views" if self._samples else "board")
        self._refresh_samples()
        self._refresh_stored()

    def _build_step_header(self, key: str, name: str) -> None:
        button = (
            ui.button(on_click=lambda k=key: self._open_step(k))
            .props("flat no-caps align=left")
            .classes("handeye-step")
            .mark(f"handeye-step-{key}")
        )
        with button:
            self._step_icons[key] = ui.icon("radio_button_unchecked").classes(
                "handeye-step-icon"
            )
            ui.label(name).classes("handeye-step-name")
            self._step_summaries[key] = (
                ui.label()
                .classes("handeye-step-summary")
                .mark(f"handeye-step-summary-{key}")
            )
        self._step_headers[key] = button
        # A refresh earlier in the build never wrote this header.
        self._last_stage = None

    def _open_step(self, key: str) -> None:
        self._step = key
        for k, body in self._step_bodies.items():
            body.set_visibility(k == key)
        for k, header in self._step_headers.items():
            if k == key:
                header.classes(add="handeye-step-open")
            else:
                header.classes(remove="handeye-step-open")
        self._refresh_stage()

    def _build_board_step(self) -> None:
        self._build_camera_hint()
        self._build_mount_controls()
        self._build_board_section()

    def _build_views_step(self) -> None:
        self._build_camera_section()
        self._build_views_section()

    def _build_camera_hint(self) -> None:
        self._camera_hint = ui.row().classes("items-center")
        with self._camera_hint:
            ui.icon("videocam_off").classes("text-wc-text-muted")
            self._camera_hint_label = ui.label(self._camera_hint_text()).classes(
                "text-caption text-wc-text-muted"
            )

    def _build_mount_controls(self) -> None:
        self._mount_select = (
            ui.select(
                {"tool": "Camera on tool", "fixed": "Fixed camera"},
                value=self._mount,
                label="Camera placement",
                on_change=self._change_mount,
            )
            .props("dense")
            .classes("w-full")
            .mark("camera-mount")
        )
        self._mount_hint = ui.label().classes("text-caption")
        self._fixed_controls = ui.row().classes("items-center w-full")
        with self._fixed_controls:
            device = (
                ui.select({}, label="Fixed camera device")
                .props("dense")
                .classes("flex-1")
                .mark("fixed-camera-device")
            )

            async def scan() -> None:
                devices = await run.io_bound(enumerate_video_devices) or []
                device.set_options(
                    {entry["index"]: entry["label"] for entry in devices}
                )

            def start() -> None:
                if self._auto_running:
                    ui.notify(
                        "Stop calibration before changing the camera", color="warning"
                    )
                elif device.value is not None:
                    camera_service.start(device.value)

            ui.button("Scan", on_click=scan).props("dense flat")
            ui.button("Start camera", on_click=start).props("dense outline").mark(
                "fixed-camera-start"
            )
        self._refresh_mount()

    def _change_mount(self, event) -> None:
        if event.value == self._mount:
            return
        if self._auto_running or self._samples:
            self._mount_select.set_value(self._mount)
            ui.notify("Clear samples before changing camera placement", color="warning")
            return
        self._mount = cast(Literal["tool", "fixed"], event.value)
        self._result = None
        self._refresh_mount()
        self._refresh_stage()

    def _refresh_mount(self) -> None:
        self._fixed_controls.set_visibility(self._mount == "fixed")
        self._mount_hint.set_text(
            "Keep the camera fixed; attach the board rigidly to the tool and capture varied wrist orientations."
            if self._mount == "fixed"
            else "Keep the board fixed in the workspace; move the tool-mounted camera through varied orientations."
        )

    def _build_board_section(self) -> None:
        with ui.expansion("Target board", icon="grid_on").classes("w-full"):
            with ui.row().classes("items-end gap-2"):
                sx = (
                    ui.number(
                        "Squares X",
                        value=self._spec.squares_x,
                        min=3,
                        max=20,
                        precision=0,
                    )
                    .classes("w-20")
                    .mark("handeye-squares-x")
                )
                sy = (
                    ui.number(
                        "Squares Y",
                        value=self._spec.squares_y,
                        min=3,
                        max=20,
                        precision=0,
                    )
                    .classes("w-20")
                    .mark("handeye-squares-y")
                )
                sq = (
                    ui.number(
                        "Square mm",
                        value=self._spec.square_mm,
                        min=5.0,
                        max=MAX_SQUARE_MM,
                        step=0.5,
                    )
                    .classes("w-24")
                    .mark("handeye-square-mm")
                )
                mk = (
                    ui.number(
                        "Marker mm", value=self._spec.marker_mm, min=3.0, step=0.5
                    )
                    .classes("w-24")
                    .mark("handeye-marker-mm")
                )
                dic = (
                    ui.select(
                        list(handeye.ARUCO_DICTIONARIES),
                        value=self._spec.dictionary,
                        label="Dictionary",
                    )
                    .classes("w-32")
                    .mark("handeye-dictionary")
                )

            def current_inputs() -> handeye.BoardSpec:
                """The board the five inputs describe, treating an emptied
                field as unchanged.

                NiceGUI sets a number's value to None the moment its text
                is cleared, which is what selecting a field to retype it
                does. Parsing that raises TypeError, which the caller does
                not catch, so the revert never runs and the field stays
                blank — after which every later edit to any of the five
                re-raises through it.
                """

                def num(el, fallback, cast):
                    return fallback if el.value is None else cast(el.value)

                return handeye.BoardSpec(
                    squares_x=num(sx, self._spec.squares_x, int),
                    squares_y=num(sy, self._spec.squares_y, int),
                    square_mm=num(sq, self._spec.square_mm, float),
                    marker_mm=num(mk, self._spec.marker_mm, float),
                    dictionary=str(dic.value or self._spec.dictionary),
                )

            def revert_inputs() -> None:
                sx.value = self._spec.squares_x
                sy.value = self._spec.squares_y
                sq.value = self._spec.square_mm
                mk.value = self._spec.marker_mm
                dic.value = self._spec.dictionary

            async def apply_spec() -> None:
                try:
                    spec = current_inputs()
                    detector = handeye.make_detector(spec)
                except handeye.CalibrationError as e:
                    ui.notify(str(e), color="negative")
                    revert_inputs()
                    return
                if spec == self._spec:
                    return
                if self._auto_running:
                    ui.notify(
                        "Auto-calibration is running — stop it first", color="warning"
                    )
                    revert_inputs()
                    return
                if self._samples:
                    with ui.dialog() as dialog, ui.card():
                        ui.label(
                            f"Changing the board invalidates {len(self._samples)} "
                            "captured samples. Clear them and continue?"
                        )
                        with ui.row():
                            ui.button(
                                "Cancel", on_click=lambda: dialog.submit(False)
                            ).props("flat color=wc-text")
                            ui.button(
                                "Clear & apply", on_click=lambda: dialog.submit(True)
                            ).props("color=wc-control text-color=wc-error").mark(
                                "handeye-board-apply-confirm"
                            )
                    if not await dialog:
                        revert_inputs()
                        return
                    if self._auto_running:
                        ui.notify(
                            "Auto-calibration is running — stop it first",
                            color="warning",
                        )
                        revert_inputs()
                        return
                    self._clear_samples()
                    self._refresh_samples()
                self._spec = spec
                self._detector = detector
                ng_app.storage.general["handeye/board"] = spec.to_dict()
                self._refresh_stage()

            for el in (sx, sy, sq, mk, dic):
                el.on_value_change(apply_spec)

            async def download_board() -> None:
                """Render and encode off the event loop.

                A board is rendered at print resolution, so the PNG runs to
                megapixels and its encode is long enough to stall every
                other client for the duration if it runs here.
                """
                spec = self._spec
                data = await run.cpu_bound(handeye.board_png, spec)
                ui.download(
                    data,
                    f"charuco_{spec.squares_x}x{spec.squares_y}"
                    f"_{spec.square_mm:g}mm_{spec.marker_mm:g}mm"
                    f"_{spec.dictionary}.png",
                )

            with ui.row().classes("items-center"):
                ui.button(
                    "Download board PNG", icon="download", on_click=download_board
                ).props("outline dense").mark("handeye-board-download")
                ui.label(
                    "Print at 100% scale, then measure a printed square and "
                    "correct 'Square mm' if it differs."
                ).classes("text-caption text-wc-text-muted")

    def _build_camera_section(self) -> None:
        self._camera_card = ui.element("div").classes("handeye-camera-frame")
        with self._camera_card:
            self._image = ui.interactive_image("/tool/camera/stream").classes(
                "handeye-camera"
            )
            self._image.mark("handeye-camera")
            self._status_label = ui.label("No board detected").classes(
                "handeye-camera-chip"
            )
            self._status_label.mark("handeye-detect-status")
        self._set_camera_visibility(camera_service.active)

    def _build_views_section(self) -> None:
        with ui.row().classes("w-full items-center no-wrap gap-3"):
            self._ring = _CoverageRing()
            with ui.column().classes("flex-1 min-w-0 gap-1"):
                self._sample_count = ui.label(f"0 of {TARGET_VIEWS} views").classes(
                    "handeye-count"
                )
                self._sample_count.mark("handeye-sample-count")
                with ui.row().classes("items-center no-wrap gap-2"):
                    self._auto_btn = ui.button(
                        "Auto-capture", icon="play_circle", on_click=self._on_auto_click
                    ).props("color=wc-action text-color=wc-on-bright")
                    self._auto_btn.mark("handeye-auto")
                    self._clear_btn = (
                        ui.button(icon="delete_sweep", on_click=self._on_clear)
                        .props("flat round dense")
                        .tooltip("Clear all views")
                    )
                    self._clear_btn.mark("handeye-clear")
                self._diversity_label = ui.label("").classes("panel-note")
                self._diversity_label.mark("handeye-diversity")
                self._auto_progress_label = ui.label().classes(
                    "text-caption text-wc-info"
                )
                self._auto_progress_label.mark("handeye-auto-progress")
                self._apply_auto_progress()
        self._views_expand = (
            ui.expansion("Captured views")
            .props("dense")
            .classes("w-full")
            .mark("handeye-views-expand")
        )
        with self._views_expand:
            self._views_grid = ui.element("div").classes("handeye-views w-full")

    def _build_save_section(self, commander: Commander) -> None:
        self._result_container = ui.column().classes("w-full gap-0")
        self._result_container.mark("handeye-result")
        self._data_editor = CameraCalibrationData(
            commander, self._measurement, on_save=self._save
        )
        self._save_btn = self._data_editor.save_button
        self._build_stored_section()
        if self._result is not None:
            self._show_result(self._result)
            if self._save_btn is not None:
                self._save_btn.set_enabled(True)
        elif self._solve_error is not None:
            self._show_solve_error(self._solve_error)

    def _build_stored_section(self) -> None:
        self._stored_container = ui.column().classes("w-full gap-0")
        self._stored_container.mark("handeye-stored")
        commander = self._commander
        if commander is not None and commander.scene is not None:
            self._scene_switch = ui.switch(
                "Show camera in 3D scene", on_change=self._on_scene_toggle
            )
            self._scene_switch.mark("handeye-scene-toggle")
            ui.timer(1.0, self._refresh_scene_overlay)

    def _board_summary(self) -> str:
        placement = "Camera on tool" if self._mount == "tool" else "Fixed camera"
        spec = self._spec
        return (
            f"{placement} · {spec.squares_x}×{spec.squares_y} board, "
            f"{spec.square_mm:g} mm squares"
        )

    def _save_summary(self) -> str:
        if self._saved:
            return "saved"
        if self._solving:
            return "solving…"
        if self._result is not None:
            return _verdict(self._result)[1]
        if self._solve_error is not None:
            return f"no fit: {self._solve_error}"
        to_go = SOLVE_MIN_SAMPLES - len(self._samples)
        if to_go > 0:
            return f"needs {to_go} more view{'s' if to_go > 1 else ''}"
        return "solves when the run ends" if self._auto_running else "solving…"

    def _refresh_stage(self) -> None:
        """Icons and summaries of the three headers, written only on change."""
        done = {
            "board": camera_service.active or bool(self._samples),
            "views": len(self._samples) >= SOLVE_MIN_SAMPLES,
            "save": self._saved,
        }
        summaries = {
            "board": self._board_summary(),
            "views": f"{len(self._samples)} of {TARGET_VIEWS} views",
            "save": self._save_summary(),
        }
        state = (self._step, tuple(done.values()), tuple(summaries.values()))
        if state == self._last_stage:
            return
        self._last_stage = state
        for key, header in self._step_headers.items():
            if done[key]:
                icon = "check_circle"
                header.classes(add="handeye-step-done")
            else:
                header.classes(remove="handeye-step-done")
                icon = (
                    "radio_button_checked"
                    if key == self._step
                    else "radio_button_unchecked"
                )
            self._step_icons[key].set_name(icon)
            self._step_summaries[key].set_text(summaries[key])

    # ------------------------------------------------------------- detection

    def _camera_hint_text(self) -> str:
        """Tool-aware guidance for the camera-off state: a tool that declares
        a camera mount (like the MSG) gets pointed at its device assignment."""
        commander = self._commander
        spec = None
        if commander is not None:
            try:
                spec = commander.robot.tools[_selected_tool_key()]
            except KeyError:
                spec = None
        if spec is not None and spec.camera_spec is not None:
            return (
                f"{spec.display_name} has a camera mount but no video device "
                "assigned — pick one in Settings → Camera."
            )
        return "No camera active — enable a tool camera in Settings."

    def _set_camera_visibility(self, active: bool) -> None:
        if active and self._step == "board" and not self._advanced:
            # A running camera is what the board step was waiting for;
            # capturing is the operator's next act.
            self._advanced = True
            self._open_step("views")
        if self._camera_card is not None:
            self._camera_card.set_visibility(active)
        if self._camera_hint is not None:
            self._camera_hint.set_visibility(not active)
        if not active and self._camera_hint_label is not None:
            hint = self._camera_hint_text()
            if hint != self._last_hint_text:
                self._last_hint_text = hint
                self._camera_hint_label.set_text(hint)
        if active != self._camera_was_active:
            if active and self._image is not None:
                # Force the browser to reconnect the MJPEG stream.
                self._image.set_source(f"/tool/camera/stream?t={time.time()}")
            self._camera_was_active = active
            self._refresh_stage()

    async def _detect_tick(self) -> None:
        self._set_camera_visibility(camera_service.active)
        self._refresh_auto_ui()
        if _selected_tool_key() != self._last_stored_tool:
            self._refresh_stored()
        if self._detect_busy:
            return
        if not camera_service.active:
            self._set_detection(None, "No camera active")
            return
        self._detect_busy = True
        try:
            try:
                frame = handeye.decode_jpeg(camera_service.snapshot().jpeg)
            except CameraUnavailable as error:
                self._set_detection(None, str(error))
                return
            if frame is None or min(frame.shape[:2]) < 64:
                self._decode_failures += 1
                message = (
                    "Camera frames could not be decoded — the camera may use an "
                    "unsupported MJPEG format"
                    if self._decode_failures >= DECODE_FAILURE_HINT
                    else "No frame"
                )
                self._set_detection(None, message)
                return
            self._decode_failures = 0
            detection = await run.io_bound(handeye.detect_board, frame, self._detector)
            if detection is None:
                self._set_detection(None, "No board detected")
            else:
                self._set_detection(
                    detection, f"Board detected — {len(detection.corners)} corners"
                )
        finally:
            self._detect_busy = False

    def _set_detection(self, detection: handeye.Detection | None, message: str) -> None:
        """Reflect the detection in the UI, writing only what changed — the
        tick repeats the same idle state 5x/s and must not flood the outbox."""
        self._last_detection = detection
        if self._status_label is not None and message != self._last_status_text:
            self._last_status_text = message
            self._status_label.set_text(message)
            self._status_label.classes(
                replace="handeye-camera-chip"
                + (" handeye-camera-chip-found" if detection is not None else "")
            )
        if self._image is not None:
            content = (
                ""
                if detection is None
                else "".join(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                    f'stroke="{hex_of("measure-position")}" stroke-width="1.5" fill="none"/>'
                    for x, y in detection.corners.reshape(-1, 2)
                )
            )
            if content != self._last_overlay_content:
                self._last_overlay_content = content
                self._image.set_content(content)

    # --------------------------------------------------------------- capture

    async def _capture_sample(self) -> None:
        """Take one stationary (TCP pose, fresh frame) sample, or raise
        :class:`_CaptureRefused`. Needs no UI context."""
        commander = self._commander
        if commander is None:
            raise _CaptureRefused("Panel is not connected to a robot", fatal=True)
        if float(np.max(np.abs(robot_state.speeds))) > STATIONARY_SPEED_DEG_S:
            raise _CaptureRefused("Robot is moving — hold still to capture")
        try:
            before = await observe_tcp(commander.client)
            observation = await camera_service.next_snapshot()
        except (CameraUnavailable, TimeoutError, ValueError) as error:
            # A dropped frame, an arm still settling or a mid-capture tool
            # readback are worth another attempt; the auto run retries them.
            raise _CaptureRefused(str(error)) from error
        except (OSError, NotImplementedError) as error:
            raise _CaptureRefused(str(error), fatal=True) from error
        binding = CaptureBinding(
            observation.camera_id,
            observation.session_id,
            commander.robot.backend_package,
            TcpCalibration(
                before.applied, before.binding.tool_key, before.binding.variant_key
            ),
        )
        if self._sample_binding is not None and binding != self._sample_binding:
            raise _CaptureRefused(
                "Camera, tool or TCP changed — clear samples first", fatal=True
            )
        frame = handeye.decode_jpeg(observation.jpeg)
        if frame is None:
            raise _CaptureRefused("No camera frame available")
        analysis = await run.io_bound(handeye.analyse_view, frame, self._detector)
        if analysis is None:
            raise _CaptureRefused("Board not detected in the captured frame")
        detection, cells, tilt = analysis
        if (
            self._samples
            and detection.image_size != self._samples[0].detection.image_size
        ):
            raise _CaptureRefused(
                "Camera resolution changed — clear samples to restart", fatal=True
            )
        try:
            after = await observe_tcp(commander.client)
            latest = camera_service.snapshot()
        except (CameraUnavailable, TimeoutError, ValueError) as error:
            raise _CaptureRefused(str(error)) from error
        except (OSError, NotImplementedError) as error:
            raise _CaptureRefused(str(error), fatal=True) from error
        drift = (
            np.linalg.inv(before.nominal_tool.matrix()) @ after.nominal_tool.matrix()
        )
        turned_deg = np.degrees(
            np.arccos(np.clip((np.trace(drift[:3, :3]) - 1.0) / 2.0, -1.0, 1.0))
        )
        if (
            after.binding != before.binding
            or after.applied != before.applied
            or latest.session_id != observation.session_id
            or np.linalg.norm(drift[:3, 3]) > STILL_TRANSLATION_MM
            or turned_deg > STILL_ROTATION_DEG
        ):
            raise _CaptureRefused(
                "Camera or robot changed during capture; hold still and try again"
            )
        self._sample_binding = binding
        self._sample_tool_key = binding.tool.tool_key
        self._result = None
        self._solve_error = None
        if self._save_btn is not None:
            self._save_btn.set_enabled(False)
        pose = before.nominal_tool.matrix() @ Pose(before.applied).matrix()
        self._samples.append(
            handeye.HandEyeSample(
                pose,
                detection,
                observation.received_at,
                thumbnail=handeye.thumbnail_jpeg(frame),
                cells=cells,
                tilt=tilt,
            )
        )
        self._sample_revision += 1
        self._refresh_samples()

    def _on_clear(self) -> None:
        if self._auto_running:
            ui.notify("Auto-calibration is running — stop it first", color="warning")
            return
        self._clear_samples()
        self._refresh_samples()

    def _clear_samples(self) -> None:
        self._sample_revision += 1
        self._samples = []
        self._sample_tool_key = None
        self._sample_binding = None
        self._result = None
        self._solve_error = None
        if self._result_container is not None:
            self._result_container.clear()
        if self._save_btn is not None:
            self._save_btn.set_enabled(False)

    def _delete_sample(self, index: int) -> None:
        if 0 <= index < len(self._samples):
            self._sample_revision += 1
            del self._samples[index]
        if not self._samples:
            self._sample_tool_key = None
            self._sample_binding = None
        self._result = None
        self._solve_error = None
        if self._save_btn is not None:
            self._save_btn.set_enabled(False)
        self._refresh_samples()

    def _refresh_samples(self) -> None:
        n = len(self._samples)
        if self._sample_count is not None:
            self._sample_count.set_text(f"{n} of {TARGET_VIEWS} views")
        flags = self._view_flags()
        cov = handeye.coverage(self._samples)
        if self._ring is not None:
            self._ring.update(cov, n, TARGET_VIEWS)
        if self._diversity_label is not None:
            self._diversity_label.set_text(self._hint(cov, flags))
        if self._views_expand is not None:
            self._views_expand.set_text(
                f"{n} captured view{'s' if n != 1 else ''}" if n else "Captured views"
            )
        self._refresh_views(flags)
        self._schedule_solve()
        self._refresh_stage()

    def _refresh_views(self, flags: dict[int, tuple[str, str]]) -> None:
        """The thumbnails: a capture appends one and a changed list rebuilds
        them; otherwise only their flags change, so a solve restyles in place."""
        if self._views_grid is None:
            return
        tiles = self._view_tiles
        if len(tiles) > len(self._samples) or any(
            shown is not sample
            for (shown, _, _), sample in zip(tiles, self._samples, strict=False)
        ):
            self._views_grid.clear()
            tiles.clear()
        with self._views_grid:
            for i in range(len(tiles), len(self._samples)):
                tiles.append(self._build_view_tile(i, self._samples[i]))
        for i, (sample, tile, tooltip) in enumerate(tiles):
            kind, why = flags.get(i, ("", ""))
            tile.classes(
                replace="handeye-view" + (f" handeye-view-{kind}" if kind else "")
            )
            tooltip.set_text(
                f"View {i + 1}: {len(sample.detection.corners)} corners"
                + (f", {why}" if why else "")
            )

    def _build_view_tile(
        self, i: int, sample: handeye.HandEyeSample
    ) -> tuple[handeye.HandEyeSample, ui.element, ui.tooltip]:
        with ui.element("div").classes("handeye-view") as tile:
            if sample.thumbnail:
                ui.image(
                    "data:image/jpeg;base64,"
                    + base64.b64encode(sample.thumbnail).decode()
                )
            ui.label(str(i + 1)).classes("handeye-view-index")
            ui.button(
                icon="close",
                on_click=lambda _, idx=i: self._delete_sample(idx),
            ).props("flat dense round size=xs").mark(f"handeye-sample-del-{i}")
            tooltip = ui.tooltip()
        return sample, tile, tooltip

    def _hint(self, cov: handeye.Coverage, flags: dict[int, tuple[str, str]]) -> str:
        """Where the next view should come from, while views are still
        wanted; otherwise whatever the motion diversity has to say."""
        if 0 < len(self._samples) < TARGET_VIEWS:
            if cov.next_sector is not None:
                return f"View the board {_SECTOR_PHRASE[cov.next_sector]} next."
            if cov.next_cell is not None:
                return f"Put the board in the {cov.next_cell} of the frame next."
        return self._views_advice(flags)

    def _views_advice(self, flags: dict[int, tuple[str, str]]) -> str:
        """The one thing to do before the next capture, or nothing."""
        n = len(self._samples)
        for i, (kind, why) in flags.items():
            if kind == "similar":
                return f"View {i + 1} is {why}. Tilt the tool before the next capture."
        if n >= 2:
            max_rot, max_axis = handeye.motion_diversity(
                [s.T_base_gripper for s in self._samples]
            )
            if max_rot < handeye.DEGENERATE_ROTATION_DEG:
                return (
                    f"The views differ by at most {max_rot:.0f}°. Rotate the wrist "
                    "between captures or the solve will fail."
                )
            if max_axis < handeye.AXIS_DIVERSITY_MIN_DEG:
                return (
                    "Every view rotates about one axis. Roll the wrist about a "
                    "second axis or the solve will fail."
                )
            if max_rot < handeye.WARN_ROTATION_DEG or max_axis < handeye.AXIS_WARN_DEG:
                return "Tilt and roll the tool more between views for a better fit."
        if n < SOLVE_MIN_SAMPLES:
            return "Tilt and roll the tool between views."
        return ""

    def _view_flags(self) -> dict[int, tuple[str, str]]:
        """Views worth replacing: a near repeat of an earlier orientation,
        and after a solve, a view the fit does not explain."""
        flags: dict[int, tuple[str, str]] = {}
        rotations = [sample.T_base_gripper[:3, :3] for sample in self._samples]
        for i in range(1, len(rotations)):
            for j in range(i):
                delta = math.degrees(
                    float(
                        np.linalg.norm(
                            Rotation.from_matrix(
                                rotations[j].T @ rotations[i]
                            ).as_rotvec()
                        )
                    )
                )
                if delta < handeye.DEGENERATE_ROTATION_DEG:
                    flags[i] = ("similar", f"close to view {j + 1}")
                    break
        result = self._result
        if result is not None and len(result.intrinsics.per_view_errors) == len(
            self._samples
        ):
            errors = result.intrinsics.per_view_errors
            limit = max(2.0 * float(np.median(errors)), _QUALITY_RMS_PX[1])
            for i, error in enumerate(errors):
                if error > limit:
                    flags[i] = (
                        "error",
                        f"{error:.1f} px reprojection error; recapture this view.",
                    )
        return flags

    # ------------------------------------------------------- auto-calibration

    async def _on_auto_click(self) -> None:
        commander = self._commander
        if commander is None:
            return
        if (run := self._run) is not None:
            run.halt(_PANEL_STOP)
            if not await motion_guard.stop_robot(
                commander.client, "auto-calibration stop"
            ):
                ui.notify("Stop not confirmed — use E-stop", color="negative")
            return
        if self._auto_confirming:
            return
        if (busy := motion_guard.busy_reason()) is not None:
            ui.notify(busy, color="warning")
            return
        if not camera_service.active:
            ui.notify("No camera active — assign a tool camera first", color="warning")
            return
        if self._last_detection is None:
            ui.notify(
                "Board not detected — aim the camera at the board first",
                color="warning",
            )
            return
        n = len(AUTO_VIEW_DELTAS_DEG)
        self._auto_confirming = True
        try:
            with ui.dialog() as dialog, ui.card():
                ui.label("Automatic calibration").classes("text-subtitle2")
                ui.label(
                    f"The robot moves by itself through up to {n} poses around "
                    "its current position — wrist tilts up to ~18°, rolls up to "
                    "~35° and small arm shifts — capturing a view at each and "
                    "solving at the end. Poses the controller rejects (joint "
                    "limits, collision) are skipped."
                )
                ui.label(
                    "Clear the space around the tool and stay near the E-stop. "
                    "Stop halts the robot where it is."
                ).classes("text-wc-warning")
                with ui.row():
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props(
                        "flat color=wc-text"
                    )
                    ui.button(
                        "Start", icon="play_arrow", on_click=lambda: dialog.submit(True)
                    ).props("color=wc-action text-color=wc-on-bright").mark(
                        "handeye-auto-confirm"
                    )
            try:
                confirmed = await dialog
            finally:
                dialog.delete()
        finally:
            self._auto_confirming = False
        if not confirmed or self._run is not None:
            return
        page_client = context.client
        if not require_browser_control(page_client.id):
            return
        try:
            reservation = motion_guard.reserve(CALIBRATION)
        except MotionBusy as e:
            ui.notify(str(e), color="warning")
            return
        self._run = run = _AutoRun(page_client)
        self._auto_task = background_tasks.create(
            self._auto_run(commander, run, reservation),
            name="handeye-auto-calibration",
        )

    async def _auto_run(
        self, commander: Commander, run: _AutoRun, reservation: Reservation
    ) -> None:
        """Drive the robot through :data:`AUTO_VIEW_DELTAS_DEG`, capture at
        each pose, return to the start pose, and solve. Runs as a background
        task. A halt (see :class:`_AutoRun`) stops the robot where it is and
        keeps the views taken so far; the run never drives back after one."""
        with reservation, motion_recorder.owned():
            await self._auto_run_owned(commander, run)

    async def _auto_run_owned(self, commander: Commander, run: _AutoRun) -> None:
        n = len(AUTO_VIEW_DELTAS_DEG)
        page_client = run.page_client
        captured = 0
        skipped = 0
        error: str | None = None
        overran = False
        moved = False
        parked = True
        remove_listener = motion_guard.add_stop_listener(
            lambda _generation, _reason: run.wake.set()
        )
        try:
            angles = await commander.client.angles()
            start_angles = list(angles) if angles is not None else None
            if start_angles is None:
                error = "Could not read joint angles"
            else:
                rejects = 0
                for i, deltas in enumerate(AUTO_VIEW_DELTAS_DEG):
                    if run.halted():
                        break
                    progress = f"Pose {i + 1}/{n} — {captured} captured"
                    if skipped:
                        progress += f", {skipped} skipped"
                    self._set_auto_progress(progress)
                    target = [a + d for a, d in zip(start_angles, deltas, strict=True)]
                    outcome = await self._auto_move(commander, target)
                    if outcome is _Move.REJECTED:
                        skipped += 1
                        rejects += 1
                        if rejects >= AUTO_MAX_CONSECUTIVE_REJECTS:
                            error = (
                                f"{rejects} consecutive moves rejected — check "
                                "that the robot is homed and the tool has "
                                "clearance"
                            )
                            break
                        continue
                    if outcome is not _Move.DONE:
                        overran = outcome is _Move.UNCONFIRMED
                        break
                    rejects = 0
                    moved = True
                    await self._wait_stationary(run)
                    await run.sleep(AUTO_SETTLE_S)
                    if run.halted():
                        break
                    try:
                        if await self._auto_capture(run):
                            captured += 1
                        else:
                            skipped += 1
                    except _CaptureRefused as e:
                        error = str(e)
                        break
                if moved and not overran and not run.halted():
                    outcome = await self._auto_return(commander, start_angles)
                    parked = outcome is not _Move.REJECTED
                    overran = outcome is _Move.UNCONFIRMED
            if overran:
                error = "a move did not finish in time"
            halted = run.halted()
            stopped = True
            if halted or overran:
                # Also catches a move dispatched just after a Stop went out,
                # and retries a stop that went unanswered.
                stopped = await motion_guard.stop_robot(
                    commander.client, f"auto-calibration halted: {error or run.reason}"
                )
            if page_client.has_socket_connection:
                with page_client:
                    if not parked:
                        ui.notify(
                            "Robot left at the last view pose — the planner "
                            "refused the path back to the start pose. Jog it "
                            "clear before the next move.",
                            color="warning",
                        )
                    if not stopped:
                        ui.notify("Stop not confirmed — use E-stop", color="negative")
                    if error is not None:
                        ui.notify(
                            f"Auto-calibration aborted: {error}", color="negative"
                        )
                    elif run.reason == _PANEL_STOP:
                        ui.notify(
                            f"Auto-calibration stopped — {captured} views captured",
                            color="warning",
                        )
                    elif halted:
                        ui.notify(
                            f"Auto-calibration aborted: {run.reason}", color="negative"
                        )
                    elif captured == 0:
                        ui.notify(
                            "Auto-calibration captured no views — is the board "
                            "visible from the start pose?",
                            color="negative",
                        )
                    else:
                        ui.notify(
                            f"Auto-calibration captured {captured}/{n} views",
                            color="positive" if captured >= TARGET_VIEWS else "warning",
                        )
                    if (
                        error is None
                        and not halted
                        and captured > 0
                        and len(self._samples) >= SOLVE_MIN_SAMPLES
                    ):
                        self._set_auto_progress("Solving…")
                        self._open_step("save")
                        await self._run_solve()
        except Exception:
            logger.exception("Auto-calibration run failed")
            if page_client.has_socket_connection:
                with page_client:
                    ui.notify(
                        "Auto-calibration failed — see log for details",
                        color="negative",
                    )
        finally:
            remove_listener()
            if self._run is run:
                self._run = None
            self._set_auto_progress(None)
            # A stopped or aborted run may leave views the run never solved.
            self._schedule_solve(run_ending=True)

    def _halted(self) -> bool:
        return self._run is not None and self._run.halted()

    async def _auto_return(
        self, commander: Commander, start_angles: list[float]
    ) -> _Move:
        """Drive straight back to the start pose. The arm just traversed this
        region, so a refusal means the planner genuinely vetoed the path; the
        robot is left parked at the last view and it is reported, not
        retried."""
        self._set_auto_progress("Returning to start pose")
        outcome = await self._auto_move(commander, start_angles)
        if outcome is _Move.REJECTED:
            logger.warning("Auto-calibration could not drive back to the start pose")
        return outcome

    async def _auto_move(self, commander: Commander, target: list[float]) -> _Move:
        """Joint move with duration sized so the fastest joint stays under
        :data:`AUTO_DEG_PER_S`, awaited in slices so a halt takes effect
        within a slice instead of at the end of the move.

        A Stop Commander did not send (another client of the controller)
        cancels the command without completing it and without an error, so
        ``wait_command`` resolves neither True nor raises. The action going
        idle after it ran is the only signal, and it has to halt this run: the
        controller stays enabled through a Stop, so a caller that treated the
        halt as success would capture a view at the halted pose and then drive
        the arm to the next one, seconds after a human deliberately stopped
        it. (``ControlPanel._wait_home`` makes the same distinction for the
        same reason.)

        A move whose dispatch went unanswered, or that has not finished by
        its deadline, is stopped: the next view's move would queue behind
        it, and nothing else would stop it."""
        if self._halted():
            return _Move.HALTED
        current = await commander.client.angles()
        reference = current if current is not None else target
        span = max(abs(t - c) for t, c in zip(target, reference, strict=True))
        duration = max(AUTO_MIN_MOVE_S, span / AUTO_DEG_PER_S)
        deadline = time.monotonic() + duration + AUTO_MOVE_TIMEOUT_MARGIN_S
        # A Stop pressed during angles() queues behind it and reaches the
        # controller before this move would.
        if self._halted():
            return _Move.HALTED
        try:
            index = await commander.client.move_j(target, duration=duration)
            if index >= 0:
                started = False
                while True:
                    if self._halted():
                        return _Move.HALTED
                    if await commander.client.wait_command(
                        index, timeout=AUTO_WAIT_SLICE_S
                    ):
                        return _Move.DONE
                    state = commander.status.action.state
                    if state == waldoctl.ActionState.EXECUTING:
                        started = True
                    elif state == waldoctl.ActionState.IDLE and (
                        started or not await self._queued(commander)
                    ):
                        # Idle with nothing queued and no completion is a
                        # cancelled move, whether or not this loop ever saw it
                        # execute -- a Stop inside the first wait slice lands
                        # in that window, and reading it as a timeout would
                        # let the run walk to the next view after a human
                        # stopped it.
                        if await commander.client.wait_command(
                            index, timeout=AUTO_WAIT_SLICE_S
                        ):
                            return _Move.DONE  # completed between the slice and here
                        logger.info("Auto-calibration halted: the move was cancelled")
                        if self._run is not None:
                            self._run.halt("the move was cancelled")
                        return _Move.HALTED
                    if time.monotonic() > deadline:
                        break
        except Exception as e:
            logger.warning("Auto-calibration move refused: %s", e)
            return _Move.REJECTED
        logger.warning("Auto-calibration move not confirmed; stopping the robot")
        await motion_guard.stop_robot(
            commander.client, "auto-calibration move not confirmed"
        )
        return _Move.UNCONFIRMED

    @staticmethod
    async def _queued(commander: Commander) -> bool:
        """Whether the controller still holds queued work.

        Read only to tell a move that has not started yet from one that was
        cancelled: both look idle. An unanswered query counts as queued, so an
        unreachable controller ends the move on the deadline rather than being
        reported as a Stop nobody pressed.
        """
        try:
            queued = await commander.client.queue()
        except Exception as error:
            logger.debug("Queue readback during an auto move failed: %s", error)
            return True
        return queued is None or bool(queued)

    async def _wait_stationary(self, run: _AutoRun) -> None:
        deadline = time.monotonic() + AUTO_STATIONARY_TIMEOUT_S
        while time.monotonic() < deadline and not run.halted():
            if float(np.max(np.abs(robot_state.speeds))) < STATIONARY_SPEED_DEG_S:
                return
            await run.sleep(0.05)

    async def _auto_capture(self, run: _AutoRun) -> bool:
        """Capture with retries; True on success, False when this view never
        yields a usable board or the run halts first. Fatal refusals
        propagate and abort the run."""
        for attempt in range(AUTO_CAPTURE_ATTEMPTS):
            try:
                await self._capture_sample()
                return True
            except _CaptureRefused as e:
                if e.fatal:
                    raise
                if attempt + 1 < AUTO_CAPTURE_ATTEMPTS:
                    await run.sleep(AUTO_CAPTURE_RETRY_S)
                    if run.halted():
                        return False
        return False

    def _set_auto_progress(self, text: str | None) -> None:
        self._auto_progress_text = text
        self._apply_auto_progress()

    def _apply_auto_progress(self) -> None:
        if self._auto_progress_label is None:
            return
        self._auto_progress_label.set_visibility(self._auto_progress_text is not None)
        if self._auto_progress_text is not None:
            self._auto_progress_label.set_text(self._auto_progress_text)

    def _refresh_auto_ui(self) -> None:
        """Swap the Auto-capture button into a Stop button while the run is
        active, and refresh the headers when that changes. Driven from the
        detection timer, so it also restores the idle state after a page
        reload mid-run."""
        running = self._auto_running
        if self._auto_btn is None or running == self._last_auto_running:
            return
        self._last_auto_running = running
        self._refresh_stage()
        if running:
            self._auto_btn.set_text("Stop")
            self._auto_btn.props("icon=stop color=wc-control text-color=wc-error")
        else:
            self._auto_btn.set_text("Auto-capture")
            self._auto_btn.props(
                "icon=play_circle color=wc-action text-color=wc-on-bright"
            )
        if self._clear_btn is not None:
            self._clear_btn.set_enabled(not running)
        if self._solve_btn is not None and not self._solve_btn.is_deleted:
            self._solve_btn.set_enabled(not running)

    # ----------------------------------------------------------------- solve

    def _schedule_solve(self, *, run_ending: bool = False) -> None:
        """Solve once the views have held still for :data:`SOLVE_DEBOUNCE_S`.
        A running auto run solves when it ends instead; ``run_ending`` is
        that end, scheduling from inside the run's own task. A set that
        already failed to solve is left alone until it changes."""
        if self._solve_later is not None:
            self._solve_later.cancel()
            self._solve_later = None
        if (
            len(self._samples) < SOLVE_MIN_SAMPLES
            or self._result is not None
            or self._solve_error is not None
            or (self._auto_running and not run_ending)
        ):
            return
        self._solve_later = asyncio.get_running_loop().call_later(
            SOLVE_DEBOUNCE_S, self._start_scheduled_solve
        )

    def _start_scheduled_solve(self) -> None:
        self._solve_later = None
        if self._result is None and not self._auto_running:
            background_tasks.create(self._run_solve(), name="handeye-auto-solve")

    async def _solve(self) -> None:
        if not self._auto_running:
            await self._run_solve()

    async def _run_solve(self) -> None:
        if len(self._samples) < SOLVE_MIN_SAMPLES or self._solving:
            return
        revision = self._sample_revision
        self._solving = True
        self._result = None
        if self._save_btn is not None:
            self._save_btn.set_enabled(False)
        self._refresh_stage()
        button = self._solve_btn
        if button is not None:
            button.props("loading")
        error: str | None = None
        result: handeye.HandEyeResult | None = None
        try:
            result = await run.io_bound(
                handeye.solve_hand_eye,
                list(self._samples),
                self._spec,
                method=self._method,
                mount=self._mount,
            )
        except handeye.CalibrationError as e:
            error = str(e)
        finally:
            self._solving = False
            if button is not None and not button.is_deleted:
                button.props(remove="loading")
        if revision != self._sample_revision:
            # The views changed underneath; the current set gets its own solve.
            self._schedule_solve()
            return
        if error is not None:
            self._solve_error = error
            self._show_solve_error(error)
            self._refresh_stage()
            return
        assert result is not None
        self._result = result
        self._solve_error = None
        self._show_result(result)
        if self._save_btn is not None:
            self._save_btn.set_enabled(True)
        self._refresh_samples()

    def _show_solve_error(self, message: str) -> None:
        if self._result_container is None:
            return
        self._result_container.clear()
        with self._result_container:
            ui.label(message).classes("text-caption text-wc-error").mark(
                "handeye-solve-error"
            )

    def _show_result(self, result: handeye.HandEyeResult) -> None:
        if self._result_container is None:
            return
        (x, y, z), (rx, ry, rz) = handeye.matrix_to_xyz_rpy(result.T_camera_parent)
        K = result.intrinsics.camera_matrix
        rms = result.intrinsics.reproj_rms_px
        spread = result.target_spread_mm
        headline, _, tone = _verdict(result)
        self._result_container.clear()
        with self._result_container:
            ui.label(
                f"{headline}: {rms:.2f} px reprojection, {spread:.1f} mm target spread"
            ).classes(f"handeye-verdict {tone}")
            ui.label(
                "Camera → WRF transform"
                if result.mount == "fixed"
                else "Camera → TCP transform"
            ).classes("text-caption text-wc-text-muted")
            ui.label(f"X {x:+.1f}  Y {y:+.1f}  Z {z:+.1f} mm").classes("font-mono")
            ui.label(f"R {rx:+.1f}  P {ry:+.1f}  Y {rz:+.1f} °").classes("font-mono")
            ui.label(
                f"Residuals: rotation {result.rot_residual_deg[0]:.2f}°, "
                f"translation {result.trans_residual_mm[0]:.1f} mm (mean)"
            ).classes("text-caption")
            if (
                result.mount == "tool"
                and float(np.linalg.norm(result.T_camera_parent[:3, 3])) > 500.0
            ):
                ui.label(
                    "Camera offset exceeds 500 mm — the solution looks degenerate; "
                    "recapture with more rotation diversity."
                ).classes("text-caption text-wc-warning")
            # Diagnostics and the solver choice are expert territory — folded
            # away so the headline stays verdict + transform.
            with ui.expansion("Details").props("dense").classes("w-full text-caption"):
                ui.label(
                    f"fx {K[0, 0]:.1f}  fy {K[1, 1]:.1f}  "
                    f"cx {K[0, 2]:.1f}  cy {K[1, 2]:.1f} px"
                ).classes("font-mono text-caption")
                max_rot, max_axis = handeye.motion_diversity(
                    [s.T_base_gripper for s in self._samples]
                )
                ui.label(
                    f"Largest rotation between views {max_rot:.1f}°, "
                    f"axis spread {max_axis:.1f}°"
                ).classes("text-caption")
                with ui.row().classes("items-center gap-2 text-caption"):
                    ui.label(f"{result.n_views} views · method")
                    method_select = (
                        ui.select(list(handeye.HAND_EYE_METHODS), value=result.method)
                        .props("dense options-dense borderless")
                        .classes("w-28")
                    )
                    method_select.mark("handeye-method")
                    method_select.on_value_change(
                        lambda e: setattr(self, "_method", str(e.value))
                    )
                    self._solve_btn = (
                        ui.button("Re-solve", icon="calculate", on_click=self._solve)
                        .props("dense flat no-caps")
                        .mark("handeye-solve")
                    )

    async def _measurement(
        self, setup: SetupSnapshot, reference: str
    ) -> CameraCalibration:
        if (
            self._result is None
            or self._sample_binding is None
            or self._commander is None
        ):
            raise ValueError("Capture and solve a calibration first")
        observation = camera_service.snapshot()
        frame = handeye.decode_jpeg(observation.jpeg)
        if frame is None:
            raise CameraUnavailable("Camera image cannot be decoded")
        calibration = calibration_from_result(
            self._result, self._spec, self._sample_binding, setup, reference
        )
        tool = (
            await read_applied_tcp(self._commander.client)
            if calibration.mount == "tool"
            else None
        )
        if camera_service.snapshot().session_id != observation.session_id:
            raise CameraUnavailable("Camera changed while checking calibration")
        calibration.validate(
            setup,
            camera_id=observation.camera_id,
            image_size=(frame.shape[1], frame.shape[0]),
            backend=self._commander.robot.backend_package,
            tool=tool,
        )
        return calibration

    async def _save(self) -> None:
        if self._data_editor is not None:
            self._saved = await self._data_editor.save()
            self._refresh_stage()

    def _refresh_stored(self, tool_key: str | None = None) -> None:
        if self._stored_container is None:
            return
        self._stored_container.clear()
        tool_key = tool_key or _selected_tool_key()
        self._last_stored_tool = tool_key
        stored = ng_app.storage.general.get(f"handeye/{tool_key}")
        self._refresh_stage()
        with self._stored_container:
            if not stored:
                return
            try:
                info = handeye.from_storage_dict(stored)
            except (KeyError, TypeError, ValueError) as e:
                ui.label(f"Stored calibration unreadable: {e}").classes(
                    "text-caption text-wc-error"
                )
                return
            x, y, z = info["xyz_mm"]
            ui.label(
                f"Stored ({tool_key}): X {x:+.1f} Y {y:+.1f} Z {z:+.1f} mm · "
                f"{info['n_samples']} views · RMS {info['reproj_rms_px']:.2f} px · "
                f"{info['timestamp']}"
            ).classes("text-caption")
            current_offset = ng_app.storage.general.get(
                f"tcp_offset_{tool_key}", {"x": 0, "y": 0, "z": 0}
            )
            snapshot = info["tcp_offset_snapshot"]
            if any(
                abs(float(current_offset.get(k, 0)) - float(snapshot.get(k, 0))) > 1e-9
                for k in ("x", "y", "z", "roll", "pitch", "yaw")
            ):
                ui.label(
                    "TCP offset changed since this calibration was saved — "
                    "the stored transform no longer matches the current TCP."
                ).classes("text-caption text-wc-warning")

    # ------------------------------------------------------------- 3D scene

    def _on_scene_toggle(self) -> None:
        commander = self._commander
        if commander is None or commander.scene is None:
            return
        if self._scene_switch is not None and not self._scene_switch.value:
            commander.scene.clear(SCENE_GROUP)

    async def _refresh_scene_overlay(self) -> None:
        commander = self._commander
        if (
            commander is None
            or commander.scene is None
            or self._scene_switch is None
            or not self._scene_switch.value
            or self._data_editor is None
        ):
            return
        try:
            if self._result is not None:
                setup = SetupSnapshot()
                calibration = await self._measurement(setup, "WRF")
            else:
                setup = self._data_editor.snapshot()
                calibration = setup.cameras[self._data_editor.name.value]
            observation = camera_service.snapshot()
            frame = handeye.decode_jpeg(observation.jpeg)
            if frame is None:
                raise CameraUnavailable("Camera image cannot be decoded")
            tool = (
                await read_applied_tcp(commander.client)
                if calibration.mount == "tool"
                else None
            )
            if camera_service.snapshot().session_id != observation.session_id:
                raise CameraUnavailable("Camera changed while checking calibration")
            pose = (
                Pose.from_matrix(np.asarray(robot_state.pose).reshape(4, 4))
                if calibration.mount == "tool"
                else None
            )
            T_base_cam_m = calibration.world_pose(
                setup,
                camera_id=observation.camera_id,
                image_size=(frame.shape[1], frame.shape[0]),
                backend=commander.robot.backend_package,
                tool=tool,
                tcp_pose=pose,
            ).matrix()
        except (ValueError, KeyError, OSError, TimeoutError, CameraUnavailable):
            commander.scene.clear(SCENE_GROUP)
            return
        T_base_cam_m[:3, 3] /= 1000.0
        origin = T_base_cam_m[:3, 3]
        axes = T_base_cam_m[:3, :3]
        axis_len = 0.05
        depth = FRUSTUM_DEPTH_MM / 1000.0
        half_w, half_h = depth * 0.4, depth * 0.3
        corners = [
            origin + axes @ np.array([sx * half_w, sy * half_h, depth])
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
        ]
        with commander.scene.overlay(SCENE_GROUP) as scene:
            for axis, color in zip(
                axes.T,
                (
                    SceneColors.AXIS_X_HEX,
                    SceneColors.AXIS_Y_HEX,
                    SceneColors.AXIS_Z_HEX,
                ),
                strict=True,
            ):
                scene.line(
                    origin.tolist(), (origin + axis_len * axis).tolist()
                ).material(color)
            for c in corners:
                scene.line(origin.tolist(), c.tolist()).material(
                    hex_of("path-checkpoint")
                )
            for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
                scene.line(a.tolist(), b.tolist()).material(hex_of("path-checkpoint"))
