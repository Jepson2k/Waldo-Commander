"""
Path preview client for offline simulation and visualization.

Wraps a backend's DryRunRobotClient with visualization
metadata collection (path segments, targets, colors).
"""

import inspect
import linecache
import logging
import re
from typing import Any

import numpy as np

from waldoctl import DryRunResult

from waldo_commander.common.theme import get_color_for_move_type
from waldo_commander.state import ToolAction, ToolSelection

logger = logging.getLogger(__name__)

# Pre-compiled regex patterns for performance
_LITERAL_LIST_RE = re.compile(
    r"(?:move_j|move_l|move_c|move_s|move_p)\s*\(\s*(?:\w+\s*=\s*)?\["
    r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
    r"(?:\s*,\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)*\s*\]"
)
_DURATION_RE = re.compile(r"duration\s*=\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")

# Methods that produce trajectory segments for visualization.
MOTION_METHODS: dict[str, str] = {
    "move_j": "joints",
    "move_l": "cartesian",
    "move_c": "smooth_arc",
    "move_s": "smooth_spline",
    "move_p": "cartesian",
    "jog_j": "jog",
    "jog_l": "jog",
    "servo_j": "jog",
    "servo_l": "jog",
}

# Valid method names on the real RobotClient. Anything not in this set
# raises AttributeError so typos in user scripts fail during dry-run
# the same way they would on real hardware.


class _ToolCollectionProxy:
    """Wraps DryRunRobotClient.tool with collection + visualization metadata.

    Intercepts tool action method calls, delegates to the dry-run tool
    (which dispatches through the planner), collects the DryRunResult,
    and augments with tool visualization metadata for 3D arrow rendering.
    """

    def __init__(self, preview_client: "PathPreviewClient"):
        self._preview = preview_client

    def __getattr__(self, name: str) -> Any:
        dry_run_tool = self._preview._client.tool

        def interceptor(*args: Any, **kwargs: Any) -> Any:
            method = getattr(dry_run_tool, name)
            result = method(*args, **kwargs)
            self._preview._record_tool_action(name, args, kwargs, result)
            return result

        return interceptor


class PathPreviewClient:
    """Wraps DryRunRobotClient with visualization metadata collection.

    Delegates all commands to the backend's DryRunClient (which runs
    through the real command pipeline with ControllerState). After each
    motion, collects path segment dicts for 3D visualization.

    Methods are resolved via __getattr__:
    - Motion methods: dispatch through _client + collect visualization
    - All other methods: delegate to _client (which raises AttributeError
      for unknown names, catching typos in user scripts)
    """

    def __init__(
        self,
        dry_run_client_cls: type,
        segment_collector: list[dict] | None = None,
        target_collector: list[dict] | None = None,
        tool_action_collector: list[ToolAction] | None = None,
        tool_selection_collector: list | None = None,
        initial_joints: list[float] | np.ndarray | None = None,
        tool_meta_registry: dict[str, dict] | None = None,
    ):
        self.segment_collector: list[dict] = (
            [] if segment_collector is None else segment_collector
        )
        self.target_collector: list[dict] = (
            [] if target_collector is None else target_collector
        )
        self.tool_action_collector: list[ToolAction] = (
            [] if tool_action_collector is None else tool_action_collector
        )
        self.tool_selection_collector: list = (
            [] if tool_selection_collector is None else tool_selection_collector
        )
        self._tool_meta_registry: dict[str, dict] = tool_meta_registry or {}
        self._tool_metadata: dict | None = None
        self.accumulated_errors: list[str] = []

        init_deg: list[float] | None = None
        if initial_joints is not None:
            init_deg = np.degrees(np.asarray(initial_joints, dtype=np.float64)).tolist()

        self._client = dry_run_client_cls(initial_joints_deg=init_deg)
        self._tool_proxy = _ToolCollectionProxy(self)
        self.last_joints_rad: list[float] | None = None
        self._blend_move_type: str = ""
        self._pending_sleep: float = 0.0
        self._last_move_non_blocking: bool = False
        self._current_tool_position: float = 0.0  # 0=open, 1=closed
        self._first_motion_seen: bool = False  # Track travel-to-start segments

        logger.debug("PathPreviewClient initialized")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._flush_blend()

    def close(self):
        self._flush_blend()

    @property
    def tool(self) -> _ToolCollectionProxy:
        """Return a proxy that delegates to DryRunRobotClient.tool and collects results."""
        return self._tool_proxy

    def _record_tool_action(
        self,
        method_name: str,
        args: tuple,
        kwargs: dict,
        result: DryRunResult | None = None,
    ) -> None:
        """Record a tool action with TCP pose for path preview visualization."""
        if self._tool_metadata is None:
            return

        line_no = self._get_caller_line_number()

        # Determine target positions from the method call
        if method_name == "set_position" and args:
            target_pos = (float(args[0]),)
        elif method_name == "open":
            target_pos = (0.0,)
        elif method_name == "close":
            target_pos = (1.0,)
        else:
            return

        start_pos = (self._current_tool_position,)
        self._current_tool_position = target_pos[0]

        # Get TCP pose from the DryRunResult (preferred) or last segment
        # Include all 6 elements (x,y,z,rx,ry,rz) so rotation can transform the axis
        tcp_pose = None
        if result is not None and result.tcp_poses.shape[0] > 0:
            tcp_pose = result.tcp_poses[-1].tolist()
        elif self.segment_collector:
            last_seg = self.segment_collector[-1]
            if last_seg.get("points"):
                tcp_pose = last_seg["points"][-1]

        duration = result.duration if result is not None else 0.0

        # If there's a pending sleep from time.sleep() after a non-blocking
        # move (wait=False), the tool fires mid-motion. Offset into the
        # preceding segment instead of using the end-of-move position.
        sleep_offset = self._pending_sleep
        # Don't reset — let accumulation continue from move start so
        # subsequent tool actions see the correct absolute offset.

        pose_at_offset, tcp_path = self._slice_trajectory(sleep_offset, duration)
        if pose_at_offset is not None and tcp_pose is not None and len(tcp_pose) >= 3:
            tcp_pose[:3] = pose_at_offset[:3]
        elif pose_at_offset is not None:
            tcp_pose = pose_at_offset + [0.0, 0.0, 0.0]

        action = ToolAction(
            tcp_pose=tcp_pose,
            motions=self._tool_metadata["motions"],
            target_positions=target_pos,
            start_positions=start_pos,
            activation_type=self._tool_metadata["activation_type"],
            line_number=line_no,
            method=method_name,
            estimated_duration=duration,
            sleep_offset=sleep_offset,
            segment_index=len(self.segment_collector) - 1,
            tcp_path=tcp_path,
        )
        self.tool_action_collector.append(action)

    def _slice_trajectory(
        self, offset: float, duration: float
    ) -> tuple[list[float] | None, list[list[float]] | None]:
        """Slice TCP path from the preceding segment's trajectory points.

        Uses the same points at the same rate as the arm trajectory — no
        re-sampling. For mid-motion actions (offset > 0), slices around the
        offset point. For end-of-move actions (offset == 0), slices the tail.

        If the arm is stationary (no segment or single-point segment), returns
        the TCP pose repeated for the action duration.

        Returns (tcp_pose_at_offset, tcp_path_slice).
        """
        if not self.segment_collector:
            return None, None

        last_seg = self.segment_collector[-1]
        points = last_seg.get("points")
        seg_duration = last_seg.get("estimated_duration")

        if not points or not seg_duration or seg_duration <= 0:
            # Stationary: repeat TCP position
            if points and len(points) >= 1:
                pose = list(points[-1])
                n = max(2, int(duration * 100))  # ~100Hz
                return pose, [pose] * n
            return None, None

        n_pts = len(points)
        if n_pts < 2:
            pose = list(points[0])
            n = max(2, int(duration * 100))
            return pose, [pose] * n

        # Compute index range for the slice
        dur_indices = max(1, int(n_pts * duration / seg_duration))

        if offset > 0:
            # Mid-motion: center slice around offset point
            offset_idx = int(min(1.0, offset / seg_duration) * (n_pts - 1))
            half = dur_indices // 2
            start = max(0, offset_idx - half)
            end = min(n_pts, start + dur_indices)
        else:
            # End-of-move: arm is stationary at final position
            return list(points[-1]), None

        tcp_pose = list(points[offset_idx])
        path_slice = [list(p) for p in points[start:end]]

        if len(path_slice) < 2:
            return tcp_pose, None

        # If all points are at the same position, the robot is stationary —
        # return None to force single-point rendering instead of cascading
        p0 = path_slice[0]
        if all(
            abs(p[0] - p0[0]) < 1e-4
            and abs(p[1] - p0[1]) < 1e-4
            and abs(p[2] - p0[2]) < 1e-4
            for p in path_slice[1:]
        ):
            return tcp_pose, None

        return tcp_pose, path_slice

    def _flush_blend(self) -> None:
        """Flush pending blend buffer from the underlying dry-run client."""
        results = self._client.flush()
        for result in results:
            self._collect_from_result(result, self._blend_move_type or "joints")
        self._blend_move_type = ""

    # ---- Source introspection ----

    def _get_caller_line_number(self) -> int:
        try:
            frame = inspect.currentframe()
            while frame:
                if frame.f_code.co_filename == "simulation_script.py":
                    return frame.f_lineno
                frame = frame.f_back
        except (AttributeError, RuntimeError):
            pass
        return 0

    def _get_source_line(self, line_no: int) -> str:
        try:
            line = linecache.getline("simulation_script.py", line_no)
            if line:
                return line.strip()
        except (OSError, ValueError, IndexError):
            pass
        return ""

    def _has_literal_list_args(self, line: str) -> bool:
        return bool(_LITERAL_LIST_RE.search(line))

    @staticmethod
    def _extract_requested_duration(line: str) -> float | None:
        """Extract duration=<value> from a source line. Returns None if not found or <= 0."""
        match = _DURATION_RE.search(line)
        if match:
            val = float(match.group(1))
            return val if val > 0 else None
        return None

    # ---- Segment collection ----

    def _collect_from_result(
        self, result: DryRunResult | None, move_type: str, checkpoint: str | None = None
    ):
        if result is None:
            return

        if result.end_joints_rad.size > 0:
            self.last_joints_rad = result.end_joints_rad.tolist()

        if result.tcp_poses.shape[0] == 0:
            return

        line_no = self._get_caller_line_number()
        source_line = self._get_source_line(line_no)

        valid = result.valid
        has_error = result.error is not None

        end_joints = self.last_joints_rad if self.last_joints_rad else []

        estimated = result.duration  # 0.0 is valid (e.g. teleport/home)
        requested = self._extract_requested_duration(source_line)
        if estimated is not None and requested is not None:
            timing_feasible = estimated <= requested * 1.05
        else:
            timing_feasible = True

        joint_traj_rad = getattr(result, "joint_trajectory_rad", None)
        joint_traj = joint_traj_rad.tolist() if joint_traj_rad is not None else None

        if valid is not None:
            # Per-pose validity: split into runs of consecutive valid/invalid
            self._collect_validity_segments(
                result,
                valid,
                move_type,
                line_no,
                end_joints,
                estimated,
                requested,
                timing_feasible,
                joint_traj_rad,
            )
        else:
            # All valid or all invalid (legacy path)
            is_valid = not has_error
            points = result.tcp_poses[:, :3].tolist()
            segment = {
                "points": points,
                "color": get_color_for_move_type(move_type, is_valid),
                "is_valid": is_valid,
                "line_number": line_no,
                "joints": end_joints,
                "move_type": move_type,
                "is_dashed": len(points) <= 2,
                "show_arrows": True,
                "estimated_duration": estimated,
                "requested_duration": requested,
                "timing_feasible": timing_feasible,
                "joint_trajectory": joint_traj,
                "checkpoint": checkpoint,
                "is_travel": not self._first_motion_seen,
            }
            self.segment_collector.append(segment)

        has_literal_args = self._has_literal_list_args(source_line)

        if has_literal_args:
            end_pose_m = result.tcp_poses[-1].copy()

            target_id = f"auto_{line_no}"
            target = {
                "id": target_id,
                "line_number": line_no,
                "pose": end_pose_m.tolist(),
                "move_type": move_type,
                "scene_object_id": "",
            }
            self.target_collector.append(target)
            logger.debug("Created target %s at line %d", target_id, line_no)

    def _collect_failed_target(
        self,
        line_no: int,
        move_type: str,
        args: tuple,
        kwargs: dict,
    ) -> None:
        """Create a target marker for a move that failed (out of range, IK failure).

        Extracts the intended pose from the move arguments so the user can
        at least see where the unreachable target is. Targets from non-literal
        source (e.g. computed expressions) are still rendered red but won't
        back-edit on drag — drag interaction is a separate concern.
        """
        # Extract intended pose from args/kwargs (runtime values, regardless
        # of whether the source line is a literal or computed expression)
        pose: list[float] | None = None
        pose_kwarg = kwargs.get("pose")
        if pose_kwarg is not None:
            pose = [float(v) for v in pose_kwarg[:6]]
        elif move_type in ("cartesian", "smooth_arc", "smooth_spline") and args:
            # move_l/move_p/move_c: first arg is [x,y,z,rx,ry,rz] in mm/deg
            pose = [float(v) for v in args[0][:6]]
        # move_j with joint angles: skip — we'd need FK to get TCP pose

        if pose is None or len(pose) < 3:
            return

        # Convert mm/deg to m/rad for consistency with valid targets
        pose_m = [
            pose[0] / 1000.0,
            pose[1] / 1000.0,
            pose[2] / 1000.0,
            *pose[3:],
        ]

        target_id = f"auto_{line_no}"
        self.target_collector.append(
            {
                "id": target_id,
                "line_number": line_no,
                "pose": pose_m,
                "move_type": move_type,
                "scene_object_id": "",
                "is_valid": False,
            }
        )
        logger.debug("Created failed-move target %s at line %d", target_id, line_no)

    def _collect_validity_segments(
        self,
        result: DryRunResult,
        valid: np.ndarray,
        move_type: str,
        line_no: int,
        end_joints: list[float],
        estimated: float | None,
        requested: float | None,
        timing_feasible: bool,
        joint_traj_rad: np.ndarray | None = None,
    ) -> None:
        """Split a result with per-pose validity into green/red segments."""
        poses = result.tcp_poses
        n = len(valid)

        # Find runs of consecutive same-validity poses
        i = 0
        while i < n:
            run_valid = bool(valid[i])
            j = i + 1
            while j < n and bool(valid[j]) == run_valid:
                j += 1

            # Include one overlapping point at boundaries for visual continuity
            end = min(j + 1, n) if j < n else j
            start = max(i - 1, 0) if i > 0 else i
            run_poses = poses[start:end, :3].tolist()

            run_joint_traj = None
            if joint_traj_rad is not None:
                run_joint_traj = joint_traj_rad[start:end].tolist()

            if len(run_poses) >= 2:
                segment = {
                    "points": run_poses,
                    "color": get_color_for_move_type(move_type, run_valid),
                    "is_valid": run_valid,
                    "line_number": line_no,
                    "joints": end_joints if j >= n else [],
                    "move_type": move_type,
                    "is_dashed": False,
                    "show_arrows": run_valid,
                    "estimated_duration": estimated if j >= n else None,
                    "requested_duration": requested if j >= n else None,
                    "timing_feasible": timing_feasible,
                    "joint_trajectory": run_joint_traj,
                    "is_travel": not self._first_motion_seen,
                }
                self.segment_collector.append(segment)

            i = j

    # ---- Explicit: home ----

    def home(self, **kw: Any) -> bool:
        self._flush_blend()
        self._first_motion_seen = True
        try:
            result = self._client.home(**kw)
        except Exception as e:
            logger.warning("home failed: %s", e)
            return True
        self._collect_from_result(result, "joints", checkpoint="home")
        return True

    def checkpoint(self, label: str) -> int:
        """Record a checkpoint marker in the timeline.

        Creates a zero-width segment so the checkpoint appears in the
        timeline without taking any duration.
        """
        self._flush_blend()
        try:
            self._client.checkpoint(label)
        except (AttributeError, NotImplementedError):
            pass  # dry-run client may not implement checkpoint
        line_no = self._get_caller_line_number()
        segment = {
            "points": [],
            "color": "#00000000",
            "is_valid": True,
            "line_number": line_no,
            "joints": self.last_joints_rad or [],
            "move_type": "checkpoint",
            "is_dashed": False,
            "show_arrows": False,
            "estimated_duration": 0.0,
            "requested_duration": None,
            "timing_feasible": True,
            "joint_trajectory": None,
            "checkpoint": label,
            "is_travel": not self._first_motion_seen,
        }
        self.segment_collector.append(segment)
        return 0

    # ---- Dynamic dispatch ----

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        # Motion methods: dispatch through _client + collect visualization
        move_type = MOTION_METHODS.get(name)
        if move_type is not None:

            def motion_method(*args: Any, **kwargs: Any) -> bool:
                try:
                    self._first_motion_seen = True
                    self._pending_sleep = 0.0  # Reset sleep accumulator on new motion
                    self._last_move_non_blocking = not kwargs.get("wait", True)
                    method = getattr(self._client, name)
                    result = method(*args, **kwargs)
                    if result is None:
                        # Buffered for blending — track move_type of first buffered cmd
                        if not self._blend_move_type:
                            self._blend_move_type = move_type
                    else:
                        # Result returned (single dispatch or flushed composite)
                        mt = self._blend_move_type or move_type
                        self._blend_move_type = ""
                        # IK failure with no trajectory: dry run returns an
                        # empty-poses DryRunResult with .error set. Without this
                        # branch, _collect_from_result silently drops it and
                        # nothing renders — no segment, no marker.
                        if result.tcp_poses.shape[0] == 0 and result.error is not None:
                            line_no = self._get_caller_line_number()
                            self.accumulated_errors.append(
                                f"Line {line_no}: {result.error.title}"
                            )
                            self._collect_failed_target(
                                line_no, move_type, args, kwargs
                            )
                        else:
                            self._collect_from_result(result, mt)
                    return True
                except Exception as e:
                    self._first_motion_seen = True
                    line_no = self._get_caller_line_number()
                    self.accumulated_errors.append(f"Line {line_no}: {e}")
                    logger.warning("%s failed: %s", name, e)
                    # Still create a target for the failed move so the user
                    # can see and drag it to a valid position
                    self._collect_failed_target(
                        line_no,
                        move_type,
                        args,
                        kwargs,
                    )
                    return False

            return motion_method

        # Intercept select_tool to update tool metadata from registry
        if name == "select_tool":
            self._flush_blend()
            client_method = getattr(self._client, name)

            def set_tool_wrapper(*args: Any, **kw: Any) -> Any:
                result = client_method(*args, **kw)
                self._current_tool_position = 0.0  # New tool starts open
                if args:
                    key = str(args[0]).strip().upper()
                    variant_key = kw.get(
                        "variant_key", args[1] if len(args) > 1 else ""
                    )
                    entry = self._tool_meta_registry.get(key)
                    if entry:
                        # Prefer variant-specific motions if available
                        variants = entry.get("variants", {})
                        if variant_key and variant_key in variants:
                            self._tool_metadata = {
                                "motions": variants[variant_key]["motions"],
                                "activation_type": entry["activation_type"],
                            }
                        elif entry.get("motions"):
                            self._tool_metadata = entry
                        else:
                            self._tool_metadata = None
                    else:
                        self._tool_metadata = None
                    self.tool_selection_collector.append(
                        ToolSelection(
                            tool_key=key,
                            variant_key=str(variant_key),
                            segment_index=len(self.segment_collector) - 1,
                            line_number=self._get_caller_line_number(),
                        )
                    )
                return result

            return set_tool_wrapper

        # Intercept set_tcp_offset — flush blend since it changes kinematics
        if name == "set_tcp_offset":
            self._flush_blend()
            return getattr(self._client, name)

        # All other methods: flush blend first, then delegate to backend.
        # It raises AttributeError for unknown names, catching typos.
        self._flush_blend()
        return getattr(self._client, name)


class AsyncPathPreviewClient:
    """Async wrapper around PathPreviewClient."""

    def __init__(
        self,
        dry_run_client_cls: type,
        segment_collector: list[dict] | None = None,
        target_collector: list[dict] | None = None,
        tool_action_collector: list[ToolAction] | None = None,
        tool_selection_collector: list | None = None,
        initial_joints: list[float] | np.ndarray | None = None,
        tool_meta_registry: dict[str, dict] | None = None,
    ):
        self._sync_client = PathPreviewClient(
            dry_run_client_cls=dry_run_client_cls,
            segment_collector=segment_collector,
            target_collector=target_collector,
            tool_action_collector=tool_action_collector,
            tool_selection_collector=tool_selection_collector,
            initial_joints=initial_joints,
            tool_meta_registry=tool_meta_registry,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._sync_client._flush_blend()

    async def close(self):
        self._sync_client._flush_blend()

    @property
    def segment_collector(self) -> list[dict]:
        return self._sync_client.segment_collector

    @property
    def target_collector(self) -> list[dict]:
        return self._sync_client.target_collector

    @property
    def tool_action_collector(self) -> list[ToolAction]:
        return self._sync_client.tool_action_collector

    @property
    def tool_selection_collector(self) -> list:
        return self._sync_client.tool_selection_collector

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._sync_client, name)
        if callable(attr) and name != "close":

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                return attr(*args, **kwargs)

            object.__setattr__(self, name, wrapper)
            return wrapper
        return attr
