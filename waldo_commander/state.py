import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

import numpy as np
from nicegui import binding
from waldoctl import (
    ActionState,
    PathSegment,
    ProgramTarget,
    ToolAction,
    ToolSelection,
    ToolStatus,
)

# Re-exports for legacy import sites — these dataclasses live in waldoctl now
# (one canonical type per shape) but plenty of WC modules still import them
# from ``waldo_commander.state``. Keep the re-export so the type checker
# resolves the names and downstream code can migrate to ``waldoctl`` at
# its own pace.
__all__ = [
    "ActionState",
    "PathSegment",
    "ProgramTarget",
    "ToolAction",
    "ToolSelection",
    "ToolStatus",
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


class ChangeNotifierMixin:
    """Mixin providing add/remove/notify listener patterns on two channels.

    The change channel (``add_change_listener`` / ``remove_change_listener`` /
    ``notify_changed``) fans out broad state mutations to all observers. The
    step channel (``add_step_listener`` / ``remove_step_listener`` /
    ``notify_step_changed``) is a parallel pipe for high-frequency script-step
    events (~20Hz) that only playback needs to observe, so they bypass the
    URDF scene reconciler and other change-listeners.

    Both channels use copy-on-write: add/remove replace the list reference so
    that notify_* can iterate without allocation or mutation risk.

    Subclasses using @dataclass should declare:
        _change_listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)
        _step_listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)
    If omitted, each list is auto-created on first use.
    """

    _change_listeners: list[Callable[[], None]]
    _step_listeners: list[Callable[[], None]]

    def _get_listeners(self) -> list[Callable[[], None]]:
        try:
            return self._change_listeners
        except AttributeError:
            self._change_listeners = []
            return self._change_listeners

    def _get_step_listeners(self) -> list[Callable[[], None]]:
        try:
            return self._step_listeners
        except AttributeError:
            self._step_listeners = []
            return self._step_listeners

    def add_change_listener(self, callback: Callable[[], None]) -> None:
        listeners = self._get_listeners()
        if callback not in listeners:
            self._change_listeners = [*listeners, callback]

    def remove_change_listener(self, callback: Callable[[], None]) -> None:
        # Use != (not `is not`) so bound methods removable by their func: each
        # access of `obj.method` creates a fresh bound-method object that fails
        # `is`, but bound methods compare equal by (instance, func).
        self._change_listeners = [cb for cb in self._get_listeners() if cb != callback]

    def notify_changed(self) -> None:
        for cb in self._get_listeners():
            cb()

    def add_step_listener(self, callback: Callable[[], None]) -> None:
        listeners = self._get_step_listeners()
        if callback not in listeners:
            self._step_listeners = [*listeners, callback]

    def remove_step_listener(self, callback: Callable[[], None]) -> None:
        self._step_listeners = [
            cb for cb in self._get_step_listeners() if cb != callback
        ]

    def notify_step_changed(self) -> None:
        for cb in self._get_step_listeners():
            cb()


class AngleArray:
    """Dual-representation angle array storing both degrees and radians.

    Provides zero-allocation access to angles in either unit. Conversion
    happens once at update time via set_deg() or set_rad().
    """

    __slots__ = ("_deg", "_rad")

    def __init__(self, size: int = 6) -> None:
        self._deg = np.zeros(size, dtype=np.float64)
        self._rad = np.zeros(size, dtype=np.float64)

    @property
    def deg(self) -> np.ndarray:
        """Angles in degrees."""
        return self._deg

    @property
    def rad(self) -> np.ndarray:
        """Angles in radians."""
        return self._rad

    def set_deg(self, values: np.ndarray) -> None:
        """Set angles from degrees, computing radians in-place."""
        self._deg[:] = values
        np.deg2rad(self._deg, out=self._rad)

    def set_rad(self, values: np.ndarray) -> None:
        """Set angles from radians, computing degrees in-place."""
        self._rad[:] = values
        np.rad2deg(self._rad, out=self._deg)

    def __len__(self) -> int:
        return len(self._deg)

    def __getitem__(self, idx: int) -> float:
        """Index access returns degrees (for backwards compatibility)."""
        return float(self._deg[idx])


class ToolTimeSeries:
    """Rolling time series buffer for tool telemetry (position, current).

    Every status update is pushed directly.  Chart reads via
    ``get_series_if_dirty()`` — no locking needed since both sides run on the
    same asyncio event loop.

    Uses column-oriented storage to avoid zip-transpose on every read.
    """

    __slots__ = ("_ts", "_pos", "_cur", "_maxlen", "_dirty")

    def __init__(self, max_points: int = 500) -> None:
        self._maxlen = max_points
        self._ts: deque[float] = deque(maxlen=max_points)
        self._pos: deque[float] = deque(maxlen=max_points)
        self._cur: deque[float] = deque(maxlen=max_points)
        self._dirty: bool = False

    def push(self, position: float, current: float) -> None:
        """Append a sample unconditionally."""
        self._ts.append(time.time())
        self._pos.append(position)
        self._cur.append(current)
        self._dirty = True

    def get_series_if_dirty(
        self,
    ) -> tuple[list[float], list[float], list[float]] | None:
        """Return ``(timestamps, positions, currents)`` if new samples exist."""
        if not self._dirty:
            return None
        self._dirty = False
        return list(self._ts), list(self._pos), list(self._cur)

    def clear(self) -> None:
        self._ts.clear()
        self._pos.clear()
        self._cur.clear()
        self._dirty = False


# ProgramTarget, PathSegment, ToolAction, ToolSelection are owned by waldoctl
# (re-exported above from ``waldoctl``). The WC-local duplicates have been
# removed so ``simulation_state``'s field types unify with
# ``commander.programs.active.dry_run.*`` and the type checker stops flagging
# cross-module list assignments.


@bindable_dataclass
class SimulationState(ChangeNotifierMixin):
    # path_segments / targets / tool_actions / tool_selections moved to
    # commander.programs.active.dry_run.* — readers go through the active
    # program directly; writers update the owning program's dry_run.
    current_step_index: int = 0
    total_steps: int = 0
    paths_visible: bool = True
    sim_playback_time: float = 0.0  # Current playback position (seconds)
    sim_total_duration: float = 0.0  # Total timeline duration (seconds)
    sim_playback_active: bool = False  # True when simulation playback timer is ticking
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )
    _step_listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def reset(self) -> None:
        self.current_step_index = 0
        self.total_steps = 0
        self.paths_visible = True
        self.sim_playback_time = 0.0
        self.sim_total_duration = 0.0
        self.sim_playback_active = False


# ``RecordingState`` migrated to ``commander.programs.active.recording``;
# session-wide check is ``services.programs.is_any_program_recording()``.


# Extended shared state singletons for cross-module access
# Only scalar fields are bindable - numpy arrays are excluded to avoid comparison issues
@bindable_dataclass(
    bindable_fields=[
        "connected",
        "x",
        "y",
        "z",
        "rx",
        "ry",
        "rz",
        "io_inputs",
        "io_outputs",
        "io_estop",
        "tool_key",
        "tool_variant_key",
        "tool_position",
        "tool_current",
        "tool_engaged",
        "tool_part_detected",
        "simulator_active",
        "action_current",
        "action_state",
        "action_params",
        "editing_mode",
        "tcp_speed",
    ]
)
class RobotState(ChangeNotifierMixin):
    # Preallocated arrays for zero-allocation hot path updates
    angles: AngleArray = field(default_factory=AngleArray)  # joint angles (deg/rad)
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
    # Movement enablement arrays from STATUS (12 ints each)
    joint_en: np.ndarray = field(default_factory=lambda: np.ones(12, dtype=np.int32))
    cart_en: dict[str, np.ndarray] = field(default_factory=dict)
    connected: bool = False
    # Derived scalars for convenient, high-performance UI bindings
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    # Dynamic IO lists (length determined by robot.digital_inputs / digital_outputs)
    io_inputs: list[int] = field(default_factory=list)
    io_outputs: list[int] = field(default_factory=list)
    io_estop: int = 1
    tool_key: str = "NONE"
    tool_variant_key: str = ""
    tool_position: float = 0.0
    tool_current: float = 0.0
    tool_engaged: bool = False
    tool_part_detected: bool = False
    tool_time_series: ToolTimeSeries = field(default_factory=ToolTimeSeries)
    speeds: np.ndarray = field(
        default_factory=lambda: np.zeros(6, dtype=np.float64)
    )  # deg/s
    tcp_speed: float = 0.0  # mm/s
    simulator_active: bool = False
    action_current: str = ""
    action_state: ActionState = ActionState.IDLE
    action_params: str = ""
    executing_index: int = -1
    completed_index: int = -1
    last_update_ts: float = 0.0  # timestamp of last STATUS update
    # Editing mode - when True, x/y/z/angles are controlled by target editor
    editing_mode: bool = False
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )

    def init_cart_en(self, frames: tuple[str, ...]) -> None:
        """Initialize cart_en arrays for each Cartesian frame."""
        self.cart_en = {f: np.ones(12, dtype=np.int32) for f in frames}

    def reset(self) -> None:
        """Reset to defaults. Arrays are zeroed in-place; cart_en frames preserved."""
        self.angles.set_deg(np.zeros(len(self.angles), dtype=np.float64))
        self.orientation.set_deg(np.zeros(3, dtype=np.float64))
        self.pose[:] = 0.0
        self.io[:] = 0
        self.tool_status = ToolStatus()
        self.joint_en[:] = 1
        for arr in self.cart_en.values():
            arr[:] = 1
        self.connected = False
        self.x = self.y = self.z = 0.0
        self.rx = self.ry = self.rz = 0.0
        self.io_inputs = []
        self.io_outputs = []
        self.io_estop = 1
        self.tool_key = "NONE"
        self.tool_variant_key = ""
        self.tool_position = 0.0
        self.tool_current = 0.0
        self.tool_engaged = False
        self.tool_part_detected = False
        self.tool_time_series.clear()
        self.speeds[:] = 0.0
        self.tcp_speed = 0.0
        self.simulator_active = False
        self.action_current = ""
        self.action_state = ActionState.IDLE
        self.action_params = ""
        self.executing_index = -1
        self.completed_index = -1
        self.last_update_ts = 0.0
        self.editing_mode = False


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
    simulation_state.reset()
    playback_coordination.reset()
    readiness_state.reset()
    # Editor tabs / action log live on the commander locator now; reset via
    # their services so the public surface stays in sync with WC's mirrors.
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
