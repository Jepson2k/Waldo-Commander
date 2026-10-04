"""Keybindings table and quick-start tour: the Settings dialog's Shortcuts and
Getting started categories, and the first-visit dialog."""

from collections.abc import Callable

from nicegui import app as ng_app, ui

from waldo_commander.components.readout import CHIP_COLORS
from waldo_commander.components.robot_buddy import Mood, Reaction, RobotBuddy
from waldo_commander.services.keybindings import keybindings_manager


class HelpMenu:
    """The keybindings table, the quick-start stepper and the first-visit dialog."""

    FIRST_VISIT_KEY = "parol_first_visit_shown"
    SAFETY_ACKNOWLEDGED_KEY = "parol_safety_acknowledged"

    def __init__(self) -> None:
        self._dialog: ui.dialog | None = None
        self._safety_accepted: ui.checkbox | None = None

    def _build_keybindings_content(self) -> None:
        """Build the keybindings table content."""
        categories = keybindings_manager.get_all_bindings()

        with ui.column().classes("w-full p-4 gap-4").mark("keybindings-content"):
            if not categories:
                ui.label("No keybindings registered").classes("text-wc-text-muted")
                return

            # Sort categories into a fixed order for consistent display.
            category_order = [
                "Robot Control",
                "Playback",
                "Recording",
                "Cartesian Jog",
                "Speed Control",
            ]
            sorted_categories = sorted(
                categories.items(),
                key=lambda x: (
                    category_order.index(x[0]) if x[0] in category_order else 999,
                    x[0],
                ),
            )

            for category, bindings in sorted_categories:
                with ui.column().classes("w-full gap-1"):
                    ui.label(category).classes("wc-label text-wc-text-muted")

                    rows = []
                    for i, binding in enumerate(bindings):
                        key_parts = []
                        if binding.requires_ctrl:
                            key_parts.append("Ctrl")
                        if binding.requires_alt:
                            key_parts.append("Alt")
                        if binding.requires_shift:
                            key_parts.append("Shift")
                        key_parts.append(binding.display)

                        rows.append(
                            {
                                "id": f"{category}-{i}",
                                "keys": key_parts,
                                "description": binding.description,
                            }
                        )

                    columns = [
                        {
                            "name": "keys",
                            "label": "Key",
                            "field": "keys",
                            "align": "left",
                        },
                        {
                            "name": "description",
                            "label": "Description",
                            "field": "description",
                            "align": "left",
                        },
                    ]

                    table = (
                        ui.table(columns=columns, rows=rows, row_key="id")
                        .props("flat dense hide-header hide-pagination")
                        .classes("keybindings-table")
                    )

                    table.add_slot(
                        "body-cell-keys",
                        """
                        <q-td :props="props" class="keys-cell">
                            <span class="kbd-group">
                                <template v-for="(key, idx) in props.value" :key="idx">
                                    <span class="kbd-key">{{ key }}</span>
                                    <span v-if="idx < props.value.length - 1" class="kbd-plus">+</span>
                                </template>
                            </span>
                        </q-td>
                    """,
                    )

    _TUTORIALS_URL = (
        "https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets"
    )

    def _build_quickstart_stepper(
        self,
        include_safety_step: bool = False,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        """Build the quick-start stepper with its tutorial videos.

        ``include_safety_step`` prepends the safety acknowledgment (first visit
        only); ``on_finish`` runs after Finish marks the tour seen, in place of
        closing the first-visit dialog.
        """
        # Descriptions mirror docs/index.md to keep the in-app tutorial and public docs in sync.
        steps = [
            {
                "title": "Basic Controls",
                "description": """
                    Jog in joint space (one joint at a time) or Cartesian space (translate in XYZ, rotate around RX/RY/RZ). Cartesian translation currently operates in the World reference frame while cartesian rotation operates in Tool reference frame. Future support is planned for additional reference frames.

                    Keyboard shortcuts: **WASD** + **Q/E** for Cartesian movement, **[** / **]** to adjust speed. Clicking a jog button or key sends a single step; holding it jogs continuously until you release.
                """,
                "video": "basic_control.mp4",
            },
            {
                "title": "Connecting Your Robot",
                "description": """
                    Open **Settings** from the gear in the bottom-left rail and select your hardware connection. On Linux you'll need access to the serial device — add yourself to the `dialout` group or set up a udev rule. Connection status is shown by Waldo, the little robot at the left end of the footer:
                """,
                "video": "connecting_to_robot.mp4",
                "status_legend": True,
            },
            {
                "title": "Programming, Recording, and Path Visualization",
                "description": """
                    Write robot programs in Python using the built-in editor with auto-complete for all robot commands. Or jog the robot into position and let the recorder generate `move_j` / `move_l` calls for you — I/O and tool actions are captured too. Right-click in the 3D view to place targets, press **T** to add one at the current pose, or drag existing targets with the gizmo to reposition them.

                    Run programs against the simulator to preview the motion path in 3D. The path traces the TCP position through each move, color-coded by reachability. Execute on hardware when you're ready.
                """,
                "video": "recording_and_previewing_actions.mp4",
            },
            {
                "title": "I/O and Tool Control",
                "description": """
                    Toggle digital outputs, read inputs, and monitor E-stop state. For grippers, slide the position and current controls and watch the gripper track in real time — a live chart plots position and current over time. Tool and variant switching happens under **Settings → Tool**; the 3D model updates to show the attached tool.
                """,
                "video": "attaching_a_tool.mp4",
            },
        ]

        with ui.scroll_area().classes("w-full h-full tutorial-scroll"):
            with (
                ui.stepper()
                .props(
                    "vertical header-nav flat active-color=wc-text done-color=wc-text-muted"
                )
                .classes("p-0 w-full") as stepper
            ):
                if include_safety_step:
                    with ui.step("Safety Notice").classes("gap-2").mark("safety-step"):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon("warning", size="md").classes("text-wc-warning")
                            ui.label("Please read before continuing").classes(
                                "text-lg font-medium"
                            )

                        with ui.column().classes("gap-2 ml-1"):
                            warnings = [
                                "This software provides no safety guarantees and assumes no liability",
                                "User accepts full responsibility for robot operation",
                                "Simulator mode is not physics-accurate and does not guarantee repeatability on real hardware",
                                "The digital E-STOP is not a substitute for the hardware emergency stop",
                                "Incorrect kinematics calculations could result in sudden robotic movements",
                                "Keep clear of all moving parts during operation",
                            ]
                            for warning in warnings:
                                with ui.row().classes("items-start gap-2"):
                                    ui.icon("circle", size="6px").classes(
                                        "text-wc-warning mt-2 shrink-0"
                                    )
                                    ui.label(warning).classes("text-sm")

                        with ui.stepper_navigation().classes("mt-4"):
                            self._safety_accepted = ui.checkbox(
                                "I have read and accept responsibility"
                            ).classes("mr-4")
                            next_btn = ui.button(
                                "Continue", on_click=stepper.next
                            ).props("color=wc-action text-color=wc-on-bright")
                            next_btn.bind_enabled_from(self._safety_accepted, "value")

                            def on_accept(e):
                                if e.args:
                                    ng_app.storage.general[
                                        self.SAFETY_ACKNOWLEDGED_KEY
                                    ] = True

                            self._safety_accepted.on("update:model-value", on_accept)

                for i, step in enumerate(steps):
                    with ui.step(step["title"]).classes("gap-2"):
                        ui.video(f"{self._TUTORIALS_URL}/{step['video']}").classes(
                            "w-full rounded-lg"
                        ).props('preload="none"').style("max-height: 360px;")

                        ui.markdown(step["description"]).classes("text-md text-wc-text")
                        if step.get("status_legend"):
                            self._build_status_legend()

                        with ui.stepper_navigation():
                            if i < len(steps) - 1:
                                ui.button("Next", on_click=stepper.next).props(
                                    "color=wc-action text-color=wc-on-bright"
                                )
                            else:
                                ui.button(
                                    "Finish",
                                    on_click=lambda: self._on_finish(on_finish),
                                ).props("color=wc-action text-color=wc-on-bright")
                            if i > 0:
                                ui.button("Back", on_click=stepper.previous).props(
                                    "flat color=wc-text"
                                )

    def _build_status_legend(self) -> None:
        """The footer's status chip in each state, drawn as the footer draws it."""
        with ui.column().classes("gap-1 ml-2").mark("status-legend"):
            for mood, text in (
                (Mood.HAPPY, "Connected to robot hardware"),
                (Mood.SAD, "Robot mode but disconnected"),
                (Mood.NEUTRAL, "Simulator mode"),
            ):
                fill, ink = CHIP_COLORS[mood]
                with ui.row().classes("items-center gap-3 no-wrap"):
                    with ui.chip().props(f"dense color={fill} text-color={ink}"):
                        RobotBuddy(mood, size=20, color="currentColor")
                    ui.label(text).classes("text-md text-wc-text")

    def _on_finish(self, on_finish: Callable[[], None] | None) -> None:
        """Mark the tour seen, then hand over or close the first-visit dialog."""
        ng_app.storage.general[self.FIRST_VISIT_KEY] = True
        if on_finish is not None:
            on_finish()
        elif self._dialog:
            self._dialog.close()

    def check_first_visit(self) -> None:
        """Check if this is the first visit and show tutorial dialog if so."""
        if not ng_app.storage.general.get(self.FIRST_VISIT_KEY, False):
            self.show_dialog()

    def show_dialog(self) -> None:
        """Show the first-time tutorial dialog (alias for backwards compatibility)."""
        self.create_first_time_dialog().open()

    def create_first_time_dialog(self) -> ui.dialog:
        """Create and return the first-time tutorial dialog."""
        # Persistent so it can't be dismissed by clicking outside.
        self._dialog = ui.dialog().props("persistent")

        safety_already_acknowledged = ng_app.storage.general.get(
            self.SAFETY_ACKNOWLEDGED_KEY, False
        )

        with self._dialog:
            with ui.card().classes("overlay-card tutorial-dialog-card"):
                with ui.column().classes("w-full h-full gap-0"):
                    with ui.row().classes("items-center gap-3 no-wrap"):
                        buddy = RobotBuddy(Mood.HAPPY, size=44, interactive=True)
                        buddy.react(Reaction.GREET)
                        ui.label("Welcome to PAROL Commander!").classes(
                            "text-xl font-bold"
                        )

                    ui.label(
                        "Let's get you started with a quick tour of the interface."
                    ).classes("text-sm text-wc-text-muted mb-3 shrink-0")

                    self._build_quickstart_stepper(
                        include_safety_step=not safety_already_acknowledged
                    )

                    # Footer stays hidden until safety is acknowledged.
                    footer = (
                        ui.row()
                        .classes("w-full items-center pt-3 shrink-0")
                        .style("border-top: 1px solid var(--wc-glass-border);")
                    )
                    if self._safety_accepted and not safety_already_acknowledged:
                        footer.bind_visibility_from(self._safety_accepted, "value")

                    with footer:
                        dont_show = ui.checkbox("Don't show this again")
                        dont_show.on(
                            "update:model-value",
                            lambda e: self._save_dont_show_pref(e.args),
                        )
                        ui.space()
                        ui.button("Skip Tour", on_click=self._dialog.close).props(
                            "flat color=wc-text"
                        )

        return self._dialog

    def _save_dont_show_pref(self, value: bool) -> None:
        """Save don't show again preference to server storage."""
        if value:
            ng_app.storage.general[self.FIRST_VISIT_KEY] = True


help_menu = HelpMenu()
