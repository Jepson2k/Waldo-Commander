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
from nicegui import Client, app as ng_app, background_tasks, ui
from pinokin import arrays_equal_n
import waldoctl
from waldoctl import (
    Commander,
    FrameJogAvailability,
    GripperTool,
    LinearMotion,
    Panel,
    PanelSlot,
    RobotClient,
    RobotStatus,
    Settings,
    iter_plugin_panels,
)

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
from waldo_commander.components.playback import playback
from waldo_commander.components.script_execution import script_exec
from waldo_commander.components.readout import ReadoutPanel
from waldo_commander.constants import config, DEFAULT_CAMERA, RESERVED_TAB_IDS
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
from waldo_commander.mcp import start_mcp_server, stop_mcp_server
from waldo_commander.services.urdf_scene.scene_handle import WcSceneHandle
from waldo_commander.services.action_log import action_log_service
from waldo_commander.services.control_lease import (
    BROWSER,
    browser_claim_if_unheld,
    control_lease,
    restore_control_mode,
)
from waldo_commander.services.programs import EditorPrograms, is_any_program_running
from waldo_commander.services import startup_mode
from waldo_commander.services.urdf_scene.envelope_renderer import workspace_envelope
from waldo_commander.state import (
    automation_state,
    robot_state,
    controller_state,
    ui_state,
    readiness_state,
    playback_coordination,
    global_phase_timer,
    robot_events,
)

logger = logging.getLogger(__name__)

STATIC_DIR = pkg_files("waldo_commander").joinpath("static")
ng_app.add_static_files("/static", str(STATIC_DIR))

# Initialized in main() after CLI parsing.
client: RobotClient

# Multicast-driven status consumer (runs once per app)
status_consumer_task: asyncio.Task | None = None
# Set during _on_shutdown so the asyncio exception handler can swallow
# expected cancellation/connection errors that fire as tasks unwind.
_shutting_down: bool = False

# Assigned in main(), None until then.
control_panel: ControlPanel = None  # ty: ignore[invalid-assignment]
readout_panel: ReadoutPanel = None  # ty: ignore[invalid-assignment]
editor_panel: EditorPanel = None  # ty: ignore[invalid-assignment]


@dataclass
class _PageState:
    """Per-browser-connection state.  Set to None atomically on disconnect."""

    page_client: Client
    connection_notification: ui.notification | None = None
    warning_notification: ui.notification | None = None
    warning_banner_text: str = ""
    ping_timer: ui.timer | None = None
    last_ping_ok: bool = False


_page_state: _PageState | None = None


def _client_alive(pc: Client) -> bool:
    """Both checks needed: ``_deleted`` covers the brief window between
    NiceGUI marking the client dead and removing it from Client.instances."""
    return not pc._deleted and pc.id in Client.instances


# Pre-allocated buffers for numba pipelines (scratch space)
_rotation_matrix_buffer: np.ndarray = np.zeros((3, 3), dtype=np.float64)
_rpy_rad_buffer: np.ndarray = np.zeros(3, dtype=np.float64)
_pose_result_buffer: np.ndarray = np.zeros(6, dtype=np.float64)  # [x,y,z,rx,ry,rz]
_DEG_TO_RAD: float = math.pi / 180.0


_ui_metrics = LoopMetrics()

# _on_shutdown() waits on this for _on_startup() to finish.
_startup_complete: asyncio.Event = asyncio.Event()


def _update_connection_notification() -> None:
    """Show or dismiss persistent notification based on robot connection state."""
    ps = _page_state
    if ps is None:
        return

    # Gate on scene-ready (not app_ready) so the banner still works when the
    # backend never streams a STATUS frame; the scene signal also guarantees
    # the page is past serialization, so elements are safe to modify.
    if not readiness_state.urdf_scene_ready.is_set():
        return

    needs_warning = (
        not waldoctl.commander.status.simulator_active
        and not waldoctl.commander.status.connected
    )

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


def _update_warning_notification() -> None:
    """Persistent banner while warning-class conditions stand.

    Same mechanism as the hard-error connection banner, colored as a
    warning; it leaves when the conditions self-clear. History lives in
    the warnings/errors log under the movement log."""
    ps = _page_state
    if ps is None or not readiness_state.urdf_scene_ready.is_set():
        return
    entries = waldoctl.commander.status.warnings.entries
    msg = "; ".join(str(e[2]) for e in entries)
    if msg == ps.warning_banner_text:
        return
    if ps.warning_notification is not None:
        ps.warning_notification.dismiss()
        # dismiss() only tells the client; the server element normally
        # deletes itself on the client's dismiss event, which never comes
        # without a real browser.
        if not ps.warning_notification.is_deleted:
            ps.warning_notification.delete()
        ps.warning_notification = None
    if msg:
        ps.warning_notification = ui.notification(
            message=msg,
            type="warning",
            close_button=True,
            timeout=0,
        )
    ps.warning_banner_text = msg


async def initialize_urdf_scene() -> None:
    """Initialize the URDF scene with error handling."""
    robot = ui_state.active_robot
    urdf_path = Path(robot.urdf_path)
    mesh_dir = Path(robot.mesh_dir)

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
        # Resolve per-variant TCP, overriding origin and rpy independently (a
        # variant that sets only tcp_rpy keeps its rotation — the old inline
        # logic dropped it because it gated the whole override on tcp_origin).
        origin, rpy = waldoctl.resolve_variant_tcp(
            tool.tcp_origin, tool.tcp_rpy, tool.variants, variant_key
        )
        return ToolPose(origin=list(origin), rpy=list(rpy))

    scene_config = UrdfSceneConfig(
        tool_pose_resolver=tool_pose_resolver,
        gizmo_scale=1.35,  # 1.0 = default STL scale
        package_map={robot.backend_package: mesh_dir},
        material=material_color,
        background_color=bg_color,
        sim_color=SceneColors.SIM_AMBER_HEX,
        sim_opacity=0.9,
        # Kinematic-mapping defaults are correct for PAROL6, so none are set here.
    )

    ui_state.urdf_scene = UrdfScene(urdf_path, config=scene_config)
    ui_state.urdf_scene.show(
        material=scene_config.material,
        background_color=scene_config.background_color,
    )

    # Align TCP and load tool mesh from the controller's active tool.
    try:
        result = await client.tools()
        if result and result.tool:
            vk = ng_app.storage.general.get(f"tool_variant_{result.tool}")
            ui_state.urdf_scene.apply_tool_everywhere(result.tool, variant_key=vk)
    except Exception as e:
        logger.error("Failed to sync TCP tool pose: %s", e)

    if ui_state.urdf_scene.scene:
        scene: ui.scene = ui_state.urdf_scene.scene
        scene._props["grid"] = (10, 100)
        # Fill parent container (absolute canvas).
        scene.classes(remove="h-[66vh]").style(
            "width: 100%; height: 100%; margin: 0; display: block;"
        )
        scene.move_camera(**DEFAULT_CAMERA, duration=0.0)

        # World coordinate frame at origin (fixed).
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

    ui_state.urdf_joint_names = list(ui_state.urdf_scene.get_joint_names())

    logger.debug("URDF scene initialized with joints: %s", ui_state.urdf_joint_names)

    readiness_state.signal_urdf_scene_ready()

    # Settings page may have built before the scene was ready.
    stored_tool = ng_app.storage.general.get("selected_tool")
    if stored_tool and stored_tool != "NONE" and ui_state.urdf_scene:
        vk = ng_app.storage.general.get(f"tool_variant_{stored_tool}")
        ui_state.urdf_scene.apply_tool_everywhere(stored_tool, variant_key=vk)
    else:
        # Gizmo sync needs fresh FK even without a tool change.
        ui_state.urdf_scene.invalidate_fk_cache()

    # Generate the workspace hull with the correct tool offset (after tool applied).
    if not os.environ.get("WALDO_SKIP_ENVELOPE") and not workspace_envelope.is_ready:
        workspace_envelope.generate(
            tool_offset_z=ui_state.urdf_scene._current_tool_offset_z
        )

    control_panel.sync_gizmo_to_urdf()

    # Keep-out shapes persist per-process on commander.scene but this scene is
    # rebuilt per page load — re-render them or barriers turn invisible while
    # still enforced.
    scene_handle = waldoctl.commander.scene
    if scene_handle is not None:
        scene_handle.render()

    # Scene wasn't ready earlier, so apply simulator appearance now.
    if waldoctl.commander.status.simulator_active:
        ui_state.urdf_scene.set_simulator_appearance(True)


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

    global status_consumer_task
    ps = _page_state
    if ps is not None and ps.ping_timer is not None:
        ps.ping_timer.active = True
    if status_consumer_task is None or status_consumer_task.done():
        status_consumer_task = asyncio.create_task(_status_consumer())
    asyncio.create_task(_check_config_skew())
    controller_state.running = True
    logger.debug("Controller started")


async def stop_controller() -> None:
    global status_consumer_task
    try:
        robot = ui_state.robot
        if robot is not None:
            logger.info("Stopping controller...")
            await asyncio.to_thread(robot.stop)

        ps = _page_state
        if ps is not None and ps.ping_timer is not None:
            ps.ping_timer.active = False
        if status_consumer_task is not None and not status_consumer_task.done():
            status_consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_consumer_task

        controller_state.running = False
        waldoctl.commander.status.connected = False
        logger.info("Controller stopped")
    except Exception as e:
        logger.error("Stop controller failed: %s", e)


# One auto-switch per app run: set when the switch begins (blocks re-entry
# while in flight) and cleared only if it fails, so check_ping keeps retrying
# until the first success and never flips the user back afterwards.
_hw_auto_switched: bool = False


async def _switch_to_hardware() -> None:
    """One-time simulator→robot switch once hardware appears."""
    global _hw_auto_switched
    _hw_auto_switched = True
    try:
        await client.simulator(False)
        await client.reset()
    except Exception as e:
        _hw_auto_switched = False
        logger.warning("auto robot-mode switch failed: %s", e)
        return
    logger.info("Hardware detected — switching to robot mode")
    waldoctl.commander.status.simulator_active = False


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
            if new_ok:
                # A reconnect may be a RESTARTED controller (fresh, empty
                # program layer) or one whose world changed while we were
                # unreachable — adopt its readback truth; never push a
                # GUI-remembered copy.
                scene_handle = waldoctl.commander.scene
                if scene_handle is not None:
                    asyncio.create_task(scene_handle.refresh_from_backend())
        ps.last_ping_ok = new_ok
    except Exception as e:
        logger.debug("ping failed: %s", e)
        if ps.last_ping_ok:
            logger.debug("ping: connected True → False (exception)")
        ps.last_ping_ok = False

    # A ping that was in flight when shutdown began can resume here after
    # _clear_commander has run; bail before touching the commander surface.
    if _shutting_down:
        return

    # Hardware auto-detect: a slow-booting controller misses the page-load
    # window, so the switch retries here until its first success.
    if (
        ps.last_ping_ok
        and waldoctl.commander.status.simulator_active
        and not _hw_auto_switched
        and startup_mode.stored_startup_mode() != startup_mode.SIM
    ):
        await _switch_to_hardware()

    # Update robot connectivity status. The multicast status consumer drives
    # the joint/cartesian button sync at status rate; the calls below cover
    # the "no STATUS frames" paths (stream went silent, or none ever arrived)
    # that the consumer cannot, since they read
    # waldoctl.commander.status.connected directly.
    waldoctl.commander.status.connected = ps.last_ping_ok
    _update_connection_notification()
    if readout_panel is not None:
        readout_panel.update_conn_io()
    if control_panel is not None:
        control_panel.sync_gizmo_for_jog_state()
        control_panel.refresh_control_indicator()


def update_ui_from_status() -> None:
    """Update UI elements from robot_state (called from multicast consumer)"""
    # Editing sync handles position/angle updates in editing mode.
    skip_position_updates = waldoctl.commander.status.editing_mode
    # Teleport syncs backend during sim playback/scrubbing.
    skip_scene_updates = (
        skip_position_updates or playback_coordination.sim_pose_override
    )

    if not skip_scene_updates:
        with global_phase_timer.phase("scene"):
            update_urdf_angles(waldoctl.commander.status.joints.angles.deg)
            if ui_state.urdf_scene:
                ui_state.urdf_scene.update_from_robot_state()

    if not skip_position_updates:
        # robot_state.pose is already numpy float64; pass directly to numba.
        pose_extraction_pipeline(
            robot_state.pose,
            _rotation_matrix_buffer,
            _rpy_rad_buffer,
            _pose_result_buffer,
        )

        _pose = waldoctl.commander.status.pose
        _pose.x = _pose_result_buffer[0]
        _pose.y = _pose_result_buffer[1]
        _pose.z = _pose_result_buffer[2]
        _pose.rx = _pose_result_buffer[3]
        _pose.ry = _pose_result_buffer[4]
        _pose.rz = _pose_result_buffer[5]
        # Orientation array is kept on robot_state for rad access from FK
        # consumers (IK solver, motion recorder). Scalars above are the
        # public surface.
        robot_state.orientation.set_deg(_pose_result_buffer[3:6])

    # Push IO derived fields into ``commander.status.io`` for public
    # consumers (UI panels, MCP, future plugins).
    n_in = ui_state.active_robot.digital_inputs
    n_out = ui_state.active_robot.digital_outputs
    io = waldoctl.commander.status.io
    buf = robot_state.io
    # Compare element-wise against the existing lists and rebuild only on
    # change — avoids coercing the list to a temp ndarray every tick (IO
    # rarely changes; 0/1 values reuse cached int/bool singletons).
    if len(io.inputs) != n_in or any(int(buf[i]) != io.inputs[i] for i in range(n_in)):
        io.inputs = buf[:n_in].tolist()
    if len(io.outputs) != n_out or any(
        int(buf[n_in + i]) != io.outputs[i] for i in range(n_out)
    ):
        io.outputs = buf[n_in : n_in + n_out].tolist()
    io.estop = int(buf[n_in + n_out])

    # Push tool status fields into commander.status.tool. Each leaf
    # assignment fires its bindable_dataclass bindings synchronously;
    # tuple reassignments (positions / channels) propagate to gripper
    # readouts that bind through a backward function.
    ts = robot_state.tool_status
    pub_tool = waldoctl.commander.status.tool
    tool_key_changed = ts.key != pub_tool.key
    pub_tool.key = ts.key
    pub_tool.positions = ts.positions
    pub_tool.engaged = ts.engaged
    pub_tool.part_detected = ts.part_detected
    pub_tool.state = ts.state
    pub_tool.fault_code = ts.fault_code
    pub_tool.channels = ts.channels
    robot_state.tool_time_series.push(pub_tool.position, pub_tool.current)

    # Build gripper tab on first tool detection
    if tool_key_changed and pub_tool.key != "NONE":
        try:
            if ui_state._build_gripper_content is not None:
                ui_state._build_gripper_content()
            if ui_state._gripper_tab is not None:
                ui_state._gripper_tab.props(remove="disable")
        except RuntimeError:
            pass

    if control_panel.tool_actions:
        control_panel.tool_actions.update_visual()

    if control_panel.estop:
        control_panel.estop.check_state_change()

    # Skip notifying listeners until the scene is built to avoid a race with
    # NiceGUI page serialization (envelope proximity updates depend on this).
    # Scene-ready rather than app_ready so a boot without a STATUS frame
    # (robot unplugged) still drives the status-dependent UI.
    if not readiness_state.urdf_scene_ready.is_set():
        return

    _update_connection_notification()
    _update_warning_notification()
    if readout_panel is not None:
        readout_panel.update_event_log()
    if control_panel is not None:
        control_panel.sync_freedrive_visual()
    if tool_key_changed:
        robot_state.notify_changed()


def _discover_plugin_panels() -> None:
    """Populate ``ui_state.plugin_panels`` from the ``waldoctl.panels`` group.

    Cached for the process: once any panel is discovered the populated list
    short-circuits further scans (an empty result is falsy, so a no-plugins
    install simply re-scans per page build — cheap). Plugins listed in
    ``commander.settings.plugins.disabled_panels``, or whose id collides with a
    core tab, are skipped; survivors whose ``applies_to(commander)`` returns
    ``False`` are filtered out. Final order is ``(slot, order, id)`` so tab
    layout is deterministic.
    """
    if ui_state.plugin_panels:
        return
    commander = waldoctl.commander
    disabled = set(commander.settings.plugins.disabled_panels)
    panels: list[Panel] = []
    for cls in iter_plugin_panels():
        if cls.id in disabled:
            continue
        if cls.id in RESERVED_TAB_IDS:
            logger.warning(
                "Skipping plugin panel %s: id %r collides with a core tab",
                cls.__name__,
                cls.id,
            )
            continue
        try:
            p = cls()
            if p.applies_to(commander):
                panels.append(p)
        except Exception as e:
            logger.warning("Plugin panel %s init failed: %s", cls, e)
    panels.sort(key=lambda p: (p.slot.value, p.order, p.id))
    ui_state.plugin_panels = panels


def _add_plugin_tabs(slot: PanelSlot) -> None:
    """Add a ``ui.tab`` for each discovered plugin panel in *slot*.

    Call inside the relevant ``ui.tabs()`` context.
    """
    for p in ui_state.plugin_panels:
        if p.slot is slot:
            tab = ui.tab(name=p.id, label="", icon=p.tab_icon or "extension")
            if p.tab_tooltip:
                tab.tooltip(p.tab_tooltip)
            tab.mark(f"tab-{p.id}")


def _add_plugin_tab_panels(slot: PanelSlot, commander: Commander) -> None:
    """Add a built ``ui.tab_panel`` for each discovered plugin panel in *slot*.

    Call inside the relevant ``ui.tab_panels()`` context.
    """
    for p in ui_state.plugin_panels:
        if p.slot is slot:
            with ui.tab_panel(p.id).classes("gap-2 overlay-card overflow-hidden"):
                # A third-party plugin's build() must not blank the whole page;
                # leave an empty-but-valid tab panel on failure (mirrors the
                # init guard in _discover_plugin_panels).
                try:
                    p.build(commander)
                except Exception as e:
                    logger.warning("Plugin panel %s build failed: %s", p.id, e)


def _build_left_panels(panels_wrap: ui.element) -> dict:
    """Build top (program/io/gripper) and bottom (log/help) panel groups.

    Returns a dict of references needed by _setup_panel_persistence().
    """
    _discover_plugin_panels()
    commander = waldoctl.commander
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

        _add_plugin_tabs(PanelSlot.LEFT_TOP_TAB)

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
                                waldoctl.commander.status.tool,
                                "key",
                                backward=lambda k: (
                                    f"Gripper: {k}" if k != "NONE" else "Gripper"
                                ),
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
                            waldoctl.commander.status.tool,
                            "key",
                            backward=_update_features,
                        )
                        ui.space()
                        ui.button(icon="close", on_click=close_top_panels).props(
                            "flat round dense color=white"
                        )
                    ui_state.gripper_page = GripperPage(client)
                    ui_state.gripper_page.build()

            ui_state._build_gripper_content = _build_gripper_content

        _add_plugin_tab_panels(PanelSlot.LEFT_TOP_TAB, commander)

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

        _add_plugin_tabs(PanelSlot.LEFT_BOTTOM_TAB)

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

        _add_plugin_tab_panels(PanelSlot.LEFT_BOTTOM_TAB, commander)

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
                    # A persisted tab id can name a plugin that's since been
                    # disabled / uninstalled; restoring it would select a tab
                    # that no longer exists. Null out anything not currently a
                    # core or discovered-plugin tab.
                    top_valid = {"program", "io", "gripper"} | {
                        p.id
                        for p in ui_state.plugin_panels
                        if p.slot is PanelSlot.LEFT_TOP_TAB
                    }
                    bottom_valid = {"response", "help"} | {
                        p.id
                        for p in ui_state.plugin_panels
                        if p.slot is PanelSlot.LEFT_BOTTOM_TAB
                    }
                    if "top" in saved_tabs:
                        top_tab = saved_tabs["top"]
                        if top_tab not in top_valid or (
                            top_tab == "gripper" and ui_state.gripper_page is None
                        ):
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
                        if bottom_tab not in bottom_valid:
                            bottom_tab = None
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

    # Lottie player for E-STOP dialog animations; load early in HEAD.
    ui.add_head_html(
        '<script type="module" defer src="https://unpkg.com/@lottiefiles/lottie-player@latest/dist/lottie-player.js"></script>'
    )
    ui.add_head_html('<script src="/static/js/keybindings.js" defer></script>')
    ui.add_head_html('<script src="/static/js/robot-faces.js" defer></script>')

    with ui.column().classes("relative w-screen h-screen overflow-hidden gap-0"):
        with ui.column().classes("absolute inset-0 z-0"):

            async def _init():
                try:
                    await asyncio.wait_for(
                        readiness_state.app_ready.wait(), timeout=3.0
                    )
                except asyncio.TimeoutError:
                    # No STATUS frame (com_port configured but no robot wired).
                    # The scene only needs local URDF assets and guarded client
                    # calls, so render it anyway instead of blocking the page.
                    loading_spinner.set_visibility(False)
                    loading_status.text = (
                        "Robot disconnected — proceeding without live data"
                    )

                await initialize_urdf_scene()

                # Seed connectivity for the first render; the hardware
                # auto-detect lives in check_ping, which keeps retrying, so
                # a controller that boots slower than the page still flips
                # out of simulator when its first frames arrive.
                try:
                    result = await client.ping()
                    hw_now = bool(result.hardware_connected) if result else False
                except Exception:
                    hw_now = False
                waldoctl.commander.status.connected = hw_now

                control_panel.update_robot_btn_visual()
                readout_panel.update_conn_io()

                # Enable gripper tab if a tool is already active
                if (
                    waldoctl.commander.status.tool.key
                    and waldoctl.commander.status.tool.key != "NONE"
                ):
                    if ui_state._build_gripper_content is not None:
                        ui_state._build_gripper_content()
                    if ui_state._gripper_tab is not None:
                        ui_state._gripper_tab.props(remove="disable")

                if not readiness_state.app_ready.is_set():
                    # No STATUS frame will trigger the banner update — show it now.
                    _update_connection_notification()

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

        # Overlay panels and HUD elements.
        with (
            ui.column().classes("absolute inset-0 z-20").style("pointer-events: none;")
        ):
            with (
                ui.element("div")
                .classes("panels-wrap absolute inset-0 z-30")
                .style("pointer-events: none;") as panels_wrap
            ):
                panel_refs = _build_left_panels(panels_wrap)

        readout_panel.build("tr")
        control_panel.build("br")

        _setup_panel_persistence(panel_refs)

    from waldo_commander.services.keybindings import setup_keybindings

    setup_keybindings(help_menu)


# Guard against duplicate startup/shutdown handler registration during tests:
# when NiceGUI fails to reset between tests, runpy.run_path() re-executes main.py.


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


async def _start_plugin_panels() -> None:
    """Run ``Panel.start`` once per process for every discovered plugin panel.

    Called after the page is built so plugin UI references are valid. Each
    panel is marked started *before* its ``start`` is awaited, so a page reload
    landing mid-start can't double-start it; only panels not yet started are
    run, so a panel enabled/installed after an empty first build still starts.
    Errors in one plugin's ``start`` do not stop the others.
    """
    commander = waldoctl.commander
    pending = [
        p for p in ui_state.plugin_panels if p.id not in ui_state._started_panel_ids
    ]
    if not pending:
        return
    ui_state._started_panel_ids.update(p.id for p in pending)
    results = await asyncio.gather(
        *(p.start(commander) for p in pending),
        return_exceptions=True,
    )
    for p, r in zip(pending, results):
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            logger.warning("Plugin panel %r start failed: %s", p.id, r)


async def _stop_plugin_panels() -> None:
    """Run ``Panel.stop`` for every discovered plugin panel.

    Each ``stop`` is bounded by a 2-second timeout so a misbehaving plugin
    cannot block app shutdown, and the stops run concurrently (mirroring
    ``_start_plugin_panels``) so N stuck plugins add ~2s to shutdown, not
    N*2s.  Errors are logged, never raised.
    """

    async def _stop_one(p: Panel) -> None:
        try:
            await asyncio.wait_for(p.stop(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Plugin panel %r stop timed out", p.id)
        except Exception as e:
            logger.warning("Plugin panel %r stop failed: %s", p.id, e)

    await asyncio.gather(*(_stop_one(p) for p in ui_state.plugin_panels))


async def _set_initial_mode(port: str) -> None:
    """Pick the boot mode: without a serial port the simulator always runs
    (a stored HARDWARE preference would otherwise boot with no transport at
    all — a dead app); with a port, the persisted ``startup_mode``
    preference wins, else keep the real transport and let the hardware
    auto-detect in ``check_ping`` flip out of simulator when frames arrive
    (unless simulator mode is pinned).
    """
    stored = startup_mode.stored_startup_mode()
    use_sim = not port or stored == startup_mode.SIM
    if use_sim:
        try:
            await client.simulator(True)
        except Exception as e:
            logger.error("startup: simulator(True) failed: %s", e)
        waldoctl.commander.status.simulator_active = True
    try:
        await client.reset()
    except Exception as e:
        logger.warning("startup: reset failed (may retry): %s", e)


def _register_handlers() -> None:
    """Register startup/shutdown handlers only once.

    Skip registration if NiceGUI is already started (e.g., during test reruns
    when NiceGUI didn't fully reset between tests).
    """
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
                # initialize_urdf_scene already reads the same variant
                # storage key when wiring the scene mesh; pass it to
                # select_tool too so the controller's per-variant TCP
                # matches the scene on first boot.
                saved_variant = ng_app.storage.general.get(
                    f"tool_variant_{saved_tool}", ""
                )
                await client.select_tool(saved_tool, variant_key=saved_variant)
                logger.debug(
                    "startup: set tool to %s (variant=%r)", saved_tool, saved_variant
                )
        except Exception as e:
            logger.warning("startup: select_tool failed: %s", e)

        # Adopt the controller's applied collision world (installation shapes
        # exist even with no program loaded — the GUI must ask, not push).
        scene_handle = waldoctl.commander.scene
        if scene_handle is not None:
            await scene_handle.refresh_from_backend()

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
            playback.sync_mode()
            # Spawn the MCP server if the user has opted in (no-op otherwise).
            await start_mcp_server()
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
        # Stop the MCP server (no-op if it was never started).
        await stop_mcp_server()

        # Timeout avoids hanging forever if startup never completes.
        try:
            await asyncio.wait_for(_startup_complete.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Shutdown: startup did not complete within 10s, proceeding anyway"
            )

        try:
            if is_any_program_running() and script_exec.script_handle:
                logger.debug("Stopping running script process during shutdown...")
                from waldo_commander.services.script_runner import stop_script

                await stop_script(script_exec.script_handle, timeout=2.0)
                script_exec.script_handle = None
                running_tab_id = script_exec.launching_tab_id
                running_tab = (
                    waldoctl.commander.programs.get(running_tab_id)
                    if running_tab_id
                    else None
                )
                if running_tab is not None:
                    running_tab.execution.is_running = False
                script_exec.cleanup_stepping()
        except Exception as e:
            logger.warning("Error stopping script during shutdown: %s", e)

        # Cancel all timers first.
        if ui_state._joint_jog_timer is not None:
            ui_state._joint_jog_timer.cancel()
        if ui_state._cart_jog_timer is not None:
            ui_state._cart_jog_timer.cancel()
        if _page_state is not None and _page_state.ping_timer is not None:
            _page_state.ping_timer.cancel()

        if control_panel is not None:
            control_panel.cleanup()
        if ui_state.gripper_page is not None:
            ui_state.gripper_page.cleanup()
        if editor_panel is not None:
            editor_panel.cleanup()

        # Stop plugin panels before the controller goes away so they can
        # cancel any in-flight requests against the live client.
        await _stop_plugin_panels()

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

        # Drop the locator last — any consumer still alive after this point
        # will get the clear "commander not initialised" RuntimeError instead
        # of dotting into a torn-down Commander.
        waldoctl._clear_commander()


_register_handlers()


def _cleanup_script_processes_sync() -> None:
    """Synchronously kill any running script subprocess.

    This is called from atexit and signal handlers as a last-resort cleanup.
    """
    try:
        if script_exec.script_handle:
            proc = script_exec.script_handle.get("proc")
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


atexit.register(_cleanup_script_processes_sync)


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
    if is_active:
        # The active tab is the default controller — claim the lease when it's
        # free or held by a prior browser tab (but not from a live MCP holder).
        browser_claim_if_unheld(this_client.id)

    def _on_disconnect():
        # Synchronous handler so the active-slot release happens *inline*
        # during NiceGUI's handle_disconnect() — async handlers are scheduled
        # as background tasks and would let the new client see a stale slot
        # on refresh.
        global _page_state
        if ui_state.active_client_id == this_client.id:
            control_lease.release(BROWSER, this_client.id)
            ui_state.active_client_id = None
        # Editor + page-state teardown must only run for the active client.
        # A shadow tab disconnecting must not touch the active tab's
        # listeners, timers, or script-watch tasks — _on_disconnect is
        # registered before the shadow `return`, so shadow tabs reach here.
        if _page_state is not None and _page_state.page_client is this_client:
            _page_state = None
            if editor_panel is not None:
                editor_panel.cleanup()

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

    apply_theme("dark")
    ui.query(".nicegui-content").classes("p-0")
    inject_layout_css()

    build_page_content()

    # Plugin panels: kick off their long-running tasks now that UI is ready.
    asyncio.create_task(_start_plugin_panels())

    # Reflect startup-determined mode in UI; update connectivity only upward
    # (don't downgrade connected→disconnected from a transient ping failure;
    # the 1 Hz check_ping handles that with retries).
    try:
        result = await client.ping()
        hw_ok = result.hardware_connected if result else False
        if hw_ok:
            waldoctl.commander.status.connected = True
    except Exception as e:
        logger.warning("Connectivity check failed: %s", e)

    # Wire jog timers to ui_state so the control panel can access them.
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


def _maybe_clear_sim_pose_override() -> None:
    """Release the scrub pose-override once the teleport has propagated.

    While the user scrubs, the status loop holds ``sim_pose_override`` so the
    live pose doesn't fight the scrubbed pose. Once playback is no longer active
    and ≥100ms has passed since the last teleport, the override is released so
    the loop resumes showing the real robot pose.
    """
    # Cheap flag first: in the steady state (nobody scrubbing) this short-
    # circuits before resolving the active program every status tick.
    if not playback_coordination.sim_pose_override:
        return
    active_pb = waldoctl.commander.programs.active
    is_active = active_pb is not None and active_pb.dry_run.playback.is_active
    if (
        not is_active
        and playback_coordination.last_teleport_ts > 0
        and (time.monotonic() - playback_coordination.last_teleport_ts) > 0.1
    ):
        playback_coordination.sim_pose_override = False
        playback_coordination.last_teleport_ts = 0.0


#: One-shot config-skew message, surfaced as a notification by the status
#: consumer once a page context exists.
_config_skew_msg: str | None = None


async def _check_config_skew() -> None:
    """Compare the controller's config fingerprint against the packaged
    mirror this UI plans previews with.

    A tuned deployment (edited limits, motion feel, gripper keys) is
    legitimate — but the UI's offline previews then run different numbers
    than the arm, so the mismatch is worth one loud warning. Backends
    without a CONFIG_INFO query are skipped silently.
    """
    global _config_skew_msg
    info_fn = getattr(client, "config_info", None)
    if info_fn is None:
        return
    try:
        info = await info_fn()
    except NotImplementedError:
        return
    except Exception as e:
        logger.debug("config skew check failed: %s", e)
        return
    if not info or not info.get("fingerprint"):
        return
    try:
        import hashlib

        # par6 is an optional backend; resolved dynamically so type checking
        # passes in environments that ship a different backend.
        import importlib

        par6_config = importlib.import_module("par6.config")

        src = par6_config.data_root() / "config"
        digest = hashlib.sha256()
        for f in [src / "PAR6.toml"] + sorted((src / "grippers").glob("*.toml")):
            digest.update(f.name.encode())
            digest.update(b"\n")
            digest.update(f.read_bytes())
        expected = digest.hexdigest()
    except Exception as e:
        logger.debug("config skew check skipped: %s", e)
        return
    if info["fingerprint"] != expected:
        msg = (
            "Controller config differs from the packaged mirror "
            f"({info.get('path', '?')}) — offline previews may not match "
            "the arm's limits and feel."
        )
        logger.warning("config skew: %s", msg)
        # A simulator daemon runs a re-ticked config by design (dev, CI);
        # the sticky notification is for a live deployment.
        try:
            sim = await client.is_simulator()
        except Exception:
            sim = True
        if not sim:
            _config_skew_msg = msg


# Re-arm window for the cycle-start input: a rising edge within this many
# seconds of the last fire is ignored (switch-bounce / double-tap protection).
CYCLE_START_DEBOUNCE_S = 1.0
# Once at home, the home output only drops after drifting this far beyond the
# tolerance, so a joint hovering at the threshold can't chatter the output.
HOME_OUTPUT_HYSTERESIS_DEG = 0.5


async def _run_cycle_start(page_client: Client) -> None:
    """Start the active program off a hardware cycle-start pulse.

    Enters the page's client context — ``script_exec.start()`` reads the
    editor widgets and notifies on that page. The toast tells an operator
    who forgot the setting is on why the robot just started.
    """
    if not _client_alive(page_client):
        return
    try:
        with page_client:
            ui.notify(
                "Cycle start: Input 1 triggered the program",
                type="info",
                position="top",
            )
            await script_exec.start()
    except Exception as e:
        logger.warning("Cycle-start program launch failed: %s", e)


async def _write_home_output(value: int) -> None:
    """Write Output 2 and flip the tracker only on success — a failed write
    leaves the transition pending so the next tick retries."""
    a = automation_state
    try:
        await waldoctl.commander.client.write_io(1, value)
        a._home_out_on = bool(value)
    except Exception as e:
        logger.warning("Home-output write_io(1, %s) failed: %s", value, e)
    finally:
        a._home_write_inflight = False


def _automation_tick(page_client: Client | None) -> None:
    """Opt-in I/O automation watchers, one call per status tick.

    They read the published ``commander.status`` surface (one tick behind
    the wire — irrelevant at status rate) so the watchers see exactly what
    every other public consumer sees. Both watchers are edge-detecting:
    they act only on transitions, never per tick.
    """
    _cycle_start_tick(page_client)
    _home_output_tick()


def _cycle_start_tick(page_client: Client | None) -> None:
    """Cycle start: rising edge on Input 1 runs the active program.

    The edge tracker runs even while disabled so enabling with the input
    held high never fires until a fresh low→high transition. ``io`` is only
    published while a page is connected and this watcher reads one tick
    behind the publish, so the tracker only trusts values seen after a full
    live tick — a press latched while headless (or held high at launch)
    never fires.
    """
    a = automation_state
    st = waldoctl.commander.status
    io = st.io

    cur = io.inputs[0] if io.inputs else 0
    if page_client is None:
        a._cycle_prev_input = -1
        a._cycle_io_fresh = False
        return
    fresh = a._cycle_io_fresh
    a._cycle_io_fresh = True
    if not fresh or a._cycle_prev_input < 0:
        # First live tick still reads pre-connect values; the next tick
        # seeds the tracker from the freshly published input, no firing.
        a._cycle_prev_input = cur if fresh else -1
        return
    prev = a._cycle_prev_input
    a._cycle_prev_input = cur
    if (
        a.cycle_start_enabled
        and prev == 0
        and cur == 1
        and time.monotonic() - a._cycle_last_fire >= CYCLE_START_DEBOUNCE_S
        and not is_any_program_running()
        and robot_state.homed
        and (st.connected or st.simulator_active)
        and io.estop == 1
        and not st.editing_mode
        and waldoctl.commander.programs.active is not None
    ):
        a._cycle_last_fire = time.monotonic()
        background_tasks.create(_run_cycle_start(page_client), name="cycle-start")


def _home_output_tick() -> None:
    """Home output: Output 2 mirrors "all joints within tolerance of the
    home pose".

    ``status.homed`` is only the referencing flag, so the distance is
    computed here. Editing/scrub windows hold the last state — the published
    angles then show a preview pose, not the robot. Disabling with the
    output still on issues one final clearing write.
    """
    a = automation_state
    st = waldoctl.commander.status

    if not (a.home_output_enabled or a._home_out_on):
        return
    desired = False
    if a.home_output_enabled:
        if st.editing_mode or playback_coordination.sim_pose_override:
            return
        robot = ui_state.active_robot
        if robot.digital_outputs < 2:
            return
        angles = st.joints.angles.deg
        home = robot.joints.home.deg
        n = len(home)
        if len(angles) != n:
            return
        dist = 0.0
        for i in range(n):
            d = angles[i] - home[i]
            if d < 0.0:
                d = -d
            if d > dist:
                dist = d
        band = a.home_tolerance_deg
        if a._home_out_on:
            band += HOME_OUTPUT_HYSTERESIS_DEG
        desired = dist <= band
    if desired != a._home_out_on and not a._home_write_inflight:
        a._home_write_inflight = True
        background_tasks.create(
            _write_home_output(1 if desired else 0), name="home-output-write"
        )


async def _status_consumer() -> None:
    """Consume multicast status and populate ``commander.status``."""
    # Shadows of the last-applied jog-enable wire arrays, kept local so each
    # app start (and each test) begins fresh. The per-direction lists are only
    # rebuilt when a shadow mismatches (zero-alloc compare via arrays_equal_n);
    # the copy happens on change, not every tick.
    joint_en_shadow: np.ndarray | None = None
    cart_en_shadow: dict[str, np.ndarray] = {}
    scene_epoch_shadow: int | None = None
    torques_shadow: np.ndarray | None = None
    torques_ext_shadow: np.ndarray | None = None
    homing_shadow: tuple | None = None
    try:
        # Wait for server to be responsive before subscribing to multicast
        await client.wait_ready(timeout=15.0)
        async for status in client.stream_status_shared():
            try:
                now = time.perf_counter()
                _ui_metrics.tick(now)

                # Rate-limited debug log every 3s.
                if _ui_metrics.should_log(now, 3.0):
                    for p in global_phase_timer.phases.values():
                        p.compute_stats()

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
                    st = waldoctl.commander.status
                    # Copy status data (in-place fills to avoid allocations)
                    if (
                        not st.editing_mode
                        and not playback_coordination.sim_pose_override
                    ):
                        st.joints.angles.set_deg(status.angles)
                    robot_state.pose[:] = status.pose
                    robot_state.io[:] = status.io
                    if not playback_coordination.sim_pose_override:
                        robot_state.tool_status = status.tool_status

                    # Speeds arrive as rad/s from backend — convert to deg/s for display
                    np.rad2deg(status.speeds, out=robot_state.speeds)
                    robot_state.homed = status.homed
                    pose = st.pose
                    pose.tcp_speed = 0.3 * status.tcp_speed + 0.7 * pose.tcp_speed

                    # Mark backend ready on first valid STATUS
                    readiness_state.mark_backend_done()

                    # Movement enablement: split the interleaved joint_en /
                    # cart_en wire arrays into the per-direction lists exposed on
                    # ``commander.status.joints`` / ``...pose.cart_jog``. This
                    # runs every tick, so dirty-check each wire array against a
                    # snapshot via the zero-alloc ``arrays_equal_n`` and rebuild
                    # the lists only on change (the snapshot copy is taken then,
                    # not per tick).
                    j_en = status.joint_en
                    n_dof = len(j_en) // 2
                    joints = st.joints
                    if (
                        joint_en_shadow is None
                        or len(joints.can_jog_pos) != n_dof
                        or not arrays_equal_n(j_en, joint_en_shadow)
                    ):
                        joints.can_jog_pos = [bool(j_en[2 * j]) for j in range(n_dof)]
                        joints.can_jog_neg = [
                            bool(j_en[2 * j + 1]) for j in range(n_dof)
                        ]
                        joint_en_shadow = j_en.copy()
                    cart_jog = st.pose.cart_jog
                    for frame, arr in status.cart_en.items():
                        frame_av = cart_jog.by_frame.get(frame)
                        if frame_av is None:
                            frame_av = FrameJogAvailability()
                            cart_jog.by_frame[frame] = frame_av
                        n_axes = len(arr) // 2
                        shadow = cart_en_shadow.get(frame)
                        if (
                            shadow is None
                            or len(frame_av.can_jog_pos) != n_axes
                            or not arrays_equal_n(arr, shadow)
                        ):
                            frame_av.can_jog_pos = [
                                bool(arr[2 * i]) for i in range(n_axes)
                            ]
                            frame_av.can_jog_neg = [
                                bool(arr[2 * i + 1]) for i in range(n_axes)
                            ]
                            cart_en_shadow[frame] = arr.copy()

                    coll = st.collision
                    # Content compare (both hold (str, str) tuples) — a length
                    # check misses same-length pair swaps. Copy: the decoder
                    # refills status.collision_pairs in place.
                    if (
                        coll.active != status.collision_active
                        or coll.pairs != status.collision_pairs
                    ):
                        coll.active = status.collision_active
                        coll.pairs = list(status.collision_pairs)

                    # v0.8.0 status surface (torques, controller state,
                    # warnings, link health, homing, clearance). Guarded
                    # per-field: a pre-v0.8.0 backend's buffer simply lacks
                    # them and the sub-objects keep their defaults.
                    tau = getattr(status, "tau", None)
                    if tau is not None and (
                        torques_shadow is None
                        or not arrays_equal_n(tau, torques_shadow)
                    ):
                        joints.torques = [float(v) for v in tau]
                        torques_shadow = tau.copy()
                    tau_ext = getattr(status, "tau_ext", None)
                    if tau_ext is not None and (
                        torques_ext_shadow is None
                        or not arrays_equal_n(tau_ext, torques_ext_shadow)
                    ):
                        joints.torques_ext = [float(v) for v in tau_ext]
                        torques_ext_shadow = tau_ext.copy()

                    # Controller state chip: mode name is the backend enum's
                    # name (vendor-neutral for display). Skipped, never
                    # reset, when the backend's buffer lacks the field.
                    ctrl = st.controller
                    mode = getattr(status, "mode", None)
                    if mode is not None:
                        if ctrl.mode != mode.name:
                            ctrl.mode = mode.name
                        enabled = bool(getattr(status, "enabled", False))
                        if ctrl.enabled != enabled:
                            ctrl.enabled = enabled
                        freedrive = bool(getattr(status, "freedrive", False))
                        if ctrl.freedrive != freedrive:
                            ctrl.freedrive = freedrive

                    # Warning-class conditions (self-clearing): content
                    # compare, copy on change — the decoder refills the
                    # buffer list in place.
                    entries = getattr(status, "warnings", None)
                    if entries is not None and st.warnings.entries != entries:
                        # New conditions go to the durable log; the banner
                        # tracks only the standing (self-clearing) set.
                        prev = {tuple(e) for e in st.warnings.entries}
                        for e in entries:
                            if tuple(e) not in prev:
                                robot_events.add(
                                    str(e[4]) if len(e) > 4 else "warning",
                                    str(e[2]) if len(e) > 2 else str(e),
                                    str(e[3]) if len(e) > 3 else "",
                                )
                        st.warnings.entries = list(entries)

                    link = getattr(status, "link_health", None)
                    if link:
                        lh = st.link_health
                        link_state = link["state"].name
                        if lh.state != link_state:
                            lh.state = link_state
                        if lh.restarts != link.get("restarts", 0):
                            lh.restarts = int(link.get("restarts", 0))
                        if lh.tx_errors != link.get("tx_errors", 0):
                            lh.tx_errors = int(link.get("tx_errors", 0))
                        if lh.rx_frames != link.get("rx_frames", 0):
                            lh.rx_frames = int(link.get("rx_frames", 0))

                    # Per-joint homing progress: (state, phase) enum-name
                    # pairs, rebuilt only when the raw tuple moves.
                    homing = getattr(status, "homing", None)
                    if homing is not None:
                        homing_key = (
                            bool(homing.get("active", False)),
                            int(homing.get("sequence_step", 0)),
                            tuple(homing.get("joints", ())),
                        )
                        if homing_key != homing_shadow:
                            homing_shadow = homing_key
                            hm = st.homing
                            hm.active = homing_key[0]
                            hm.sequence_step = homing_key[1]
                            hm.joints = [
                                (state.name, phase.name)
                                for state, phase in homing_key[2]
                            ]

                    if hasattr(status, "min_clearance_m"):
                        clearance = status.min_clearance_m
                        if st.min_clearance_m != clearance:
                            st.min_clearance_m = clearance

                    # Collision-world epoch moved (first frame after connect,
                    # a program's set_shapes, another client, a restart) —
                    # adopt the controller's world via readback.
                    if status.scene_epoch != scene_epoch_shadow:
                        scene_epoch_shadow = status.scene_epoch
                        scene_handle = waldoctl.commander.scene
                        if scene_handle is not None:
                            asyncio.create_task(scene_handle.refresh_from_backend())

                    action = st.action
                    action.current_name = status.action_current
                    action.state = status.action_state
                    # action_params is per-command metadata used by the
                    # action_log_service dedup; not on the public Action
                    # surface, kept in the status update tuple below.
                    robot_state.executing_index = status.executing_index
                    robot_state.completed_index = status.completed_index
                    st.last_update = time.time()

                    # Auto-clear scrub override after teleport has had time to propagate
                    _maybe_clear_sim_pose_override()

                    ps = _page_state
                    pc = ps.page_client if ps is not None else None
                    if pc is not None and not _client_alive(pc):
                        pc = None

                    # Runs before update_ui_from_status pushes this tick's IO,
                    # so the watchers read the values published last tick.
                    _automation_tick(pc)

                    if pc is not None:
                        with pc:
                            global _config_skew_msg
                            if _config_skew_msg is not None:
                                ui.notify(_config_skew_msg, color="warning", timeout=0)
                                _config_skew_msg = None
                            update_ui_from_status()

                            readout_panel.update_conn_io()
                            action_log_service.process_status(
                                action.current_name,
                                status.action_params,
                                action.state,
                                robot_state.executing_index,
                                robot_state.completed_index,
                            )
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

    # Defaults come from config (lazy: reads env vars at access time).
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

    # The human's AI control mode (Inspect/Auto-edits/Autopilot) survives
    # restarts like the other persisted settings.
    restore_control_mode()

    # Initialize robot, client, and component instances. The persisted GUI
    # backend selection is honored below an explicit --robot / WALDO_ROBOT
    # override, and only when that backend is actually installed.
    robot = get_robot(
        name=args.robot,
        preferred=ng_app.storage.general.get("plugins/backend"),
    )
    ui_state.robot = robot
    # Per-frame cart_jog buffers are seeded below once the Commander is
    # registered. The status loop fills them on each tick.
    # Resize IO buffer to match robot's pin count. The derived
    # ``commander.status.io.inputs / outputs`` lists are populated below
    # once the Commander is registered (default is []; status loop fills
    # them on each tick).
    io_size = robot.digital_inputs + robot.digital_outputs + 1  # +1 for estop
    robot_state.io = np.zeros(io_size, dtype=np.int32)
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
    ui_state.readout_panel = readout_panel

    # Assemble the public Commander locator. Populated here (post panel
    # construction) so consumers can reach robot / client / live status /
    # programs / settings through `waldoctl.commander.*` from anywhere.
    # Sub-objects are constructed with defaults; the status loop populates
    # `commander.status` on each tick, the editor populates `commander.programs`
    # as tabs open, and the settings panel binds to `commander.settings`.
    commander = Commander(
        robot=robot,
        client=client,
        status=RobotStatus(),
        programs=EditorPrograms(),
        settings=Settings(),
        scene=WcSceneHandle(),
    )
    waldoctl._set_commander(commander)

    # Seed the IO buffers so consumers (readout chips, e-stop monitor) see
    # the right list lengths before the first STATUS broadcast arrives.
    commander.status.io.inputs = [0] * robot.digital_inputs
    commander.status.io.outputs = [0] * robot.digital_outputs

    # Seed per-frame cart_jog availability so the cartesian-button sync code
    # has a frame_av to read on the first tick (before STATUS arrives).
    for frame in robot.cartesian_frames:
        commander.status.pose.cart_jog.by_frame[frame] = FrameJogAvailability()

    # Restore plugin settings from prior session.
    commander.settings.plugins.backend = ng_app.storage.general.get("plugins/backend")
    commander.settings.plugins.disabled_panels = list(
        ng_app.storage.general.get("plugins/disabled_panels", [])
    )

    # Restore MCP server settings from prior session. `enabled` defaults to
    # False so the server stays off until the user explicitly opts in.
    commander.settings.mcp.enabled = bool(
        ng_app.storage.general.get("mcp/enabled", False)
    )
    commander.settings.mcp.host = str(
        ng_app.storage.general.get("mcp/host", commander.settings.mcp.host)
    )
    commander.settings.mcp.port = int(
        ng_app.storage.general.get("mcp/port", commander.settings.mcp.port)
    )

    # Restore I/O automation settings from prior session (both default off).
    automation_state.cycle_start_enabled = bool(
        ng_app.storage.general.get("automation/cycle_start", False)
    )
    automation_state.home_output_enabled = bool(
        ng_app.storage.general.get("automation/home_output", False)
    )
    automation_state.home_tolerance_deg = float(
        ng_app.storage.general.get(
            "automation/home_tolerance_deg", automation_state.home_tolerance_deg
        )
    )

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
