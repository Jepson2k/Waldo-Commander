"""Eye-in-hand hand-eye calibration panel.

Workflow: print a generated ChArUco board, fix it in the workspace, jog the
robot so the tool camera sees the board from 10-15 rotation-diverse poses,
capture a synchronized (TCP pose, frame) sample at each, then solve camera
intrinsics + the camera→TCP transform and save it per tool.

Registered through the ``waldoctl.panels`` entry-point group, so the host
mounts it like any third-party panel; being in-tree it may also use
Waldo-Commander internals (camera service, robot_state) directly.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime

import numpy as np
from nicegui import app as ng_app
from nicegui import run, ui
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
        self._result: handeye.HandEyeResult | None = None
        self._last_detection: handeye.Detection | None = None
        self._detect_busy = False
        self._decode_failures = 0
        self._camera_was_active = camera_service.active
        self._reset_element_refs()

    def _reset_element_refs(self) -> None:
        self._image: ui.interactive_image | None = None
        self._camera_card: ui.card | None = None
        self._camera_hint: ui.row | None = None
        self._status_label: ui.label | None = None
        self._capture_btn: ui.button | None = None
        self._solve_btn: ui.button | None = None
        self._save_btn: ui.button | None = None
        self._method_select: ui.select | None = None
        self._sample_count: ui.label | None = None
        self._samples_container: ui.column | None = None
        self._diversity_label: ui.label | None = None
        self._result_container: ui.column | None = None
        self._stored_container: ui.column | None = None
        self._scene_switch: ui.switch | None = None
        self._commander: Commander | None = None

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
            self._build_samples_section()
            self._build_solve_section()
            self._build_stored_section()

        ui.timer(DETECT_INTERVAL_S, self._detect_tick)
        self._refresh_samples()
        self._refresh_stored()

    def _build_board_section(self) -> None:
        with ui.expansion("Target board", icon="grid_on").classes("w-full"):
            with ui.row().classes("items-end"):
                sx = ui.number(
                    "Squares X", value=self._spec.squares_x, min=3, max=20, precision=0
                )
                sy = ui.number(
                    "Squares Y", value=self._spec.squares_y, min=3, max=20, precision=0
                )
                sq = ui.number(
                    "Square mm", value=self._spec.square_mm, min=5.0, step=0.5
                )
                mk = ui.number(
                    "Marker mm", value=self._spec.marker_mm, min=3.0, step=0.5
                )
                dic = ui.select(
                    list(handeye.ARUCO_DICTIONARIES),
                    value=self._spec.dictionary,
                    label="Dictionary",
                )

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
            ui.label("No camera active — enable a tool camera in Settings.").classes(
                "text-caption text-grey"
            )
        self._status_label = ui.label("No board detected").classes("text-caption")
        self._status_label.mark("handeye-detect-status")
        self._set_camera_visibility(camera_service.active)

    def _build_samples_section(self) -> None:
        with ui.row().classes("items-center"):
            self._capture_btn = ui.button(
                "Capture", icon="add_a_photo", on_click=self._capture
            )
            self._capture_btn.mark("handeye-capture")
            ui.button("Clear", icon="delete_sweep", on_click=self._on_clear).props(
                "outline"
            ).mark("handeye-clear")
            self._sample_count = ui.label("0 samples")
            self._sample_count.mark("handeye-sample-count")
        self._diversity_label = ui.label("").classes("text-caption")
        self._diversity_label.mark("handeye-diversity")
        self._samples_container = ui.column().classes("w-full gap-0")

    def _build_solve_section(self) -> None:
        with ui.row().classes("items-center"):
            self._method_select = ui.select(
                list(handeye.HAND_EYE_METHODS), value="PARK", label="Method"
            ).classes("w-32")
            self._method_select.mark("handeye-method")
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

    # ------------------------------------------------------------- detection

    def _set_camera_visibility(self, active: bool) -> None:
        if self._camera_card is not None:
            self._camera_card.set_visibility(active)
        if self._camera_hint is not None:
            self._camera_hint.set_visibility(not active)
        if active and not self._camera_was_active and self._image is not None:
            # Force the browser to reconnect the MJPEG stream.
            self._image.set_source(f"/tool/camera/stream?t={time.time()}")
        self._camera_was_active = active

    async def _detect_tick(self) -> None:
        self._set_camera_visibility(camera_service.active)
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
        self._last_detection = detection
        if self._status_label is not None:
            self._status_label.set_text(message)
        if self._image is not None:
            if detection is None:
                self._image.set_content("")
            else:
                self._image.set_content(
                    "".join(
                        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                        'stroke="#2dd4bf" stroke-width="1.5" fill="none"/>'
                        for x, y in detection.corners.reshape(-1, 2)
                    )
                )
        if self._capture_btn is not None:
            self._capture_btn.set_enabled(detection is not None)

    # --------------------------------------------------------------- capture

    async def _capture(self) -> None:
        commander = self._commander
        if commander is None:
            return
        if float(np.max(np.abs(robot_state.speeds))) > STATIONARY_SPEED_DEG_S:
            ui.notify("Robot is moving — hold still to capture", color="warning")
            return
        raw = camera_service.get_latest_frame()
        frame = handeye.decode_jpeg(raw)
        if frame is None:
            ui.notify("No camera frame available", color="warning")
            return
        detection = await run.io_bound(handeye.detect_board, frame, self._detector)
        if detection is None:
            ui.notify("Board not detected in the captured frame", color="warning")
            return
        if (
            self._samples
            and detection.image_size != self._samples[0].detection.image_size
        ):
            ui.notify(
                "Camera resolution changed — clear samples to restart", color="negative"
            )
            return
        tool_key = _selected_tool_key()
        if self._sample_tool_key is None:
            self._sample_tool_key = tool_key
        elif tool_key != self._sample_tool_key:
            ui.notify(
                f"Tool changed ({self._sample_tool_key} → {tool_key}) — "
                "clear samples first",
                color="negative",
            )
            return

        pose = await self._current_pose_matrix(commander)
        if pose is None:
            ui.notify("Could not read robot pose", color="negative")
            return
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
            self._solve_btn.set_enabled(n >= SOLVE_MIN_SAMPLES)

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

    # ----------------------------------------------------------------- solve

    async def _solve(self) -> None:
        if len(self._samples) < SOLVE_MIN_SAMPLES:
            return
        method = str(self._method_select.value) if self._method_select else "PARK"
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
            ui.label(
                f"fx {K[0, 0]:.1f}  fy {K[1, 1]:.1f}  "
                f"cx {K[0, 2]:.1f}  cy {K[1, 2]:.1f} px"
            ).classes("font-mono text-caption")
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
            ui.label(f"{result.n_views} views · {result.method}").classes(
                "text-caption text-grey"
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
        stored = ng_app.storage.general.get(f"handeye/{tool_key}")
        with self._stored_container:
            if not stored:
                ui.label(f"No stored calibration for tool {tool_key}").classes(
                    "text-caption text-grey"
                )
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
