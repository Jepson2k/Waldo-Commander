"""Help menu component with keybindings and quick start tutorial."""

from nicegui import app as ng_app, ui

from waldo_commander.services.keybindings import keybindings_manager


class HelpMenu:
    """Help dialog with vertical tabs for keybindings and quick start tutorial."""

    FIRST_VISIT_KEY = "parol_first_visit_shown"
    SAFETY_ACKNOWLEDGED_KEY = "parol_safety_acknowledged"

    def __init__(self) -> None:
        self._dialog: ui.dialog | None = None
        self._stepper: ui.stepper | None = None
        self._keybindings_container: ui.element | None = None
        self._safety_accepted: ui.checkbox | None = None

    def show_help_dialog(self) -> None:
        """Show the main help dialog with vertical tabs."""
        if self._dialog:
            self._dialog.delete()
            self._dialog = None

        self._dialog = ui.dialog().classes("help-dialog").mark("help-dialog")

        with self._dialog:
            with ui.card().classes("overlay-card help-dialog-card p-0 overflow-hidden"):
                with ui.row().classes("gap-0"):
                    # Left side: vertical tabs
                    with ui.column().classes("help-tabs-column shrink-0"):
                        with (
                            ui.tabs()
                            .props("vertical dense")
                            .classes("help-vertical-tabs") as tabs
                        ):
                            keybindings_tab = (
                                ui.tab(name="keybindings", label="", icon="keyboard")
                                .classes("help-tab")
                                .tooltip("Keybindings")
                                .mark("tab-keybindings")
                            )
                            quickstart_tab = (
                                ui.tab(name="quickstart", label="", icon="school")
                                .classes("help-tab")
                                .tooltip("Quick Start")
                                .mark("tab-quickstart")
                            )

                    # Right side: content
                    with ui.column().classes("flex-1 gap-0 overflow-hidden"):
                        # Header with close button
                        with (
                            ui.row()
                            .classes("w-full items-center px-4 py-2 shrink-0")
                            .style("border-bottom: 1px solid rgba(255,255,255,0.1);")
                        ):
                            ui.label("Help").classes("text-lg font-medium")
                            ui.space()
                            with (
                                ui.link(
                                    "",
                                    "https://jepson2k.github.io/Waldo-Commander/",
                                    new_tab=True,
                                )
                                .classes("text-gray-400")
                                .tooltip("View tutorials online")
                            ):
                                ui.icon("open_in_new", size="sm")
                            ui.button(icon="close", on_click=self._dialog.close).props(
                                "flat round dense color=white"
                            )

                        # Tab panels with vertical animation (matching vertical tabs)
                        with (
                            ui.tab_panels(tabs, value=quickstart_tab)
                            .classes("w-full overflow-hidden")
                            .props(
                                "animated transition-prev=slide-up transition-next=slide-down"
                            )
                        ):
                            with ui.tab_panel(keybindings_tab).classes("p-0"):
                                with (
                                    ui.scroll_area()
                                    .classes("w-full")
                                    .style("max-height: 80vh;")
                                ):
                                    self._build_keybindings_content()

                            with (
                                ui.tab_panel(quickstart_tab)
                                .classes("p-0")
                                .style("width: 720px; height: 700px; max-height: 85vh;")
                            ):
                                self._build_quickstart_stepper()

        self._dialog.open()

    def _build_keybindings_content(self) -> None:
        """Build the keybindings table content."""
        categories = keybindings_manager.get_all_bindings()

        with ui.column().classes("w-full p-4 gap-4").mark("keybindings-content"):
            if not categories:
                ui.label("No keybindings registered").classes("text-gray-500")
                return

            # Sort categories for consistent display
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
                    ui.label(category).classes("text-sm font-medium text-gray-400")

                    # Build rows with key parts as list for template rendering
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

                    # Custom slot to render keys as keyboard icons
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

    def _build_quickstart_stepper(self, include_safety_step: bool = False) -> None:
        """Build quick start stepper with tutorial videos.

        Args:
            include_safety_step: If True, prepend a safety acknowledgment step.
                                 Used for first-time visit dialog only.
        """
        # Step descriptions are markdown that mirrors the relevant sections
        # of docs/index.md so the in-app tutorial and the public docs stay
        # in sync. Inline HTML spans are used for the colored status markers.
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
                    In the control panel, switch to the **Settings** tab and select your hardware connection. On Linux you'll need access to the serial device — add yourself to the `dialout` group or set up a udev rule. Connection status is shown in the top right corner.

                    - <span style="color: #4caf50">■</span> Connected to robot hardware
                    - <span style="color: #f44336">■</span> Robot mode but disconnected
                    - <span style="color: #9e9e9e">■</span> Simulator mode
                """,
                "video": "connecting_to_robot.mp4",
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
                    Toggle digital outputs, read inputs, and monitor E-stop state. For grippers, slide the position and current controls and watch the gripper track in real time — a live chart plots position and current over time. Tool and variant switching happens in the **Settings** tab; the 3D model updates to show the attached tool.
                """,
                "video": "attaching_a_tool.mp4",
            },
        ]

        with ui.scroll_area().classes("w-full h-full tutorial-scroll"):
            with (
                ui.stepper()
                .props("vertical header-nav flat active-color=white done-color=grey-5")
                .classes("p-0")
                .style("width: 700px;") as self._stepper
            ):
                # Safety step (only shown on first visit)
                if include_safety_step:
                    with ui.step("Safety Notice").classes("gap-2").mark("safety-step"):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon("warning", size="md").classes("text-amber-500")
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
                                        "text-amber-500 mt-2 shrink-0"
                                    )
                                    ui.label(warning).classes("text-sm")

                        with ui.stepper_navigation().classes("mt-4"):
                            self._safety_accepted = ui.checkbox(
                                "I have read and accept responsibility"
                            ).classes("mr-4")
                            next_btn = ui.button(
                                "Continue", on_click=self._stepper.next
                            ).props("color=primary")
                            next_btn.bind_enabled_from(self._safety_accepted, "value")

                            # Store acknowledgment when checkbox is checked
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
                        ).props('preload="metadata"').style("max-height: 360px;")

                        # sanitize=False is safe here: the content is a
                        # hardcoded literal that includes inline color spans
                        # for the connection-status markers, and DOMPurify
                        # would strip the inline styles otherwise.
                        ui.markdown(step["description"], sanitize=False).classes(
                            "text-md text-gray-300"
                        )

                        with ui.stepper_navigation():
                            if i < len(steps) - 1:
                                ui.button("Next", on_click=self._stepper.next).props(
                                    "color=primary"
                                )
                            else:
                                ui.button("Finish", on_click=self._on_finish).props(
                                    "color=primary"
                                )
                            if i > 0:
                                ui.button(
                                    "Back", on_click=self._stepper.previous
                                ).props("flat")

    def _on_finish(self) -> None:
        """Handle finish button click - mark tutorial complete and close dialog."""
        ng_app.storage.general[self.FIRST_VISIT_KEY] = True
        if self._dialog:
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
        # Persistent dialog - can't be dismissed by clicking outside
        self._dialog = ui.dialog().props("persistent")

        # Check if safety was already acknowledged in a previous session
        safety_already_acknowledged = ng_app.storage.general.get(
            self.SAFETY_ACKNOWLEDGED_KEY, False
        )

        with self._dialog:
            with ui.card().classes("overlay-card tutorial-dialog-card"):
                with ui.column().classes("w-full h-full gap-0"):
                    # Header
                    ui.label("Welcome to PAROL Commander!").classes("text-xl font-bold")

                    ui.label(
                        "Let's get you started with a quick tour of the interface."
                    ).classes("text-sm text-gray-400 mb-3 shrink-0")

                    # Quick start stepper (with safety step only if not already acknowledged)
                    self._build_quickstart_stepper(
                        include_safety_step=not safety_already_acknowledged
                    )

                    # Footer - hidden until safety is acknowledged (or always visible if already acknowledged)
                    footer = (
                        ui.row()
                        .classes("w-full items-center pt-3 shrink-0")
                        .style("border-top: 1px solid rgba(255,255,255,0.1);")
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
                            "flat"
                        )

        return self._dialog

    def _save_dont_show_pref(self, value: bool) -> None:
        """Save don't show again preference to server storage."""
        if value:
            ng_app.storage.general[self.FIRST_VISIT_KEY] = True


# Singleton
help_menu = HelpMenu()
