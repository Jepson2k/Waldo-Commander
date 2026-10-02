"""Separate pivot-position calibration and orientation teaching controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from nicegui import ui
from waldoctl import Commander
from waldo_commander.services.tcp_calibration import (
    PivotCalibration,
    calibrate_tcp_position,
    teach_tcp_orientation,
)
from waldoctl.setup import Pose, PoseValues, SetupSnapshot, TcpCalibration

from waldo_commander.services.control_lease import require_browser_control
from waldo_commander.services.motion_guard import motion_guard
from waldo_commander.services.tcp_calibration import (
    ToolBinding,
    ToolChanged,
    apply_tcp_calibration,
    observe_tcp,
)
from waldo_commander.state import ui_state


class TcpCalibrationEditor:
    def __init__(
        self,
        commander: Commander,
        get_snapshot: Callable[[], SetupSnapshot],
        set_snapshot: Callable[[SetupSnapshot], None],
        get_reference_snapshot: Callable[[], SetupSnapshot],
        on_change: Callable[[], None],
    ) -> None:
        self.commander = commander
        self.get_snapshot = get_snapshot
        self.set_snapshot = set_snapshot
        # Frame edits pending on the Frames tab are saved with the taught
        # orientation, so teaching resolves against them.
        self.get_reference_snapshot = get_reference_snapshot
        self.on_change = on_change
        self.samples: list[Pose] = []
        # The tool the controller last reported (keeps pivot samples on one
        # tool), and the tools the displayed position and orientation belong
        # to. They differ after loading a saved calibration for a tool that is
        # not fitted, or after teaching one half on another tool.
        self.binding: ToolBinding | None = None
        self.position_binding: ToolBinding | None = None
        self.rotation_binding: ToolBinding | None = None
        # Bumped by every new measurement, selection and clear, so a reply
        # that arrives after one of them is dropped instead of landing in the
        # entry now shown.
        self._generation = 0
        self.position: PivotCalibration | None = None
        self.taught: tuple[tuple[float, float, float], str] | None = None
        self.saved_measurement: TcpCalibration | None = None
        self.was_connected = commander.status.connected
        with ui.column().classes("w-full"):
            ui.label(
                "Hold the tip on a fixed point. Capture 4+ varied orientations."
            ).classes("text-caption")
            with ui.row().classes("w-full items-center"):
                self.name = (
                    ui.input("Calibration name", value="tip")
                    .props("dense")
                    .classes("flex-1 min-w-0")
                    .mark("tcp-calibration-name")
                )
                self.existing = (
                    ui.select([], label="Saved in setup")
                    .props("dense")
                    .classes("flex-1 min-w-0")
                    .mark("tcp-calibration-existing")
                )
            self.tool_label = (
                ui.label("Read the applied TCP or capture a pose to identify the tool.")
                .classes("text-caption")
                .mark("tcp-calibration-tool")
            )
            with ui.grid(columns=3).classes("w-full"):
                self.coordinates = [
                    ui.number(label, value=0.0, format="%.3f")
                    .props("dense")
                    .classes("w-full")
                    .mark(f"tcp-calibration-{key}")
                    for key, label in zip(
                        ("x", "y", "z", "roll", "pitch", "yaw"),
                        (
                            "X (mm)",
                            "Y (mm)",
                            "Z (mm)",
                            "Roll (°)",
                            "Pitch (°)",
                            "Yaw (°)",
                        ),
                    )
                ]
            self.message = (
                ui.label("Save setup keeps these values. Apply updates the controller.")
                .classes("text-caption")
                .mark("tcp-calibration-status")
            )
            with (
                ui.expansion("Measure TCP", icon="straighten")
                .classes("w-full")
                .mark("tcp-measure-details")
            ):
                with ui.row().classes("items-center"):
                    ui.button("Capture pivot pose", on_click=self.capture).props(
                        "dense color=wc-action text-color=wc-on-bright"
                    ).mark("tcp-calibration-capture")
                    ui.button("Clear samples", on_click=self.clear_samples).props(
                        "dense flat"
                    ).mark("tcp-calibration-clear")
                    self.count = ui.label("0 new samples").classes("text-caption")
                with ui.row().classes("items-center"):
                    self.tolerance = (
                        ui.number("Max error (mm)", value=1.0, min=0.001)
                        .props("dense")
                        .classes("w-32")
                        .mark("tcp-calibration-tolerance")
                    )
                    ui.button("Solve position", on_click=self.solve).props(
                        "dense color=wc-action text-color=wc-on-bright"
                    ).mark("tcp-calibration-solve")
                ui.label(
                    "Align the tool with the reference axes, then teach orientation."
                ).classes("text-caption")
                with ui.row().classes("items-center"):
                    self.reference = (
                        ui.select(["WRF"], value="WRF", label="Reference axes")
                        .props("dense")
                        .classes("w-40")
                        .mark("tcp-calibration-reference")
                    )
                    ui.button(
                        "Teach orientation", on_click=self.teach_orientation
                    ).props("dense color=wc-action text-color=wc-on-bright").mark(
                        "tcp-calibration-orientation"
                    )
            with ui.row():
                ui.button("Read applied", on_click=self.read_applied).props(
                    "dense flat"
                ).mark("tcp-calibration-read")
                ui.button("Keep calibration", on_click=self.set_calibration).props(
                    "dense color=wc-action text-color=wc-on-bright"
                ).mark("tcp-calibration-set")
                ui.button("Apply to controller", on_click=self.apply).props(
                    "dense outline"
                ).mark("tcp-calibration-apply")
            self.sample_table = (
                ui.table(
                    columns=[
                        {"name": k, "label": label, "field": k, "align": "left"}
                        for k, label in (
                            ("sample", "Sample"),
                            ("pose", "Registered tool · mm / degrees"),
                        )
                    ],
                    rows=[],
                    row_key="sample",
                )
                .props("dense flat")
                .classes("w-full")
                .mark("tcp-calibration-samples")
            )
        self.sample_table.set_visibility(False)
        self.refresh()
        ui.timer(0.5, self.check_connection)

    def refresh(self) -> None:
        snapshot = self.get_snapshot()
        self.existing.set_options(
            list(snapshot.tcp_calibrations),
            value=self.existing.value
            if self.existing.value in snapshot.tcp_calibrations
            else None,
        )
        options = ["WRF", *snapshot.frames]
        self.reference.set_options(
            options,
            value=self.reference.value if self.reference.value in options else "WRF",
        )

    def check_connection(self) -> None:
        connected = self.commander.status.connected
        if self.was_connected and not connected:
            self.clear_samples()
            self.binding = None
            self.saved_measurement = None
            self.taught = None
            self.tool_label.set_text(
                "Connection lost; read the tool again before applying."
            )
            self.message.set_text(
                "Capture ended when the connection was lost. Existing saved calibrations remain available."
            )
            self.on_change()
        self.was_connected = connected

    def draft_state(self) -> tuple:
        """What a save records besides the six numbers: bindings and provenance."""
        return (
            self.position_binding,
            self.rotation_binding,
            self.position,
            self.taught,
        )

    def reset(self) -> None:
        self.clear_samples()
        self.binding = None
        self.position_binding = None
        self.rotation_binding = None
        self.saved_measurement = None
        self.taught = None

    def _begin(self) -> int:
        self._generation += 1
        return self._generation

    def _superseded(self, generation: int) -> bool:
        if generation == self._generation:
            return False
        self.message.set_text(
            "The selection changed while measuring; nothing was recorded."
        )
        return True

    def _halves(self, done: str) -> str:
        """``done``, plus a warning when the position and orientation belong to different tools."""
        position, rotation = self.position_binding, self.rotation_binding
        if position is None or rotation is None or position == rotation:
            return done
        return (
            f"{done} The position belongs to {_describe(position)} and the "
            f"orientation to {_describe(rotation)}; measure both on one tool."
        )

    def bind_tool(self, binding: ToolBinding) -> None:
        if self.binding is not None and binding != self.binding:
            self.clear_samples()
            self.saved_measurement = None
            self.taught = None
            self.binding = binding
            self.tool_label.set_text(_describe(binding))
            self.on_change()
            raise ValueError("Tool or variant changed; samples cleared. Capture again.")
        self.binding = binding
        self.tool_label.set_text(_describe(binding))

    def clear_samples(self) -> None:
        self._generation += 1
        self.samples.clear()
        self.position = None
        self.count.set_text("0 new samples")
        self.sample_table.rows = []
        self.sample_table.set_visibility(False)
        self.sample_table.update()

    async def capture(self) -> None:
        generation = self._begin()
        try:
            observation = await observe_tcp(self.commander.client)
            if self._superseded(generation):
                return
            self.bind_tool(observation.binding)
            self.samples.append(observation.nominal_tool)
            self.count.set_text(f"{len(self.samples)} samples")
            self.sample_table.rows = [
                {"sample": i + 1, "pose": ", ".join(f"{v:.2f}" for v in p.values)}
                for i, p in enumerate(self.samples)
            ]
            self.sample_table.set_visibility(True)
            self.sample_table.update()
            self.message.set_text(
                "Captured. Keep the same tip on the fixed point and change orientation for the next pose."
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.message.set_text(str(error))

    def solve(self) -> None:
        try:
            result = calibrate_tcp_position(
                self.samples, max_error_mm=float(self.tolerance.value)
            )
            for element, value in zip(self.coordinates[:3], result.offset_mm):
                element.set_value(value)
            self.position = result
            self.position_binding = self.binding
            self.message.set_text(
                self._halves(
                    f"Position: {result.sample_count} samples · RMS {result.rms_error_mm:.3f} mm · max {result.max_error_mm:.3f} mm. Orientation is unchanged."
                )
            )
            self.on_change()
        except (TypeError, ValueError) as error:
            self.message.set_text(str(error))

    async def teach_orientation(self) -> None:
        generation = self._begin()
        try:
            observation = await observe_tcp(self.commander.client)
            if self._superseded(generation):
                return
            self.bind_tool(observation.binding)
            reference = self.get_reference_snapshot().resolve(
                Pose((0, 0, 0, 0, 0, 0), frame=self.reference.value)
            )
            rotation = teach_tcp_orientation(observation.nominal_tool, reference)
            self.taught = (rotation, self.reference.value)
            self.rotation_binding = self.binding
            for element, value in zip(self.coordinates[3:], rotation):
                element.set_value(value)
            self.message.set_text(
                self._halves(
                    f"Orientation taught against {self.reference.value}; position is unchanged."
                )
            )
            self.on_change()
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            TimeoutError,
        ) as error:
            self.message.set_text(str(error))

    async def read_applied(self) -> None:
        generation = self._begin()
        try:
            observation = await observe_tcp(self.commander.client)
            if self._superseded(generation):
                return
            self.bind_tool(observation.binding)
            self.position_binding = self.rotation_binding = observation.binding
            for element, value in zip(self.coordinates, observation.applied):
                element.set_value(value)
            self.message.set_text("Read the controller's applied TCP transform.")
            self.on_change()
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.message.set_text(str(error))

    def calibration(self) -> TcpCalibration:
        position, rotation = self.position_binding, self.rotation_binding
        binding = position if position is not None else rotation
        if binding is None:
            raise ValueError(
                "Read or capture the current tool before applying or saving"
            )
        if rotation is not None and rotation != binding:
            raise ValueError(
                "The position and orientation belong to different tools; measure both on one tool"
            )
        if any(element.value is None for element in self.coordinates):
            raise ValueError("Fill all six TCP coordinates")
        values = cast(
            PoseValues, tuple(float(element.value) for element in self.coordinates)
        )
        measured = (
            self.position
            if self.position is not None
            and tuple(values[:3]) == self.position.offset_mm
            else None
        )
        saved = (
            self.saved_measurement
            if self.saved_measurement is not None
            and self.saved_measurement.values == values
            else None
        )
        reference = (
            self.taught[1]
            if self.taught is not None and tuple(values[3:]) == self.taught[0]
            else None
        )
        return TcpCalibration(
            values,
            binding.tool_key,
            binding.variant_key,
            position_rms_mm=measured.rms_error_mm
            if measured
            else saved.position_rms_mm
            if saved
            else None,
            position_samples=measured.sample_count
            if measured
            else saved.position_samples
            if saved
            else 0,
            orientation_reference=reference
            or (saved.orientation_reference if saved else None),
        )

    def load(self, name: str | None) -> None:
        if name not in self.get_snapshot().tcp_calibrations:
            return
        calibration = self.get_snapshot().tcp_calibrations[name]
        self.clear_samples()
        self.taught = None
        self.saved_measurement = calibration
        self.binding = ToolBinding(calibration.tool_key, calibration.variant_key)
        self.position_binding = self.rotation_binding = self.binding
        self.tool_label.set_text(_describe(self.binding))
        self.name.set_value(name)
        if calibration.orientation_reference in self.reference.options:
            self.reference.set_value(calibration.orientation_reference)
        for element, value in zip(self.coordinates, calibration.values):
            element.set_value(value)
        details = []
        if calibration.position_rms_mm is not None:
            details.append(
                f"{calibration.position_samples} pivot samples · RMS {calibration.position_rms_mm:.3f} mm"
            )
        if calibration.orientation_reference is not None:
            details.append(f"orientation: {calibration.orientation_reference}")
        self.message.set_text("Loaded saved values. " + " · ".join(details))

    def set_calibration(self) -> None:
        try:
            self.set_snapshot(
                self.get_snapshot().with_tcp_calibration(
                    self.name.value, self.calibration()
                )
            )
            self.message.set_text("Calibration kept. Save setup to persist it.")
        except ValueError as error:
            self.message.set_text(str(error))

    async def apply(self) -> None:
        from waldo_commander.components.settings import adopt_applied_tcp

        page_id = ui_state.active_client_id
        try:
            calibration = self.calibration()
            if not require_browser_control(page_id):
                return
            await apply_tcp_calibration(
                motion_guard.guarded(self.commander.client, page_id), calibration
            )
            adopt_applied_tcp(calibration)
            self.message.set_text("Controller confirmed the displayed TCP transform.")
        except ToolChanged as error:
            adopt_applied_tcp(error.applied)
            self.message.set_text(str(error))
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.message.set_text(str(error))


def _describe(binding: ToolBinding) -> str:
    return f"{binding.tool_key} · {binding.variant_key or 'default variant'}"
