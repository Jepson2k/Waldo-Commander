"""Eye-in-hand hand-eye calibration panel.

Workflow: print a generated ChArUco board, fix it in the workspace, and aim
the tool camera at it. Then either jog the robot to 10-15 rotation-diverse
poses by hand, capturing a synchronized (TCP pose, frame) sample at each, or
let Auto-calibrate drive the robot through a built-in pose set around the
start pose, capturing at each stop. Solving recovers camera intrinsics + the
camera→TCP transform, saved per tool.

Registered through the ``waldoctl.panels`` entry-point group, so the host
mounts it like any third-party panel; being in-tree it may also use
Waldo-Commander internals (camera service, robot_state) directly.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime

import numpy as np
from nicegui import Client, app as ng_app
from nicegui import background_tasks, context, run, ui
from scipy.spatial.transform import Rotation
from waldoctl import Commander, Panel, PanelSlot

from waldo_commander.services import handeye
from waldo_commander.services.camera_service import camera_service
from waldo_commander.state import robot_state

logger = logging.getLogger(__name__)

STATIONARY_SPEED_DEG_S = 0.5
SOLVE_MIN_SAMPLES = 4
RECOMMENDED_SAMPLES = 8
DETECT_INTERVAL_S = 0.2
# Consecutive undecodable frames before surfacing a camera-format hint.
DECODE_FAILURE_HINT = 15
SCENE_GROUP = "handeye-camera"
FRUSTUM_DEPTH_MM = 120.0

_QUALITY_RMS_PX = (1.0, 2.0)
_QUALITY_SPREAD_MM = (2.0, 5.0)

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

# Autonomous moves are deliberately slow: duration is sized so the fastest
# joint stays under AUTO_DEG_PER_S. Tests lower these for the simulator.
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
# Returning to the start pose means moving back *into* the mounted tool's
# clearance margin. A tool like the MSG gripper sits permanently inside that
# margin (its body clears L4 by ~2 mm against a 5 mm margin), so the
# controller treats every pose as already-colliding and only accepts moves
# that give up no clearance against the start of the planned path — one long
# move back to the start pose gets refused even though the arm just came from
# there, and so does the controller's own home command. Walking the same
# straight joint-space line in sub-steps re-bases that comparison each step
# and is accepted. The step count is capped so the clearance the return can
# give up in total stays far short of real contact.
AUTO_RETURN_STEPS = 8


class _CaptureRefused(Exception):
    """A sample could not be taken. ``fatal`` marks conditions that
    invalidate the whole capture set (tool/resolution changed, robot
    unreachable) rather than just this attempt."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _selected_tool_key() -> str:
    return ng_app.storage.general.get("selected_tool", "NONE")


def _quality_color(value: float, thresholds: tuple[float, float]) -> str:
    if value < thresholds[0]:
        return "text-positive"
    if value < thresholds[1]:
        return "text-warning"
    return "text-negative"


class HandEyeCalibrationPanel(Panel):
    id = "handeye"
    display_name = "Hand-Eye Calibration"
    slot = PanelSlot.LEFT_TOP_TAB
    tab_icon = "center_focus_strong"
    tab_tooltip = "Hand-eye calibration"
    order = 50
    # Registered with the host's panel-resize system: opens at the default
    # size, drag-resizable, chosen size persists. The live view scales to
    # the pane, so a high-resolution tool camera can't balloon the layout.
    min_width = 440
    min_height = 320
    default_width = 600
    default_height = 640

    def __init__(self) -> None:
        stored = ng_app.storage.general.get("handeye/board")
        try:
            self._spec = (
                handeye.BoardSpec.from_dict(stored) if stored else handeye.BoardSpec()
            )
        except (KeyError, TypeError, ValueError):
            self._spec = handeye.BoardSpec()
        self._detector = handeye.make_detector(self._spec)
        self._samples: list[handeye.HandEyeSample] = []
        self._sample_tool_key: str | None = None
        self._method = "PARK"
        self._result: handeye.HandEyeResult | None = None
        self._last_detection: handeye.Detection | None = None
        self._detect_busy = False
        self._decode_failures = 0
        self._camera_was_active = camera_service.active
        self._auto_task: asyncio.Task | None = None
        self._auto_cancel = False
        self._auto_progress_text: str | None = None
        self._reset_element_refs()

    def _reset_element_refs(self) -> None:
        self._image: ui.interactive_image | None = None
        self._camera_card: ui.card | None = None
        self._camera_hint: ui.row | None = None
        self._status_label: ui.label | None = None
        self._capture_btn: ui.button | None = None
        self._solve_btn: ui.button | None = None
        self._save_btn: ui.button | None = None
        self._sample_count: ui.label | None = None
        self._samples_container: ui.column | None = None
        self._samples_detail: ui.expansion | None = None
        self._diversity_label: ui.label | None = None
        self._result_container: ui.column | None = None
        self._stored_container: ui.column | None = None
        self._scene_switch: ui.switch | None = None
        self._samples_section: ui.column | None = None
        self._solve_section: ui.column | None = None
        self._stored_section: ui.column | None = None
        self._commander: Commander | None = None
        self._camera_hint_label: ui.label | None = None
        self._last_status_text: str | None = None
        self._last_overlay_content: str | None = None
        self._last_capture_enabled: bool | None = None
        self._last_hint_text: str | None = None
        self._last_stored_tool: str | None = None
        self._auto_btn: ui.button | None = None
        self._clear_btn: ui.button | None = None
        self._auto_progress_label: ui.label | None = None
        self._last_auto_running: bool | None = None

    @property
    def _auto_running(self) -> bool:
        return self._auto_task is not None and not self._auto_task.done()

    # ------------------------------------------------------------------ build

    def build(self, commander: Commander) -> None:
        self._reset_element_refs()
        self._commander = commander

        with ui.column().classes("w-full gap-2"):
            with ui.row().classes("w-full items-center"):
                ui.label("Hand-Eye Calibration").classes("text-subtitle1")
                ui.space()
                ui.label().bind_text_from(
                    ng_app.storage.general,
                    "selected_tool",
                    lambda t: f"Tool: {t or 'NONE'}",
                ).classes("text-caption text-grey")

            self._build_board_section()
            self._build_camera_section()
            # Progressive disclosure: each later workflow stage stays hidden
            # until it is reachable (camera live -> capture, enough samples ->
            # solve, something saved or solved -> stored/scene).
            self._samples_section = ui.column().classes("w-full gap-2")
            with self._samples_section:
                self._build_samples_section()
            self._solve_section = ui.column().classes("w-full gap-2")
            with self._solve_section:
                self._build_solve_section()
            self._stored_section = ui.column().classes("w-full gap-2")
            with self._stored_section:
                self._build_stored_section()

        ui.timer(DETECT_INTERVAL_S, self._detect_tick)
        self._refresh_samples()
        self._refresh_stored()
        self._refresh_stage()

    def _build_board_section(self) -> None:
        with ui.expansion("Target board", icon="grid_on").classes("w-full"):
            with ui.row().classes("items-end gap-2"):
                sx = ui.number(
                    "Squares X", value=self._spec.squares_x, min=3, max=20, precision=0
                ).classes("w-20")
                sy = ui.number(
                    "Squares Y", value=self._spec.squares_y, min=3, max=20, precision=0
                ).classes("w-20")
                sq = ui.number(
                    "Square mm", value=self._spec.square_mm, min=5.0, step=0.5
                ).classes("w-24")
                mk = ui.number(
                    "Marker mm", value=self._spec.marker_mm, min=3.0, step=0.5
                ).classes("w-24")
                dic = ui.select(
                    list(handeye.ARUCO_DICTIONARIES),
                    value=self._spec.dictionary,
                    label="Dictionary",
                ).classes("w-32")

            def current_inputs() -> handeye.BoardSpec:
                return handeye.BoardSpec(
                    squares_x=int(sx.value),
                    squares_y=int(sy.value),
                    square_mm=float(sq.value),
                    marker_mm=float(mk.value),
                    dictionary=str(dic.value),
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
                if self._samples:
                    with ui.dialog() as dialog, ui.card():
                        ui.label(
                            f"Changing the board invalidates {len(self._samples)} "
                            "captured samples. Clear them and continue?"
                        )
                        with ui.row():
                            ui.button("Cancel", on_click=lambda: dialog.submit(False))
                            ui.button(
                                "Clear & apply", on_click=lambda: dialog.submit(True)
                            ).props("color=negative").mark(
                                "handeye-board-apply-confirm"
                            )
                    if not await dialog:
                        revert_inputs()
                        return
                    self._clear_samples()
                self._spec = spec
                self._detector = detector
                ng_app.storage.general["handeye/board"] = spec.to_dict()

            for el in (sx, sy, sq, mk, dic):
                el.on_value_change(apply_spec)

            with ui.row().classes("items-center"):
                ui.button(
                    "Download board PNG",
                    icon="download",
                    on_click=lambda: ui.download(
                        handeye.board_png(self._spec),
                        f"charuco_{self._spec.squares_x}x{self._spec.squares_y}"
                        f"_{self._spec.square_mm:g}mm_{self._spec.marker_mm:g}mm"
                        f"_{self._spec.dictionary}.png",
                    ),
                ).props("outline dense").mark("handeye-board-download")
                ui.label(
                    "Print at 100% scale, then measure a printed square and "
                    "correct 'Square mm' if it differs."
                ).classes("text-caption text-grey")

    def _build_camera_section(self) -> None:
        self._camera_card = ui.card().tight().classes("w-full")
        with self._camera_card:
            self._image = ui.interactive_image("/tool/camera/stream").classes("w-full")
            self._image.mark("handeye-camera")
        self._camera_hint = ui.row().classes("items-center")
        with self._camera_hint:
            ui.icon("videocam_off").classes("text-grey")
            self._camera_hint_label = ui.label(self._camera_hint_text()).classes(
                "text-caption text-grey"
            )
        self._status_label = ui.label("No board detected").classes("text-caption")
        self._status_label.mark("handeye-detect-status")
        self._set_camera_visibility(camera_service.active)

    def _build_samples_section(self) -> None:
        with ui.row().classes("items-center"):
            self._auto_btn = ui.button(
                "Auto-calibrate", icon="play_circle", on_click=self._on_auto_click
            )
            self._auto_btn.mark("handeye-auto")
            self._capture_btn = ui.button(
                "Capture", icon="add_a_photo", on_click=self._capture
            ).props("outline")
            self._capture_btn.mark("handeye-capture")
            self._clear_btn = ui.button(
                "Clear", icon="delete_sweep", on_click=self._on_clear
            ).props("outline")
            self._clear_btn.mark("handeye-clear")
            self._sample_count = ui.label("0 samples")
            self._sample_count.mark("handeye-sample-count")
        self._auto_progress_label = ui.label().classes("text-caption text-primary")
        self._auto_progress_label.mark("handeye-auto-progress")
        self._apply_auto_progress()
        self._diversity_label = ui.label("").classes("text-caption")
        self._diversity_label.mark("handeye-diversity")
        self._samples_detail = (
            ui.expansion("Sample details").props("dense").classes("w-full text-caption")
        )
        with self._samples_detail:
            self._samples_container = ui.column().classes("w-full gap-0")

    def _build_solve_section(self) -> None:
        with ui.row().classes("items-center"):
            self._solve_btn = ui.button("Solve", icon="calculate", on_click=self._solve)
            self._solve_btn.mark("handeye-solve")
            self._save_btn = ui.button("Save", icon="save", on_click=self._save).props(
                "outline"
            )
            self._save_btn.mark("handeye-save")
            self._save_btn.set_enabled(False)
        self._result_container = ui.column().classes("w-full gap-0")
        self._result_container.mark("handeye-result")
        if self._result is not None:
            self._show_result(self._result)

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

    def _refresh_stage(self) -> None:
        """Show each workflow stage only once it is reachable. Repeated
        set_visibility calls with an unchanged value are no-ops, so this is
        safe to drive from the detection timer."""
        if self._samples_section is not None:
            self._samples_section.set_visibility(
                camera_service.active or bool(self._samples)
            )
        if self._solve_section is not None:
            self._solve_section.set_visibility(
                len(self._samples) >= SOLVE_MIN_SAMPLES or self._result is not None
            )
        if self._stored_section is not None:
            has_stored = bool(
                ng_app.storage.general.get(f"handeye/{_selected_tool_key()}")
            )
            self._stored_section.set_visibility(has_stored or self._result is not None)

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
        if self._camera_card is not None:
            self._camera_card.set_visibility(active)
        if self._camera_hint is not None:
            self._camera_hint.set_visibility(not active)
        if self._status_label is not None:
            self._status_label.set_visibility(active)
        if not active and self._camera_hint_label is not None:
            hint = self._camera_hint_text()
            if hint != self._last_hint_text:
                self._last_hint_text = hint
                self._camera_hint_label.set_text(hint)
        if active and not self._camera_was_active and self._image is not None:
            # Force the browser to reconnect the MJPEG stream.
            self._image.set_source(f"/tool/camera/stream?t={time.time()}")
        self._camera_was_active = active

    async def _detect_tick(self) -> None:
        self._set_camera_visibility(camera_service.active)
        self._refresh_stage()
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
            frame = handeye.decode_jpeg(camera_service.get_latest_frame())
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
        if self._image is not None:
            content = (
                ""
                if detection is None
                else "".join(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                    'stroke="#2dd4bf" stroke-width="1.5" fill="none"/>'
                    for x, y in detection.corners.reshape(-1, 2)
                )
            )
            if content != self._last_overlay_content:
                self._last_overlay_content = content
                self._image.set_content(content)
        enabled = detection is not None and not self._auto_running
        if self._capture_btn is not None and enabled != self._last_capture_enabled:
            self._last_capture_enabled = enabled
            self._capture_btn.set_enabled(enabled)

    # --------------------------------------------------------------- capture

    async def _capture(self) -> None:
        if self._auto_running:
            ui.notify("Auto-calibration is running", color="warning")
            return
        try:
            await self._capture_sample()
        except _CaptureRefused as e:
            ui.notify(str(e), color="negative" if e.fatal else "warning")

    async def _capture_sample(self) -> None:
        """Take one synchronized (TCP pose, frame) sample, or raise
        :class:`_CaptureRefused`. Shared by the Capture button and the
        auto-calibration run."""
        commander = self._commander
        if commander is None:
            raise _CaptureRefused("Panel is not connected to a robot", fatal=True)
        if float(np.max(np.abs(robot_state.speeds))) > STATIONARY_SPEED_DEG_S:
            raise _CaptureRefused("Robot is moving — hold still to capture")
        raw = camera_service.get_latest_frame()
        frame = handeye.decode_jpeg(raw)
        if frame is None:
            raise _CaptureRefused("No camera frame available")
        detection = await run.io_bound(handeye.detect_board, frame, self._detector)
        if detection is None:
            raise _CaptureRefused("Board not detected in the captured frame")
        if (
            self._samples
            and detection.image_size != self._samples[0].detection.image_size
        ):
            raise _CaptureRefused(
                "Camera resolution changed — clear samples to restart", fatal=True
            )
        tool_key = _selected_tool_key()
        if self._sample_tool_key is None:
            self._sample_tool_key = tool_key
        elif tool_key != self._sample_tool_key:
            raise _CaptureRefused(
                f"Tool changed ({self._sample_tool_key} → {tool_key}) — "
                "clear samples first",
                fatal=True,
            )

        pose = await self._current_pose_matrix(commander)
        if pose is None:
            raise _CaptureRefused("Could not read robot pose", fatal=True)
        self._samples.append(handeye.HandEyeSample(pose, detection, time.time()))
        self._refresh_samples()

    async def _current_pose_matrix(self, commander: Commander) -> np.ndarray | None:
        """TCP pose as 4x4 (mm), preferring a fresh status round-trip over the
        multicast cache."""
        try:
            st = await commander.client.status()
        except NotImplementedError:
            st = None
        status_pose = getattr(st, "pose", None) if st is not None else None
        if status_pose is not None:
            return np.asarray(status_pose, dtype=np.float64).reshape(4, 4)
        pose = np.asarray(robot_state.pose, dtype=np.float64)
        if pose.size != 16 or not np.any(pose):
            return None
        return pose.reshape(4, 4).copy()

    def _on_clear(self) -> None:
        if self._auto_running:
            ui.notify("Auto-calibration is running — stop it first", color="warning")
            return
        self._clear_samples()
        self._refresh_samples()

    def _clear_samples(self) -> None:
        self._samples = []
        self._sample_tool_key = None

    def _delete_sample(self, index: int) -> None:
        if 0 <= index < len(self._samples):
            del self._samples[index]
        if not self._samples:
            self._sample_tool_key = None
        self._refresh_samples()

    def _refresh_samples(self) -> None:
        n = len(self._samples)
        if self._sample_count is not None:
            self._sample_count.set_text(f"{n} sample{'s' if n != 1 else ''}")
        if self._solve_btn is not None:
            self._solve_btn.set_enabled(
                n >= SOLVE_MIN_SAMPLES and not self._auto_running
            )
        if self._samples_detail is not None:
            self._samples_detail.set_visibility(n > 0)
        self._refresh_stage()

        if self._diversity_label is not None:
            if n < 2:
                self._diversity_label.set_text(
                    f"Capture {SOLVE_MIN_SAMPLES}+ views (10–15 recommended), "
                    "varying wrist orientation ≥30° between views."
                )
                self._diversity_label.classes(replace="text-caption text-grey")
            else:
                max_rot, _ = handeye.motion_diversity(
                    [s.T_base_gripper for s in self._samples]
                )
                if max_rot < handeye.DEGENERATE_ROTATION_DEG:
                    self._diversity_label.set_text(
                        f"Max relative rotation {max_rot:.1f}° — rotate the wrist "
                        "between captures or the solve will fail."
                    )
                    self._diversity_label.classes(replace="text-caption text-negative")
                elif max_rot < handeye.WARN_ROTATION_DEG or n < RECOMMENDED_SAMPLES:
                    self._diversity_label.set_text(
                        f"{n} views, max relative rotation {max_rot:.1f}° — more "
                        "views / larger rotations improve accuracy."
                    )
                    self._diversity_label.classes(replace="text-caption text-warning")
                else:
                    self._diversity_label.set_text(
                        f"{n} views, max relative rotation {max_rot:.1f}°."
                    )
                    self._diversity_label.classes(replace="text-caption text-positive")

        if self._samples_container is not None:
            self._samples_container.clear()
            with self._samples_container:
                for i, s in enumerate(self._samples):
                    with ui.row().classes("items-center text-caption"):
                        ui.label(f"#{i + 1}")
                        ui.label(f"{len(s.detection.corners)} corners")
                        if i > 0:
                            R_rel = (
                                self._samples[i - 1].T_base_gripper[:3, :3].T
                                @ s.T_base_gripper[:3, :3]
                            )
                            delta = math.degrees(
                                float(
                                    np.linalg.norm(
                                        Rotation.from_matrix(R_rel).as_rotvec()
                                    )
                                )
                            )
                            ui.label(f"Δrot {delta:.1f}°")
                        ui.button(
                            icon="close",
                            on_click=lambda _, idx=i: self._delete_sample(idx),
                        ).props("flat dense round size=sm").mark(
                            f"handeye-sample-del-{i}"
                        )

    # ------------------------------------------------------- auto-calibration

    async def _on_auto_click(self) -> None:
        commander = self._commander
        if commander is None:
            return
        if self._auto_running:
            self._auto_cancel = True
            await commander.client.stop()
            ui.notify("Stopping after the current move", color="warning")
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
                "Stop cancels after the current move finishes."
            ).classes("text-warning")
            with ui.row():
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button(
                    "Start", icon="play_arrow", on_click=lambda: dialog.submit(True)
                ).mark("handeye-auto-confirm")
        try:
            confirmed = await dialog
        finally:
            dialog.delete()
        if not confirmed:
            return
        self._auto_cancel = False
        self._auto_task = background_tasks.create(
            self._auto_run(commander, context.client), name="handeye-auto-calibration"
        )

    async def _auto_run(self, commander: Commander, page_client: Client) -> None:
        """Drive the robot through :data:`AUTO_VIEW_DELTAS_DEG`, capture at
        each pose, return to the start pose, and solve. Runs as a background
        task; Stop sets ``_auto_cancel`` and halts the in-flight move, and the
        run aborts if the page that started it disconnects."""
        n = len(AUTO_VIEW_DELTAS_DEG)
        captured = 0
        skipped = 0
        error: str | None = None
        moved = False
        try:
            angles = await commander.client.angles()
            start_angles = list(angles) if angles is not None else None
            if start_angles is None:
                error = "Could not read joint angles"
            else:
                rejects = 0
                for i, deltas in enumerate(AUTO_VIEW_DELTAS_DEG):
                    if self._auto_cancel or not page_client.has_socket_connection:
                        break
                    progress = f"Pose {i + 1}/{n} — {captured} captured"
                    if skipped:
                        progress += f", {skipped} skipped"
                    self._set_auto_progress(progress)
                    target = [a + d for a, d in zip(start_angles, deltas, strict=True)]
                    if await self._auto_move(commander, target) < 0:
                        if self._auto_cancel:
                            break
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
                    rejects = 0
                    moved = True
                    if self._auto_cancel:
                        break
                    await self._wait_stationary()
                    await asyncio.sleep(AUTO_SETTLE_S)
                    try:
                        if await self._auto_capture():
                            captured += 1
                        else:
                            skipped += 1
                    except _CaptureRefused as e:
                        error = str(e)
                        break
            parked = True
            if moved and start_angles is not None and not self._auto_cancel:
                parked = await self._auto_return(commander, start_angles)
            if page_client.has_socket_connection:
                with page_client:
                    if not parked:
                        ui.notify(
                            "Robot left at the last view pose — the planner "
                            "refused the path back to the start pose. Jog it "
                            "clear before the next move.",
                            color="warning",
                        )
                    if error is not None:
                        ui.notify(
                            f"Auto-calibration aborted: {error}", color="negative"
                        )
                    elif self._auto_cancel:
                        ui.notify(
                            f"Auto-calibration stopped — {captured} views captured",
                            color="warning",
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
                            color="positive"
                            if captured >= RECOMMENDED_SAMPLES
                            else "warning",
                        )
                    if (
                        error is None
                        and not self._auto_cancel
                        and captured > 0
                        and len(self._samples) >= SOLVE_MIN_SAMPLES
                    ):
                        self._set_auto_progress("Solving…")
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
            self._auto_cancel = False
            self._set_auto_progress(None)

    async def _auto_return(
        self, commander: Commander, start_angles: list[float]
    ) -> bool:
        """Drive back to the start pose, returning whether the robot got
        there. One direct move covers the ordinary case; when the planner
        refuses it — see :data:`AUTO_RETURN_STEPS` — the same line is walked
        in sub-steps from wherever the arm actually stands."""
        self._set_auto_progress("Returning to start pose")
        if await self._auto_move(commander, start_angles) >= 0:
            return True
        angles = await commander.client.angles()
        if angles is None:
            return False
        here = list(angles)
        for k in range(1, AUTO_RETURN_STEPS + 1):
            fraction = k / AUTO_RETURN_STEPS
            target = [
                h + (s - h) * fraction for h, s in zip(here, start_angles, strict=True)
            ]
            if await self._auto_move(commander, target) < 0:
                logger.warning(
                    "Auto-calibration could not drive back to the start pose "
                    "(step %d of %d refused)",
                    k,
                    AUTO_RETURN_STEPS,
                )
                return False
        return True

    async def _auto_move(self, commander: Commander, target: list[float]) -> int:
        """Joint move with duration sized so the fastest joint stays under
        :data:`AUTO_DEG_PER_S`; returns the command index, or -1 when the
        controller rejects the move or the motion errors out. Completion is
        awaited in slices so Stop takes effect within a slice instead of at
        the end of the move."""
        current = await commander.client.angles()
        reference = current if current is not None else target
        span = max(abs(t - c) for t, c in zip(target, reference, strict=True))
        duration = max(AUTO_MIN_MOVE_S, span / AUTO_DEG_PER_S)
        deadline = time.monotonic() + duration + AUTO_MOVE_TIMEOUT_MARGIN_S
        try:
            index = await commander.client.move_j(target, duration=duration)
            if index < 0:
                return -1
            while not await commander.client.wait_command(
                index, timeout=AUTO_WAIT_SLICE_S
            ):
                if self._auto_cancel or time.monotonic() > deadline:
                    break
            return index
        except Exception as e:
            logger.warning("Auto-calibration move failed: %s", e)
            return -1

    async def _wait_stationary(self) -> None:
        deadline = time.monotonic() + AUTO_STATIONARY_TIMEOUT_S
        while time.monotonic() < deadline:
            if float(np.max(np.abs(robot_state.speeds))) < STATIONARY_SPEED_DEG_S:
                return
            await asyncio.sleep(0.05)

    async def _auto_capture(self) -> bool:
        """Capture with retries; True on success, False when this view never
        yields a usable board. Fatal refusals propagate and abort the run."""
        for attempt in range(AUTO_CAPTURE_ATTEMPTS):
            try:
                await self._capture_sample()
                return True
            except _CaptureRefused as e:
                if e.fatal:
                    raise
                if attempt + 1 < AUTO_CAPTURE_ATTEMPTS:
                    await asyncio.sleep(AUTO_CAPTURE_RETRY_S)
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
        """Swap the Auto-calibrate button into a Stop button while the run is
        active. Driven from the detection timer, so it also restores the idle
        state after a page reload mid-run."""
        running = self._auto_running
        if self._auto_btn is None or running == self._last_auto_running:
            return
        self._last_auto_running = running
        if running:
            self._auto_btn.set_text("Stop")
            self._auto_btn.props("icon=stop color=negative")
        else:
            self._auto_btn.set_text("Auto-calibrate")
            self._auto_btn.props("icon=play_circle color=primary")
        if self._clear_btn is not None:
            self._clear_btn.set_enabled(not running)
        if self._solve_btn is not None:
            self._solve_btn.set_enabled(
                not running and len(self._samples) >= SOLVE_MIN_SAMPLES
            )

    # ----------------------------------------------------------------- solve

    async def _solve(self) -> None:
        if self._auto_running:
            ui.notify("Auto-calibration is running", color="warning")
            return
        await self._run_solve()

    async def _run_solve(self) -> None:
        if len(self._samples) < SOLVE_MIN_SAMPLES:
            return
        method = self._method
        assert self._solve_btn is not None
        self._solve_btn.props("loading")
        try:
            result = await run.io_bound(
                handeye.solve_hand_eye, list(self._samples), self._spec, method=method
            )
        except handeye.CalibrationError as e:
            ui.notify(str(e), color="negative")
            return
        finally:
            self._solve_btn.props(remove="loading")
        if result is None:
            return
        self._result = result
        self._show_result(result)
        self._refresh_stage()
        if self._save_btn is not None:
            self._save_btn.set_enabled(True)
        if float(np.linalg.norm(result.T_cam2gripper[:3, 3])) > 500.0:
            ui.notify(
                "Camera offset exceeds 500 mm — the solution looks degenerate; "
                "recapture with more rotation diversity",
                color="warning",
            )

    def _show_result(self, result: handeye.HandEyeResult) -> None:
        if self._result_container is None:
            return
        (x, y, z), (rx, ry, rz) = handeye.matrix_to_xyz_rpy(result.T_cam2gripper)
        K = result.intrinsics.camera_matrix
        rms = result.intrinsics.reproj_rms_px
        self._result_container.clear()
        with self._result_container:
            ui.label("Camera → TCP transform").classes("text-caption text-grey")
            ui.label(f"X {x:+.1f}  Y {y:+.1f}  Z {z:+.1f} mm").classes("font-mono")
            ui.label(f"R {rx:+.1f}  P {ry:+.1f}  Y {rz:+.1f} °").classes("font-mono")
            with ui.row().classes("text-caption"):
                ui.label(f"Reproj RMS {rms:.2f} px").classes(
                    _quality_color(rms, _QUALITY_RMS_PX)
                )
                ui.label(
                    f"Residuals rot {result.rot_residual_deg[0]:.2f}° / "
                    f"trans {result.trans_residual_mm[0]:.1f} mm (mean)"
                )
                ui.label(f"Target spread {result.target_spread_mm:.1f} mm").classes(
                    _quality_color(result.target_spread_mm, _QUALITY_SPREAD_MM)
                )
            # Diagnostics and the solver choice are expert territory — folded
            # away so the headline stays transform + quality.
            with ui.expansion("Details").props("dense").classes("w-full text-caption"):
                ui.label(
                    f"fx {K[0, 0]:.1f}  fy {K[1, 1]:.1f}  "
                    f"cx {K[0, 2]:.1f}  cy {K[1, 2]:.1f} px"
                ).classes("font-mono text-caption")
                with ui.row().classes("items-center text-caption"):
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
                    ui.label("— re-solve to apply a different method").classes(
                        "text-grey"
                    )

    def _save(self) -> None:
        result = self._result
        if result is None:
            return
        tool_key = self._sample_tool_key or _selected_tool_key()
        tcp_offset = ng_app.storage.general.get(
            f"tcp_offset_{tool_key}", {"x": 0, "y": 0, "z": 0}
        )
        ng_app.storage.general[f"handeye/{tool_key}"] = handeye.to_storage_dict(
            result,
            self._spec,
            tool_key,
            tcp_offset,
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        ui.notify(f"Calibration saved for tool {tool_key}", color="positive")
        self._refresh_stored()

    def _refresh_stored(self) -> None:
        if self._stored_container is None:
            return
        self._stored_container.clear()
        tool_key = _selected_tool_key()
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
                    "text-caption text-negative"
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
                for k in ("x", "y", "z")
            ):
                ui.label(
                    "TCP offset changed since this calibration was saved — "
                    "the stored transform no longer matches the current TCP."
                ).classes("text-caption text-warning")

    # ------------------------------------------------------------- 3D scene

    def _on_scene_toggle(self) -> None:
        commander = self._commander
        if commander is None or commander.scene is None:
            return
        if self._scene_switch is not None and not self._scene_switch.value:
            commander.scene.clear(SCENE_GROUP)

    def _active_transform(self) -> np.ndarray | None:
        if self._result is not None:
            return self._result.T_cam2gripper
        stored = ng_app.storage.general.get(f"handeye/{_selected_tool_key()}")
        if stored:
            try:
                return np.asarray(stored["T_cam2gripper_mm"], dtype=np.float64).reshape(
                    4, 4
                )
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _refresh_scene_overlay(self) -> None:
        commander = self._commander
        if (
            commander is None
            or commander.scene is None
            or self._scene_switch is None
            or not self._scene_switch.value
        ):
            return
        X = self._active_transform()
        pose = np.asarray(robot_state.pose, dtype=np.float64)
        if X is None or pose.size != 16 or not np.any(pose):
            commander.scene.clear(SCENE_GROUP)
            return
        T_base_cam_m = (pose.reshape(4, 4) @ X).copy()
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
                axes.T, ("#ef4444", "#22c55e", "#3b82f6"), strict=True
            ):
                scene.line(
                    origin.tolist(), (origin + axis_len * axis).tolist()
                ).material(color)
            for c in corners:
                scene.line(origin.tolist(), c.tolist()).material("#94a3b8")
            for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
                scene.line(a.tolist(), b.tolist()).material("#94a3b8")
