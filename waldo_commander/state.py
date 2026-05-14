import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TYPE_CHECKING

import numpy as np
from nicegui import binding
from waldoctl import ActionState, ToolStatus

from waldo_commander.common.loop_timer import PhaseTimer


class EnvelopeMode(Enum):
    """Workspace envelope visibility modes."""

    AUTO = "auto"
    ON = "on"
    OFF = "off"


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
    """Mixin providing add/remove/notify change-listener pattern.

    Uses copy-on-write: add/remove replace the list reference so that
    notify_changed can iterate without allocation or mutation risk.

    Subclasses using @dataclass should declare:
        _change_listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)
    If omitted, the list is auto-created on first use.
    """

    _change_listeners: list[Callable[[], None]]

    def _get_listeners(self) -> list[Callable[[], None]]:
        try:
            return self._change_listeners
        except AttributeError:
            self._change_listeners = []
            return self._change_listeners

    def add_change_listener(self, callback: Callable[[], None]) -> None:
        listeners = self._get_listeners()
        if callback not in listeners:
            self._change_listeners = [*listeners, callback]

    def remove_change_listener(self, callback: Callable[[], None]) -> None:
        self._change_listeners = [
            cb for cb in self._get_listeners() if cb is not callback
        ]

    def notify_changed(self) -> None:
        for cb in self._get_listeners():
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


@dataclass(slots=True)
class ProgramTarget:
    id: str  # Unique identifier
    line_number: int  # Line number in the editor (1-based)
    pose: list[float]  # [x, y, z, rx, ry, rz]
    move_type: str  # "cartesian", "pose", "joints"
    scene_object_id: str  # ID of the 3D marker object in the scene
    is_valid: bool = True  # False when move failed (out of range, IK failure)

    @classmethod
    def from_dict(cls, d: dict) -> "ProgramTarget":
        """Deserialize from dict."""
        return cls(**d)


@dataclass(slots=True)
class PathSegment:
    points: list[list[float]]  # List of [x, y, z] points defining the segment
    color: str  # Hex color code (green, blue, orange, red)
    is_valid: bool  # Whether the segment is reachable (IK valid)
    line_number: int  # Source line number in program
    joints: list[float] | None = None  # Joint angles at end of segment
    move_type: str = "cartesian"  # "cartesian", "joints", "smooth_*"
    is_dashed: bool = True  # Whether to render as dashed line
    show_arrows: bool = True  # Whether to show direction arrows
    joint_trajectory: list[list[float]] | None = (
        None  # Full joint trajectory for smooth playback
    )
    # Timing validation fields
    estimated_duration: float | None = None  # Computed duration from trajectory builder
    requested_duration: float | None = None  # User-requested duration
    timing_feasible: bool = True  # Whether motion achievable in requested time
    checkpoint: str | None = (
        None  # Checkpoint type (e.g. "home") — playback pauses here
    )
    is_travel: bool = (
        False  # True for travel-to-start segments (before first motion command)
    )

    @classmethod
    def from_dict(cls, d: dict) -> "PathSegment":
        """Deserialize from dict."""
        return cls(**d)


@dataclass(slots=True)
class ToolAction:
    tcp_pose: list[float] | None
    motions: list[dict[str, Any]]
    target_positions: tuple[float, ...]
    activation_type: str
    line_number: int
    method: str
    start_positions: tuple[
        float, ...
    ] = ()  # Jaw positions at start of action (0=open, 1=closed)
    estimated_duration: float = 0.0
    sleep_offset: float = (
        0.0  # Seconds into preceding non-blocking move when tool fires
    )
    segment_index: int = -1  # Index of preceding path segment (-1 if none)
    tcp_path: list[list[float]] | None = None  # TCP poses sampled over action duration


@dataclass(slots=True)
class ToolSelection:
    """Records a select_tool() call during simulation for timeline playback."""

    tool_key: str
    variant_key: str = ""
    segment_index: int = -1  # -1 means before any motion
    line_number: int = 0


@bindable_dataclass
class SimulationState(ChangeNotifierMixin):
    targets: list[ProgramTarget] = field(default_factory=list)
    path_segments: list[PathSegment] = field(default_factory=list)
    tool_actions: list[ToolAction] = field(default_factory=list)
    tool_selections: list[ToolSelection] = field(default_factory=list)
    current_step_index: int = 0
    total_steps: int = 0
    is_playing: bool = False
    playback_speed: float = 1.0  # Multiplier
    preview_mode: bool = False  # True=Dry Run, False=Real Execute
    paths_visible: bool = True
    envelope_mode: EnvelopeMode = EnvelopeMode.AUTO
    active_cursor_line: int = 0  # 1-indexed editor cursor line, 0 = none
    sim_playback_time: float = 0.0  # Current playback position (seconds)
    sim_total_duration: float = 0.0  # Total timeline duration (seconds)
    sim_playback_active: bool = False  # True when simulation playback timer is ticking
    sim_pose_override: bool = (
        False  # True while scrubbing/playing — suppresses status-loop URDF updates
    )
    last_teleport_ts: float = 0.0  # monotonic time of last teleport send; used by status loop to delay handback
    script_running: bool = False  # True while a user script is executing
    is_loading: bool = False  # True while a path-preview simulation is running
    last_sim_error: str | None = (
        None  # Most recent simulation error (for diagnostics push)
    )
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )

    def reset(self) -> None:
        self.targets.clear()
        self.path_segments.clear()
        self.tool_actions.clear()
        self.tool_selections.clear()
        self.current_step_index = 0
        self.total_steps = 0
        self.is_playing = False
        self.playback_speed = 1.0
        self.preview_mode = False
        self.paths_visible = True
        self.envelope_mode = EnvelopeMode.AUTO
        self.active_cursor_line = 0
        self.sim_playback_time = 0.0
        self.sim_total_duration = 0.0
        self.sim_playback_active = False
        self.sim_pose_override = False
        self.last_teleport_ts = 0.0
        self.script_running = False
        self.is_loading = False
        self.last_sim_error = None


@bindable_dataclass
class RecordingState:
    is_recording: bool = False

    def reset(self) -> None:
        self.is_recording = False


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

    # Control panel UI state
    jog_speed: int = 50
    jog_accel: int = 50
    incremental_jog: bool = False
    joint_step_deg: float = 1.0
    gizmo_visible: bool = True

    # Gripper panel state
    gripper_speed_sync: bool = True
    gripper_speed: int = 50
    gripper_current: int = 500
    tool_target_position: float = 0.0

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
    _playback: Any = None

    # Program panel visibility (tracked for tab flash when panel closed)
    program_panel_visible: bool = False

    # Post-init required fields (assert on access, set via assignment)
    editor_panel = _RequiredField()
    control_panel = _RequiredField()
    readout_panel = _RequiredField()
    playback = _RequiredField()
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


@dataclass(slots=True)
class EditorTab:
    """State for a single editor tab."""

    id: str  # Unique tab identifier (UUID hex)
    filename: str  # Display name / filename
    file_path: str | None  # Full path if saved to server
    content: str  # Current editor content
    saved_content: str  # Content at last save (for dirty tracking)
    output_log: list[str] = field(default_factory=list)  # Per-tab output log entries
    path_segments: list[PathSegment] = field(
        default_factory=list
    )  # Per-tab simulation paths
    targets: list[ProgramTarget] = field(default_factory=list)  # Per-tab targets
    tool_actions: list[ToolAction] = field(default_factory=list)  # Per-tab tool actions
    tool_selections: list[ToolSelection] = field(
        default_factory=list
    )  # Per-tab tool selections
    final_joints_rad: list[float] | None = None  # Final joint position from simulation
    last_sim_joints_deg: np.ndarray | None = None  # Robot position when last simulated
    created_at: float = 0.0  # Timestamp

    @property
    def is_dirty(self) -> bool:
        """Return True if content differs from saved content."""
        return self.content != self.saved_content


@bindable_dataclass
class EditorTabsState(ChangeNotifierMixin):
    """State for multi-tab editor.

    Holds both pure tab data (``tabs``, ``active_tab_id``) and the widget
    references for the active tab (``active_textarea``,
    ``active_filename_input``). Mixing widget refs with data here is
    intentional: the refs' lifecycle is tied directly to tab switching, and
    consolidating them on the tab-state object lets sub-controllers
    (decorations, simulation engine, motion recorder, script execution) read
    the active tab's widgets without back-references into EditorPanel.
    """

    tabs: list[EditorTab] = field(default_factory=list)
    active_tab_id: str | None = None
    # Updated by EditorPanel on every tab switch / initial build.
    active_textarea: Any = None  # ui.codemirror | None at runtime
    active_filename_input: Any = None  # ui.input | None at runtime
    _change_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False
    )

    def get_active_tab(self) -> EditorTab | None:
        """Get the currently active tab."""
        if not self.active_tab_id:
            return None
        return next((t for t in self.tabs if t.id == self.active_tab_id), None)

    def find_tab_by_path(self, file_path: str | None) -> EditorTab | None:
        """Find a tab with the given file path. Returns None if file_path is None."""
        if file_path is None:
            return None
        return next((t for t in self.tabs if t.file_path == file_path), None)

    def find_tab_by_id(self, tab_id: str) -> EditorTab | None:
        """Find a tab by its ID."""
        return next((t for t in self.tabs if t.id == tab_id), None)

    def add_tab(self, tab: EditorTab) -> None:
        """Add a new tab."""
        self.tabs.append(tab)
        self.notify_changed()

    def remove_tab(self, tab_id: str) -> None:
        """Remove a tab by ID."""
        self.tabs = [t for t in self.tabs if t.id != tab_id]
        if self.active_tab_id == tab_id:
            self.active_tab_id = self.tabs[0].id if self.tabs else None
        self.notify_changed()

    def reset(self) -> None:
        self.tabs = []
        self.active_tab_id = None
        self.active_textarea = None
        self.active_filename_input = None


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
# Action Log
# ===========================================================================


class ActionStatus(Enum):
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ActionLogEntry:
    """Single entry in the action log."""

    command_name: str
    params: str = ""
    status: ActionStatus = ActionStatus.EXECUTING
    command_index: int = -1
    count: int = 1
    timestamp: float = 0.0


class ActionLog:
    """Session-scoped action log with coalescing of repeated commands."""

    def __init__(self, max_entries: int = 200) -> None:
        self._entries: deque[ActionLogEntry] = deque(maxlen=max_entries)
        self._last_executing_index: int = -1
        self._last_completed_index: int = -1
        self._version: int = 0

    @property
    def entries(self) -> deque[ActionLogEntry]:
        return self._entries

    @property
    def version(self) -> int:
        return self._version

    @property
    def latest(self) -> ActionLogEntry | None:
        return self._entries[-1] if self._entries else None

    def process_status(
        self,
        action_current: str,
        action_params: str,
        action_state: ActionState,
        executing_index: int,
        completed_index: int,
    ) -> bool:
        """Process a status update, returning True if the log changed."""
        changed = False

        # Detect new command starting
        if (
            executing_index > self._last_executing_index
            and action_state == ActionState.EXECUTING
        ):
            name = action_current.removesuffix("Command")
            latest = self.latest
            if (
                latest
                and latest.command_name == name
                and latest.params == action_params
                and latest.status == ActionStatus.COMPLETED
            ):
                latest.count += 1
                latest.status = ActionStatus.EXECUTING
                latest.command_index = executing_index
                latest.timestamp = time.time()
            else:
                self._entries.append(
                    ActionLogEntry(
                        command_name=name,
                        params=action_params,
                        command_index=executing_index,
                        timestamp=time.time(),
                    )
                )
            self._last_executing_index = executing_index
            changed = True

        # Detect command completion
        if completed_index > self._last_completed_index:
            matched = False
            for entry in reversed(self._entries):
                if entry.command_index == completed_index:
                    entry.status = ActionStatus.COMPLETED
                    matched = True
                    break
            # Coalesced entries may have overwritten command_index;
            # fall back to marking the latest EXECUTING entry as completed
            if not matched and self._entries:
                for entry in reversed(self._entries):
                    if entry.status == ActionStatus.EXECUTING:
                        entry.status = ActionStatus.COMPLETED
                        break
            self._last_completed_index = completed_index
            changed = True

        # Detect failure (action goes IDLE but completed_index didn't advance)
        if (
            action_state != ActionState.EXECUTING
            and self._entries
            and self._entries[-1].status == ActionStatus.EXECUTING
            and completed_index == self._last_completed_index
            and executing_index == self._last_executing_index
        ):
            self._entries[-1].status = ActionStatus.FAILED
            changed = True

        if changed:
            self._version += 1
        return changed

    def clear(self) -> None:
        self._entries.clear()
        self._last_executing_index = -1
        self._last_completed_index = -1
        self._version += 1


# Module-level singletons
robot_state: RobotState = RobotState()
controller_state: ControllerState = ControllerState()
ui_state: UiState = UiState()
simulation_state: SimulationState = SimulationState()
recording_state: RecordingState = RecordingState()
readiness_state: ReadinessState = ReadinessState()
editor_tabs_state: EditorTabsState = EditorTabsState()
action_log: ActionLog = ActionLog()


def reset_all_state() -> None:
    """Reset all state singletons to defaults. For test isolation."""
    robot_state.reset()
    controller_state.reset()
    ui_state.reset()
    simulation_state.reset()
    recording_state.reset()
    readiness_state.reset()
    editor_tabs_state.reset()
    action_log.clear()


# Global timing instrumentation - import and use from any module
# Usage: with global_phase_timer.phase("my_operation"): ...
global_phase_timer = PhaseTimer(
    [
        "status",  # Receiving/parsing status + updating panels
        "scene",  # 3D scene updates (angles, TCP ball, envelope)
        "jog",  # Joint and cartesian jog API calls
    ]
)
