import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

import numpy as np
from nicegui import binding
from waldoctl import (
    AngleArray,
    ChangeNotifierMixin,
    Panel,
    PathSegment,
    ProgramTarget,
    ShapeChange,
    ToolAction,
    ToolSelection,
    ToolStatus,
    ToolTimeSeries,
)

# Re-exports for legacy import sites: these dataclasses live in waldoctl now,
# but several WC modules still import them here while they migrate at their own pace.
__all__ = [
    "PathSegment",
    "ProgramTarget",
    "ShapeChange",
    "ToolAction",
    "ToolSelection",
]

from waldo_commander.common.loop_timer import PhaseTimer


logger = logging.getLogger(__name__)

# Type-checking shim so Pylance sees bindable_dataclass as a dataclass transform.
if TYPE_CHECKING:
    from typing import dataclass_transform

    from waldo_commander.services.urdf_scene import UrdfScene
    from waldoctl import Robot

    @dataclass_transform(field_specifiers=(field,))
    def bindable_dataclass(cls=None, /, **kwargs):
        return cls
else:
    bindable_dataclass = binding.bindable_dataclass


# ProgramTarget, PathSegment, ToolAction, ToolSelection are owned by waldoctl
# (re-exported above) so simulation_state's field types unify with
# commander.programs.active.dry_run.*.


@bindable_dataclass
class SimulationState(ChangeNotifierMixin):
    # Simulation results and playback scalars live on commander.programs.active
    # .dry_run.*. What stays here is the WC-side notification channels consumers
    # subscribe to globally — one change-listener registration covers every tab
    # switch and dry-run update.
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )
    _step_listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)


# ``RecordingState`` migrated to ``commander.programs.active.recording``;
# session-wide check is ``services.programs.is_any_program_recording()``.


# Shared state singleton for cross-module access. No fields are bindable
# (bindable_fields=[]) — the members are numpy arrays / objects read
# imperatively, and the migrated scalar fields now live on commander.status.*.
@bindable_dataclass(bindable_fields=[])
class RobotState(ChangeNotifierMixin):
    # orientation stays here as a rad-access companion for FK/IK consumers that
    # don't want to deg2rad on every read; angles moved to commander.status.joints.
    orientation: AngleArray = field(
        default_factory=lambda: AngleArray(size=3)
    )  # rx/ry/rz (deg/rad)
    pose: np.ndarray = field(
        default_factory=lambda: np.zeros(16, dtype=np.float64)
    )  # homogeneous transform flattened
    io: np.ndarray = field(
        default_factory=lambda: np.zeros(5, dtype=np.int32)
    )  # [inputs..., outputs..., estop] — resized at startup
    tool_status: ToolStatus = field(default_factory=ToolStatus)
    # tool_time_series stays here as a WC-internal rolling buffer backing the
    # gripper chart; the rest of the tool/pose/io scalars live on commander.status.*.
    tool_time_series: ToolTimeSeries = field(default_factory=ToolTimeSeries)
    speeds: np.ndarray = field(
        default_factory=lambda: np.zeros(6, dtype=np.float64)
    )  # deg/s
    # All joints homed, from the status stream. Seeds dry-run previews so an
    # unhomed robot's preview mirrors the controller's planned-motion gate.
    homed: bool = True
    executing_index: int = -1
    completed_index: int = -1
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )

    def reset(self) -> None:
        """Reset to defaults. Arrays are zeroed in-place."""
        self.orientation.set_deg(np.zeros(3, dtype=np.float64))
        self.pose[:] = 0.0
        self.io[:] = 0
        self.tool_status = ToolStatus()
        self.tool_time_series.clear()
        self.speeds[:] = 0.0
        self.homed = True
        self.executing_index = -1
        self.completed_index = -1


@dataclass
class ControllerState:
    running: bool = False

    def reset(self) -> None:
        self.running = False


@dataclass
class AutomationState:
    """I/O automation preferences plus the status-loop watcher trackers.

    Settings hydrate from ``app.storage.general`` at startup and dual-write
    from the settings rows; the underscore fields are edge-detection state
    owned by the watchers in ``main._automation_tick``.
    """

    cycle_start_enabled: bool = False
    home_output_enabled: bool = False
    home_tolerance_deg: float = 2.0

    # -1 = no trusted input sample yet, so a held-high input never fires on
    # start. Trust requires a page-published value: io is only refreshed while
    # a page is connected, and the watcher reads one tick behind the publish.
    _cycle_prev_input: int = -1
    _cycle_io_fresh: bool = False
    _cycle_last_fire: float = float("-inf")
    _home_out_on: bool = False
    _home_write_inflight: bool = False

    def reset(self) -> None:
        self.cycle_start_enabled = False
        self.home_output_enabled = False
        self.home_tolerance_deg = 2.0
        self._cycle_prev_input = -1
        self._cycle_io_fresh = False
        self._cycle_last_fire = float("-inf")
        self._home_out_on = False
        self._home_write_inflight = False


@dataclass
class PlaybackCoordination:
    """WC-private coordination between dry-run playback and the status loop.

    Not part of the public ``waldoctl.commander`` surface — these flags are
    internal to how WC suppresses status-loop URDF writes while scrubbing or
    playing back a simulated trajectory, so the live robot pose doesn't fight
    the scene with the scrubbed pose.
    """

    sim_pose_override: bool = False
    """True while scrubbing/playing — suppresses status-loop URDF updates."""
    last_teleport_ts: float = 0.0
    """Monotonic time of last teleport send; used by status loop to delay handback."""

    def reset(self) -> None:
        self.sim_pose_override = False
        self.last_teleport_ts = 0.0


class _RequiredField:
    """Descriptor for fields that must be set post-init (asserts on access)."""

    def __set_name__(self, _owner: type, name: str) -> None:
        self._attr = f"_{name}"
        self._name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        val = getattr(obj, self._attr, None)
        if val is None:
            raise RuntimeError(f"{self._name} not initialized")
        return val

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(obj, self._attr, value)


@bindable_dataclass
class UiState:
    # Set once at startup; required thereafter (see active_robot).
    robot: "Robot | None" = None

    urdf_scene: "UrdfScene | None" = None
    urdf_joint_names: list[str] | None = None

    # Tab currently allowed to control the robot. None during the brief
    # window between a takeover click and the reloaded client reconnecting.
    # See main.index_page / main.check_ping for the lifecycle.
    active_client_id: str | None = None
    urdf_index_mapping: list[int] = field(default_factory=lambda: list(range(6)))
    current_tool_stls: list[Any] = field(default_factory=list)

    # User preferences (jog speed/accel/step, gripper speed/current/sync, gizmo visibility)
    # now live on ``commander.settings.{jog, gripper, view}`` (waldoctl).

    # Camera device: -1 = disabled, int = device index, str = device name
    camera_device: int | str = -1

    # Page-scoped UI elements (set post-build)
    response_log: Any = None
    io_page: Any = None
    gripper_page: Any = None
    _gripper_tab: Any = None
    _build_gripper_content: Any = None

    # Private storage for timers and panels (set post-build)
    _joint_jog_timer: Any = None
    _cart_jog_timer: Any = None
    _editor_panel: Any = None
    _control_panel: Any = None
    _readout_panel: Any = None

    # Program panel visibility (tracked for tab flash when panel closed)
    program_panel_visible: bool = False

    # Editor widget refs live here, not on the public ProgramTabs surface, since
    # NiceGUI element handles don't belong there. EditorPanel writes them on every
    # tab switch; sub-controllers read them without back-references into EditorPanel.
    active_textarea: Any = None  # ui.codemirror | None at runtime
    active_filename_input: Any = None  # ui.input | None at runtime
    textareas_by_tab: dict[str, Any] = field(default_factory=dict)

    # Plugin panels discovered via the `waldoctl.panels` entry-point group.
    # Populated on first page build, cached for the process; ordered by (slot, order, id).
    plugin_panels: list[Panel] = field(default_factory=list)
    # Ids of panels whose Panel.start() has run. Tracked per panel (not one flag) so a
    # panel discovered after an empty first build still starts, and the start guard
    # can't be straddled by a reload race.
    _started_panel_ids: set[str] = field(default_factory=set)

    # Post-init required fields (assert on access, set via assignment)
    editor_panel = _RequiredField()
    control_panel = _RequiredField()
    readout_panel = _RequiredField()
    joint_jog_timer = _RequiredField()
    cart_jog_timer = _RequiredField()

    @property
    def active_robot(self) -> "Robot":
        """Get robot, asserting it's set."""
        assert self.robot is not None, "robot not set"
        return self.robot

    def reset(self) -> None:
        """Reset UI state. Does not reset robot (set once at startup)."""
        self.urdf_scene = None
        self.active_client_id = None
        self.plugin_panels = []
        self._started_panel_ids = set()


# Editor tabs now live on commander.programs (waldoctl), with a WC-side concrete
# subclass at waldo_commander.services.programs.EditorPrograms. Per-tab dry-run/log
# data is on Program.dry_run / Program.log; active widget refs on UiState.


@dataclass
class ReadinessState:
    """Tracks application initialization readiness for tests.

    This provides precise synchronization points that tests can await
    instead of using blind sleep() calls.

    Events:
        app_ready: Set when app is fully ready (startup done + backend streaming + page init)
        urdf_scene_ready: Set when URDF 3D scene is fully initialized
    """

    app_ready: asyncio.Event = field(default_factory=asyncio.Event)
    urdf_scene_ready: asyncio.Event = field(default_factory=asyncio.Event)

    app_ready_ts: float = 0.0
    urdf_scene_ready_ts: float = 0.0

    # Internal tracking flags for app_ready
    _startup_done: bool = False
    _backend_done: bool = False
    _page_done: bool = False

    def reset(self) -> None:
        """Reset all events for test isolation."""
        self.app_ready = asyncio.Event()
        self.urdf_scene_ready = asyncio.Event()
        self.app_ready_ts = 0.0
        self.urdf_scene_ready_ts = 0.0
        self._startup_done = False
        self._backend_done = False
        self._page_done = False

    def _check_app_ready(self) -> None:
        """Check if all conditions are met and signal app_ready if so."""
        if self._startup_done and self._backend_done and self._page_done:
            if not self.app_ready.is_set():
                self.app_ready_ts = time.time()
                self.app_ready.set()
                logger.debug("Readiness: app_ready signaled")

    def mark_startup_done(self) -> None:
        """Mark startup as complete (call from _on_startup finally block)."""
        if not self._startup_done:
            self._startup_done = True
            logger.debug("Readiness: startup done")
            self._check_app_ready()

    def mark_backend_done(self) -> None:
        """Mark backend as ready (call from _status_consumer on first valid status)."""
        if not self._backend_done:
            self._backend_done = True
            logger.debug("Readiness: backend done")
            self._check_app_ready()

    def mark_page_done(self) -> None:
        """Mark page as ready (call from index_page after setup)."""
        if not self._page_done:
            self._page_done = True
            logger.debug("Readiness: page done")
            self._check_app_ready()

    def signal_urdf_scene_ready(self) -> None:
        """Signal that URDF scene is ready (call from initialize_urdf_scene)."""
        if not self.urdf_scene_ready.is_set():
            self.urdf_scene_ready_ts = time.time()
            self.urdf_scene_ready.set()
            logger.debug("Readiness: urdf_scene_ready signaled")


# Action log now lives on waldo_commander.services.action_log; its data fields
# (ActionStatus / ActionLogEntry / history) on commander.status.action.


# Module-level singletons
robot_state: RobotState = RobotState()
controller_state: ControllerState = ControllerState()
ui_state: UiState = UiState()
simulation_state: SimulationState = SimulationState()
readiness_state: ReadinessState = ReadinessState()
playback_coordination: PlaybackCoordination = PlaybackCoordination()
automation_state: AutomationState = AutomationState()


def reset_all_state() -> None:
    """Reset all state singletons to defaults. For test isolation."""
    robot_state.reset()
    controller_state.reset()
    ui_state.reset()
    playback_coordination.reset()
    readiness_state.reset()
    automation_state.reset()
    # Editor tabs / action log live on the commander locator now; reset via
    # their services so each service's own bookkeeping (dedup cursors) is
    # cleared alongside the public surface it writes to.
    from waldo_commander.services.action_log import action_log_service
    from waldo_commander.services.control_lease import control_lease
    from waldo_commander.services.edit_decisions import clear as clear_edit_decisions

    action_log_service.clear()
    control_lease.reset()
    clear_edit_decisions()
    import waldoctl

    try:
        programs = waldoctl.commander.programs
    except RuntimeError:
        pass
    else:
        programs.items = []
        programs.active_id = None
        programs.notify_changed()
    ui_state.active_textarea = None
    ui_state.active_filename_input = None
    ui_state.textareas_by_tab.clear()


# Global timing instrumentation - import and use from any module
# Usage: with global_phase_timer.phase("my_operation"): ...
global_phase_timer = PhaseTimer(
    [
        "status",  # Receiving/parsing status + updating panels
        "scene",  # 3D scene updates (angles, TCP ball, envelope)
        "jog",  # Joint and cartesian jog API calls
    ]
)
