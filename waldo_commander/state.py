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
    PathSegment,
    ProgramTarget,
    ToolAction,
    ToolSelection,
    ToolStatus,
    ToolTimeSeries,
)

# Re-exports for legacy import sites — these dataclasses live in waldoctl now
# (one canonical type per shape) but several WC modules still import them
# from ``waldo_commander.state``. Keep the re-export so the type checker
# resolves the names and downstream code can migrate to ``waldoctl`` at
# its own pace.
__all__ = [
    "PathSegment",
    "ProgramTarget",
    "ToolAction",
    "ToolSelection",
]

from waldo_commander.common.loop_timer import PhaseTimer


logger = logging.getLogger(__name__)

# Type-checking shim for bindable_dataclass to satisfy Pylance without changing runtime
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
# (re-exported above from ``waldoctl``). The WC-local duplicates have been
# removed so ``simulation_state``'s field types unify with
# ``commander.programs.active.dry_run.*`` and the type checker stops flagging
# cross-module list assignments.


@bindable_dataclass
class SimulationState(ChangeNotifierMixin):
    # Per-program simulation results live on ``commander.programs.active
    # .dry_run.*`` (``path_segments`` / ``targets`` / ``tool_actions`` /
    # ``tool_selections`` / ``total_steps`` / ``total_duration``).
    # Playback timeline scalars (``current_step`` / ``playback_time`` /
    # ``playback_speed`` / ``is_playing`` / ``is_active`` /
    # ``active_cursor_line``) live on ``dry_run.playback.*``.
    #
    # What stays here: the WC-side notification channels that consumers
    # subscribe to globally (a single change-listener registration covers
    # every tab switch + every dry-run update). View prefs like
    # ``paths_visible`` now live on ``commander.settings.view``.
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )
    _step_listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)


# ``RecordingState`` migrated to ``commander.programs.active.recording``;
# session-wide check is ``services.programs.is_any_program_recording()``.


# Extended shared state singletons for cross-module access
# Only scalar fields are bindable - numpy arrays are excluded to avoid comparison issues
@bindable_dataclass(bindable_fields=[])
class RobotState(ChangeNotifierMixin):
    # ``angles`` (joint angles in deg/rad) moved to
    # ``commander.status.joints.angles`` — same ``AngleArray`` interface.
    # ``orientation`` stays here as a rad-access companion for FK/IK
    # consumers that don't want to deg2rad on every read.
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
    # Movement enablement arrays live on commander.status.joints.can_jog_pos
    # / can_jog_neg (6 bools each) and
    # commander.status.pose.cart_jog.by_frame[<frame>].can_jog_{pos,neg}.
    # Connection / simulator / editing-mode / last-update timestamp + TCP
    # linear speed + Cartesian pose scalars (x/y/z/rx/ry/rz) all live on
    # ``commander.status.{...}`` from waldoctl. IO inputs/outputs/estop
    # live on ``commander.status.io``. The numpy ``orientation`` array
    # stays here as a rad-access companion for FK / IK consumers.
    # All tool fields live on commander.status.tool — readers project
    # positions[0] / channels[0] inline when they need the single-DOF
    # scalar. tool_time_series stays here as a WC-internal rolling buffer
    # backing the gripper chart.
    tool_time_series: ToolTimeSeries = field(default_factory=ToolTimeSeries)
    speeds: np.ndarray = field(
        default_factory=lambda: np.zeros(6, dtype=np.float64)
    )  # deg/s
    # action_current / action_state live on commander.status.action.
    # ``action_params`` is per-command metadata for the dedup service; it
    # flows directly from the StatusBuffer into action_log_service without
    # a singleton mirror.
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
        self.executing_index = -1
        self.completed_index = -1


@dataclass
class ControllerState:
    running: bool = False

    def reset(self) -> None:
        self.running = False


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
    # Unified robot instance (set at startup, required)
    robot: "Robot | None" = None

    # URDF scene instance (holds UrdfSceneConfig)
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

    # Editor widget refs — moved off the legacy ``EditorTabsState`` because
    # NiceGUI element handles don't belong on the public ``ProgramTabs``
    # surface. EditorPanel writes these on every tab switch / build; the
    # sub-controllers (decorations, motion_recorder, script_execution) read
    # the active tab's widgets without back-references into EditorPanel.
    active_textarea: Any = None  # ui.codemirror | None at runtime
    active_filename_input: Any = None  # ui.input | None at runtime
    textareas_by_tab: dict[str, Any] = field(default_factory=dict)

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


# ===========================================================================
# Editor tabs — migrated to ``commander.programs`` (waldoctl) with a WC-side
# concrete subclass at ``waldo_commander.services.programs.EditorPrograms``.
# Per-tab dry-run/log data lives on ``Program.dry_run`` and ``Program.log``.
# Active widget refs migrated to ``UiState`` (active_textarea, etc.).
# ===========================================================================


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


# ===========================================================================
# Action log — moved to ``waldo_commander.services.action_log`` and the
# data fields (``ActionStatus`` / ``ActionLogEntry`` / ``history``) now live
# on ``commander.status.action`` from waldoctl. Nothing remains here.
# ===========================================================================


# Module-level singletons
robot_state: RobotState = RobotState()
controller_state: ControllerState = ControllerState()
ui_state: UiState = UiState()
simulation_state: SimulationState = SimulationState()
readiness_state: ReadinessState = ReadinessState()
playback_coordination: PlaybackCoordination = PlaybackCoordination()


def reset_all_state() -> None:
    """Reset all state singletons to defaults. For test isolation."""
    robot_state.reset()
    controller_state.reset()
    ui_state.reset()
    playback_coordination.reset()
    readiness_state.reset()
    # Editor tabs / action log live on the commander locator now; reset via
    # their services so each service's own bookkeeping (dedup cursors) is
    # cleared alongside the public surface it writes to.
    from waldo_commander.services.action_log import action_log_service

    action_log_service.clear()
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
