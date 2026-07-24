"""End-to-end hand-eye calibration workflow through the panel UI.

Drives the real app (fake-serial controller) with a synthetic camera: for
each robot pose the fake backend serves a rendered ChArUco view consistent
with a known ground-truth camera mount, so the panel's capture → solve →
save flow must recover that transform.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import cv2
import numpy as np
import pytest
import waldoctl
from nicegui import app as ng_app
from nicegui.testing import User
from parol6.protocol.wire import StatusResultStruct
from scipy.spatial.transform import Rotation

from tests.helpers.charuco_render import board_center, render_board_view
from tests.helpers.wait import wait_for_app_ready
from waldo_commander.components.handeye_calibration import (
    STATIONARY_SPEED_DEG_S,
    HandEyeCalibrationPanel,
)
from waldo_commander.services import handeye
from waldo_commander.services.camera_service import camera_service
from waldo_commander.state import robot_state, ui_state

IMAGE_SIZE = (640, 480)
K_TRUE = np.array([[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]])

X_TRUE = np.eye(4)
X_TRUE[:3, :3] = Rotation.from_rotvec(np.radians([5.0, -4.0, 88.0])).as_matrix()
X_TRUE[:3, 3] = (35.0, -20.0, 55.0)

# Wrist+arm deltas from home. The board is fixed in the workspace, so large
# rotations are only possible about the camera's optical axis (J4/J6 rolls);
# J5 tilts swing the board toward the FOV edge and must stay small, and J1-J3
# moves translate the camera to vary the viewing distance.
VIEW_DELTAS_DEG = [
    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0, 0.0, 35.0),
    (0.0, 5.0, -7.0, 0.0, 0.0, -30.0),
    (0.0, -5.0, 7.0, 0.0, 7.0, 15.0),
    (0.0, 0.0, 0.0, 12.0, -7.0, -20.0),
    (0.0, 4.0, -5.0, -12.0, 5.0, 0.0),
    (4.0, -3.0, 4.0, 0.0, -6.0, 25.0),
]


class _FrameBackend:
    """Capture backend serving whatever JPEG the test put in the holder."""

    holder: ClassVar[dict[str, bytes]] = {"jpeg": b""}

    def open(self, device: int | str, width: int, height: int) -> bool:
        return True

    def read_frame(self) -> bytes | None:
        return self.holder["jpeg"] or None

    def close(self) -> None:
        pass


def _jpeg(image_bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    return buf.tobytes()


def _blank_jpeg() -> bytes:
    return _jpeg(np.full((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), 255, dtype=np.uint8))


async def _wait_for(condition, timeout: float = 5.0, message: str = "") -> None:
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message or "condition not met in time")


async def _current_pose() -> np.ndarray:
    st = await waldoctl.commander.client.status()
    assert isinstance(st, StatusResultStruct)
    return np.asarray(st.pose, dtype=np.float64).reshape(4, 4)


@pytest.mark.integration
async def test_handeye_panel_workflow(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from waldo_commander.services import camera_service as cam_module

    monkeypatch.setattr(cam_module, "LinuxpyBackend", _FrameBackend)
    monkeypatch.setattr(cam_module, "OpenCVBackend", _FrameBackend)
    _FrameBackend.holder["jpeg"] = _blank_jpeg()
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()

    camera_service.start(0)
    try:
        await user.open("/")
        await wait_for_app_ready()

        # The in-tree entry point surfaces the tab without monkeypatched discovery.
        await user.should_see(marker="tab-handeye")
        user.find(marker="tab-handeye").click()
        await asyncio.sleep(0)
        await user.should_see(marker="handeye-capture")

        panel = next(p for p in ui_state.plugin_panels if p.id == "handeye")
        assert isinstance(panel, HandEyeCalibrationPanel)
        spec = panel._spec

        client = waldoctl.commander.client
        angles = await client.angles()
        assert angles is not None
        home_angles = list(angles)

        # Board fixed in the base frame: centered 550 mm ahead of the home
        # pose's camera and pitched 40° — an oblique board is what makes
        # focal length (and thus depth) observable from planar views; the
        # FOV-limited wrist tilts alone cannot provide that foreshortening.
        T0 = await _current_pose()
        Tc = np.eye(4)
        Tc[:3, 3] = -board_center(spec)
        Rx = np.eye(4)
        Rx[:3, :3] = Rotation.from_euler("X", np.radians(40.0)).as_matrix()
        Tz = np.eye(4)
        Tz[:3, 3] = (0.0, 0.0, 550.0)
        T_base_target = T0 @ X_TRUE @ Tz @ Rx @ Tc

        for i, deltas in enumerate(VIEW_DELTAS_DEG):
            target = [a + d for a, d in zip(home_angles, deltas, strict=True)]
            assert (
                await client.move_j(target, duration=0.8, wait=True, timeout=15.0) >= 0
            )

            T_i = await _current_pose()
            T_cam_target = np.linalg.inv(T_i @ X_TRUE) @ T_base_target
            rendered = render_board_view(spec, K_TRUE, T_cam_target, IMAGE_SIZE)
            assert (
                handeye.detect_board(rendered, handeye.make_detector(spec)) is not None
            ), f"view {i}: board left the camera FOV — test geometry broken"
            frame = _jpeg(rendered)
            _FrameBackend.holder["jpeg"] = frame
            await _wait_for(
                lambda f=frame: camera_service.get_latest_frame() == f,
                message="camera loop did not pick up the rendered frame",
            )
            await _wait_for(
                lambda: (
                    float(np.max(np.abs(robot_state.speeds))) < STATIONARY_SPEED_DEG_S
                ),
                message="robot never reported stationary",
            )
            await user.should_see("Board detected")

            user.find(marker="handeye-capture").click()
            await _wait_for(
                lambda n=i + 1: len(panel._samples) == n,
                message=f"capture {i + 1} did not register",
            )
        n_views = len(VIEW_DELTAS_DEG)
        await user.should_see(f"{n_views} samples")

        user.find(marker="handeye-solve").click()
        await _wait_for(lambda: panel._result is not None, timeout=30.0)
        await user.should_see("Camera → TCP transform")

        result = panel._result
        assert result is not None
        trans_err = float(np.linalg.norm(result.T_cam2gripper[:3, 3] - X_TRUE[:3, 3]))
        rot_err = np.degrees(
            np.linalg.norm(
                Rotation.from_matrix(
                    result.T_cam2gripper[:3, :3].T @ X_TRUE[:3, :3]
                ).as_rotvec()
            )
        )
        # This test guards the capture/solve plumbing; solver accuracy under
        # ideal orbit geometry is covered by test_handeye_service, so the
        # bounds here are looser than the unit test's.
        assert trans_err < 15.0, f"translation off by {trans_err:.1f} mm"
        assert rot_err < 2.0, f"rotation off by {rot_err:.2f} deg"

        user.find(marker="handeye-save").click()
        await asyncio.sleep(0)
        tool_key = ng_app.storage.general.get("selected_tool", "NONE")
        stored = ng_app.storage.general.get(f"handeye/{tool_key}")
        assert stored is not None
        np.testing.assert_allclose(
            np.asarray(stored["T_cam2gripper_mm"]).reshape(4, 4),
            result.T_cam2gripper,
        )
        assert stored["n_samples"] == n_views

        # Without a detectable board the capture path stays gated.
        _FrameBackend.holder["jpeg"] = _blank_jpeg()
        await user.should_see("No board detected")
        user.find(marker="handeye-capture").click()
        await asyncio.sleep(0.2)
        assert len(panel._samples) == n_views
    finally:
        camera_service.stop()
        ng_app.storage.general.pop("handeye/NONE", None)
        ng_app.storage.general.pop("handeye/board", None)
