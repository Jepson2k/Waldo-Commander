"""Workspace envelope renderer for UrdfScene.

Provides workspace envelope visualization:
- Convex hull mesh rendering (cached as STL)
- Proximity clipping planes
- Tool offset adjustments
- Workspace envelope calculation with caching

Kept as a mixin (not full composition) because the per-Object3D APIs act on
owned state the renderer already manages — no external scene references need
threading.
"""

import hashlib
import logging
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
from nicegui import app, ui, run
from nicegui.events import SceneClipPlane

import waldoctl
from waldoctl import EnvelopeMode

from waldo_commander.common.theme import SceneColors
from waldo_commander.state import simulation_state, robot_state


logger = logging.getLogger(__name__)


# Default sample count for envelope generation (used to find max reach)
# 500k samples → ~9 samples per joint (9^6 = 531441 grid points)
DEFAULT_ENVELOPE_SAMPLES = 500000

# Envelope proximity clipping configuration
ENVELOPE_CAP_DEPTH = 0.08  # 80mm visible cap depth on the sphere
ENVELOPE_PROXIMITY_THRESHOLD = 0.10  # 100mm from boundary triggers display

# Cache directory and file paths
CACHE_DIR = Path.home() / ".waldo-commander"
HULL_STL_FILENAME = "workspace_hull.stl"
HULL_STL_PATH = CACHE_DIR / HULL_STL_FILENAME

# Storage key for hull cache metadata
STORAGE_KEY = "workspace_hull_cache"


# -----------------------------------------------------------------------------
# WorkspaceEnvelope class (generates convex hull with caching)
# -----------------------------------------------------------------------------


def _compute_cache_key(
    tool_offset_z: float,
    joint_limits_rad: np.ndarray,
    urdf_path: str,
) -> str:
    """Compute cache key from joint limits, tool offset, and URDF content.

    All parameters are required — callers must supply them explicitly.
    Raises on bad input instead of returning a fallback key.
    """
    data = np.asarray(joint_limits_rad).tobytes() + np.array([tool_offset_z]).tobytes()
    urdf_bytes = Path(urdf_path).read_bytes()
    data += hashlib.md5(urdf_bytes).digest()
    return hashlib.md5(data).hexdigest()[:12]


def _save_hull_as_stl(vertices: np.ndarray, faces: np.ndarray, path: Path) -> bool:
    """Save convex hull as STL file.

    Args:
        vertices: Hull vertex positions, shape (N, 3)
        faces: Triangle face indices, shape (F, 3)
        path: Output STL file path

    Returns:
        True if successful
    """
    try:
        from stl import mesh as stl_mesh

        # Ensure cache directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Create mesh from hull data using vectorized indexing
        hull_mesh = stl_mesh.Mesh(np.zeros(len(faces), dtype=stl_mesh.Mesh.dtype))
        hull_mesh.vectors = vertices[faces]  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

        hull_mesh.save(str(path))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        logger.info("Saved workspace hull STL to %s", path)
        return True
    except Exception as e:
        logger.error("Failed to save hull STL: %s", e)
        return False


class WorkspaceEnvelope:
    """Calculates workspace envelope using convex hull with STL caching."""

    def __init__(self):
        self.max_reach: float = 0.0  # Maximum reach radius in meters
        self.stl_url: str = ""  # URL to serve the STL file
        self._generated = False
        self._generating = False
        self._current_tool_offset_z: float = 0.0
        self._pending_tool_offset: float | None = None
        # Stored from _get_hull_params() so cache key computation never reads ui_state
        self._urdf_path: str | None = None
        self._joint_limits_rad: np.ndarray | None = None

    @property
    def is_ready(self) -> bool:
        """Whether the envelope has been generated and is available."""
        return self._generated

    @property
    def is_generating(self) -> bool:
        """Whether envelope generation is currently in progress."""
        return self._generating

    def _get_hull_params(self) -> tuple[str | None, list | None]:
        """Get URDF path and joint limits from the active robot.

        Also stores them on the instance for subsequent cache key computations.
        This is the ONLY method that reads ui_state.
        """
        from waldo_commander.state import ui_state

        if ui_state.robot is None:
            return None, None
        urdf_path = ui_state.active_robot.urdf_path
        joint_limits_rad = ui_state.active_robot.joints.limits.position.rad
        # Store for cache key use — avoids TOCTOU race
        self._urdf_path = urdf_path
        self._joint_limits_rad = joint_limits_rad
        return urdf_path, joint_limits_rad.tolist()

    def _ensure_static_files_registered(self) -> None:
        """Register static files directory for serving STL.

        Note: We always attempt registration because NiceGUI test framework
        resets the app between tests, clearing routes but not our flag.
        """
        if not CACHE_DIR.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            app.add_static_files("/waldo-commander-cache", str(CACHE_DIR))
            logger.debug(
                "Registered static files: /waldo-commander-cache -> %s", CACHE_DIR
            )
        except ValueError:
            # Route already registered (NiceGUI raises ValueError for duplicates)
            pass

    def _get_stl_url(self) -> str:
        """Get URL for the cached STL file."""
        return f"/waldo-commander-cache/{HULL_STL_FILENAME}"

    def _load_from_cache(self, tool_offset_z: float) -> bool:
        """Try to load hull from cache.

        Requires _get_hull_params() to have been called first (stores
        _urdf_path and _joint_limits_rad on the instance).

        Args:
            tool_offset_z: Current tool Z offset in meters

        Returns:
            True if cache was valid and loaded
        """
        if self._urdf_path is None or self._joint_limits_rad is None:
            logger.debug("No hull params stored, cannot check cache")
            return False

        try:
            cache = app.storage.general.get(STORAGE_KEY)
            if not cache:
                logger.debug("No hull cache found")
                return False

            current_key = _compute_cache_key(
                tool_offset_z, self._joint_limits_rad, self._urdf_path
            )
            if cache.get("cache_key") != current_key:
                logger.info(
                    "Hull cache key mismatch (cached=%s, current=%s), will regenerate",
                    cache.get("cache_key"),
                    current_key,
                )
                return False

            if not HULL_STL_PATH.exists():
                logger.info("Hull STL file missing, will regenerate")
                return False

            self.max_reach = cache.get("max_reach", 0.0)
            self._current_tool_offset_z = tool_offset_z
            self._ensure_static_files_registered()
            self.stl_url = self._get_stl_url()
            self._generated = True

            logger.info(
                "Loaded workspace hull from cache: max_reach=%.4fm", self.max_reach
            )
            return True
        except Exception as e:
            logger.warning("Failed to load hull from cache: %s", e)
            return False

    def _save_to_cache(self, max_reach: float, tool_offset_z: float) -> None:
        """Save hull metadata to cache. Uses stored params from _get_hull_params()."""
        if self._urdf_path is None or self._joint_limits_rad is None:
            logger.warning("Cannot save cache: hull params not stored")
            return
        try:
            cache_key = _compute_cache_key(
                tool_offset_z, self._joint_limits_rad, self._urdf_path
            )
            app.storage.general[STORAGE_KEY] = {
                "cache_key": cache_key,
                "max_reach": max_reach,
                "tool_offset_z": tool_offset_z,
            }
            logger.debug("Saved hull cache metadata: key=%s", cache_key)
        except Exception as e:
            logger.warning("Failed to save hull cache: %s", e)

    def _process_hull_result(
        self, result: dict[str, Any] | None, tool_offset_z: float
    ) -> bool:
        """Process hull generation result: save STL, register static files, update cache.

        Args:
            result: Dict with max_reach, vertices, faces from _generate_hull_cpu_bound,
                    or None if generation failed.
            tool_offset_z: Tool TCP Z offset in meters.

        Returns:
            True if hull was saved and registered successfully.
        """
        if result is None:
            logger.warning("Workspace generation returned no data")
            return False

        self.max_reach = result["max_reach"]
        vertices = np.array(result["vertices"])
        faces = np.array(result["faces"])

        if not _save_hull_as_stl(vertices, faces, HULL_STL_PATH):
            logger.warning("Failed to save hull STL")
            return False

        self._ensure_static_files_registered()
        self.stl_url = self._get_stl_url()
        self._save_to_cache(self.max_reach, tool_offset_z)
        self._generated = True
        logger.info(
            "Workspace hull generation complete: max_reach=%.4fm, %d triangles",
            self.max_reach,
            len(faces),
        )
        return True

    def generate(
        self,
        samples: int = DEFAULT_ENVELOPE_SAMPLES,
        tool_offset_z: float = 0.0,
    ) -> bool:
        """Start workspace generation in background (non-blocking).

        Checks cache first, only regenerates if needed.

        Args:
            samples: Number of random configurations to sample
            tool_offset_z: Tool TCP Z offset in meters

        Returns:
            True if generation started or already complete
        """
        # Resolve hull params once (stores on instance for cache key use)
        urdf_path, joint_limits_rad = self._get_hull_params()
        if urdf_path is None or joint_limits_rad is None:
            logger.warning("Cannot generate hull: no robot loaded")
            return False

        # Try loading from cache first
        if self._load_from_cache(tool_offset_z):
            simulation_state.notify_changed()
            return True

        if self._generated:
            return True

        if self._generating:
            logger.info("Workspace generation already in progress")
            return True

        # Skip actual generation in tests (WALDO_SKIP_ENVELOPE=1)
        # Check after _generated/_generating so tests can still verify those code paths
        if os.environ.get("WALDO_SKIP_ENVELOPE"):
            logger.debug("Skipping workspace hull generation (WALDO_SKIP_ENVELOPE)")
            return False

        self._generating = True
        self._current_tool_offset_z = tool_offset_z
        logger.info(
            "Starting background workspace hull generation with %d samples...", samples
        )

        async def start_background_generation():
            result = None
            try:
                result = await run.cpu_bound(
                    _generate_hull_cpu_bound,
                    samples,
                    tool_offset_z,
                    urdf_path,
                    joint_limits_rad,
                )
            except Exception as e:
                # Fallback to sync in-process generation when subprocess fails
                # (common in test environments where process pool is unavailable)
                logger.warning("Subprocess hull generation failed (%s), using sync", e)
                try:
                    result = _generate_hull_cpu_bound(
                        samples,
                        tool_offset_z,
                        urdf_path,
                        joint_limits_rad,
                    )
                except Exception as e2:
                    logger.error("Sync hull generation also failed: %s", e2)

            try:
                if self._process_hull_result(result, tool_offset_z):
                    simulation_state.notify_changed()
            finally:
                self._generating = False
                # Check for pending tool offset change queued during generation
                pending = self._pending_tool_offset
                if pending is not None:
                    self._pending_tool_offset = None
                    self.reset()
                    self.generate(tool_offset_z=pending)

        ui.timer(0.0, start_background_generation, once=True)
        return True

    def generate_sync(
        self,
        samples: int = DEFAULT_ENVELOPE_SAMPLES,
        tool_offset_z: float = 0.0,
    ) -> bool:
        """Generate workspace data synchronously (blocking).

        Use this for testing or when background execution isn't needed.

        Args:
            samples: Number of random configurations to sample
            tool_offset_z: Tool TCP Z offset in meters

        Returns:
            True if generation successful
        """
        # Resolve hull params once (stores on instance for cache key use)
        urdf_path, joint_limits_rad = self._get_hull_params()
        if urdf_path is None or joint_limits_rad is None:
            logger.warning("Cannot generate hull: no robot loaded")
            return False

        # Try loading from cache first
        if self._load_from_cache(tool_offset_z):
            simulation_state.notify_changed()
            return True

        if self._generated:
            return True

        if self._generating:
            logger.info("Workspace generation already in progress")
            return False

        self._generating = True
        self._current_tool_offset_z = tool_offset_z
        logger.info(
            "Generating workspace hull synchronously with %d samples...", samples
        )

        try:
            result = _generate_hull_cpu_bound(
                samples,
                tool_offset_z,
                urdf_path,
                joint_limits_rad,
            )
            return self._process_hull_result(result, tool_offset_z)
        except Exception as e:
            logger.error("Workspace generation failed: %s", e)
            return False
        finally:
            self._generating = False

    def reset(self) -> None:
        """Reset envelope data to allow regeneration."""
        self.max_reach = 0.0
        self.stl_url = ""
        self._generated = False
        self._generating = False
        self._urdf_path = None
        self._joint_limits_rad = None

    def invalidate_cache(self) -> None:
        """Invalidate cache and delete STL file."""
        try:
            if STORAGE_KEY in app.storage.general:
                del app.storage.general[STORAGE_KEY]
            if HULL_STL_PATH.exists():
                HULL_STL_PATH.unlink()
            logger.info("Invalidated workspace hull cache")
        except Exception as e:
            logger.warning("Failed to invalidate cache: %s", e)
        self.reset()

    def get_radius_with_tool_offset(self, tool_offset_z: float) -> float:
        """Get effective workspace radius including tool offset.

        Args:
            tool_offset_z: Tool Z offset in meters (can be negative)

        Returns:
            Effective radius = max_reach + abs(tool_offset_z)
        """
        return self.max_reach + abs(tool_offset_z)

    def needs_regeneration(self, tool_offset_z: float) -> bool:
        """Check if hull needs regeneration due to tool change.

        Args:
            tool_offset_z: New tool Z offset in meters

        Returns:
            True if regeneration needed
        """
        # If generation is in progress, queue for regeneration after completion
        if self._generating:
            if abs(tool_offset_z - self._current_tool_offset_z) > 1e-6:
                self._pending_tool_offset = tool_offset_z
            return False
        if not self._generated:
            return True
        # Refresh stored params from ui_state
        self._get_hull_params()
        if self._urdf_path is None or self._joint_limits_rad is None:
            return True
        current_key = _compute_cache_key(
            tool_offset_z, self._joint_limits_rad, self._urdf_path
        )
        cache = app.storage.general.get(STORAGE_KEY, {})
        return cache.get("cache_key") != current_key


def _generate_hull_cpu_bound(
    samples: int,
    tool_offset_z: float,
    urdf_path: str,
    joint_limits_rad: list,
) -> dict[str, Any] | None:
    """CPU-bound function to calculate workspace convex hull.

    This function runs in a separate process via run.cpu_bound.

    Args:
        samples: Number of random configurations to sample
        tool_offset_z: Tool TCP Z offset in meters
        urdf_path: Path to URDF file for creating pinokin Robot
        joint_limits_rad: Joint limits in radians, shape (num_joints, 2)

    Returns:
        Dict with max_reach, vertices, faces, or None if failed
    """
    # Configure logging in subprocess (each worker needs its own config)
    # basicConfig() won't work if handlers exist, so force-add one
    import logging as subprocess_logging
    import sys

    sub_logger = subprocess_logging.getLogger(__name__)
    if not sub_logger.handlers:
        handler = subprocess_logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            subprocess_logging.Formatter(
                "%(levelname)s %(name)s:%(filename)s:%(lineno)d %(message)s"
            )
        )
        sub_logger.addHandler(handler)
        sub_logger.setLevel(subprocess_logging.DEBUG)

    try:
        from scipy.spatial import ConvexHull
        from pinokin import Robot

        robot = Robot(urdf_path)
        limits_arr = np.array(joint_limits_rad)

        # Generate evenly spaced joint configurations using a grid
        num_joints = len(joint_limits_rad)
        samples_per_joint = max(2, int(round(samples ** (1 / num_joints))))
        actual_samples = samples_per_joint**num_joints
        sub_logger.info(
            "Using %d samples per joint (%d total grid points)",
            samples_per_joint,
            actual_samples,
        )

        joint_ranges = [
            np.linspace(limits_arr[i, 0], limits_arr[i, 1], samples_per_joint)
            for i in range(num_joints)
        ]

        grids = np.meshgrid(*joint_ranges, indexing="ij")
        q_samples = np.column_stack([g.ravel() for g in grids])

        # Calculate FK for each configuration using batch_fk
        T_list = robot.batch_fk(q_samples)  # list of 4x4 matrices

        # Extract positions (TCP positions)
        pos = np.array([T[:3, 3] for T in T_list])  # (N, 3)

        # Apply tool offset along Z axis of each TCP frame
        if tool_offset_z != 0:
            z_axes = np.array([T[:3, 2] for T in T_list])  # (N, 3)
            pos += z_axes * tool_offset_z

        # Calculate distances from origin (robot base)
        distances = np.linalg.norm(pos, axis=1)
        max_reach = float(distances.max())

        # Compute convex hull
        try:
            hull = ConvexHull(pos)
            hull_verts = pos[hull.vertices]
            idx_map = np.empty(len(pos), dtype=np.intp)
            idx_map[hull.vertices] = np.arange(len(hull.vertices))
            faces = idx_map[hull.simplices].tolist()
            vertices = hull_verts.tolist()

            sub_logger.info(
                "Convex hull: %d hull vertices, %d faces, max_reach=%.4fm",
                len(hull.vertices),
                len(faces),
                max_reach,
            )

            return {
                "max_reach": max_reach,
                "vertices": vertices,
                "faces": faces,
            }
        except Exception as e:
            sub_logger.error("ConvexHull computation failed: %s", e)
            # Fall back to just max_reach without hull
            return None

    except ImportError as e:
        sub_logger.error("Failed to import in CPU-bound task: %s", e)
        return None
    except Exception as e:
        sub_logger.error("Workspace generation failed in CPU-bound task: %s", e)
        return None


# Singleton instance
workspace_envelope = WorkspaceEnvelope()


# -----------------------------------------------------------------------------
# EnvelopeRenderer mixin
# -----------------------------------------------------------------------------


class EnvelopeRenderer:
    """Renderer mixin providing envelope visualization for UrdfScene.

    Kept as a mixin (not full composition) because the per-Object3D APIs
    (envelope_object.set_clipping_planes etc.) act on owned state that the
    renderer already manages — no external scene references need threading.
    """

    # These attributes are defined in the main UrdfScene class
    scene: Any
    simulation_group: Any

    def _init_envelope_state(self) -> None:
        """Initialize envelope state variables."""
        self.envelope_object: Any | None = None

        # Track envelope visibility state to avoid redundant visible() calls
        self._envelope_visible: bool = False

        # Track current tool offset for envelope calculations
        self._current_tool: str = "none"
        self._current_tool_offset_z: float = 0.0

    @staticmethod
    def _is_near_boundary(x_m: float, y_m: float, z_m: float) -> bool:
        """Check if a point (in meters) is within proximity threshold of the workspace boundary."""
        if not workspace_envelope.is_ready or workspace_envelope.max_reach <= 0:
            return False
        dist = math.sqrt(x_m * x_m + y_m * y_m + z_m * z_m)
        return dist >= workspace_envelope.max_reach - ENVELOPE_PROXIMITY_THRESHOLD

    def _create_envelope_object(self) -> bool:
        """Create the envelope hull STL object if not already created.

        Returns:
            True if envelope object exists (created or already existed)
        """
        if self.envelope_object:
            return True
        if not workspace_envelope.stl_url:
            return False
        try:
            with self.simulation_group:
                self.envelope_object = ui.scene.stl(
                    workspace_envelope.stl_url, wireframe=True
                ).with_name("envelope:hull")
                self.envelope_object.material(SceneColors.ENVELOPE_HEX, 0.8)
            self._envelope_visible = True
            return True
        except Exception as e:
            logger.error("Failed to create envelope hull: %s", e)
            return False

    def _update_envelope_from_robot_state(self) -> None:
        """Update envelope visibility based on mode and robot TCP position.

        For OFF/ON modes, only acts on mode transitions (not every tick).
        For AUTO, checks TCP proximity to the boundary each tick.
        """
        mode = waldoctl.commander.settings.view.envelope_mode

        if mode is EnvelopeMode.OFF:
            if self._envelope_visible:
                self._hide_envelope()
            return

        if mode is EnvelopeMode.ON:
            if not self._envelope_visible:
                self._show_envelope(clipped=False)
            return

        # Auto — show with proximity clipping when near the boundary
        tcp = (robot_state.x / 1000.0, robot_state.y / 1000.0, robot_state.z / 1000.0)
        if self._is_near_boundary(*tcp):
            self._show_envelope(clipped=True, approaching_positions=[tcp])
        else:
            self._hide_envelope()

    def _show_envelope(
        self,
        *,
        clipped: bool,
        approaching_positions: list[tuple[float, float, float]] | None = None,
    ) -> None:
        """Ensure envelope is created, visible, and optionally clipped."""
        if not workspace_envelope.is_ready:
            workspace_envelope.generate(tool_offset_z=self._current_tool_offset_z)
        if not self.envelope_object and workspace_envelope.is_ready:
            self._create_envelope_object()
        elif self.envelope_object and not self._envelope_visible:
            self.envelope_object.visible(True)
            self._envelope_visible = True

        if self.envelope_object and self.scene:
            if clipped and approaching_positions:
                planes = self._calculate_envelope_clipping_planes(
                    approaching_positions, workspace_envelope.max_reach
                )
                if planes:
                    self.envelope_object.set_clipping_planes(planes)
            else:
                self.envelope_object.clear_clipping_planes()

    def _hide_envelope(self) -> None:
        """Hide envelope and clear clipping planes."""
        if self.envelope_object and self._envelope_visible:
            self.envelope_object.visible(False)
            self._envelope_visible = False
            self.envelope_object.clear_clipping_planes()

    def _calculate_envelope_clipping_planes(
        self,
        approaching_positions: list[tuple[float, float, float]],
        max_reach: float,
    ) -> list[SceneClipPlane]:
        """Calculate clipping planes to show only nearby portions of the envelope.

        For each approaching object, creates a clipping plane that reveals a
        cap of the envelope near that object.

        Args:
            approaching_positions: List of (x, y, z) positions approaching boundary
            max_reach: Maximum reach radius of the envelope

        Returns:
            List of plane definitions for set_clipping_planes()
        """
        planes: list[SceneClipPlane] = []

        for pos in approaching_positions:
            x, y, z = pos
            dist = math.sqrt(x * x + y * y + z * z)

            if dist < 1e-6:
                # Object at origin - can't determine direction, skip
                continue

            # Normalize to get direction from origin to object
            nx = x / dist
            ny = y / dist
            nz = z / dist

            # Calculate plane distance to show a cap
            # How close to boundary (0 = at boundary, positive = inside)
            distance_to_boundary = max_reach - dist

            # Adaptive cap depth: larger cap when closer to boundary
            if distance_to_boundary < 0:
                # Beyond boundary - show full cap
                cap_depth = ENVELOPE_CAP_DEPTH
            elif distance_to_boundary < ENVELOPE_PROXIMITY_THRESHOLD:
                # Within threshold - scale cap depth proportionally
                proximity_ratio = 1.0 - (
                    distance_to_boundary / ENVELOPE_PROXIMITY_THRESHOLD
                )
                cap_depth = ENVELOPE_CAP_DEPTH * proximity_ratio
            else:
                # Too far from boundary - no cap needed
                continue

            # Plane distance
            plane_d = -(max_reach - cap_depth)

            planes.append(SceneClipPlane(nx=nx, ny=ny, nz=nz, d=plane_d))

        return planes

    def _update_envelope_for_tool_change(self, tool_offset_z: float) -> None:
        """Handle tool offset change - regenerate hull if needed.

        Args:
            tool_offset_z: New tool Z offset in meters
        """
        if workspace_envelope.needs_regeneration(tool_offset_z):
            logger.info(
                "Tool offset changed to %.4fm, regenerating workspace hull",
                tool_offset_z,
            )
            # Delete current envelope object
            if self.envelope_object:
                try:
                    self.envelope_object.delete()
                except Exception as e:
                    logger.debug("Envelope cleanup: %s", e)
                self.envelope_object = None
                self._envelope_visible = False

            # Reset and regenerate
            workspace_envelope.reset()
            workspace_envelope.generate(tool_offset_z=tool_offset_z)

    def _update_envelope_radius(self) -> None:
        """Update envelope for current tool offset.

        Called when tool selection changes.
        """
        self._update_envelope_for_tool_change(self._current_tool_offset_z)
