import argparse
import asyncio
import atexit
import contextlib
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from importlib.resources import files as pkg_files
from pathlib import Path


import numpy as np
from nicegui import Client, app as ng_app, ui
from waldoctl import GripperTool, LinearMotion, RobotClient

from waldo_commander.common.logging_config import (
    attach_ui_log,
    configure_logging,
    TRACE,
)
from waldo_commander.common.loop_timer import LoopMetrics, format_hz_summary
from waldo_commander.common.theme import (
    apply_theme,
    inject_layout_css,
    is_dark_theme,
    PANEL_RESIZE_CONFIG,
    SceneColors,
)
from waldo_commander.components.control import ControlPanel
from waldo_commander.components.editor import EditorPanel
from waldo_commander.components.gripper import GripperPage
from waldo_commander.components.help_menu import help_menu
from waldo_commander.components.io import IoPage
from waldo_commander.components.readout import ReadoutPanel
from waldo_commander.constants import config, DEFAULT_CAMERA
from waldo_commander.numba_pipelines import (
    pose_extraction_pipeline,
    warmup_pipelines,
)
from waldo_commander.profiles import get_robot
from waldo_commander.services.camera_service import camera_service
from waldo_commander.services.path_visualizer import warm_process_pool
from waldo_commander.services.urdf_scene import (
    UrdfScene,
    UrdfSceneConfig,
    ToolPose,
    init_angle_buffers,
    update_urdf_angles,
)
from waldo_commander.services.urdf_scene.envelope_mixin import workspace_envelope
from waldo_commander.state import (
    action_log,
    robot_state,
    controller_state,
    simulation_state,
    ui_state,
    readiness_state,
    global_phase_timer,
)

logger = logging.getLogger(__name__)

STATIC_DIR = pkg_files("waldo_commander").joinpath("static")
ng_app.add_static_files("/static", str(STATIC_DIR))

# ------------------------ Global UI/state ------------------------

# Global client instance - initialized in main() after CLI parsing
client: RobotClient

# Multicast-driven status consumer (runs once per app)
status_consumer_task: asyncio.Task | None = None
# Set during _on_shutdown so the asyncio exception handler can swallow
# expected cancellation/connection errors that fire as tasks unwind.
_shutting_down: bool = False

# Component instances (assigned in main(), None until then)
control_panel: ControlPanel = None  # ty: ignore[invalid-assignment]
readout_panel: ReadoutPanel = None  # ty: ignore[invalid-assignment]
editor_panel: EditorPanel = None  # ty: ignore[invalid-assignment]


@dataclass
class _PageState:
    """Per-browser-connection state.  Set to None atomically on disconnect."""

    page_client: Client
    connection_notification: ui.notification | None = None
    ping_timer: ui.timer | None = None
    last_ping_ok: bool = False


_page_state: _PageState | None = None

# Pre-allocated buffers for numba pipelines (scratch space)
_rotation_matrix_buffer: np.ndarray = np.zeros((3, 3), dtype=np.float64)
_rpy_rad_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
_pose_result_buffer: np.ndarray = np.zeros(6, dtype=np.float64)  # [x,y,z,rx,ry,rz]
_DEG_TO_RAD: float = math.pi / 180.0


# Frontend timing metrics (unified via LoopMetrics)
_ui_metrics = LoopMetrics()

# Startup completion event - used by _on_shutdown() to wait for _on_startup() to finish
_startup_complete: asyncio.Event = asyncio.Event()


def _update_connection_notification() -> None:
    """Show or dismiss persistent notification based on robot connection state."""
    ps = _page_state
    if ps is None:
        return

    # Skip if app not ready - avoid modifying elements during page serialization
    if not readiness_state.app_ready.is_set():
        return

    needs_warning = not robot_state.simulator_active and not robot_state.connected

    if needs_warning and ps.connection_notification is None:
        ps.connection_notification = ui.notification(
            message="Robot mode requires a hardware connection. Connect robot or switch to Simulator mode.",
            type="negative",
            close_button=True,
            timeout=0,
        )
    elif not needs_warning and ps.connection_notification is not None:
        ps.connection_notification.dismiss()
        ps.connection_notification = None


# --------------- URDF Scene Functions ---------------
async def initialize_urdf_scene() -> None:
    """Initialize the URDF scene with error handling."""
    robot = ui_state.active_robot
    urdf_path = Path(robot.urdf_path)
    mesh_dir = Path(robot.mesh_dir)

    # Detect theme and set appropriate colors
    is_dark = is_dark_theme()
    bg_color = (
        SceneColors.BACKGROUND_DARK_HEX if is_dark else SceneColors.BACKGROUND_LIGHT_HEX
    )
    material_color = (
        SceneColors.MATERIAL_DARK_HEX if is_dark else SceneColors.MATERIAL_LIGHT_HEX
    )

    # Create tool pose resolver from robot tools
    def tool_pose_resolver(
        tool_key: str, variant_key: str | None = None
    ) -> ToolPose | None:
        """Look up tool TCP from robot.tools and return as ToolPose."""
        if not tool_key or tool_key.upper() == "NONE":
            return None
        r = ui_state.active_robot
        try:
            tool = r.tools[tool_key]
        except KeyError:
            return None
        # Per-variant TCP overrides tool-level TCP
        if variant_key is not None:
            for v in tool.variants:
                if v.key == variant_key and v.tcp_origin is not None:
                    return ToolPose(
                        origin=list(v.tcp_origin),
                        rpy=list(v.tcp_rpy) if v.tcp_rpy else list(tool.tcp_rpy),
                    )
        return ToolPose(
            origin=list(tool.tcp_origin),
            rpy=list(tool.tcp_rpy),
        )

    # Create UrdfScene config with all settings
    scene_config = UrdfSceneConfig(
        tool_pose_resolver=tool_pose_resolver,
        gizmo_scale=1.35,  # Make gizmo larger (1.0 = default STL scale)
        package_map={robot.backend_package: mesh_dir},
        # Appearance settings
        material=material_color,
        background_color=bg_color,
        sim_color=SceneColors.SIM_AMBER_HEX,
        sim_opacity=0.9,
        # Kinematic mapping settings (defaults are fine for PAROL6)
    )

    # Create new scene with config
    ui_state.urdf_scene = UrdfScene(urdf_path, config=scene_config)
    ui_state.urdf_scene.show(
        material=scene_config.material,
        background_color=scene_config.background_color,
    )

    # Align TCP and load tool mesh from controller's active tool
    try:
        result = await client.tools()
        if result and result.tool:
            vk = ng_app.storage.general.get(f"tool_variant_{result.tool}")
            ui_state.active_robot.set_active_tool(result.tool, variant_key=vk)
            ui_state.urdf_scene.apply_tool(result.tool, variant_key=vk)
    except Exception as e:
        logger.error("Failed to sync TCP tool pose: %s", e)

    # Override the scene height and set closer camera position
    if ui_state.urdf_scene.scene:
        scene: ui.scene = ui_state.urdf_scene.scene
        scene._props["grid"] = (10, 100)
        # Fill parent container (absolute canvas): width/height 100%
        scene.classes(remove="h-[66vh]").style(
            "width: 100%; height: 100%; margin: 0; display: block;"
        )
        scene.move_camera(**DEFAULT_CAMERA, duration=0.0)

        # Add large world coordinate frame at origin (fixed)
        world_axes_size = 0.30
        scene.line([0, 0, 0], [world_axes_size, 0, 0]).material(
            SceneColors.AXIS_X_HEX
        )  # X
        scene.line([0, 0, 0], [0, world_axes_size, 0]).material(
            SceneColors.AXIS_Y_HEX
        )  # Y
        scene.line([0, 0, 0], [0, 0, world_axes_size]).material(
            SceneColors.AXIS_Z_HEX
        )  # Z

    # Cache joint names for mapping
    ui_state.urdf_joint_names = list(ui_state.urdf_scene.get_joint_names())

    logger.debug("URDF scene initialized with joints: %s", ui_state.urdf_joint_names)

    # Signal URDF scene ready for tests
    readiness_state.signal_urdf_scene_ready()

    # Ensure stored tool is fully applied (settings page may have built before scene was ready)
    stored_tool = ng_app.storage.general.get("selected_tool")
    if stored_tool and stored_tool != "NONE" and ui_state.urdf_scene:
        vk = ng_app.storage.general.get(f"tool_variant_{stored_tool}")
        ui_state.active_robot.set_active_tool(stored_tool, variant_key=vk)
        ui_state.urdf_scene.apply_tool(stored_tool, variant_key=vk)
    else:
        # Invalidate FK cache even without tool change (gizmo sync needs fresh FK)
        ui_state.urdf_scene.invalidate_fk_cache()

    # Generate workspace hull with correct tool offset (after tool is applied)
    if not os.environ.get("WALDO_SKIP_ENVELOPE") and not workspace_envelope.is_ready:
        workspace_envelope.generate(
            tool_offset_z=ui_state.urdf_scene._current_tool_offset_z
        )

    # Sync gizmo settings to URDF scene now that it's ready
    control_panel.sync_gizmo_to_urdf()

    # Apply simulator appearance if in simulator mode (scene wasn't ready earlier)
    if robot_state.simulator_active:
        ui_state.urdf_scene.set_simulator_appearance(True)


# --------------- Controller controls ---------------


async def start_controller(com_port: str | None) -> None:
    """Start the robot controller or attach to an existing one.

    In EXCLUSIVE_START mode this will *fail hard* if a controller is already
    running at the configured host/port instead of silently reusing it.
    """
    robot = ui_state.active_robot
    # If AUTO_START requested, ensure a server is running at the target tuple.
    # 60s timeout (vs parol6's 10s default) accommodates first-run numba JIT
    # warmup on slower machines; cached runs are much faster.
    if config.exclusive_start:
        await asyncio.to_thread(
            robot.start,
            host=config.controller_host,
            port=config.controller_port,
            com_port=com_port,
            timeout=60,
        )
    else:
        # If a controller is already running, reuse it
        if await asyncio.to_thread(
            robot.is_available,
            host=config.controller_host,
            port=config.controller_port,
        ):
            logger.info(
                "Controller already running at %s:%s; reusing external server",
                config.controller_host,
                config.controller_port,
            )
        else:
            raise ConnectionError(
                f"No controller found at {config.controller_host}:{config.controller_port}"
            )

    # enable ping timer now that we are connected
    global status_consumer_task
    ps = _page_state
    if ps is not None and ps.ping_timer is not None:
        ps.ping_timer.active = True
    # start multicast consumer
    if status_consumer_task is None or status_consumer_task.done():
        status_consumer_task = asyncio.create_task(_status_consumer())
    controller_state.running = True
    logger.debug("Controller started")


async def stop_controller() -> None:
    global status_consumer_task
    try:
        robot = ui_state.robot
        if robot is not None:
            logger.info("Stopping controller...")
            await asyncio.to_thread(robot.stop)

        # Disable ping timer and stop multicast consumer on disconnect
        ps = _page_state
        if ps is not None and ps.ping_timer is not None:
            ps.ping_timer.active = False
        if status_consumer_task is not None and not status_consumer_task.done():
            status_consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_consumer_task

        controller_state.running = False
        robot_state.connected = False
        logger.info("Controller stopped")
    except Exception as e:
        logger.error("Stop controller failed: %s", e)


# --------------- Connectivity Check ---------------
async def check_ping() -> None:
    """Check connectivity via PING (1Hz) and arbitrate multi-tab ownership.

    This timer fires in *every* open tab (active and shadow). It first
    decides whether this tab is the active controller; shadow tabs short-
    circuit before touching any panel state.
    """
    # Multi-tab arbitration. ui.timer fires the callback inside the timer's
    # owning client context, so ui.context.client.id is this tab's id.
    this_id = ui.context.client.id
    active_id = ui_state.active_client_id
    if active_id is None:
        # No tab holds the slot — claim it by reloading. The reloaded
        # client lands fresh in index_page and atomically sets the slot.
        ui.navigate.reload()
        return
    if active_id != this_id:
        # Some other tab holds the slot. Make sure we're showing the
        # takeover overlay and skip the active-tab heartbeat (the panel
        # singletons point at the active tab's widgets, not ours). The
        # build is idempotent per Client via _waldo_overlay_shown.
        _build_takeover_overlay("Session was taken over by another tab")
        return

    ps = _page_state
    if ps is None:
        return

    try:
        result = await client.ping()
        new_ok = result.hardware_connected if result else False
        if new_ok != ps.last_ping_ok:
            logger.debug(
                "ping: connected %s → %s (hw_connected=%s, result=%s)",
                ps.last_ping_ok,
                new_ok,
                getattr(result, "hardware_connected", "N/A"),
                result,
            )
        ps.last_ping_ok = new_ok
    except Exception as e:
        logger.debug("ping failed: %s", e)
        if ps.last_ping_ok:
            logger.debug("ping: connected True → False (exception)")
        ps.last_ping_ok = False

    # Update robot connectivity status. The multicast status consumer drives
    # the joint/cartesian button sync at status rate; the two calls below
    # cover the "stream went silent" path that the consumer cannot, since
    # they read robot_state.connected directly.
    robot_state.connected = ps.last_ping_ok
    if readout_panel is not None:
        readout_panel.update_conn_io()
    if control_panel is not None:
        control_panel.sync_gizmo_for_jog_state()


# --------------- UI Update Functions ---------------
def update_ui_from_status() -> None:
    """Update UI elements from robot_state (called from multicast consumer)"""
    # Skip position/angle updates when in editing mode (editing sync handles these)
    skip_position_updates = robot_state.editing_mode
    # Skip URDF scene updates during sim playback/scrubbing (teleport syncs backend)
    skip_scene_updates = skip_position_updates or simulation_state.sim_pose_override

    # Update URDF scene with new angles and TCP ball
    if not skip_scene_updates:
        with global_phase_timer.phase("scene"):
            update_urdf_angles(robot_state.angles.deg)
            if ui_state.urdf_scene:
                ui_state.urdf_scene.update_from_robot_state()

    if not skip_position_updates:
        # robot_state.pose is already numpy float64 - pass directly to numba
        pose_extraction_pipeline(
            robot_state.pose,
            _rotation_matrix_buffer,
            _rpy_rad_buffer,
            _pose_result_buffer,
        )

        robot_state.x = _pose_result_buffer[0]
        robot_state.y = _pose_result_buffer[1]
        robot_state.z = _pose_result_buffer[2]
        # Set both scalar fields (for UI binding) and orientation array (for rad access)
        robot_state.rx = _pose_result_buffer[3]
        robot_state.ry = _pose_result_buffer[4]
        robot_state.rz = _pose_result_buffer[5]
        robot_state.orientation.set_deg(_pose_result_buffer[3:6])

    # Push IO derived fields into bindable RobotState (numpy int32 array)
    n_in = ui_state.active_robot.digital_inputs
    n_out = ui_state.active_robot.digital_outputs
    _io_in = robot_state.io[:n_in]
    _io_out = robot_state.io[n_in : n_in + n_out]
    if not np.array_equal(_io_in, robot_state.io_inputs):
        robot_state.io_inputs = _io_in.tolist()
    if not np.array_equal(_io_out, robot_state.io_outputs):
        robot_state.io_outputs = _io_out.tolist()
    robot_state.io_estop = int(robot_state.io[n_in + n_out])

    # Push tool status derived fields into bindable RobotState
    ts = robot_state.tool_status
    tool_key_changed = ts.key != robot_state.tool_key
    robot_state.tool_key = ts.key
    robot_state.tool_position = ts.positions[0] if ts.positions else 0.0
    robot_state.tool_engaged = ts.engaged
    robot_state.tool_part_detected = ts.part_detected
    robot_state.tool_current = ts.channels[0] if len(ts.channels) > 0 else 0.0
    robot_state.tool_time_series.push(
        robot_state.tool_position, robot_state.tool_current
    )

    # Build gripper tab on first tool detection
    if tool_key_changed and robot_state.tool_key != "NONE":
        try:
            if ui_state._build_gripper_content is not None:
                ui_state._build_gripper_content()
            if ui_state._gripper_tab is not None:
                ui_state._gripper_tab.props(remove="disable")
        except RuntimeError:
            pass

    # Update control panel tool quick-action visuals
    if control_panel.tool_actions:
        control_panel.tool_actions.update_visual()

    # Monitor E-STOP state changes and show/hide dialog as needed
    if control_panel.estop:
        control_panel.estop.check_state_change()

    # Notify listeners that robot state has changed (for envelope proximity updates)
    # Skip if app not ready to avoid race with NiceGUI page serialization
    if not readiness_state.app_ready.is_set():
        return

    _update_connection_notification()
    if tool_key_changed:
        robot_state.notify_changed()


def _build_left_panels(panels_wrap: ui.element) -> dict:
    """Build top (program/io/gripper) and bottom (log/help) panel groups.

    Returns a dict of references needed by _setup_panel_persistence().
    """
    # ---- Top tab bar ----
    with (
        ui.tabs()
        .props("vertical")
        .classes("side-tab-bar absolute left-0 top-0 z-40") as side_tabs
    ):
        program_tab = ui.tab(name="program", label="", icon="code")
        program_tab.mark("tab-program")
        io_tab = ui.tab(name="io", label="", icon="settings_input_component")
        io_tab.mark("tab-io")
        gripper_tab = ui.tab(name="gripper", label="")
        with gripper_tab:
            ui.image("/static/icons/robotic-claw.svg").classes("gripper-icon").style(
                "width: 24px; height: 24px; transform: rotate(180deg); filter: brightness(0) invert(1) opacity(0.8);"
            )
        gripper_tab.props("disable")
        gripper_tab.mark("tab-gripper")
        ui_state._gripper_tab = gripper_tab

    # ---- Top panels container ----
    with (
        ui.tab_panels(side_tabs, value=None)
        .props(
            "vertical animated transition-prev=slide-right transition-next=slide-right"
        )
        .classes("left-panels-container top-panels-container z-30") as top_panels
    ):

        def close_top_panels():
            side_tabs.value = None
            top_panels.value = None
            panels_wrap.classes(remove="coupled")
            ui_state.program_panel_visible = False
            ui.run_javascript("PanelResize.onTabChange('top', '')")

        with ui.tab_panel("program").classes(
            "overlay-card program-panel resizable-panel p-0"
        ):
            editor_panel.build(close_callback=close_top_panels)
            ui.element("div").classes("resize-handle-right")
            ui.element("div").classes("resize-handle-bottom")
            ui.element("div").classes("resize-handle-corner")

        with ui.tab_panel("io").classes("gap-2 overlay-card overflow-hidden"):
            with ui.row().classes("w-full"):
                ui.label("I/O").classes("text-lg font-medium")
                ui.space()
                ui.button(icon="close", on_click=close_top_panels).props(
                    "flat round dense color=white"
                )
            ui_state.io_page = IoPage(client)
            ui_state.io_page.build()

        with ui.tab_panel("gripper").classes(
            "gap-2 overlay-card gripper-panel overflow-hidden"
        ) as gripper_panel_container:
            gripper_content_built = False

            def _build_gripper_content() -> None:
                nonlocal gripper_content_built
                if gripper_content_built:
                    return
                gripper_content_built = True
                with gripper_panel_container:
                    with ui.row().classes("w-full items-center"):
                        (
                            ui.label("Gripper")
                            .bind_text_from(
                                robot_state,
                                "tool_key",
                                backward=lambda k: f"Gripper: {k}"
                                if k != "NONE"
                                else "Gripper",
                            )
                            .classes("text-lg font-medium")
                        )
                        gripper_features_label = ui.label("").classes(
                            "text-xs text-[var(--ctk-muted)]"
                        )

                        def _update_features(k: str) -> str:
                            if k == "NONE":
                                return ""
                            parts: list[str] = []
                            try:
                                tool = client.tool
                            except (RuntimeError, KeyError, NotImplementedError):
                                return ""
                            if not isinstance(tool, GripperTool):
                                return ""
                            for m in tool.motions:
                                if isinstance(m, LinearMotion):
                                    gap = m.travel_m * 1000 * (2 if m.symmetric else 1)
                                    parts.append(f"{gap:.1f}mm gap")
                                    break
                            channels = {ch.name for ch in tool.channel_descriptors}
                            if "Current" in channels:
                                parts.append("Current")
                            return " · ".join(parts)

                        gripper_features_label.bind_text_from(
                            robot_state, "tool_key", backward=_update_features
                        )
                        ui.space()
                        ui.button(icon="close", on_click=close_top_panels).props(
                            "flat round dense color=white"
                        )
                    ui_state.gripper_page = GripperPage(client)
                    ui_state.gripper_page.build()

            ui_state._build_gripper_content = _build_gripper_content

        def update_top_layout(e=None):
            new_tab = e.args if e and e.args else side_tabs.value or ""
            ui_state.program_panel_visible = new_tab == "program"

        side_tabs.on("update:model-value", update_top_layout)

        def handle_tab_change(e):
            to_tab = e.args or ""
            ui.run_javascript(f"PanelResize.onTabChange('top', '{to_tab}')")

        side_tabs.on("update:model-value", handle_tab_change)
        ui_state.program_panel_visible = side_tabs.value == "program"

    # ---- Bottom tab bar ----
    with (
        ui.tabs(value=None)
        .props("vertical")
        .classes("side-tab-bar absolute bottom-0 left-0 z-50") as bottom_tabs
    ):
        resp_tab = ui.tab(name="response", label="", icon="article")
        resp_tab.tooltip("Log")
        resp_tab.mark("tab-log")
        help_tab = ui.tab(name="help", label="", icon="help_outline")
        help_tab.tooltip("Help")
        help_tab.mark("tab-help")

    # ---- Bottom panels container ----
    with (
        ui.tab_panels(bottom_tabs, value=None)
        .props("vertical animated transition-prev=slide-up transition-next=slide-down")
        .classes("left-panels-container bottom-panels-container") as bottom_panels
    ):

        def close_bottom_panels():
            bottom_tabs.value = None
            bottom_panels.value = None
            panels_wrap.classes(remove="coupled")
            ui.run_javascript("PanelResize.onTabChange('bottom', '')")

        with ui.tab_panel("response").classes(
            "overlay-card response-panel resizable-panel"
        ):
            with ui.row().classes("w-full"):
                ui.label("Log").classes("text-lg font-medium")
                ui.space()
                ui.button(icon="close", on_click=close_bottom_panels).props(
                    "flat round dense color=white"
                )
            ui_state.response_log = (
                ui.log(max_lines=1000)
                .classes("w-full h-full")
                .classes("no-x-scroll")
                .style(
                    "min-height: 200px !important; width: 100% !important; background: rgba(0, 0, 0, 0.65); border-radius: 10px;"
                )
            )
            ui.element("div").classes("resize-handle-top")
            ui.element("div").classes("resize-handle-right")
            ui.element("div").classes("resize-handle-corner")

        def update_bottom_layout():
            is_open = bool(bottom_tabs.value)
            top_is_resizable = side_tabs.value == "program"
            if is_open and top_is_resizable:
                panels_wrap.classes(add="coupled")
            else:
                panels_wrap.classes(remove="coupled")

        bottom_tabs.on("update:model-value", lambda _: update_bottom_layout())

        def handle_bottom_tab_change(e):
            to_tab = e.args or ""
            ui.run_javascript(f"PanelResize.onTabChange('bottom', '{to_tab}')")

        bottom_tabs.on("update:model-value", handle_bottom_tab_change)

        help_tab.on("click", lambda: help_menu.show_help_dialog())

        def _on_bottom_value_change(e):
            if e.args == "help":
                bottom_tabs.value = (
                    "response" if bottom_panels.value == "response" else None
                )

        bottom_tabs.on("update:model-value", _on_bottom_value_change)
        update_bottom_layout()

    return {
        "side_tabs": side_tabs,
        "top_panels": top_panels,
        "bottom_tabs": bottom_tabs,
        "bottom_panels": bottom_panels,
        "update_top_layout": update_top_layout,
        "update_bottom_layout": update_bottom_layout,
    }


def _setup_panel_persistence(refs: dict) -> None:
    """Configure PanelResize and restore tab state from localStorage."""
    side_tabs = refs["side_tabs"]
    top_panels = refs["top_panels"]
    bottom_tabs = refs["bottom_tabs"]
    bottom_panels = refs["bottom_panels"]
    update_top_layout = refs["update_top_layout"]
    update_bottom_layout = refs["update_bottom_layout"]

    ui.run_javascript(f"PanelResize.configure({json.dumps(PANEL_RESIZE_CONFIG)})")
    _gripper_preset = "camera" if camera_service.active else "default"
    ui.run_javascript(f'PanelResize.resizePanel("gripper", "{_gripper_preset}")')

    ui_client = ui.context.client

    async def restore_active_tabs():
        with ui_client:
            try:
                saved_tabs = await ui.run_javascript("PanelResize.getActiveTabs()")
                if saved_tabs:
                    if "top" in saved_tabs:
                        top_tab = saved_tabs["top"]
                        if top_tab == "gripper" and ui_state.gripper_page is None:
                            top_tab = None
                        side_tabs.value = top_tab
                        top_panels.value = top_tab
                        update_top_layout()
                        if top_tab:
                            ui.run_javascript(
                                f"PanelResize.onTabChange('top', '{top_tab}')"
                            )
                    if "bottom" in saved_tabs:
                        bottom_tab = saved_tabs["bottom"]
                        bottom_tabs.value = bottom_tab
                        bottom_panels.value = bottom_tab
                        update_bottom_layout()
                        if bottom_tab:
                            ui.run_javascript(
                                f"PanelResize.onTabChange('bottom', '{bottom_tab}')"
                            )
                    logger.debug("Restored active tabs: %s", saved_tabs)
            except Exception as e:
                logger.debug("Could not restore active tabs: %s", e)
            ui.run_javascript("PanelResize.onAppReady()")

    ui.timer(0.5, lambda: asyncio.create_task(restore_active_tabs()), once=True)


def build_page_content() -> None:
    """Build the Move page UI."""

    # Add Lottie player script for E-STOP dialog animations (load in HEAD early)
    ui.add_head_html(
        '<script type="module" defer src="https://unpkg.com/@lottiefiles/lottie-player@latest/dist/lottie-player.js"></script>'
    )
    # Add keybindings focus detection script
    ui.add_head_html('<script src="/static/js/keybindings.js" defer></script>')
    # Add animated robot face script
    ui.add_head_html('<script src="/static/js/robot-faces.js" defer></script>')

    with ui.column().classes("relative w-screen h-screen overflow-hidden gap-0"):
        with ui.column().classes("absolute inset-0 z-0"):

            async def _init():
                try:
                    await asyncio.wait_for(
                        readiness_state.app_ready.wait(), timeout=20.0
                    )
                except asyncio.TimeoutError:
                    loading_spinner.set_visibility(False)
                    loading_status.text = (
                        "Could not connect to controller. "
                        "Check that the controller is running and refresh the page."
                    )
                    loading_status.style(
                        "color: #ef4444; font-size: 1rem; text-align: center; "
                        "max-width: 400px;"
                    )
                    return

                await initialize_urdf_scene()

                # By now the serial transport has had time to receive
                # frames.  If hardware is detected, switch to robot mode.
                try:
                    result = await client.ping()
                    hw_now = bool(result.hardware_connected) if result else False
                except Exception:
                    hw_now = False
                if hw_now and robot_state.simulator_active:
                    logger.info("Hardware detected — switching to robot mode")
                    robot_state.simulator_active = False
                    try:
                        await client.simulator(False)
                        await client.resume()
                    except Exception as e:
                        logger.warning("auto robot-mode switch failed: %s", e)
                robot_state.connected = hw_now

                control_panel.update_robot_btn_visual()
                readout_panel.update_conn_io()

                # Enable gripper tab if a tool is already active
                if robot_state.tool_key and robot_state.tool_key != "NONE":
                    if ui_state._build_gripper_content is not None:
                        ui_state._build_gripper_content()
                    if ui_state._gripper_tab is not None:
                        ui_state._gripper_tab.props(remove="disable")

                scene_loading_overlay.classes("opacity-0 pointer-events-none")
                await asyncio.sleep(0.4)
                scene_loading_overlay.delete()

            ui.timer(0.05, _init, once=True)

        # Loading overlay — matches scene background, visible until backend is ready
        is_dark = is_dark_theme()
        bg = (
            SceneColors.BACKGROUND_DARK_HEX
            if is_dark
            else SceneColors.BACKGROUND_LIGHT_HEX
        )
        with (
            ui.column()
            .classes("absolute inset-0 z-10 items-center justify-center gap-4")
            .style(
                f"background: {bg}; transition: opacity 0.4s ease;"
            ) as scene_loading_overlay
        ):
            loading_spinner = ui.spinner("dots", size="xl", color="grey")
            loading_status = ui.label("Connecting to controller...").style(
                "color: grey; font-size: 0.9rem;"
            )

        # Main content area - overlay panels and HUD elements
        with (
            ui.column().classes("absolute inset-0 z-20").style("pointer-events: none;")
        ):
            with (
                ui.element("div")
                .classes("panels-wrap absolute inset-0 z-30")
                .style("pointer-events: none;") as panels_wrap
            ):
                panel_refs = _build_left_panels(panels_wrap)

        # HUD panels
        readout_panel.build("tr")
        control_panel.build("br")

        # Panel resize configuration and tab state restoration
        _setup_panel_persistence(panel_refs)

    # Set up global keybindings
    from waldo_commander.services.keybindings import setup_keybindings

    setup_keybindings(help_menu)


# Guard against duplicate startup/shutdown handler registration during tests
# When NiceGUI fails to reset between tests, runpy.run_path() re-executes main.py


def _quiet_shutdown_exception_handler(
    loop: asyncio.AbstractEventLoop, context: dict[str, object]
) -> None:
    """Filter expected cancellation noise once shutdown is in progress.

    During Ctrl-C, in-flight tasks (status consumer, ping timer, multicast
    socket reads) are cancelled mid-await. The resulting CancelledError /
    ConnectionResetError / "task was destroyed" messages aren't actionable —
    they're just the cost of asyncio teardown. While the app is alive we
    delegate to the default handler so real bugs still surface.
    """
    if _shutting_down:
        exc = context.get("exception")
        if isinstance(
            exc, (asyncio.CancelledError, ConnectionResetError, BrokenPipeError)
        ):
            return
        msg = str(context.get("message", ""))
        if (
            "was destroyed but it is pending" in msg
            or "coroutine was never awaited" in msg
            or "Task was destroyed" in msg
        ):
            return
    loop.default_exception_handler(context)


def _register_handlers() -> None:
    """Register startup/shutdown handlers only once.

    Skip registration if NiceGUI is already started (e.g., during test reruns
    when NiceGUI didn't fully reset between tests).
    """
    # If NiceGUI is already started, we can't register new handlers
    if ng_app.is_started:
        return

    async def _init_and_wait(port: str) -> None:
        """Start controller and wait for readiness."""
        if not controller_state.running:
            await start_controller(port)

        try:
            await client.wait_ready(timeout=15.0)
        except (TimeoutError, ConnectionError, OSError) as e:
            logger.debug("startup: wait_ready failed: %s", e)

    async def _set_initial_mode(port: str) -> None:
        """Start streaming; defer mode decision to page load.

        When a port is configured the controller already has a real serial
        transport — don't replace it with simulator.  The page-load ping
        in ``_init`` will set ``robot_state.simulator_active`` based on
        whether hardware is actually connected.
        """
        if not port:
            try:
                await client.simulator(True)
            except Exception as e:
                logger.error("startup: simulator(True) failed: %s", e)
            robot_state.simulator_active = True
        try:
            await client.resume()
        except Exception as e:
            logger.warning("startup: resume failed (may retry): %s", e)

    async def _restore_settings() -> None:
        """Restore persisted motion profile and tool selection."""
        try:
            saved_profile = ng_app.storage.general.get("motion_profile", "TOPPRA")
            await client.select_profile(saved_profile)
            logger.debug("startup: set motion profile to %s", saved_profile)
        except Exception as e:
            logger.warning("startup: select_profile failed: %s", e)

        try:
            saved_tool = ng_app.storage.general.get("selected_tool", "")
            if saved_tool:
                await client.select_tool(saved_tool)
                logger.debug("startup: set tool to %s", saved_tool)
        except Exception as e:
            logger.warning("startup: select_tool failed: %s", e)

    @ng_app.on_startup
    async def _on_startup() -> None:
        """NiceGUI startup hook.

        Any failure to start the controller (including "server already running")
        is treated as a hard error so tests cannot silently proceed in a bad state.
        """
        # Install an asyncio exception handler that swallows the cancellation
        # noise that fires when uvicorn tears down tasks during Ctrl-C.
        asyncio.get_running_loop().set_exception_handler(
            _quiet_shutdown_exception_handler
        )
        try:
            # Pre-warm process pool workers with RTB imports (runs in background)
            backend_pkg = ui_state.active_robot.backend_package
            asyncio.create_task(warm_process_pool(backend_pkg))

            try:
                port = ng_app.storage.general.get("com_port", "")
            except Exception:
                port = ""

            await _init_and_wait(port)
            await _set_initial_mode(port)
            await _restore_settings()
            # Sync editor slider mode now that simulator_active is known
            if editor_panel:
                editor_panel.playback.sync_mode()
            logger.info(
                "waldo-commander ready on http://%s:%s",
                config.server_host,
                config.server_port,
            )
        except Exception as e:
            logger.error("App startup init failed: %s", e)
            robot = ui_state.robot
            if robot is not None:
                await asyncio.to_thread(robot.stop)
            raise
        finally:
            _startup_complete.set()
            readiness_state.mark_startup_done()

    @ng_app.on_shutdown
    async def _on_shutdown() -> None:
        """NiceGUI shutdown hook - ensure controller and child processes are stopped."""
        global _shutting_down
        _shutting_down = True
        logger.debug("Nicegui Shutting Down...")
        camera_service.stop()

        # Wait for startup to complete first (with timeout to avoid hanging forever)
        try:
            await asyncio.wait_for(_startup_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Shutdown: startup did not complete within 10s, proceeding anyway"
            )

        # Stop any running script processes first
        try:
            if (
                editor_panel
                and editor_panel.script_running
                and editor_panel.script_handle
            ):
                logger.debug("Stopping running script process during shutdown...")
                from waldo_commander.services.script_runner import stop_script

                await stop_script(editor_panel.script_handle, timeout=2.0)
                editor_panel.script_handle = None
                editor_panel.script_running = False
                # Clean up stepping controller if active
                editor_panel._cleanup_stepping()
        except Exception as e:
            logger.warning("Error stopping script during shutdown: %s", e)

        # Cancel all timers first
        if ui_state._joint_jog_timer is not None:
            ui_state._joint_jog_timer.cancel()
        if ui_state._cart_jog_timer is not None:
            ui_state._cart_jog_timer.cancel()
        if _page_state is not None and _page_state.ping_timer is not None:
            _page_state.ping_timer.cancel()

        # Cleanup component timers and listeners
        if control_panel is not None:
            control_panel.cleanup()
        if ui_state.gripper_page is not None:
            ui_state.gripper_page.cleanup()
        if editor_panel is not None:
            editor_panel.cleanup()
        if ui_state.urdf_scene is not None:
            ui_state.urdf_scene.cleanup()

        # Shut down NiceGUI's process pool before stopping the controller,
        # so pool workers exit cleanly instead of becoming orphans.
        # Detach from the module global *before* calling shutdown(): NiceGUI's
        # own tear_down() also tries to kill the workers, and if it sees a
        # non-None process_pool whose internal _processes dict has been cleared
        # by our shutdown() call, it raises AssertionError on the way out.
        try:
            from nicegui import run as ng_run

            pool = ng_run.process_pool
            if pool is not None:
                ng_run.process_pool = None
                pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.debug("Error shutting down process pool: %s", e)

        await stop_controller()
        try:
            await client.close()
        except Exception as e:
            logger.debug("Error closing client: %s", e)

        # Log all multiprocessing active children and alive threads
        import multiprocessing

        for child in multiprocessing.active_children():
            logger.debug(
                "Active child: pid=%d name=%s alive=%s exitcode=%s daemon=%s",
                child.pid,
                child.name,
                child.is_alive(),
                child.exitcode,
                child.daemon,
            )
        import threading

        for t in threading.enumerate():
            if t is not threading.current_thread():
                logger.debug("Alive thread: name=%s daemon=%s", t.name, t.daemon)


# Register handlers at module load
_register_handlers()


def _cleanup_script_processes_sync() -> None:
    """Synchronously kill any running script subprocess.

    This is called from atexit and signal handlers as a last-resort cleanup.
    """
    try:
        if editor_panel and editor_panel.script_handle:
            proc = editor_panel.script_handle.get("proc")
            if proc and proc.returncode is None:
                logger.info("Killing orphaned script process (PID: %s)", proc.pid)
                try:
                    # On Unix, try to kill the entire process group
                    if sys.platform != "win32" and proc.pid:
                        try:
                            pgid = os.getpgid(proc.pid)
                            os.killpg(pgid, signal.SIGKILL)
                            logger.debug("Killed process group %s", pgid)
                        except (ProcessLookupError, OSError):
                            proc.kill()
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.debug("Error killing script process: %s", e)
    except Exception as e:
        logger.debug("Error in script cleanup: %s", e)


# Register atexit cleanup for last-resort process termination
atexit.register(_cleanup_script_processes_sync)


# --------------- Multi-tab takeover overlay ---------------
def _build_takeover_overlay(message: str) -> None:
    """Render the takeover overlay: scrim + glass card + wandering sad robot.

    Used as the entire page body for fresh shadow tabs, and as a top-layer
    overlay for previously-active tabs that have been evicted by another tab.
    All visual styling lives in theme.py under the `Takeover Overlay` section;
    this function only assigns class names.

    Idempotent per-client: sets a flag on the current Client instance so
    repeat callers (e.g. check_ping firing on a shadow tab that was already
    built with an overlay by index_page) skip a duplicate build.
    """
    from waldo_commander.components.readout import FACE_SVGS, RobotFace

    c = ui.context.client
    if getattr(c, "_waldo_overlay_shown", False):
        return
    c._waldo_overlay_shown = True  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    # robot-faces.js is normally loaded by build_page_content, which the
    # shadow branch skips. Load it here so initRobotFace / startRobotMope
    # are defined when the run_javascript bootstrap fires below.
    ui.add_head_html('<script src="/static/js/robot-faces.js" defer></script>')

    with ui.column().classes(
        "fixed inset-0 z-[9999] items-center justify-center bg-black/60"
    ):
        # Wandering sad robot — sibling of the card. JS sets transform to
        # mope around the viewport, avoiding the centered card's footprint.
        with ui.element("div").classes("robot-face robot-face-sad takeover-face"):
            ui.html(FACE_SVGS[RobotFace.SAD], sanitize=False).style(
                "width: 96px; height: 96px;"
            )

        with ui.column().classes("overlay-card items-center max-w-sm p-8"):
            ui.label("Waldo Commander").classes("text-xl font-semibold")
            ui.label(message).classes("text-sm text-center opacity-90")

            def _take_over() -> None:
                ui_state.active_client_id = None
                ui.navigate.reload()

            ui.button("Take over", on_click=_take_over).props(
                "color=primary unelevated rounded"
            ).classes("mt-2")

    # Bootstrap face animations + wandering. The robot-faces.js script tag
    # uses `defer`, so the functions may not be defined yet when this JS
    # arrives over the websocket. Poll briefly for them.
    ui.run_javascript(
        """
        (function bootstrap(retries) {
          if (typeof window.startRobotMope === 'function') {
            if (typeof window.initRobotFace === 'function') {
              window.initRobotFace('sad');
            }
            window.startRobotMope();
          } else if (retries > 0) {
            setTimeout(() => bootstrap(retries - 1), 50);
          } else {
            console.warn('takeover overlay: robot-faces.js never loaded');
          }
        })(60);
        """
    )


@ui.page("/")
async def index_page():
    global _page_state
    this_client = ui.context.client
    # Don't set _page_state yet — wait until panels are built so the
    # status consumer never touches stale panel references from a
    # previous (deleted) client.

    # Atomic claim of the multi-tab "active" slot. The CPython GIL makes
    # this race-safe enough for the rare case where two reloaded clients
    # connect simultaneously: only one branch will see an empty slot.
    # Also reclaim if the held id is stale (tab disconnected without
    # firing _on_disconnect, or test fixtures churning clients rapidly).
    held_id = ui_state.active_client_id
    if held_id is None or held_id not in Client.instances:
        ui_state.active_client_id = this_client.id
    is_active = ui_state.active_client_id == this_client.id

    def _on_disconnect():
        # Synchronous handler so the active-slot release happens *inline*
        # during NiceGUI's handle_disconnect() — async handlers are scheduled
        # as background tasks and would let the new client see a stale slot
        # on refresh.
        global _page_state
        if ui_state.active_client_id == this_client.id:
            ui_state.active_client_id = None
        # Atomically clear all per-connection state if this is still the
        # active client (avoid race on refresh).
        if _page_state is not None and _page_state.page_client is this_client:
            _page_state = None

    this_client.on_disconnect(_on_disconnect)

    if not is_active:
        # Shadow tab: render the takeover overlay only. Do NOT call
        # build_page_content / initialize_urdf_scene — those would mutate
        # the singletons that the active tab depends on. Install the
        # 1 Hz check_ping watchdog so the tab can auto-promote when the
        # active tab eventually closes.
        # Theme + layout CSS must be applied here too so the .takeover-*
        # classes (defined in theme.py) actually exist on shadow pages.
        apply_theme("dark")
        inject_layout_css()
        _build_takeover_overlay("Session active in another tab")
        ui.timer(interval=1.0, callback=check_ping, active=True)
        return

    # Theme and layout
    apply_theme("dark")
    ui.query(".nicegui-content").classes("p-0")
    inject_layout_css()

    # Build UI
    build_page_content()

    # Reflect startup-determined mode in UI; update connectivity only upward
    # (don't downgrade connected→disconnected from a transient ping failure;
    # the 1 Hz check_ping handles that with retries).
    try:
        result = await client.ping()
        hw_ok = result.hardware_connected if result else False
        if hw_ok:
            robot_state.connected = True
    except Exception as e:
        logger.warning("Connectivity check failed: %s", e)

    # Create jog timers and wire to ui_state so control panel can access them
    ui_state.joint_jog_timer = ui.timer(
        interval=config.webapp_control_interval_s,
        callback=control_panel.jog_tick,
        active=False,
    )
    ui_state.cart_jog_timer = ui.timer(
        interval=config.webapp_control_interval_s,
        callback=control_panel.cart_jog_tick,
        active=False,
    )

    # Attach logging handler to response log
    if ui_state.response_log:
        attach_ui_log(ui_state.response_log)

    # All panels built — now allow the status consumer to update UI.
    # Page-scoped connectivity check (1 Hz) is stored in the state too.
    _page_state = _PageState(
        page_client=this_client,
        ping_timer=ui.timer(interval=1.0, callback=check_ping, active=True),
    )

    # Mark page as ready for tests
    async def _mark_page_done():
        await asyncio.sleep(0)  # Yield to event loop to ensure timers are wired
        readiness_state.mark_page_done()

    asyncio.create_task(_mark_page_done())


async def _status_consumer() -> None:
    """Consume multicast status and update shared robot_state."""
    try:
        # Wait for server to be responsive before subscribing to multicast
        await client.wait_ready(timeout=15.0)
        async for status in client.stream_status_shared():
            try:
                # Track loop timing via LoopMetrics
                now = time.perf_counter()
                _ui_metrics.tick(now)

                # Rate-limited debug log every 3s
                if _ui_metrics.should_log(now, 3.0):
                    for p in global_phase_timer.phases.values():
                        p.compute_stats()

                    # Build phase timing string for non-zero phases
                    phase_strs = []
                    for name, phase in global_phase_timer.phases.items():
                        if phase.mean_s > 0.00001:
                            phase_strs.append(f"{name}={phase.mean_s * 1000:.2f}")

                    logger.debug(
                        "ui: %s | %s",
                        format_hz_summary(_ui_metrics),
                        " ".join(phase_strs),
                    )

                with global_phase_timer.phase("status"):
                    # Copy status data (in-place fills to avoid allocations)
                    if (
                        not robot_state.editing_mode
                        and not simulation_state.sim_pose_override
                    ):
                        robot_state.angles.set_deg(status.angles)
                    robot_state.pose[:] = status.pose
                    robot_state.io[:] = status.io
                    if not simulation_state.sim_pose_override:
                        robot_state.tool_status = status.tool_status

                    # Speeds arrive as rad/s from backend — convert to deg/s for display
                    np.rad2deg(status.speeds, out=robot_state.speeds)
                    robot_state.tcp_speed = (
                        0.3 * status.tcp_speed + 0.7 * robot_state.tcp_speed
                    )

                    # Mark backend ready on first valid STATUS
                    readiness_state.mark_backend_done()

                    # Movement enablement arrays
                    robot_state.joint_en[:] = status.joint_en
                    for frame, arr in status.cart_en.items():
                        if frame in robot_state.cart_en:
                            robot_state.cart_en[frame][:] = arr

                    robot_state.action_current = status.action_current
                    robot_state.action_params = status.action_params
                    robot_state.action_state = status.action_state
                    robot_state.executing_index = status.executing_index
                    robot_state.completed_index = status.completed_index
                    robot_state.last_update_ts = time.time()

                    # Auto-clear scrub override after teleport has had time to propagate
                    if (
                        simulation_state.sim_pose_override
                        and not simulation_state.sim_playback_active
                        and simulation_state.last_teleport_ts > 0
                        and (time.monotonic() - simulation_state.last_teleport_ts) > 0.1
                    ):
                        simulation_state.sim_pose_override = False
                        simulation_state.last_teleport_ts = 0.0

                    # Both checks needed: _deleted guards the brief window
                    # between NiceGUI marking the client dead and removing it
                    # from Client.instances.
                    ps = _page_state
                    pc = ps.page_client if ps is not None else None
                    if pc is not None and not pc._deleted and pc.id in Client.instances:
                        with pc:
                            # Update UI from status
                            update_ui_from_status()

                            # Update panels
                            readout_panel.update_conn_io()
                            action_log.process_status(
                                robot_state.action_current,
                                robot_state.action_params,
                                robot_state.action_state,
                                robot_state.executing_index,
                                robot_state.completed_index,
                            )
                            readout_panel.update_action_log()
                            control_panel.refresh_joint_enablement()
                            control_panel.sync_cartesian_button_states()
                            control_panel.sync_gizmo_for_jog_state()
                            if ui_state.gripper_page is not None:
                                ui_state.gripper_page.update_chart()
                                ui_state.gripper_page.update_status()

            except Exception as e:
                logger.debug("Status consumer parse error: %s", e)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Status consumer error: %s", e)


def main():
    global client, control_panel, readout_panel, editor_panel

    # CLI: web bind, controller target, and log level
    # Defaults come from config (lazy evaluation - reads env vars at access time)
    parser = argparse.ArgumentParser(description="PAROL6 NiceGUI Webserver")
    parser.add_argument(
        "--host", default=config.server_host, help="Webserver bind host"
    )
    parser.add_argument(
        "--port", type=int, default=config.server_port, help="Webserver bind port"
    )
    parser.add_argument(
        "--controller-host",
        default=config.controller_host,
        help="Controller host to connect to",
    )
    parser.add_argument(
        "--controller-port",
        type=int,
        default=config.controller_port,
        help="Controller UDP port",
    )
    parser.add_argument(
        "--log-level",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set log level",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity; -v=INFO, -vv=DEBUG",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Enable WARNING logging"
    )
    parser.add_argument(
        "--robot",
        default=None,
        help="Robot backend name (default: auto-detect or WALDO_ROBOT env var)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on file changes (dev mode)",
    )
    args, _ = parser.parse_known_args()

    # Entry-point wrappers (pip console_scripts) set __name__ to the module name,
    # not "__main__".  NiceGUI's reload relies on __mp_main__ which only works when
    # the module is executed via `python -m`.  Re-exec transparently.
    if args.reload and __name__ != "__main__":
        os.execvp(
            sys.executable,
            [sys.executable, "-m", "waldo_commander.main"] + sys.argv[1:],
        )

    # Apply CLI overrides to config (these take precedence over env vars)
    config.set("server_host", args.host)
    config.set("server_port", args.port)
    config.set("controller_host", args.controller_host)
    config.set("controller_port", args.controller_port)

    # Resolve log level priority: explicit --log-level > -v/-q > env default
    if args.log_level:
        if args.log_level == "TRACE":
            config.set("log_level", TRACE)
        else:
            config.set("log_level", getattr(logging, args.log_level))
    elif args.verbose >= 3:
        os.environ["WALDO_TRACE"] = "1"
        config.set("log_level", TRACE)
    elif args.verbose >= 2:
        config.set("log_level", logging.DEBUG)
    elif args.verbose == 1:
        config.set("log_level", logging.INFO)
    elif args.quiet:
        config.set("log_level", logging.WARNING)
    # else: use env var default (no override needed)

    # Initialize robot, client, and component instances
    robot = get_robot(name=args.robot)
    ui_state.robot = robot
    # Initialize cart_en buffers from robot's cartesian frames
    robot_state.init_cart_en(robot.cartesian_frames)
    # Resize IO buffer to match robot's pin count
    io_size = robot.digital_inputs + robot.digital_outputs + 1  # +1 for estop
    robot_state.io = np.zeros(io_size, dtype=np.int32)
    robot_state.io_inputs = [0] * robot.digital_inputs
    robot_state.io_outputs = [0] * robot.digital_outputs
    robot_state.speeds = np.zeros(robot.joints.count, dtype=np.float64)
    # Resize pipeline buffers to match this robot's joint count
    init_angle_buffers(robot.joints.count)
    # Use longer timeout for CI environments where scheduling can cause delays
    client = robot.create_async_client(
        host=config.controller_host, port=config.controller_port, timeout=5.0
    )
    control_panel = ControlPanel(client)
    readout_panel = ReadoutPanel()
    editor_panel = EditorPanel()
    # Store panels in ui_state for cross-module access
    ui_state.control_panel = control_panel
    ui_state.editor_panel = editor_panel
    ui_state.playback = editor_panel.playback
    ui_state.readout_panel = readout_panel

    # Configure logging
    configure_logging(config.log_level)
    logger.debug(
        "Webserver bind: host=%s port=%s", config.server_host, config.server_port
    )
    logger.debug(
        "Controller target: host=%s port=%s",
        config.controller_host,
        config.controller_port,
    )

    # Pre-compile numba functions to avoid JIT lag during hot path
    warmup_pipelines()

    try:
        ui.run(
            title="PAROL6 NiceGUI Commander",
            host=config.server_host,
            port=config.server_port,
            reload=args.reload,
            uvicorn_reload_excludes=".*, .py[cod], .sw.*, ~*, programs/*, .*/*, .nicegui/*",
            show=False,
            loop="uvloop" if sys.platform != "win32" else "asyncio",
            http="httptools",
            binding_refresh_interval=0.05,
        )
    except KeyboardInterrupt:
        # The NiceGUI on_shutdown hook already cleaned up child processes,
        # threads, and the controller; nothing else to do here.
        if logger.isEnabledFor(logging.DEBUG):
            import multiprocessing
            import threading

            for child in multiprocessing.active_children():
                logger.debug(
                    "exit: active child pid=%d name=%s alive=%s exitcode=%s daemon=%s",
                    child.pid,
                    child.name,
                    child.is_alive(),
                    child.exitcode,
                    child.daemon,
                )
            for t in threading.enumerate():
                if t is not threading.current_thread():
                    logger.debug(
                        "exit: alive thread name=%s daemon=%s", t.name, t.daemon
                    )
        print("waldo-commander: shutdown complete")


if __name__ in {"__main__", "__mp_main__"}:
    main()
