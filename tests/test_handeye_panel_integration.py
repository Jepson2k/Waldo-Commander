"""End-to-end hand-eye calibration workflow through the panel UI.

Drives the real app (fake-serial controller) with a synthetic camera: for
each robot pose the fake backend serves a rendered ChArUco view consistent
with a known ground-truth camera mount, so the panel's capture → solve →
save flow must recover that transform.

The MSG gripper is the tool under calibration — it has the built-in camera
mount, making it the primary hand-eye use case. It is selected through the
settings UI so the tool TCP switch and per-tool camera plumbing both engage,
and the solved transform is camera→MSG-TCP, saved in a named setup.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import ClassVar

import cv2
import numpy as np
import pytest
import waldoctl
from nicegui import app as ng_app
from nicegui import ui
from nicegui.testing import User
from parol6.protocol.wire import StatusResultStruct
from scipy.spatial.transform import Rotation
from waldoctl.setup import Frame, SetupSnapshot
from waldo_commander.setup import SetupStore, export_snapshot
from waldo_commander.camera import CameraUnavailable

from tests.helpers.charuco_render import board_center, render_board_view
from tests.helpers.wait import (
    enable_sim,
    ensure_robot_ready_for_motion,
    wait_for_app_ready,
)
from waldo_commander.components.handeye_calibration import (
    AUTO_VIEW_DELTAS_DEG,
    STATIONARY_SPEED_DEG_S,
    HandEyeCalibrationPanel,
)
from waldo_commander.services import handeye
from waldo_commander.services.camera_service import camera_service
from waldo_commander.state import robot_state, ui_state

IMAGE_SIZE = (1280, 960)
K_TRUE = np.array([[1600.0, 0.0, 640.0], [0.0, 1600.0, 480.0], [0.0, 0.0, 1.0]])

X_TRUE = np.eye(4)
X_TRUE[:3, :3] = Rotation.from_rotvec(np.radians([5.0, -4.0, 88.0])).as_matrix()
X_TRUE[:3, 3] = (35.0, -20.0, 55.0)

# The panel's auto-calibration pose set doubles as the manual-capture drive
# plan here — the deltas and their ordering rationale (FOV limits, MSG
# collision envelope, why fifteen views) live with AUTO_VIEW_DELTAS_DEG in
# the panel module. Within that envelope focal length (and thus depth) stays
# only marginally observable, so the intrinsics solve amplifies corner noise
# into hand-eye translation error; fifteen views average that noise down.
VIEW_DELTAS_DEG = AUTO_VIEW_DELTAS_DEG


class _FrameBackend:
    """Capture backend serving whatever JPEG the test put in the holder."""

    holder: ClassVar[dict[str, bytes]] = {"jpeg": b""}

    def open(self, device: int | str, width: int, height: int) -> bool:
        return True

    def read_frame(self) -> bytes | None:
        return self.holder["jpeg"] or None

    def close(self) -> None:
        pass


class _LiveBoardBackend:
    """Capture backend rendering the board view for the robot's *current*
    pose. The auto-calibration routine moves the robot itself, so frames
    must track the live pose instead of being injected per-view by the test.
    Renders only when the robot is stationary at a new pose; during motion it
    serves the previous render (detection mid-move is irrelevant — the panel
    only captures once stationary)."""

    spec: ClassVar[handeye.BoardSpec | None] = None
    T_base_target: ClassVar[np.ndarray | None] = None
    _cache: ClassVar[tuple[bytes, bytes] | None] = None

    def open(self, device: int | str, width: int, height: int) -> bool:
        return True

    def read_frame(self) -> bytes | None:
        spec, T_bt = self.spec, self.T_base_target
        pose = np.asarray(robot_state.pose, dtype=np.float64)
        if spec is None or T_bt is None or pose.size != 16 or not np.any(pose):
            return None
        moving = float(np.max(np.abs(robot_state.speeds))) >= STATIONARY_SPEED_DEG_S
        key = pose.tobytes()
        cache = type(self)._cache
        if cache is not None and (moving or cache[0] == key):
            return cache[1]
        T_cam_target = np.linalg.inv(pose.reshape(4, 4) @ X_TRUE) @ T_bt
        jpeg = _jpeg(render_board_view(spec, K_TRUE, T_cam_target, IMAGE_SIZE))
        type(self)._cache = (key, jpeg)
        return jpeg

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
@pytest.mark.parametrize("mount", ["tool", "fixed"])
async def test_handeye_panel_workflow(
    user: User, monkeypatch: pytest.MonkeyPatch, tmp_path, mount
) -> None:
    from waldo_commander.services import camera_service as cam_module

    monkeypatch.setattr(cam_module, "LinuxpyBackend", _FrameBackend)
    monkeypatch.setattr(cam_module, "OpenCVBackend", _FrameBackend)
    _FrameBackend.holder["jpeg"] = _blank_jpeg()
    monkeypatch.setenv("WALDO_SETUP_DIR", str(tmp_path))
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()

    try:
        await user.open("/")
        await wait_for_app_ready()

        # The camera rides the MSG gripper's built-in mount. Select MSG
        # through the settings UI so the TCP switch and per-tool camera
        # plumbing both engage.
        user.find(kind=ui.tab, content="Settings").click()
        await asyncio.sleep(0)
        tool_select = next(iter(user.find(marker="select-tool").elements))
        assert isinstance(tool_select, ui.select)

        async def select_tool(key: str) -> None:
            tool_select.set_value(key)
            await _wait_for(
                lambda: ng_app.storage.general.get("selected_tool") == key,
                message=f"{key} tool selection did not apply",
            )

        await select_tool("MSG")

        # The in-tree entry point surfaces the tab without monkeypatched discovery.
        await user.should_see(marker="tab-handeye")
        user.find(marker="tab-handeye").click()
        await asyncio.sleep(0)
        await user.should_see(marker="handeye-board-download")

        panel = next(p for p in ui_state.plugin_panels if p.id == "handeye")
        assert isinstance(panel, HandEyeCalibrationPanel)
        spec = panel._spec
        mount_select = next(iter(user.find(marker="camera-mount").elements))
        mount_select.set_value(mount)
        await asyncio.sleep(0)
        reference_setup = SetupSnapshot().with_frame(
            "stand", Frame((100, 50, 0, 0, 0, 30))
        )
        SetupStore(tmp_path).save("bench", reference_setup)
        assert panel._data_editor is not None
        panel._data_editor.reference.set_value("stand")

        # Progressive disclosure: with no camera active, only the board
        # section and the hint are shown — capture and solve stay hidden.
        assert panel._samples_section is not None
        assert panel._solve_section is not None
        assert not panel._samples_section.visible
        assert not panel._solve_section.visible

        # When the installed parol6 declares MSG's built-in camera mount, the
        # panel points at the device assignment instead of the generic hint.
        # Gated on the spec so this passes against parol6 versions from
        # before the mount declaration (CI falls back to the pinned tag
        # until the matching parol6 branch lands).
        if waldoctl.commander.robot.tools["MSG"].camera_spec is not None:
            await user.should_see("has a camera mount")

        # Assign a device to the mount and re-run the tool-camera flow.
        ng_app.storage.general["tool_camera/MSG"] = 0
        await select_tool("NONE")
        await select_tool("MSG")
        await _wait_for(
            lambda: camera_service.active,
            message="MSG tool camera did not start",
        )
        # The detection timer reveals the capture stage once the camera runs.
        await _wait_for(
            lambda: (
                panel._samples_section is not None and panel._samples_section.visible
            ),
            message="capture stage did not appear after camera start",
        )
        assert not panel._solve_section.visible

        client = waldoctl.commander.client
        # Earlier suite tests share this controller and may leave the arm
        # anywhere; the view deltas assume the standby pose, so pin it. With
        # the MSG mounted this is the planned, collision-checked home move.
        assert await client.home(wait=True, timeout=30.0) >= 0
        angles = await client.angles()
        assert angles is not None
        home_angles = list(angles)

        # Board fixed in the base frame: centered 650 mm ahead of the home
        # pose's camera and pitched 40° — an oblique board is what makes
        # focal length (and thus depth) observable from planar views; the
        # FOV-limited wrist tilts alone cannot provide that foreshortening.
        T0 = await _current_pose()
        Tc = np.eye(4)
        Tc[:3, 3] = -board_center(spec)
        Rx = np.eye(4)
        Rx[:3, :3] = Rotation.from_euler("X", np.radians(40.0)).as_matrix()
        Tz = np.eye(4)
        Tz[:3, 3] = (0.0, 0.0, 650.0)
        T_base_target = T0 @ X_TRUE @ Tz @ Rx @ Tc
        fixed_camera = T0 @ X_TRUE
        tool_board = X_TRUE @ Tz @ Rx @ Tc

        for i, deltas in enumerate(VIEW_DELTAS_DEG):
            target = [a + d for a, d in zip(home_angles, deltas, strict=True)]
            assert (
                await client.move_j(target, duration=0.8, wait=True, timeout=15.0) >= 0
            )

            T_i = await _current_pose()
            T_cam_target = (
                np.linalg.inv(fixed_camera) @ T_i @ tool_board
                if mount == "fixed"
                else np.linalg.inv(T_i @ X_TRUE) @ T_base_target
            )
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
            # The first hi-res detection after a cold start can outlast
            # should_see's short retry window — wait on the tick's state.
            await _wait_for(
                lambda: (panel._last_status_text or "").startswith("Board detected"),
                timeout=15.0,
                message=f"view {i}: detect tick did not report the board",
            )
            await user.should_see("Board detected")

            user.find(marker="handeye-capture").click()
            await _wait_for(
                lambda n=i + 1: len(panel._samples) == n,
                message=f"capture {i + 1} did not register",
            )
        n_views = len(VIEW_DELTAS_DEG)
        await user.should_see(f"{n_views} samples")
        # Enough samples to solve — the solve stage is revealed.
        assert panel._solve_section.visible

        user.find(marker="handeye-solve").click()
        await _wait_for(lambda: panel._result is not None, timeout=30.0)
        await user.should_see(
            "Camera → WRF transform" if mount == "fixed" else "Camera → TCP transform"
        )

        result = panel._result
        assert result is not None
        expected = fixed_camera if mount == "fixed" else X_TRUE
        trans_err = float(
            np.linalg.norm(result.T_camera_parent[:3, 3] - expected[:3, 3])
        )
        rot_err = np.degrees(
            np.linalg.norm(
                Rotation.from_matrix(
                    result.T_camera_parent[:3, :3].T @ expected[:3, :3]
                ).as_rotvec()
            )
        )
        # This test guards the capture/solve plumbing; solver accuracy under
        # ideal orbit geometry is covered by test_handeye_service. The
        # FOV-limited tilts here leave depth marginally observable, so the
        # solve amplifies tiny pose/corner differences between runs — the
        # bounds only need to separate "recovered the mount" from garbage.
        assert trans_err < 30.0, f"translation off by {trans_err:.1f} mm"
        assert rot_err < 3.0, f"rotation off by {rot_err:.2f} deg"

        user.find(marker="handeye-save").click()
        await user.should_see("Saved bench/camera", retries=50)
        saved = SetupStore(tmp_path).load("bench")
        calibration = saved.cameras["camera"]
        actual = (
            saved.frame_matrix("stand") @ calibration.pose.matrix()
            if mount == "fixed"
            else calibration.pose.matrix()
        )
        np.testing.assert_allclose(actual, result.T_camera_parent, atol=1e-8)
        assert calibration.quality.sample_count == n_views
        exported = {}
        exec(export_snapshot(saved), exported)
        np.testing.assert_allclose(
            exported["setup"].cameras["camera"].pose.matrix(),
            calibration.pose.matrix(),
            atol=1e-8,
        )
        user.find(marker="camera-load").click()
        await user.should_see("Bindings match:", retries=50)
        user.find(marker="camera-export").click()

        if mount == "tool":
            original = handeye.to_storage_dict(
                result, spec, "MSG", {"x": 0, "y": 0, "z": 0}, "2025-01-01T00:00:00Z"
            )
            ng_app.storage.general["handeye/MSG"] = original
            panel._data_editor.confirm.set_value(True)
            user.find(marker="camera-import").click()
            await user.should_see("Imported measurement:", retries=50)
            imported = SetupStore(tmp_path).load("bench").cameras["camera"]
            assert imported.calibrated_at == "2025-01-01T00:00:00Z"
            np.testing.assert_allclose(
                imported.pose.matrix(), result.T_camera_parent, atol=1e-8
            )
            assert ng_app.storage.general["handeye/MSG"] == original
        else:
            SetupStore(tmp_path).save(
                "bench", saved.with_frame("stand", Frame((101, 50, 0, 0, 0, 30)))
            )
            # A capture gap must not mask a changed setup once frames resume.
            _FrameBackend.holder["jpeg"] = b""

            def frame_is_stale() -> bool:
                try:
                    camera_service.snapshot()
                except CameraUnavailable:
                    return True
                return False

            await _wait_for(
                frame_is_stale, message="camera did not observe the capture gap"
            )
            user.find(marker="camera-load").click()
            await asyncio.sleep(0)
            _FrameBackend.holder["jpeg"] = frame
            try:
                await user.should_see("Camera reference frame changed", retries=50)
            except AssertionError as error:
                error.add_note(
                    f"Calibration check reported: {panel._data_editor.message.text}"
                )
                raise

        # A page rebuild (reload, second tab) must not restart the camera: a
        # new capture session would refuse every later capture as "changed".
        session = camera_service.snapshot().session_id
        ui_state.active_client_id = None
        await user.open("/")
        await wait_for_app_ready()
        user.find(marker="tab-handeye").click()
        await asyncio.sleep(0)
        assert camera_service.snapshot().session_id == session, (
            "rebuilding the page restarted the camera"
        )
        assert next(p for p in ui_state.plugin_panels if p.id == "handeye") is panel
        await _wait_for(
            lambda: (panel._last_status_text or "").startswith("Board detected"),
            timeout=15.0,
            message="detect tick did not report the board after the reload",
        )
        user.find(marker="handeye-capture").click()
        await _wait_for(
            lambda: len(panel._samples) == n_views + 1,
            message="a capture after the page reload was refused",
        )
        n_views += 1

        # Without a detectable board the capture path stays gated.
        blank = _blank_jpeg()
        _FrameBackend.holder["jpeg"] = blank
        await _wait_for(
            lambda: camera_service.get_latest_frame() == blank,
            message="camera loop did not pick up the blank frame",
        )
        # Detection on a hi-res frame can outlast should_see's retry window
        # on a loaded runner — wait on the tick's own state first.
        await _wait_for(
            lambda: panel._last_status_text == "No board detected",
            timeout=15.0,
            message="detect tick did not clear the board",
        )
        await user.should_see("No board detected")
        user.find(marker="handeye-capture").click()
        await asyncio.sleep(0.2)
        assert len(panel._samples) == n_views
    except BaseException:
        import traceback

        traceback.print_exc()
        raise
    finally:
        camera_service.stop()
        for key in ("handeye/MSG", "handeye/board", "tool_camera/MSG", "selected_tool"):
            ng_app.storage.general.pop(key, None)


@pytest.mark.integration
# The routine drives the full pose set and walks back along it, so this test
# runs ~30 simulated moves plus a solve — well past the 90 s global budget.
# Trimming the pose set would cost exactly what the test is for: proving the
# built-in poses are collision-safe end to end and calibrate unattended.
@pytest.mark.timeout(420)
async def test_handeye_auto_calibration(
    user: User, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Auto-calibrate drives the robot through the built-in pose set on its
    own: confirm the dialog, then the routine moves, captures and solves
    unattended, tries to drive back to the start pose, and leaves saving to
    the user. A second run checks that Stop cancels cleanly, keeping the
    extra samples without re-solving.
    """
    from waldo_commander.components import handeye_calibration as hp
    from waldo_commander.services import camera_service as cam_module

    monkeypatch.setattr(cam_module, "LinuxpyBackend", _LiveBoardBackend)
    monkeypatch.setattr(cam_module, "OpenCVBackend", _LiveBoardBackend)
    # The real pacing constants are hardware-safe (slow); the simulator does
    # not need the caution.
    monkeypatch.setattr(hp, "AUTO_MIN_MOVE_S", 0.8)
    monkeypatch.setattr(hp, "AUTO_DEG_PER_S", 60.0)
    monkeypatch.setattr(hp, "AUTO_SETTLE_S", 0.25)
    _LiveBoardBackend.spec = None
    _LiveBoardBackend.T_base_target = None
    _LiveBoardBackend._cache = None
    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
    panel: HandEyeCalibrationPanel | None = None

    try:
        await user.open("/")
        await wait_for_app_ready()

        user.find(kind=ui.tab, content="Settings").click()
        await asyncio.sleep(0)
        tool_select = next(iter(user.find(marker="select-tool").elements))
        assert isinstance(tool_select, ui.select)

        async def select_tool(key: str) -> None:
            tool_select.set_value(key)
            await _wait_for(
                lambda: ng_app.storage.general.get("selected_tool") == key,
                message=f"{key} tool selection did not apply",
            )

        await select_tool("MSG")
        await user.should_see(marker="tab-handeye")
        found = next(p for p in ui_state.plugin_panels if p.id == "handeye")
        assert isinstance(found, HandEyeCalibrationPanel)
        panel = found

        # Same fixed-board geometry as the manual test, but handed to the
        # backend up front — the routine picks its own poses.
        T0 = await _current_pose()
        Tc = np.eye(4)
        Tc[:3, 3] = -board_center(panel._spec)
        Rx = np.eye(4)
        Rx[:3, :3] = Rotation.from_euler("X", np.radians(40.0)).as_matrix()
        Tz = np.eye(4)
        Tz[:3, 3] = (0.0, 0.0, 650.0)
        _LiveBoardBackend.T_base_target = T0 @ X_TRUE @ Tz @ Rx @ Tc
        _LiveBoardBackend.spec = panel._spec

        ng_app.storage.general["tool_camera/MSG"] = 0
        await select_tool("NONE")
        await select_tool("MSG")
        await _wait_for(
            lambda: camera_service.active, message="MSG tool camera did not start"
        )

        user.find(marker="tab-handeye").click()
        await asyncio.sleep(0)
        # Pin the start pose for the same reason as the manual workflow test:
        # the shared controller carries over whatever pose the suite left.
        assert await waldoctl.commander.client.home(wait=True, timeout=30.0) >= 0
        angles = await waldoctl.commander.client.angles()
        assert angles is not None
        home_angles = list(angles)

        async def wait_board_detected() -> None:
            await _wait_for(
                lambda: (panel._last_status_text or "").startswith("Board detected"),
                timeout=15.0,
                message="detect tick did not report the board",
            )

        await wait_board_detected()
        # One dropped frame during the run is retried, not fatal.
        real_next_snapshot = camera_service.next_snapshot
        drops: list[str] = []

        async def flaky_next_snapshot(**kwargs):
            if not drops:
                drops.append("dropped")
                raise CameraUnavailable(
                    "No new camera frame arrived before the deadline"
                )
            return await real_next_snapshot(**kwargs)

        monkeypatch.setattr(camera_service, "next_snapshot", flaky_next_snapshot)

        user.find(marker="handeye-auto").click()
        await user.should_see(marker="handeye-auto-confirm")
        user.find(marker="handeye-auto-confirm").click()
        await _wait_for(lambda: panel._auto_running, message="auto run did not start")

        n_views = len(AUTO_VIEW_DELTAS_DEG)
        await _wait_for(
            lambda: panel._auto_task is not None and panel._auto_task.done(),
            timeout=240.0,
            message="auto run did not finish",
        )

        # All poses are accepted from home (the manual test proves that) and
        # every view renders a detectable board; a small allowance covers a
        # rare timing skip.
        assert len(panel._samples) >= n_views - 3, (
            f"only {len(panel._samples)}/{n_views} views captured"
        )
        result = panel._result
        assert result is not None, "auto run did not solve"
        assert drops, "the dropped frame was never exercised"
        trans_err = float(np.linalg.norm(result.T_camera_parent[:3, 3] - X_TRUE[:3, 3]))
        rot_err = np.degrees(
            np.linalg.norm(
                Rotation.from_matrix(
                    result.T_camera_parent[:3, :3].T @ X_TRUE[:3, :3]
                ).as_rotvec()
            )
        )
        assert trans_err < 30.0, f"translation off by {trans_err:.1f} mm"
        assert rot_err < 3.0, f"rotation off by {rot_err:.2f} deg"

        # The run ends where it started. With the MSG mounted this exercises
        # the collision guard on the way back — the whole sweep stays clear
        # of the arm, so anything short of the start pose means the planner
        # wrongly refused the return and left the robot parked mid-sweep.
        end_angles = await waldoctl.commander.client.angles()
        assert end_angles is not None
        drift = max(abs(e - h) for e, h in zip(end_angles, home_angles, strict=True))
        assert drift < 1.0, f"did not return to the start pose (drift {drift:.2f}°)"

        # Solve is automatic, saving is not — a bad autonomous run must not
        # clobber a stored calibration.
        assert ng_app.storage.general.get("handeye/MSG") is None

        # Stop ends a run in flight: the samples already taken survive, the
        # previous solve is left alone, and the progress line clears.
        n_before = len(panel._samples)
        await wait_board_detected()
        user.find(marker="handeye-auto").click()
        await user.should_see(marker="handeye-auto-confirm")
        user.find(marker="handeye-auto-confirm").click()
        await _wait_for(
            lambda: panel._auto_running,
            timeout=30.0,
            message="second run did not start",
        )
        user.find(marker="handeye-auto").click()
        await _wait_for(
            lambda: panel._auto_task is not None and panel._auto_task.done(),
            timeout=60.0,
            message="Stop did not end the run",
        )
        assert len(panel._samples) >= n_before
        assert panel._result is (result if len(panel._samples) == n_before else None)
        assert panel._auto_progress_text is None

        # A refused move is a planner verdict the routine is built to absorb,
        # not a defect — but the controller logs each one at ERROR. Drop just
        # those records so the fixture's blanket ERROR check still guards
        # everything else.
        caplog.get_records("call")[:] = [
            r
            for r in caplog.get_records("call")
            if "Self-collision predicted" not in r.getMessage()
        ]
    except BaseException:
        import traceback

        traceback.print_exc()
        raise
    finally:
        camera_service.stop()
        _LiveBoardBackend.spec = None
        _LiveBoardBackend.T_base_target = None
        _LiveBoardBackend._cache = None
        # Let the panel settle on the stopped camera before the fixture tears
        # the app down. A live board keeps the detection tick rewriting the
        # corner overlay, and an element update still in flight when the
        # fixture restores NiceGUI's globals fails to emit — which NiceGUI
        # logs, which the log panel renders, which queues another update.
        if panel is not None:
            with contextlib.suppress(AssertionError):
                await _wait_for(
                    lambda: panel is not None
                    and panel._last_status_text == "No camera active",
                    timeout=3.0,
                )
        await asyncio.sleep(0.5)
        for key in ("handeye/MSG", "handeye/board", "tool_camera/MSG", "selected_tool"):
            ng_app.storage.general.pop(key, None)


@pytest.mark.integration
async def test_auto_move_distinguishes_late_completion_from_stop(
    user: User, monkeypatch
):
    """A timed-out poll can return after a newer completion status arrived."""
    await user.open("/")
    await wait_for_app_ready()
    commander = waldoctl.commander
    client = commander.client
    assert await client.home(wait=True, timeout=30) >= 0
    angles = list(await client.angles())
    target = [*angles[:5], angles[5] - 10]
    panel = HandEyeCalibrationPanel()
    wait_command = client.wait_command
    polls = 0
    delayed = False

    async def delayed_timeout(index, timeout=10):
        nonlocal polls, delayed
        polls += 1
        done = await wait_command(index, timeout=timeout)
        if polls == 2 and not done:
            assert await wait_command(index, timeout=10)
            await _wait_for(
                lambda: commander.status.action.state == waldoctl.ActionState.IDLE
            )
            delayed = True
        return done

    monkeypatch.setattr(client, "wait_command", delayed_timeout)
    try:
        index = await panel._auto_move(commander, target)
        assert delayed, "The deadline/completion interleaving was not exercised"
        assert index >= 0 and not panel._auto_cancel, (
            "A completed move was mistaken for a Stop"
        )
        np.testing.assert_allclose(await client.angles(), target, atol=0.1)
        saw_running = asyncio.Event()

        async def observed_poll(index, timeout=10):
            done = await wait_command(index, timeout=timeout)
            if (
                not done
                and commander.status.action.state == waldoctl.ActionState.EXECUTING
            ):
                saw_running.set()
            return done

        monkeypatch.setattr(client, "wait_command", observed_poll)
        pending = asyncio.create_task(panel._auto_move(commander, angles))
        await asyncio.wait_for(saw_running.wait(), timeout=5)
        await client.stop()
        assert await asyncio.wait_for(pending, timeout=5) < 0
        assert panel._auto_cancel
    finally:
        await client.stop()


async def test_an_external_stop_ends_the_auto_run(user: User) -> None:
    """A Stop from anywhere else aborts auto-calibration.

    The controller cancels the command without completing it and without an
    error, so `wait_command` resolves neither True nor raises — and it stays
    enabled through a Stop. A run that read the halt as success would capture
    a view at the halted pose and then drive the arm to the next one, seconds
    after a human deliberately stopped it.

    Driven against the controller: the halt is an ack ladder and a status
    edge, and a fake that answers both tests the panel against this test's
    idea of a Stop rather than against the one the arm performs.
    """
    from waldo_commander.components.handeye_calibration import (
        HandEyeCalibrationPanel,
    )

    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
    await user.open("/")
    await wait_for_app_ready()
    await enable_sim(user)
    await ensure_robot_ready_for_motion()
    commander = waldoctl.commander
    client = commander.client
    panel = HandEyeCalibrationPanel()

    start = await client.angles()
    assert start is not None
    target = list(start)
    target[0] += 25.0  # long enough at the auto speed to stop it mid-move
    moving = asyncio.create_task(panel._auto_move(commander, target))
    try:
        assert await client.wait_status(
            lambda s: s.action_state == waldoctl.ActionState.EXECUTING, timeout=10
        ), "the auto move never started"
        assert await client.stop() > 0
        index = await asyncio.wait_for(moving, 30)
    finally:
        if not moving.done():
            moving.cancel()
            await asyncio.gather(moving, return_exceptions=True)
        await client.reset()

    assert index < 0, "a halted move must not report the index of a finished one"
    assert panel._auto_cancel, "the run must stop, not roll on to the next view"
    halted = await client.angles()
    assert halted is not None
    assert abs(halted[0] - target[0]) > 1.0, (
        "the arm reached the view it was stopped on the way to"
    )


@pytest.mark.integration
async def test_clearing_a_board_field_reverts_instead_of_wedging(user: User) -> None:
    """NiceGUI sets a number's value to None the moment its text is cleared,
    which is what selecting a field to retype it does. Parsing that raised
    TypeError past the handler's except, so the revert never ran, the field
    stayed blank, and every later edit to any of the five inputs raised
    through it again.

    Driven through the panel's own inputs: the bug lives in the handler they
    fire, so a test that rebuilds the spec itself cannot see it.
    """
    from waldo_commander.components.handeye_calibration import (
        HandEyeCalibrationPanel,
    )

    ui_state.plugin_panels = []
    ui_state._started_panel_ids = set()
    await user.open("/")
    await wait_for_app_ready()
    await user.should_see(marker="tab-handeye")
    user.find(marker="tab-handeye").click()
    await asyncio.sleep(0)
    await user.should_see(marker="handeye-board-download")
    panel = next(p for p in ui_state.plugin_panels if p.id == "handeye")
    assert isinstance(panel, HandEyeCalibrationPanel)

    def field(marker):
        return next(iter(user.find(marker=marker).elements))

    before = panel._spec
    field("handeye-squares-x").set_value(None)
    await asyncio.sleep(0)
    assert panel._spec == before, "an emptied field means unchanged, not zero"

    # The field the user retypes next still applies — which it could not while
    # the cleared one left the handler raising on every later edit.
    field("handeye-square-mm").set_value(before.square_mm + 5.0)
    await asyncio.sleep(0)
    assert panel._spec.square_mm == pytest.approx(before.square_mm + 5.0)
    assert panel._spec.squares_x == before.squares_x
    applied = panel._spec

    # A board the detector refuses is reverted in the inputs, so what the
    # fields show is the board being used.
    field("handeye-marker-mm").set_value(applied.square_mm + 5.0)
    await asyncio.sleep(0)
    assert panel._spec == applied, "a refused board must not be adopted"
    assert field("handeye-marker-mm").value == pytest.approx(applied.marker_mm), (
        "the refused value is reverted, not left in the field"
    )
