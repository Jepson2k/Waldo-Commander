"""Explicit save, import and export of measured camera setup snapshots."""

from collections.abc import Awaitable, Callable

from nicegui import app as ng_app
from nicegui import ui
from waldoctl import Commander
from waldoctl.camera import CameraCalibration
from waldoctl.setup import SetupSnapshot

from waldo_commander.camera import CameraUnavailable
from waldo_commander.services import handeye
from waldo_commander.services.camera_calibration import import_saved_handeye
from waldo_commander.services.camera_service import camera_service
from waldo_commander.services.tcp_calibration import read_applied_tcp
from waldo_commander.setup import SetupStore, export_snapshot


class CameraCalibrationData:
    def __init__(
        self,
        commander: Commander,
        measurement: Callable[[SetupSnapshot, str], Awaitable[CameraCalibration]],
    ) -> None:
        self.commander = commander
        self.measurement = measurement
        self.store = SetupStore()
        with ui.expansion("Saved camera data", icon="save").classes(
            "w-full"
        ) as self.container:
            with ui.row().classes("items-center w-full"):
                self.setup_name = (
                    ui.select(
                        list(dict.fromkeys(["bench", *self.store.names()])),
                        value="bench",
                        label="Setup",
                        new_value_mode="add-unique",
                    )
                    .props("use-input")
                    .props("dense")
                    .classes("flex-1 min-w-0")
                    .mark("camera-setup")
                )
                self.name = (
                    ui.input("Camera name", value="camera")
                    .props("dense")
                    .classes("flex-1 min-w-0")
                    .mark("camera-name")
                )
                self.reference = (
                    ui.input("Reference frame", value="WRF")
                    .props("dense")
                    .classes("w-full")
                    .mark("camera-reference")
                )
            ui.label("Save keeps the result in this setup.").classes("text-caption")
            with ui.row():
                ui.button("Load / check", on_click=self.load).props(
                    "dense outline"
                ).mark("camera-load")
                ui.button("Export snapshot", on_click=self.export).props(
                    "dense outline"
                ).mark("camera-export")
            self.message = (
                ui.label().classes("text-caption").mark("camera-data-message")
            )
            with ui.expansion("Import older measurement").classes("w-full"):
                self.confirm = ui.checkbox(
                    "Same physical camera, lens and mount as the stored measurement"
                ).mark("camera-import-confirm")
                ui.button(
                    "Import for current tool", on_click=self.import_measurement
                ).props("dense outline").mark("camera-import")
                ui.label(
                    "Binds a copy to the current camera and tool variant."
                ).classes("text-caption")

    def snapshot(self) -> SetupSnapshot:
        try:
            return self.store.load(self.setup_name.value)
        except FileNotFoundError:
            return SetupSnapshot()

    def describe(self, calibration: CameraCalibration) -> str:
        width, height = calibration.intrinsics.image_size
        return f"{calibration.mount} camera · {width}×{height} · {calibration.quality.sample_count} views · RMS {calibration.quality.reproj_rms_px:.2f} px · {calibration.calibrated_at}"

    async def save(self) -> None:
        try:
            setup = self.snapshot()
            calibration = await self.measurement(setup, self.reference.value)
            self.store.save(
                self.setup_name.value, setup.with_camera(self.name.value, calibration)
            )
            self.container.set_value(True)
            self.message.set_text(
                f"Saved {self.setup_name.value}/{self.name.value}: {self.describe(calibration)}"
            )
        except (ValueError, OSError, TimeoutError, CameraUnavailable) as error:
            self.message.set_text(str(error))
            self.container.set_value(True)

    async def load(self) -> None:
        try:
            setup = self.store.load(self.setup_name.value)
            calibration = setup.cameras[self.name.value]
            self.reference.set_value(
                calibration.pose.frame if calibration.mount == "fixed" else "WRF"
            )
            observation = await camera_service.next_snapshot()
            frame = handeye.decode_jpeg(observation.jpeg)
            if frame is None:
                raise CameraUnavailable("Camera image cannot be decoded")
            tool = (
                await read_applied_tcp(self.commander.client)
                if calibration.mount == "tool"
                else None
            )
            if camera_service.snapshot().session_id != observation.session_id:
                raise CameraUnavailable("Camera changed while checking calibration")
            calibration.validate(
                setup,
                camera_id=observation.camera_id,
                image_size=(frame.shape[1], frame.shape[0]),
                backend=self.commander.robot.backend_package,
                tool=tool,
            )
            self.message.set_text(f"Bindings match: {self.describe(calibration)}")
        except (
            ValueError,
            KeyError,
            OSError,
            TimeoutError,
            CameraUnavailable,
        ) as error:
            self.message.set_text(f"Cannot use camera calibration: {error}")

    def export(self) -> None:
        try:
            setup = self.store.load(self.setup_name.value)
            if self.name.value not in setup.cameras:
                raise ValueError("Save or select a camera calibration first")
            ui.download(
                export_snapshot(setup).encode(),
                f"{self.setup_name.value}_camera_setup.py",
            )
        except (ValueError, OSError) as error:
            self.message.set_text(str(error))

    async def import_measurement(self) -> None:
        try:
            if not self.confirm.value:
                raise ValueError(
                    "Confirm the original physical camera, lens and mount before importing"
                )
            observation = camera_service.snapshot()
            frame = handeye.decode_jpeg(observation.jpeg)
            if frame is None:
                raise CameraUnavailable("Camera image cannot be decoded")
            tool = await read_applied_tcp(self.commander.client)
            if camera_service.snapshot().session_id != observation.session_id:
                raise CameraUnavailable("Camera changed while importing calibration")
            original = ng_app.storage.general.get(f"handeye/{tool.tool_key}")
            if not original:
                raise ValueError("No existing hand-eye measurement for this tool")
            calibration = import_saved_handeye(
                original,
                camera_id=observation.camera_id,
                image_size=(frame.shape[1], frame.shape[0]),
                backend=self.commander.robot.backend_package,
                tool=tool,
            )
            self.store.save(
                self.setup_name.value,
                self.snapshot().with_camera(self.name.value, calibration),
            )
            self.message.set_text(f"Imported measurement: {self.describe(calibration)}")
        except (ValueError, OSError, TimeoutError, CameraUnavailable) as error:
            self.message.set_text(str(error))
