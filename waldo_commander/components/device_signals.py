"""Configure named I/O mappings without applying configuration while saving."""

from collections.abc import Callable

from nicegui import ui
from waldoctl import Commander
from waldoctl.setup import SetupSnapshot
from waldoctl.signals import DigitalSignal

from waldo_commander.services.control_lease import require_browser_control
from waldo_commander.skills.signals import read_signal, write_signal
from waldo_commander.state import ui_state


class DeviceSignalEditor:
    def __init__(
        self,
        commander: Commander,
        get_snapshot: Callable[[], SetupSnapshot],
        set_snapshot: Callable[[SetupSnapshot], None],
        on_binding_change: Callable[[], None] = lambda: None,
    ) -> None:
        self.commander = commander
        self.get_snapshot = get_snapshot
        self.set_snapshot = set_snapshot
        #: The binding is part of the setup's unsaved-edit signature, and no
        #: widget value changes when it is rebound, so the panel is told.
        self.on_binding_change = on_binding_change
        robot = ui_state.active_robot
        self.binding = (
            robot.backend_package,
            robot.digital_inputs,
            robot.digital_outputs,
        )
        with ui.column().classes("w-full"):
            ui.label("Name a digital input or output.").classes("text-caption")
            with ui.row().classes("w-full items-center"):
                self.name = (
                    ui.input("Signal name", value="part_ready")
                    .props("dense")
                    .classes("flex-1 min-w-0")
                    .mark("signal-name")
                )
                self.existing = (
                    ui.select(
                        [],
                        label="Saved in setup",
                        on_change=lambda e: self.load(e.value),
                    )
                    .props("dense")
                    .classes("flex-1 min-w-0")
                    .mark("signal-existing")
                )
            self.binding_label = (
                ui.label().classes("text-caption").mark("signal-binding")
            )
            ui.button("Use current robot", on_click=self.use_current_robot).props(
                "dense flat"
            ).mark("signal-bind")
            with ui.row().classes("items-center"):
                self.direction = (
                    ui.select(
                        {"input": "Input", "output": "Output"},
                        value="input",
                        label="Bank",
                    )
                    .props("dense")
                    .mark("signal-direction")
                )
                self.index = (
                    ui.number("Channel (0-based)", value=0, min=0, step=1)
                    .props("dense")
                    .classes("w-40")
                    .mark("signal-index")
                )
            self.active_high = ui.checkbox("Active high", value=True).mark(
                "signal-active-high"
            )
            ui.label("Active low when unchecked. E-stop cannot be mapped.").classes(
                "text-caption"
            )
            with ui.row():
                ui.button("Keep mapping", on_click=self.set_mapping).props(
                    "dense"
                ).mark("signal-set")
                ui.button("Remove mapping", on_click=self.remove).props(
                    "dense flat"
                ).mark("signal-remove")
                ui.button("Read", on_click=self.read).props("dense outline").mark(
                    "signal-read"
                )
            with ui.row().classes("items-center"):
                self.output_value = ui.checkbox(
                    "Logical output value", value=False
                ).mark("signal-output-value")
                ui.button("Write output", on_click=self.write).props(
                    "dense outline"
                ).mark("signal-write")
            self.message = (
                ui.label(
                    "Save setup keeps the mapping. Read and Write use the controller."
                )
                .classes("text-caption")
                .mark("signal-message")
            )
        self.refresh()
        self.show_binding()

    def show_binding(self) -> None:
        backend, inputs, outputs = self.binding
        self.binding_label.set_text(f"{backend} · {inputs} inputs · {outputs} outputs")

    def use_current_robot(self) -> None:
        robot = ui_state.active_robot
        self.binding = (
            robot.backend_package,
            robot.digital_inputs,
            robot.digital_outputs,
        )
        self.show_binding()
        self.on_binding_change()

    def refresh(self) -> None:
        options = list(self.get_snapshot().signals)
        self.existing.set_options(
            options,
            value=self.existing.value if self.existing.value in options else None,
        )

    def load(self, name: str | None) -> None:
        if name is None:
            return
        signal = self.get_snapshot().signals[name]
        self.name.set_value(name)
        self.binding = (signal.backend, signal.input_count, signal.output_count)
        self.direction.set_value(signal.direction)
        self.index.set_value(signal.index)
        self.active_high.set_value(signal.active_high)
        self.show_binding()
        self.message.set_text("Loaded saved mapping.")

    def mapping(self) -> DigitalSignal:
        index = self.index.value
        if index is None or isinstance(index, bool) or not float(index).is_integer():
            raise ValueError("Channel index must be an integer")
        backend, inputs, outputs = self.binding
        return DigitalSignal(
            backend,
            self.direction.value,
            int(index),
            inputs,
            outputs,
            self.active_high.value,
        )

    def set_mapping(self) -> None:
        try:
            self.set_snapshot(
                self.get_snapshot().with_signal(self.name.value, self.mapping())
            )
            self.message.set_text("Mapping added to the setup; Save to persist it.")
        except (ValueError, TypeError) as error:
            self.message.set_text(str(error))

    def remove(self) -> None:
        try:
            self.set_snapshot(self.get_snapshot().without("signals", self.name.value))
            self.message.set_text("Mapping removed from the setup; Save to persist it.")
        except (ValueError, KeyError) as error:
            self.message.set_text(str(error))

    async def read(self) -> None:
        try:
            observation = await read_signal.async_call(
                self.commander.client, self.mapping()
            )
            self.message.set_text(f"Observed logical value: {observation.value}")
        except (ValueError, RuntimeError, OSError) as error:
            self.message.set_text(str(error))

    async def write(self) -> None:
        if not require_browser_control(ui_state.active_client_id):
            return
        try:
            observation = await write_signal.async_call(
                self.commander.client, self.mapping(), self.output_value.value
            )
            self.message.set_text(
                f"Controller reports logical output: {observation.value}"
            )
        except (ValueError, RuntimeError, OSError) as error:
            self.message.set_text(str(error))
