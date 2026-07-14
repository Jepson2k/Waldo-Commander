import logging
from functools import partial

import waldoctl
from nicegui import ui
from waldoctl import RobotClient

from waldo_commander.services.motion_recorder import motion_recorder
from waldo_commander.state import ui_state

logger = logging.getLogger(__name__)


class IoPage:
    """I/O tab page."""

    def __init__(self, client: RobotClient) -> None:
        self.client = client

    async def set_output(self, index: int, state: int) -> None:
        """Set digital output via the robot client (0-based index)."""
        try:
            await self.client.write_io(index, state)
            motion_recorder.record_action("io", port=index, state=state)
            logger.info("OUTPUT%s -> %s", index + 1, "HIGH" if state else "LOW")
        except Exception as e:
            logger.error("Set output failed: %s", e)
            ui.notify(f"Set output failed: {e}", color="negative")

    def build(self) -> None:
        """Build the I/O page content dynamically from robot IO pin counts."""
        n_in = ui_state.active_robot.digital_inputs
        n_out = ui_state.active_robot.digital_outputs

        io = waldoctl.commander.status.io
        with ui.column().classes("gap-2"):
            with ui.row().classes("items-center gap-4"):
                for i in range(n_in):
                    (
                        ui.label(f"INPUT {i + 1}: -")
                        .bind_text_from(
                            io,
                            "inputs",
                            backward=lambda v, j=i: (
                                f"INPUT {j + 1}: {v[j] if len(v) > j else '-'}"
                            ),
                        )
                        .classes("text-sm")
                    )
                (
                    ui.label("ESTOP: unknown")
                    .bind_text_from(
                        io,
                        "estop",
                        backward=lambda v: f"ESTOP: {'OK' if v else 'TRIGGERED'}",
                    )
                    .classes("text-sm")
                )

            ui.separator()

            for i in range(n_out):
                with ui.row().classes("items-center gap-4"):
                    (
                        ui.label(f"OUTPUT {i + 1}: -")
                        .bind_text_from(
                            io,
                            "outputs",
                            backward=lambda v, j=i: (
                                f"OUTPUT {j + 1}: {v[j] if len(v) > j else '-'}"
                            ),
                        )
                        .classes("text-sm")
                    )
                    ui.button("LOW", on_click=partial(self.set_output, i, 0)).props(
                        "unelevated"
                    )
                    ui.button("HIGH", on_click=partial(self.set_output, i, 1)).props(
                        "unelevated"
                    )
