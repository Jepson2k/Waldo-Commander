"""
UrdfScene - Main class integrating editing/TCP/envelope mixins and a path renderer.

This implementation is based on the original MIT-licensed urdf_scene_nicegui project.
Attribution:
- Original authors of 'urdf_scene_nicegui' (MIT License)
- Source idea and structure adapted to integrate with NiceGUI scene and PAROL Commander

This file incorporates extensions:
- Cartesian gizmo (translate/rotate) with press/hold streaming callbacks
- Robust mesh mounting and STL URL mapping
- Visual frame parenting (WRF/TRF) with TCP anchoring and tool-offset support
- Generalized tool pose handling via injection (map/resolver)
"""

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence
from urllib.parse import urlparse
from urllib.request import url2pathname

import numpy as np
from nicegui import ui, app
from nicegui.elements.scene.scene_object3d import Object3D

import waldoctl
from waldoctl import Box, LinearMotion, RotaryMotion, MeshRole, PartMotion

from waldo_commander.common.logging_config import TRACE_ENABLED, TraceLogger
from waldo_commander.common.theme import (
    PathColors,
    SceneColors,
    get_color_for_move_type,
)
from waldo_commander.constants import WAYPOINT_SIZE_LARGE, WAYPOINT_SIZE_SMALL
from waldo_commander.services.programs import active_cursor_line
from waldo_commander.services.timeline import ObjectSample
from waldo_commander.services.urdf_scene.scene_batch import batch_scene
from waldo_commander.state import simulation_state, robot_state, ui_state

from .config import RobotAppearanceMode, ToolPose, UrdfSceneConfig
from .loader import (
    load_urdf,
    resolve_meshes_dir,
    get_transl_and_rpy,
    rot_joint,
    transl_joint,
    normalize_axis,
)
from .editing_mixin import EditingMixin
from .tcp_controls_mixin import TCPControlsMixin
from .envelope_renderer import EnvelopeRenderer
from .path_renderer import PathRenderer

logger: TraceLogger = logging.getLogger(__name__)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

SHAPE_OPACITY = 0.35
#: Render extent of unbounded geometry — the installation floor and a
#: ``plane`` keep-out — as a square of this side [m]. Backends enforce the
#: floor as a keep-out of this footprint and a plane as a half-space, so the
#: drawing is the enforced floor and a window onto a plane.
WORLD_EXTENT_M = 6.0
#: Thickness of the slab that stands in for a surface [m].
SURFACE_THICKNESS_M = 0.002
#: The installation floor is ground, not a keep-out: drawn as solid as the
#: placeholder disc it replaces, not as translucent as a shape.
FLOOR_OPACITY = 0.5

# three.js cylinders/cones extend along +Y; coal's primitives are Z-aligned.
# Rx(+90°) maps +Y -> +Z so the drawn shape matches the enforced volume.
_Y_TO_Z_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])


def _z_align_rotation(n: np.ndarray) -> np.ndarray:
    """Rotation taking +Z to the unit vector ``n`` (Rodrigues; 180° safe)."""
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, n)
    c = float(np.dot(z, n))
    s2 = float(np.dot(v, v))
    if s2 < 1e-24:
        return np.eye(3) if c > 0.0 else np.diag([1.0, -1.0, -1.0])
    k = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.eye(3) + k + k @ k * ((1.0 - c) / s2)


def _base_opacity(key: str) -> float:
    """A drawn shape's resting opacity: the floor is ground, everything
    else a keep-out or marker."""
    return FLOOR_OPACITY if key == "install:floor" else SHAPE_OPACITY


def _object_render_pose(
    kind: str, pose7: Sequence[float]
) -> tuple[tuple[float, float, float], list[list[float]]]:
    """Scene position + rotation for a tracked object's ``[x, y, z, qw, qx,
    qy, qz]`` pose, with the same Y-up correction its declared pose gets."""
    w, x, y, z = pose7[3:7]
    R = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )
    if kind in ("cylinder", "capsule", "cone"):
        R = R @ _Y_TO_Z_UP
    return (float(pose7[0]), float(pose7[1]), float(pose7[2])), R.tolist()


def _shape_render_pose(s) -> tuple[tuple[float, float, float], list[list[float]]]:
    """World position + rotation matrix for a shape's scene object.

    Corrects the Y-up axis for cylinder/capsule/cone and places a plane's slab
    on its halfspace surface (``n·x = offset`` in the shape's local frame,
    composed with the pose), so the drawn surface is the enforced one. The
    slab is a finite window onto an unbounded half-space, and a shape's
    ``margin`` is enforced but never drawn.
    """
    R_pose = np.array(
        Object3D.rotation_matrix_from_euler(s.pose[3], s.pose[4], s.pose[5])
    )
    pos = np.asarray(s.pose[:3], dtype=np.float64)
    if s.kind in ("cylinder", "capsule", "cone"):
        R = R_pose @ _Y_TO_Z_UP
    elif s.kind == "plane":
        # The slab is a thin box whose local z is its normal — align it to n.
        # coal normalizes Halfspace(n, d) to (n/|n|, d/|n|), so the enforced
        # surface sits at offset/|n| along n̂ — scale the offset to match.
        n = np.array([s.nx, s.ny, s.nz], dtype=np.float64)
        norm = float(np.linalg.norm(n))
        if norm > 1e-12:
            n = n / norm
            surface_offset = s.offset / norm
        else:
            n = np.array([0.0, 0.0, 1.0])
            surface_offset = s.offset
        R = R_pose @ _z_align_rotation(n)
        pos = pos + R_pose @ (n * surface_offset)
    else:
        R = R_pose
    return (float(pos[0]), float(pos[1]), float(pos[2])), [
        [float(v) for v in row] for row in R
    ]


def _lerp_hex(c1: tuple[int, int, int], c2: tuple[int, int, int], factor: float) -> str:
    """Linearly interpolate two RGB colors and return a hex string."""
    r = int(c1[0] + (c2[0] - c1[0]) * factor + 0.5)
    g = int(c1[1] + (c2[1] - c1[1]) * factor + 0.5)
    b = int(c1[2] + (c2[2] - c1[2]) * factor + 0.5)
    return f"#{r:02x}{g:02x}{b:02x}"


class RenderedSegment(NamedTuple):
    """Immutable record of a rendered path segment's scene objects and identity."""

    objects: list[Any]  # polyline + cones
    colors: list[str]
    uses_vc: bool  # polyline uses vertex colors?
    fingerprint: tuple  # visual properties only — line_number excluded
    line_number: int  # source line, for cursor highlight map


class RenderedItem(NamedTuple):
    """Immutable record of a rendered scene element (tool action or waypoint)."""

    objects: list[Any]
    fingerprint: tuple
    segment_index: int  # for playback opacity (-1 if N/A)


def _segment_fingerprint(
    segments: Sequence,
    seg_index: int,
    blend_range: int,
) -> tuple:
    """Compute a rendering fingerprint for a path segment.

    Includes neighbor validity within blend range so gradient colors
    update correctly when a nearby segment changes validity.
    ``line_number`` is excluded -- it doesn't affect visual output.
    """
    seg = segments[seg_index]
    pts = seg.points
    n = len(segments)
    neighbor_valid: list[bool] = []
    for d in range(1, blend_range + 1):
        before = seg_index - d
        after = seg_index + d
        neighbor_valid.append(before >= 0 and before < n and segments[before].is_valid)
        neighbor_valid.append(after >= 0 and after < n and segments[after].is_valid)
    return (
        tuple(pts[0]) if pts else (),
        tuple(pts[-1]) if pts else (),
        len(pts),
        seg.color,
        seg.is_dashed,
        seg.show_arrows,
        seg.is_valid,
        tuple(neighbor_valid),
    )


def _tool_action_fingerprint(action) -> tuple:
    """Compute a rendering fingerprint for a tool action."""
    return (
        tuple(action.tcp_pose or ()),
        action.target_positions,
        action.start_positions,
        tuple(tuple(sorted(m.items())) for m in action.motions),
        len(action.tcp_path or []),
    )


def _create_waypoint_marker(shape: str, size: float, color: str) -> Any:
    """Create a waypoint marker: diamond (lathe bicone), square (box), or sphere."""
    if shape == "diamond":
        # Bicone: a diamond profile revolved around the Y axis.
        r = size
        h = size * 1.5
        obj = ui.scene.lathe([[0, -h], [r, 0], [0, h]], segments=8)
        obj.material(color)
        return obj
    elif shape == "square":
        side = size * 2
        box = ui.scene.box(side, side, side)
        box.material(color)
        return box
    else:
        sphere = ui.scene.sphere(size)
        sphere.material(color)
        return sphere


class UrdfScene(
    EditingMixin,
    TCPControlsMixin,
    EnvelopeRenderer,
):
    """Load a URDF file as a NiceGUI Scene

    Core features:
    - Render URDF meshes (STL) using NiceGUI scene
    - Set individual/all joint axis values to animate the model
    - Add interactive Cartesian gizmo (translate arrows + rotation rings)
    - Visual parenting to WRF (world) or TRF (tool/end-effector) frame
    - TCP offset/orientation updates on tool change
    - Configurable tool pose handling via injection (no hard dependencies)
    """

    def __init__(self, path: str | Path, config: UrdfSceneConfig | None = None):
        """Load a URDF file to construct a nicegui scene.

        Args:
            path: Path to URDF file
            config: Optional configuration for scene behavior and dependencies
        """
        path = Path(path)
        self.config = config or UrdfSceneConfig()

        self.urdf_model = load_urdf(path, package_map=self.config.package_map)

        self.urdf_path = path  # for IK solver

        self.meshes_dir = resolve_meshes_dir(path, self.config.meshes_dir)
        self.meshes_url = f"{self.config.static_url_prefix}/{self.urdf_model.name}"
        self.joint_names = self.urdf_model.actuated_joint_names

        if self.config.mount_static:
            try:
                app.add_static_files(self.meshes_url, str(self.meshes_dir))
            except Exception as e:
                msg = str(e).lower()
                # Ignore duplicate registration across tests; re-raise other errors.
                if "already" in msg and "register" in msg:
                    logger.debug(
                        "Static files already registered for %s; continuing",
                        self.meshes_url,
                    )
                else:
                    raise

        # Scene-related state
        self.joint_groups: dict[str, Any] = {}
        self.joint_pos_limits: dict[str, dict[str, float | None]] = {}
        self.joint_trafos: dict = {}
        self.scene: Any | None = None
        # Pre-populate normalized joint axes from URDF so they're available before scene build.
        self.joint_axes: dict[str, np.ndarray] = {}
        for _name in self.joint_names:
            _j = next((jj for jj in self.urdf_model.joints if jj.name == _name), None)
            _raw = getattr(_j, "axis", None) if _j is not None else None
            try:
                self.joint_axes[_name] = normalize_axis(_raw)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning(
                    "Failed to normalize axis for joint '%s' in __init__: %s", _name, e
                )
                self.joint_axes[_name] = np.array([0.0, 0.0, 1.0], dtype=float)

        self._stl_scale: float = 1.0

        # TCP anchoring: created on end-link under last joint
        self.tcp_anchor: Any | None = None
        self.tcp_offset: Any | None = None

        # Simulation visualization
        self.simulation_group: Any | None = None
        self.path_group: Any | None = None
        self.targets_group: Any | None = None
        self._rendered_segments: list[RenderedSegment | None] = []  # indexed by segment
        self._line_to_segments: dict[
            int, list[int]
        ] = {}  # line_number -> segment indices
        self._rendered_tool_actions: list[RenderedItem | None] = []
        self._rendered_waypoints: list[RenderedItem | None] = []
        self._highlighted_line: int = 0
        self._rendered_playback_step: int = -1

        self._robot_meshes: list[ui.scene.stl] = []

        # Tool mesh state
        self._tool_meshes_group: Any | None = None
        self._tool_meshes: list[Any] = []
        self._tool_body_meshes: list[Any] = []  # static body parts (housing)
        self._tool_motions: list[PartMotion] = []  # driven by ToolSpec.motions
        self._tool_motion_meshes: dict[MeshRole, list[Any]] = {}  # role -> meshes
        self._tool_motion_origins: dict[
            MeshRole, list[tuple[float, float, float]]
        ] = {}  # role -> mesh origins
        self._tool_motion_rotations: dict[
            MeshRole, list[tuple[float, float, float]]
        ] = {}  # role -> mesh initial RPY
        self._tool_motion_last: tuple[float, ...] = ()  # last positions for dirty-check
        self._last_tool_engaged: bool | None = (
            None  # track engaged state for color changes
        )
        self._tool_has_motions: bool = (
            False  # whether current tool has motion descriptors
        )

        self._appearance_mode: RobotAppearanceMode = RobotAppearanceMode.LIVE

        # Collision highlight: name -> scene objects, so reported colliding
        # geometry (arm links, tool meshes, user shapes) can be tinted red.
        self._link_to_meshes: dict[str, list[Any]] = {}
        self._shape_objects: dict[str, Any] = {}
        self._ground_disc: Object3D | None = None
        # Per drawn shape: (geometry, pose, color) it was last rendered with.
        self._shape_fingerprints: dict[str, tuple] = {}
        # Declared render pose per drawn shape, so a playback override can be
        # undone without re-deriving it; and which overridden objects are
        # drawn as a guess (the preview's geometric fallback).
        self._shape_declared_pose: dict[
            str, tuple[tuple[float, float, float], list[list[float]]]
        ] = {}
        self._object_ghosts: dict[str, bool] = {}
        # Last render's draft flag — appearance repaints must keep an
        # unconfirmed program layer amber, not promote it to confirmed slate.
        self._shapes_draft: bool = False
        self._shapes_group: Any | None = None
        self._colliding_meshes: set[Any] = set()  # objects currently tinted red
        self._collision_saved: dict[
            int, tuple[Any, float]
        ] = {}  # id -> (color, opacity)
        self._last_collision_sig: tuple | None = None
        # EDITING pose whose client-side collision query is already reflected;
        # None forces a recompute (pose, geometry, or mode changed).
        self._editing_collision_q: tuple | None = None

        # Editing mode state
        n = len(self.joint_names)
        self._editing_angles: list[float] = [0.0] * n  # Joint angles during editing
        self._pre_edit_angles: list[float] = [
            0.0
        ] * n  # Saved angles to restore on exit
        self._editing_target_type: str = "cartesian"  # "cartesian" or "joint"
        self._joint_ring_touched: bool = False  # True if user rotated any joint ring

        self._scene_wrapper: Any | None = None  # hosts positioned overlays

        self._init_editing_state()
        self._init_shape_editing()
        self._init_tcp_controls_state()
        self._init_envelope_state()
        self.path_renderer = PathRenderer()

        # Event-driven updates on simulation state changes.
        simulation_state.add_change_listener(self._update_simulation_view)

    def cleanup(self) -> None:
        """Remove listeners registered by this scene."""
        simulation_state.remove_change_listener(self._update_simulation_view)

    def show(self, scale_stls: float = 1.0, material=None, background_color=None):
        """Plot a nicegui 3D scene from loaded URDF.

        Args:
            scale_stls: Scale factor for all STL files (e.g., 1e-1 if designed in mm)
            material: Color for the whole URDF (overrides mesh colors in STLs if defined)
            background_color: Scene background color (defaults to config value)
        """
        self._stl_scale = float(scale_stls)
        if background_color is None:
            background_color = self.config.background_color
        # Wrapper hosts the context menu and edit bar overlays.
        self._scene_wrapper = ui.element("div").classes("relative w-full h-full")
        with self._scene_wrapper:
            self.context_menu = ui.context_menu()
            # Clear on hide so it doesn't auto-show with stale content.
            self.context_menu.on("hide", lambda: self.context_menu.clear())
            # Polar grid sized to robot's approximate workspace (~536mm reach).
            default_radius = 0.55  # meters
            with (
                ui.scene(
                    grid=False,
                    polar_grid=(default_radius, 12, 6),  # (radius, sectors, rings)
                    raycaster_threshold=0.005,
                    background_color=background_color,
                    # White ~0.2-opacity halo at 1.5x footprint, matching the
                    # original feature-branch hoverable() visuals.
                    hover_color="#ffffff",
                    hover_opacity=0.2,
                    hover_scale=1.5,
                    on_click=self._handle_scene_click,
                    click_events=[
                        "mousedown",
                        "mouseup",
                        "mouseleave",
                        "contextmenu",
                    ],
                )
                .classes("w-full h-[66vh]")
                .on_transform_end(self._handle_transform_event) as self.scene
            ):
                # Placeholder ground for contrast with the background, shown
                # until a backend reports where its installation floor is.
                self._ground_disc = (
                    ui.scene.cylinder(
                        default_radius, default_radius, 0.001, radial_segments=64
                    )
                    .material(self.config.ground_color, opacity=0.5)
                    .rotate(math.pi / 2, 0, 0)
                )

                self._plot_stls(
                    self.urdf_model.base_link, scale=self._stl_scale, material=material
                )
                next_joints = self._get_next_joints(
                    self.urdf_model, self.urdf_model.base_link
                )
                for joint in next_joints:
                    self._recursively_add_subtree(
                        self.urdf_model,
                        joint,
                        scale_stls=self._stl_scale,
                        material=material,
                    )

                with ui.scene.group().with_name("simulation:root") as sim_grp:
                    self.simulation_group = sim_grp
                    with ui.scene.group().with_name("simulation:paths") as path_grp:
                        self.path_group = path_grp
                    with ui.scene.group().with_name(
                        "simulation:targets"
                    ) as targets_grp:
                        self.targets_group = targets_grp

            # Orientation inset (axes gizmo).
            try:
                if self.scene:
                    self.scene.set_axes_inset(
                        enabled=True,
                        anchor="bottom-left",
                        margin_x=48,
                        margin_y=-12,
                    )
                    self.scene.set_axes_labels(enabled=True)
            except Exception as e:
                logger.debug("set_axes_inset configuration failed: %s", e)

            # ESC deselects TransformControls.
            ui.keyboard(on_key=self._handle_keyboard)

            self.scene.on("ik_solved", self._on_ik_solved)
            # on_transform_start gives properly typed SceneTransformEventArguments.
            self.scene.on_transform_start(self._handle_transform_start)
            # Continuous events drive live ghost robot updates.
            self.scene.on_transform(self._handle_transform_continuous)

    def _handle_transform_continuous(self, e) -> None:
        """Handle continuous transform events for TCP ball and joint controls.

        This handler receives transform events during drag (not just at end)
        and is used for TCP ball movement (jogging or IK) and joint ring rotation.
        Does NOT handle pose targets - those use on_transform_end only.
        """
        object_name = getattr(e, "object_name", "") or ""

        # Unified TCP ball: behavior depends on appearance mode (jogging vs IK).
        if object_name in ("tcp:ball", "tcp:jog_ball", "tcp:offset"):
            self._handle_tcp_transform_for_jog(e)
            return

        if object_name.startswith("edit_joint_group:"):
            self._on_joint_group_transform(e)
            return

    def _handle_transform_start(self, e) -> None:
        """Handle TransformControls transform_start events to manage orbit and mutex."""
        object_name = getattr(e, "object_name", "") or ""
        if object_name in ("tcp:ball", "ghost:tcp_ball"):
            # Disable orbit controls for the duration of the TCP drag.
            if self.scene:
                self.scene.set_orbit_enabled(False)
            self._tcp_ball_dragging = True
            if self._appearance_mode == RobotAppearanceMode.EDITING:
                # Suspend joint controls during TCP ball manipulation in editing mode.
                if not self._joint_controls_suspended:
                    self._disable_joint_transform_controls()
                    self._joint_controls_suspended = True
            else:
                # Jogging mode: capture starting rotation (used by translate-only
                # cartesian streaming to keep rotation fixed) and notify the consumer
                # of a drag start. Must happen here, not on the first on_transform
                # event — on_transform_start already set _tcp_ball_dragging, so the
                # legacy "first event" detection inside _handle_tcp_transform_for_jog
                # never triggers.
                self._tcp_drag_start_rot_deg = tuple(robot_state.orientation.deg)
                cb = self._tcp_cartesian_move_start_callback
                if cb is not None:
                    try:
                        cb()
                    except Exception as err:
                        logger.error("TCP cartesian move start callback error: %s", err)
        elif object_name == "tcp:jog_ball":
            self._tcp_ball_dragging = True

    def _handle_transform_event(self, e) -> None:
        """Handle TransformControls transform_end events for targets.

        This handler fires only at the END of a drag operation.
        TCP ball and joint transforms are handled by _handle_transform_continuous for live updates.
        """
        object_name = getattr(e, "object_name", "") or ""
        event_type = getattr(e, "type", "")

        # Unified TCP ball: on transform_end re-enable orbit and joint controls.
        if object_name in ("tcp:ball", "ghost:tcp_ball"):
            if event_type == "transform_end":
                self._tcp_ball_dragging = False
                self._tcp_drag_start_rot_deg = None
                if self.scene:
                    self.scene.set_orbit_enabled(True)
                if self._joint_controls_suspended:
                    self._enable_joint_transform_controls()
                    self._joint_controls_suspended = False
                # Editing mode: snap ball to FK in case IK failed and it was
                # dragged to an unreachable spot.
                if self._appearance_mode == RobotAppearanceMode.EDITING:
                    self._snap_tcp_to_fk()
                else:
                    cb = getattr(self, "_tcp_cartesian_move_end_callback", None)
                    if callable(cb):
                        try:
                            cb()
                        except Exception as err:
                            logger.error(
                                "TCP cartesian move end callback error: %s", err
                            )
            return

        # Legacy jog ball names.
        if object_name in ("tcp:jog_ball", "tcp:offset"):
            if event_type == "transform_end":
                self._tcp_ball_dragging = False
                self._tcp_drag_start_rot_deg = None
                cb = getattr(self, "_tcp_cartesian_move_end_callback", None)
                if callable(cb):
                    try:
                        cb()
                    except Exception as err:
                        logger.error("TCP cartesian move end callback error: %s", err)
            return

        # Joint rings are handled by the continuous handler.
        if object_name.startswith("ghost_ring_group:"):
            return

        if object_name.startswith("shape:"):
            self._on_shape_transform(e)
            return

        if object_name.startswith("targetgroup:"):
            target_id = object_name.split("targetgroup:", 1)[1]
            target = self._find_target_by_id(target_id)
            if target:
                # Position only updates in translate mode (else None).
                if e.x is not None:
                    target.pose[0] = e.x
                if e.y is not None:
                    target.pose[1] = e.y
                if e.z is not None:
                    target.pose[2] = e.z

                # Rotation only updates in rotate mode (else None).
                if len(target.pose) >= 6:
                    if e.rx is not None:
                        target.pose[3] = e.rx
                    if e.ry is not None:
                        target.pose[4] = e.ry
                    if e.rz is not None:
                        target.pose[5] = e.rz

                # Sync to editor only on transform_end to avoid update spam.
                if event_type == "transform_end":
                    clean_pose = [v if v is not None else 0.0 for v in target.pose]
                    ui_state.editor_panel.sync_code_from_target(target_id, clean_pose)

                    # Quick IK check to update target validity and color.
                    ik_result = self._ik_for_position(clean_pose[:3])
                    is_valid = ik_result is not None
                    if target.is_valid != is_valid:
                        target.is_valid = is_valid
                        new_color = (
                            PathColors.INVALID
                            if not is_valid
                            else get_color_for_move_type(target.move_type)
                        )
                        td = self._target_objects.get(target_id)
                        if td:
                            td["color"] = new_color
                            mk = td.get("marker")
                            if mk:
                                mk.material(new_color)

    @staticmethod
    def _screen_pos(evt) -> tuple[float, float]:
        """Extract screen position from event, falling back to client coords."""
        sx = getattr(evt, "screen_x", None)
        sy = getattr(evt, "screen_y", None)
        if sx is not None and sy is not None:
            return float(sx), float(sy)
        return float(getattr(evt, "client_x", 0)), float(getattr(evt, "client_y", 0))

    def _handle_scene_click(self, e) -> None:
        """Handle mouse events for target deselection, context menu, and joint target editing."""
        click_type = getattr(e, "click_type", "")
        hits = getattr(e, "hits", []) or []

        if click_type == "mousedown":
            clicked_transform_controls = False
            clicked_ghost_part = False

            for h in hits:
                name = getattr(h, "object_name", "") or ""
                object_id = getattr(h, "object_id", "") or ""

                if (
                    name.startswith("ghost:")
                    or name.startswith("ghost_ring_")
                    or name.startswith("ghost_joint_group:")
                ):
                    clicked_ghost_part = True
                    continue

                if object_id.startswith("transformcontrols:"):
                    clicked_transform_controls = True
                    continue

            # While editing a target, clicking away does NOT auto-confirm.
            if self._editing_unified_target:
                if not clicked_ghost_part and not clicked_transform_controls:
                    return

        elif click_type == "contextmenu":
            # Record position/event now; whether to populate (and thus show) the
            # menu is decided on mouseup based on drag distance.
            self._right_click_start_pos = self._screen_pos(e)
            self._pending_context_menu_event = e

        elif click_type == "mouseup":
            button = getattr(e, "button", 0)
            if button == 2 and self._right_click_start_pos is not None:
                screen_x, screen_y = self._screen_pos(e)
                start_x, start_y = self._right_click_start_pos
                distance = math.hypot(screen_x - start_x, screen_y - start_y)

                self._right_click_start_pos = None

                # A simple click populates (shows) the menu; a drag leaves it
                # empty so it stays hidden.
                if distance <= self._right_click_drag_threshold:
                    if self._pending_context_menu_event:
                        self._populate_context_menu(self._pending_context_menu_event)

                self._pending_context_menu_event = None

    def _update_simulation_view(self) -> None:
        """Update simulation visualization (paths, etc.) based on state."""
        if not self.scene or not self.simulation_group:
            return

        # This scene's client must still exist before we mutate it.
        if self.scene.is_deleted:
            return

        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed():
                return
        except RuntimeError:
            return

        # Tolerate client deletion racing scene mutation during shutdown.
        try:
            self._do_update_simulation_view()
        except RuntimeError as e:
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                return
            raise

    def _do_update_simulation_view(self) -> None:
        """Internal implementation of simulation view update.

        All scene mutations during one reconciliation cycle (segment rendering,
        tool-action markers, waypoint/target reconcile, playback opacity) get
        batched into a single WS frame via :func:`batch_scene`. A single path
        redraw with N segments × ~10 cones each can otherwise issue 100+
        separate WS messages — without batching the browser may render a
        partial scene mid-update (some markers reorganized, others still in
        old positions).
        """
        if not self.scene:
            return
        with batch_scene(self.scene):
            self._do_update_simulation_view_body()

    def _do_update_simulation_view_body(self) -> None:
        """Body of the simulation view update (run inside a batch_scene)."""
        if not waldoctl.commander.settings.view.paths_visible:
            if self.path_group is not None:
                self.path_group.visible(False)
            if self.targets_group is not None:
                self.targets_group.visible(False)
            return

        if self.path_group is not None:
            self.path_group.visible(True)
        if self.targets_group is not None:
            self.targets_group.visible(True)

        active = waldoctl.commander.programs.active
        if active is not None:
            all_segments = active.dry_run.path_segments
            tool_actions = active.dry_run.tool_actions
            targets = active.dry_run.targets
        else:
            all_segments = []
            tool_actions = []
            targets = []
        new_count = len(all_segments)
        old_count = len(self._rendered_segments)

        # Early-out: nothing to render and nothing rendered.
        if (
            new_count == 0
            and old_count == 0
            and not tool_actions
            and not self._rendered_tool_actions
            and not targets
            and not self._rendered_waypoints
        ):
            return

        if TRACE_ENABLED:
            logger.trace(
                "SCENE: _update_simulation_view tick - current_segments=%d, "
                "rendered_count=%d",
                new_count,
                old_count,
            )

        # --- Per-segment diff rendering ---
        first_cmd_idx = -1  # first non-travel segment
        last_cmd_idx = -1  # last non-travel segment

        if new_count == 0 and old_count > 0:
            if TRACE_ENABLED:
                logger.trace("SCENE: Clearing all segments (count went to 0)")
            self._clear_path_state()
        elif new_count > 0 and self.path_group and self.scene:
            blend_range = int(self._BLEND_RANGE)
            rebuild_line_map = False

            # Diff existing slots while tracking first/last non-travel segment.
            limit = min(new_count, old_count)
            for i in range(limit):
                segment = all_segments[i]
                if segment.is_travel:
                    fp: tuple = ()
                else:
                    fp = _segment_fingerprint(all_segments, i, blend_range)
                    if first_cmd_idx == -1:
                        first_cmd_idx = i
                    last_cmd_idx = i

                old_rs = self._rendered_segments[i]
                old_fp = old_rs.fingerprint if old_rs is not None else None
                old_ln = old_rs.line_number if old_rs is not None else -1

                if fp == old_fp:
                    # Same visuals — only the source line may have shifted.
                    if segment.line_number != old_ln and old_rs is not None:
                        self._rendered_segments[i] = old_rs._replace(
                            line_number=segment.line_number,
                        )
                        rebuild_line_map = True
                    continue

                # Fingerprint differs — delete old objects, render new.
                if old_rs is not None:
                    for obj in old_rs.objects:
                        self._safe_delete(obj)
                if segment.is_travel:
                    self._rendered_segments[i] = RenderedSegment(
                        objects=[],
                        colors=[],
                        uses_vc=False,
                        fingerprint=(),
                        line_number=segment.line_number,
                    )
                else:
                    self._rendered_segments[i] = self._render_and_record_segment(
                        all_segments,
                        i,
                        fp,
                        segment.line_number,
                    )
                rebuild_line_map = True
                if TRACE_ENABLED:
                    rs = self._rendered_segments[i]
                    logger.trace(
                        "SCENE: Re-rendered segment %d -> %d objects",
                        i,
                        len(rs.objects) if rs else 0,
                    )

            # Append new slots.
            if new_count > old_count:
                for i in range(old_count, new_count):
                    segment = all_segments[i]
                    if segment.is_travel:
                        self._rendered_segments.append(
                            RenderedSegment(
                                objects=[],
                                colors=[],
                                uses_vc=False,
                                fingerprint=(),
                                line_number=segment.line_number,
                            )
                        )
                    else:
                        fp = _segment_fingerprint(all_segments, i, blend_range)
                        self._rendered_segments.append(
                            self._render_and_record_segment(
                                all_segments,
                                i,
                                fp,
                                segment.line_number,
                            )
                        )
                        if first_cmd_idx == -1:
                            first_cmd_idx = i
                        last_cmd_idx = i
                    if TRACE_ENABLED:
                        rs = self._rendered_segments[-1]
                        logger.trace(
                            "SCENE: Appended segment %d -> %d objects",
                            i,
                            len(rs.objects) if rs else 0,
                        )
                rebuild_line_map = True

            # Remove trailing slots.
            if new_count < old_count:
                for i in range(new_count, old_count):
                    old_rs = self._rendered_segments[i]
                    if old_rs is not None:
                        for obj in old_rs.objects:
                            self._safe_delete(obj)
                del self._rendered_segments[new_count:]
                rebuild_line_map = True
                if TRACE_ENABLED:
                    logger.trace(
                        "SCENE: Removed trailing segments %d-%d",
                        new_count,
                        old_count - 1,
                    )

            if rebuild_line_map:
                self._rebuild_line_to_segments()

        # --- Per-item tool action diff rendering ---
        new_ta_count = len(tool_actions)
        old_ta_count = len(self._rendered_tool_actions)
        if new_ta_count > 0 or old_ta_count > 0:
            ta_limit = min(new_ta_count, old_ta_count)
            for i in range(ta_limit):
                action = tool_actions[i]
                fp = _tool_action_fingerprint(action)
                old_ri = self._rendered_tool_actions[i]
                old_fp = old_ri.fingerprint if old_ri is not None else None
                if fp == old_fp:
                    if (
                        old_ri is not None
                        and action.segment_index != old_ri.segment_index
                    ):
                        self._rendered_tool_actions[i] = old_ri._replace(
                            segment_index=action.segment_index,
                        )
                    continue
                # Fingerprint differs — delete old, render new.
                if old_ri is not None:
                    for obj in old_ri.objects:
                        self._safe_delete(obj)
                if self.path_group and self.scene:
                    with self.scene:
                        with self.path_group:
                            objs = self.path_renderer.render_tool_action(action)
                    self._rendered_tool_actions[i] = RenderedItem(
                        objects=objs,
                        fingerprint=fp,
                        segment_index=action.segment_index,
                    )
                else:
                    self._rendered_tool_actions[i] = None
            # Append new.
            if new_ta_count > old_ta_count:
                for i in range(old_ta_count, new_ta_count):
                    action = tool_actions[i]
                    fp = _tool_action_fingerprint(action)
                    if self.path_group and self.scene:
                        with self.scene:
                            with self.path_group:
                                objs = self.path_renderer.render_tool_action(action)
                        self._rendered_tool_actions.append(
                            RenderedItem(
                                objects=objs,
                                fingerprint=fp,
                                segment_index=action.segment_index,
                            )
                        )
                    else:
                        self._rendered_tool_actions.append(None)
            # Remove trailing.
            if new_ta_count < old_ta_count:
                for i in range(new_ta_count, old_ta_count):
                    old_ri = self._rendered_tool_actions[i]
                    if old_ri is not None:
                        for obj in old_ri.objects:
                            self._safe_delete(obj)
                del self._rendered_tool_actions[new_ta_count:]

        if waldoctl.commander.settings.view.paths_visible and self._rendered_segments:
            self.update_cursor_line_highlight()

        # Waypoint markers at segment endpoints.
        if self.targets_group and self.scene:
            self._update_waypoint_and_target_markers(
                all_segments,
                targets,
                first_cmd_idx,
                last_cmd_idx,
            )

        # Must run after all elements are rendered.
        self.update_playback_opacity()

    def _render_and_record_segment(
        self,
        all_segments: Sequence,
        seg_index: int,
        fingerprint: tuple,
        line_number: int,
    ) -> RenderedSegment:
        """Render a single path segment and return its RenderedSegment record.

        Callers must ensure ``self.scene`` and ``self.path_group`` are set.
        """
        assert self.path_group is not None
        with self.scene:
            with self.path_group:
                segment = all_segments[seg_index]
                pp_colors = self._gradient_colors(all_segments, seg_index)
                objs, obj_colors, uses_vc = self.path_renderer.render_path_segment(
                    segment,
                    pp_colors,
                )
        return RenderedSegment(
            objects=objs,
            colors=obj_colors,
            uses_vc=uses_vc,
            fingerprint=fingerprint,
            line_number=line_number,
        )

    def _rebuild_line_to_segments(self) -> None:
        """Rebuild the line_number -> segment indices lookup map."""
        mapping: dict[int, list[int]] = {}
        for i, rs in enumerate(self._rendered_segments):
            if rs is not None and rs.line_number > 0:
                mapping.setdefault(rs.line_number, []).append(i)
        self._line_to_segments = mapping

    def _update_waypoint_and_target_markers(
        self,
        segments: list,
        targets: list,
        first_cmd_idx: int,
        last_cmd_idx: int,
    ) -> None:
        """Update start/end waypoint markers and editable target shapes.

        Renders at most 2 non-editable markers (path start diamond, path end square)
        plus editable targets whose shape depends on position (start/end/intermediate).
        Uses pre-computed first/last non-travel segment indices from the diff loop.
        """
        # Derive first/last command positions from segment indices.
        first_line = last_line = -1
        first_pos: list[float] | None = None
        last_pos: list[float] | None = None
        first_color = last_color = ""
        first_seg_idx = -1
        last_seg_idx = -1

        if first_cmd_idx >= 0 and first_cmd_idx < len(segments):
            seg = segments[first_cmd_idx]
            if seg.points:
                first_line = seg.line_number
                first_pos = seg.points[-1]
                first_color = get_color_for_move_type(seg.move_type)
                first_seg_idx = first_cmd_idx
        if last_cmd_idx >= 0 and last_cmd_idx < len(segments):
            seg = segments[last_cmd_idx]
            if seg.points:
                last_line = seg.line_number
                last_pos = seg.points[-1]
                last_color = get_color_for_move_type(seg.move_type)
                last_seg_idx = last_cmd_idx

        target_by_line: dict[int, Any] = {}
        for t in targets:
            target_by_line.setdefault(t.line_number, t)

        def shape_for_line(line_no: int) -> str:
            if line_no == first_line:
                return "diamond"
            if line_no == last_line:
                return "square"
            return "sphere"

        # --- Non-editable waypoints via per-item diff ---
        new_waypoints: list[tuple[tuple, str, str, list[float], int]] = []
        if first_pos is not None and first_line not in target_by_line:
            fp_w = (tuple(first_pos), "diamond", first_color)
            new_waypoints.append(
                (fp_w, "diamond", first_color, first_pos, first_seg_idx)
            )
        if (
            last_pos is not None
            and last_line not in target_by_line
            and last_line != first_line
        ):
            fp_w = (tuple(last_pos), "square", last_color)
            new_waypoints.append((fp_w, "square", last_color, last_pos, last_seg_idx))

        new_wp_count = len(new_waypoints)
        old_wp_count = len(self._rendered_waypoints)
        wp_limit = min(new_wp_count, old_wp_count)

        for i in range(wp_limit):
            fp_w, shape, color, pos, seg_idx = new_waypoints[i]
            old_ri = self._rendered_waypoints[i]
            old_fp = old_ri.fingerprint if old_ri is not None else None
            if fp_w == old_fp:
                continue
            # Fingerprint differs — delete old, render new.
            if old_ri is not None:
                for obj in old_ri.objects:
                    self._safe_delete(obj)
            if self.path_group and self.scene:
                with self.scene:
                    with self.path_group:
                        mk = _create_waypoint_marker(shape, WAYPOINT_SIZE_SMALL, color)
                        mk.move(pos[0], pos[1], pos[2])
                self._rendered_waypoints[i] = RenderedItem(
                    objects=[mk],
                    fingerprint=fp_w,
                    segment_index=seg_idx,
                )
            else:
                self._rendered_waypoints[i] = None

        # Append new waypoints.
        if new_wp_count > old_wp_count:
            for i in range(old_wp_count, new_wp_count):
                fp_w, shape, color, pos, seg_idx = new_waypoints[i]
                if self.path_group and self.scene:
                    with self.scene:
                        with self.path_group:
                            mk = _create_waypoint_marker(
                                shape, WAYPOINT_SIZE_SMALL, color
                            )
                            mk.move(pos[0], pos[1], pos[2])
                    self._rendered_waypoints.append(
                        RenderedItem(
                            objects=[mk],
                            fingerprint=fp_w,
                            segment_index=seg_idx,
                        )
                    )
                else:
                    self._rendered_waypoints.append(None)

        # Remove trailing waypoints.
        if new_wp_count < old_wp_count:
            for i in range(new_wp_count, old_wp_count):
                old_ri = self._rendered_waypoints[i]
                if old_ri is not None:
                    for obj in old_ri.objects:
                        self._safe_delete(obj)
            del self._rendered_waypoints[new_wp_count:]

        # --- Reconcile editable targets ---
        active_ids: set[str] = set()
        for target in targets:
            active_ids.add(target.id)
            shape = shape_for_line(target.line_number)
            color = (
                PathColors.INVALID
                if not target.is_valid
                else get_color_for_move_type(target.move_type)
            )

            seg_indices = self._line_to_segments.get(target.line_number)
            seg_idx = seg_indices[0] if seg_indices else -1

            if target.id not in self._target_objects:
                with self.scene:
                    with self.targets_group:
                        grp = (
                            ui.scene.group()
                            .with_name(f"targetgroup:{target.id}")
                            .hover_effect("glow")
                        )
                        with grp:
                            mk = _create_waypoint_marker(
                                shape, WAYPOINT_SIZE_LARGE, color
                            )
                            mk.with_name(f"target:{target.id}")
                    grp.move(target.pose[0], target.pose[1], target.pose[2])
                self._target_objects[target.id] = {
                    "group": grp,
                    "marker": mk,
                    "shape_type": shape,
                    "color": color,
                    "segment_index": seg_idx,
                }
            else:
                td = self._target_objects[target.id]
                td["segment_index"] = seg_idx
                # Shape or color change requires recreating the marker group.
                if td.get("shape_type") != shape or td.get("color") != color:
                    if self.scene:
                        try:
                            td["group"].disable_transform_controls()
                        except (RuntimeError, KeyError):
                            pass
                    self._safe_delete(td["group"])
                    with self.scene:
                        with self.targets_group:
                            grp = (
                                ui.scene.group()
                                .with_name(f"targetgroup:{target.id}")
                                .hover_effect("glow")
                            )
                            with grp:
                                mk = _create_waypoint_marker(
                                    shape, WAYPOINT_SIZE_LARGE, color
                                )
                                mk.with_name(f"target:{target.id}")
                        grp.move(target.pose[0], target.pose[1], target.pose[2])
                    td["group"] = grp
                    td["marker"] = mk
                    td["shape_type"] = shape
                    td["color"] = color
                elif target.id != self._editing_target_id:
                    td["group"].move(target.pose[0], target.pose[1], target.pose[2])

        # Remove stale editable targets.
        for tid in list(self._target_objects):
            if tid not in active_ids:
                td = self._target_objects.pop(tid)
                if self.scene:
                    try:
                        td["group"].disable_transform_controls()
                    except RuntimeError:
                        pass
                self._safe_delete(td["group"])
                if self._editing_target_id == tid:
                    self._editing_target_id = None

    def _safe_delete(self, obj: Any) -> None:
        """Safely delete a scene object, handling cases where it's already deleted."""
        if self.scene is None:
            return
        try:
            if hasattr(obj, "id") and obj.id in self.scene.objects:
                obj.delete()
        except (KeyError, RuntimeError):
            # KeyError: already deleted. RuntimeError: client deleted (shutdown race).
            pass

    @property
    def initialized(self) -> bool:
        """Check if scene has been initialized via show()."""
        return self.scene is not None

    @property
    def last_actuated_joint_name(self) -> str | None:
        """Get the name of the last actuated joint."""
        return self.joint_names[-1] if self.joint_names else None

    @property
    def last_actuated_group(self) -> ui.scene.group | None:
        """Get the scene group for the last actuated joint."""
        last_joint = self.last_actuated_joint_name
        return self.joint_groups.get(last_joint) if last_joint else None

    @staticmethod
    def _glow_color(hex_color: str) -> str:
        """Brighten a hex color toward white so it exceeds the bloom threshold."""
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        factor = 0.85
        r = int(r + (255 - r) * factor)
        g = int(g + (255 - g) * factor)
        b = int(b + (255 - b) * factor)
        return f"#{r:02x}{g:02x}{b:02x}"

    _INVALID_RGB = (
        int(PathColors.INVALID.lstrip("#")[0:2], 16),
        int(PathColors.INVALID.lstrip("#")[2:4], 16),
        int(PathColors.INVALID.lstrip("#")[4:6], 16),
    )
    _BLEND_RANGE = 1.0  # number of segments over which to fade toward red

    def _gradient_colors(self, segments, seg_index) -> list[str] | None:
        """Compute per-point-pair colors for a segment near invalid segments.

        Returns None if no blending needed (segment keeps its uniform color).
        """
        seg = segments[seg_index]
        n_pairs = len(seg.points) - 1
        if n_pairs <= 0 or not seg.is_valid:
            return None

        n = len(segments)
        rng = int(self._BLEND_RANGE)

        # Closest invalid segment before and after.
        dist_before = rng + 1
        for d in range(1, rng + 1):
            idx = seg_index - d
            if 0 <= idx < n and not segments[idx].is_valid:
                dist_before = d
                break

        dist_after = rng + 1
        for d in range(1, rng + 1):
            idx = seg_index + d
            if 0 <= idx < n and not segments[idx].is_valid:
                dist_after = d
                break

        if dist_before > rng and dist_after > rng:
            return None

        h = seg.color.lstrip("#")
        rgb1 = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

        colors = []
        for j in range(n_pairs):
            t = (j + 0.5) / n_pairs  # 0..1 position within segment

            d_bef = (dist_before - 1) + t
            d_aft = (dist_after - 1) + (1.0 - t)
            min_d = min(d_bef, d_aft)

            factor = max(0.0, 1.0 - min_d / self._BLEND_RANGE)
            factor = factor**3.0  # steep ramp — stays green, only red near boundary

            if factor > 0.001:
                colors.append(_lerp_hex(rgb1, self._INVALID_RGB, factor))
            else:
                colors.append(seg.color)

        if all(c == seg.color for c in colors):
            return None
        return colors

    def _restore_segment_material(self, seg_index: int) -> None:
        """Restore a segment's objects to their display color/vertex-color state."""
        if seg_index >= len(self._rendered_segments):
            return
        rs = self._rendered_segments[seg_index]
        if rs is None or not rs.objects:
            return
        for j, obj in enumerate(rs.objects):
            if j == 0 and rs.uses_vc:
                obj.material(None)
            else:
                obj.material(rs.colors[j] if j < len(rs.colors) else "")

    def update_cursor_line_highlight(self) -> None:
        """Highlight path objects for the segment matching the editor cursor line."""
        cursor_line = active_cursor_line()
        if cursor_line == self._highlighted_line:
            return
        prev_line = self._highlighted_line
        self._highlighted_line = cursor_line

        # Restore previously highlighted segments to their display color.
        if prev_line > 0:
            for i in self._line_to_segments.get(prev_line, ()):
                self._restore_segment_material(i)

        # Glow segments matching the new cursor line.
        if cursor_line > 0:
            for i in self._line_to_segments.get(cursor_line, ()):
                if i >= len(self._rendered_segments):
                    continue
                rs = self._rendered_segments[i]
                if rs is None or not rs.objects:
                    continue
                for j, obj in enumerate(rs.objects):
                    base = rs.colors[j] if j < len(rs.colors) else ""
                    obj.material(self._glow_color(base))

    def _clear_path_state(self) -> None:
        """Delete all rendered path objects and reset bookkeeping."""
        for rs in self._rendered_segments:
            if rs is not None:
                for obj in rs.objects:
                    self._safe_delete(obj)
        self._rendered_segments.clear()
        self._line_to_segments.clear()
        self._highlighted_line = 0
        self._rendered_playback_step = -1
        for ri in self._rendered_tool_actions:
            if ri is not None:
                for obj in ri.objects:
                    self._safe_delete(obj)
        self._rendered_tool_actions.clear()
        for ri in self._rendered_waypoints:
            if ri is not None:
                for obj in ri.objects:
                    self._safe_delete(obj)
        self._rendered_waypoints.clear()
        # Clear editable targets (path data fully replaced on re-simulation)
        for td in self._target_objects.values():
            if self.scene:
                try:
                    td["group"].disable_transform_controls()
                except (RuntimeError, KeyError):
                    pass
            self._safe_delete(td["group"])
        self._target_objects.clear()
        self._editing_target_id = None

    def invalidate_paths(self) -> None:
        """Clear rendered paths and reset cache, forcing a full re-render next update.

        Call when switching tabs or when path data has completely changed.
        """
        self._clear_path_state()

    def update_playback_opacity(self) -> None:
        """Fade completed elements to 50% opacity based on the active program's
        ``dry_run.playback.current_step``.

        Applies opacity to ALL element types: segments, tool actions,
        non-editable waypoints, and editable targets.
        Only touches items whose opacity actually changed.

        All ``obj.material(...)`` calls are batched into one WS frame via
        :func:`batch_scene` — a segment crossing during fast playback can
        otherwise issue up to ~11 separate frames (polyline + cones for a
        long move), which can render half-faded if interleaved with three.js.
        """
        active = waldoctl.commander.programs.active
        step = active.dry_run.playback.current_step if active is not None else 0
        prev = self._rendered_playback_step
        n_rendered = len(self._rendered_segments)
        has_any = (
            n_rendered > 0
            or self._rendered_tool_actions
            or self._rendered_waypoints
            or self._target_objects
        )
        if step == prev or not has_any:
            return
        self._rendered_playback_step = step
        if not self.scene:
            return

        with batch_scene(self.scene):
            # --- Segments: only update indices that transitioned ---
            if n_rendered > 0:
                if prev >= 0:
                    lo, hi = min(prev, step), max(prev, step)
                    for i in range(lo, min(hi, n_rendered)):
                        rs = self._rendered_segments[i]
                        if rs is None or not rs.objects:
                            continue
                        opacity = 0.5 if (step > 0 and i < step) else 1.0
                        for j, obj in enumerate(rs.objects):
                            if j == 0 and rs.uses_vc:
                                obj.material(None, opacity)
                            else:
                                c = rs.colors[j] if j < len(rs.colors) else ""
                                obj.material(c, opacity)
                else:
                    # First update — apply all segments.
                    for i in range(n_rendered):
                        rs = self._rendered_segments[i]
                        if rs is None or not rs.objects:
                            continue
                        opacity = 0.5 if (step > 0 and i < step) else 1.0
                        for j, obj in enumerate(rs.objects):
                            if j == 0 and rs.uses_vc:
                                obj.material(None, opacity)
                            else:
                                c = rs.colors[j] if j < len(rs.colors) else ""
                                obj.material(c, opacity)

            # --- Tool actions, waypoints, targets: only update items that transitioned ---
            # An item at segment_index=s changed opacity iff it crossed the step boundary.
            if prev >= 0:
                lo, hi = min(prev, step), max(prev, step)
            else:
                lo, hi = 0, n_rendered  # first update — touch all items

            for ri in self._rendered_tool_actions:
                if ri is None or not ri.objects or ri.segment_index < 0:
                    continue
                if prev >= 0 and not (lo <= ri.segment_index < hi):
                    continue
                opacity = 0.5 if (step > 0 and ri.segment_index < step) else 1.0
                for obj in ri.objects:
                    obj.material(PathColors.TOOL_ACTION, opacity)

            for ri in self._rendered_waypoints:
                if ri is None or not ri.objects or ri.segment_index < 0:
                    continue
                if prev >= 0 and not (lo <= ri.segment_index < hi):
                    continue
                opacity = 0.5 if (step > 0 and ri.segment_index < step) else 1.0
                color = ri.fingerprint[2] if len(ri.fingerprint) > 2 else ""
                for obj in ri.objects:
                    obj.material(color, opacity)

            for td in self._target_objects.values():
                seg_idx = td.get("segment_index", -1)
                if seg_idx < 0:
                    continue
                if prev >= 0 and not (lo <= seg_idx < hi):
                    continue
                opacity = 0.5 if (step > 0 and seg_idx < step) else 1.0
                color = td.get("color", "")
                marker = td.get("marker")
                if marker is not None:
                    try:
                        marker.material(color, opacity)
                    except (RuntimeError, KeyError):
                        pass

    # --------- Public API ---------

    def update_from_robot_state(self) -> None:
        """Update scene elements that depend on robot state.

        Called directly from the status update loop in main.py for reliable
        updates without context issues.
        """
        self._update_jog_ball_from_robot_state()
        self._update_envelope_from_robot_state()
        self.update_tool_animation()
        self._update_collision_highlight()

    def _update_collision_highlight(self) -> None:
        """Tint predicted-colliding geometry red.

        LIVE/SIMULATOR share the live pose, so the pairs come from the
        controller (``status.collision`` — the arm stops just short of contact,
        so they are the predicted-config pairs). EDITING shows a different pose
        (the editing angles), so its pairs are queried client-side against this
        process's checker (tool + shapes applied locally).
        """
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            q = tuple(self._editing_angles)
            if q == self._editing_collision_q:
                return
            self._editing_collision_q = q
            robot = ui_state.active_robot
            self.set_colliding(robot.colliding_pairs(np.asarray(q, dtype=np.float64)))
            return
        self.set_colliding(waldoctl.commander.status.collision.pairs)

    def _meshes_for_collision_name(self, name: str):
        """Reported vocabulary name -> the scene objects to tint.

        Names follow the CollisionStatus contract: URDF link names,
        ``shape:<name>`` / ``install:<name>`` keep-outs, ``tool:<key>:<part>``
        for attached tool geometry. The checker's tool parts use simplified
        collision meshes with no 1:1 visual counterpart, so a tool collision
        tints the whole attached tool.
        """
        if name.startswith(("shape:", "install:")):
            obj = self._shape_objects.get(name)
            return (obj,) if obj is not None else ()
        if name.startswith("tool:"):
            return tuple(self._tool_meshes)
        return self._link_to_meshes.get(name, ())

    def set_colliding(self, pairs) -> None:
        """Diff-tint reported colliding geometry red; restore cleared ones.

        Red overrides the appearance-mode tint and is restored to the saved
        (mode) color when it clears. A signature short-circuits the unchanged
        50 Hz case; only newly (un)colliding meshes are touched, batched.
        """
        if not self.scene:
            return
        sig = tuple(sorted(tuple(p) for p in pairs))
        if sig == self._last_collision_sig:
            return
        self._last_collision_sig = sig
        target: set[Any] = set()
        for a, b in pairs:
            for nm in (a, b):
                target.update(self._meshes_for_collision_name(nm))
        if target == self._colliding_meshes:
            return
        to_add = target - self._colliding_meshes
        to_remove = self._colliding_meshes - target
        with batch_scene(self.scene):
            for m in to_add:
                self._collision_saved.setdefault(id(m), (m.color, m.opacity))
                m.material(SceneColors.COLLISION_HEX, m.opacity)
            for m in to_remove:
                saved = self._collision_saved.pop(id(m), None)
                if saved is not None:
                    m.material(saved[0], saved[1])
        self._colliding_meshes = target

    def _make_shape_object(self, s):
        """Build a scene primitive for a workspace shape (visual approximation)."""
        sc = self.scene
        k = s.kind
        if k == "box":
            return sc.box(s.x, s.y, s.z)
        if k == "sphere":
            return sc.sphere(s.radius)
        if k == "cylinder":
            return sc.cylinder(s.radius, s.radius, s.length)
        if k == "capsule":
            return sc.capsule(s.radius, s.length)
        if k == "cone":
            return sc.cylinder(0.0, s.radius, s.length)
        if k == "ellipsoid":
            # Degenerate radii render as a hair-thin ellipsoid instead of
            # dividing by zero (the checker accepts them; keep parity).
            rx = s.radius_x if s.radius_x > 0 else 1e-6
            return sc.sphere(rx).scale(1.0, s.radius_y / rx, s.radius_z / rx)
        if k == "plane":
            return sc.box(WORLD_EXTENT_M, WORLD_EXTENT_M, SURFACE_THICKNESS_M)
        return None

    def render_shapes(
        self, shapes, installation=(), draft=False, floor_z_m: float | None = None
    ) -> None:
        """Draw the keep-out shapes by layer and map them for highlighting.

        Reconciles against what is already drawn, keyed by layer-qualified
        name: a pose-only change moves the existing object, a color-only
        change repaints it, a geometry change recreates it, and a shape no
        longer in either layer is deleted. Nothing is sent for a re-render
        that changes nothing, and the group is created once, so a dragged
        object survives the readback that confirms its new pose.

        ``shapes`` is the program layer — amber while ``draft`` (not yet
        confirmed by backend readback), slate once confirmed. ``installation``
        shapes come from the backend's robot config and render in their own
        muted color; they are never draft. ``floor_z_m`` is the backend's
        installation floor: drawn as the ground at that height under the
        name its collision reports use (``install:floor``), replacing the
        placeholder disc; None keeps the disc.
        """
        if not self.scene:
            return
        self._shapes_draft = draft
        program_hex = SceneColors.SHAPE_DRAFT_HEX if draft else SceneColors.SHAPE_HEX
        desired: dict[str, tuple[Any, str]] = {}
        if floor_z_m is not None:
            floor = Box(
                name="floor",
                x=WORLD_EXTENT_M,
                y=WORLD_EXTENT_M,
                z=SURFACE_THICKNESS_M,
                pose=(0.0, 0.0, floor_z_m - SURFACE_THICKNESS_M / 2, 0.0, 0.0, 0.0),
            )
            desired["install:floor"] = (floor, self.config.ground_color)
        for prefix, layer, color in (
            ("install", installation, SceneColors.SHAPE_INSTALL_HEX),
            ("shape", shapes, program_hex),
        ):
            for s in layer:
                desired[f"{prefix}:{s.name}"] = (s, color)
        changed = False
        with batch_scene(self.scene):
            show_disc = floor_z_m is None
            if (
                self._ground_disc is not None
                and self._ground_disc.visible_ != show_disc
            ):
                self._ground_disc.visible(show_disc)
            with self.scene:
                for key in [k for k in self._shape_objects if k not in desired]:
                    self._forget_shape_object(key)
                    changed = True
                if self._shapes_group is None:
                    self._shapes_group = self.scene.group().with_name("shapes")
                with self._shapes_group:
                    for key, (s, color) in desired.items():
                        geometry = (s.kind, tuple(s.params()))
                        pose = tuple(s.pose)
                        obj = self._shape_objects.get(key)
                        last = self._shape_fingerprints.get(key)
                        if obj is not None and last is not None and last[0] != geometry:
                            self._forget_shape_object(key)
                            obj = None
                        if obj is None:
                            obj = self._make_shape_object(s)
                            if obj is None:
                                continue
                            obj.with_name(key)
                            self._shape_objects[key] = obj
                            last = None
                            changed = True
                        if last is None or last[1] != pose:
                            pos, rot = _shape_render_pose(s)
                            obj.move(*pos).rotate_R(rot)
                            self._shape_declared_pose[key] = (pos, rot)
                            self._object_ghosts.pop(key, None)
                            changed = True
                        if last is None or last[2] != color:
                            opacity = _base_opacity(key)
                            if obj in self._colliding_meshes:
                                # Tinted right now: the new base color is what
                                # the highlight restores to when the collision
                                # clears, not what is painted over the tint.
                                self._collision_saved[id(obj)] = (color, opacity)
                            else:
                                obj.material(color, opacity)
                            changed = True
                        self._shape_fingerprints[key] = (geometry, pose, color)
        if not changed:
            return
        # The world changed — force the highlight to recompute next tick.
        self._last_collision_sig = None
        self._editing_collision_q = None
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            self._update_collision_highlight()

    def _forget_shape_object(self, key: str) -> None:
        """Delete one drawn shape and drop its collision bookkeeping, so a
        re-render mid-collision can't restore or tint a stale object."""
        obj = self._shape_objects.pop(key)
        self._shape_fingerprints.pop(key, None)
        self._shape_declared_pose.pop(key, None)
        self._object_ghosts.pop(key, None)
        self._colliding_meshes.discard(obj)
        self._collision_saved.pop(id(obj), None)
        self._safe_delete(obj)

    def set_object_poses(self, poses: Mapping[str, ObjectSample] | None) -> None:
        """Put world objects where the preview says they are at a playback
        instant — a pose-only move of the drawn program shapes, one batched
        frame, never a re-render — or restore their declared poses with
        None. An object whose track is a guess rather than physics is drawn
        at half opacity while overridden.
        """
        if not self.scene:
            return
        with batch_scene(self.scene):
            if poses is None:
                for key in list(self._object_ghosts):
                    obj = self._shape_objects.get(key)
                    declared = self._shape_declared_pose.get(key)
                    if obj is not None and declared is not None:
                        obj.move(*declared[0]).rotate_R(declared[1])
                        if self._object_ghosts[key]:
                            self._repaint(obj, key, ghost=False)
                self._object_ghosts.clear()
                return
            for name, sample in poses.items():
                key = f"shape:{name}"
                obj = self._shape_objects.get(key)
                fingerprint = self._shape_fingerprints.get(key)
                if obj is None or fingerprint is None:
                    continue
                pos, rot = _object_render_pose(fingerprint[0][0], sample.pose)
                obj.move(*pos).rotate_R(rot)
                ghost = not sample.physics
                if self._object_ghosts.get(key) != ghost:
                    self._repaint(obj, key, ghost=ghost)
                self._object_ghosts[key] = ghost

    def _repaint(self, obj: Any, key: str, *, ghost: bool) -> None:
        """Apply a shape's base colour at full or ghost opacity, honouring a
        collision tint the same way render_shapes does."""
        color = self._shape_fingerprints[key][2]
        opacity = _base_opacity(key) * (0.5 if ghost else 1.0)
        if obj in self._colliding_meshes:
            self._collision_saved[id(obj)] = (color, opacity)
        else:
            obj.material(color, opacity)

    def set_axis_value(self, joint_name: str, val: float) -> None:
        """Set a single joint axis value.

        Args:
            joint_name: Name of the joint to move
            val: Joint value (radians for revolute, meters for prismatic)
        """
        t, r = self.joint_trafos[joint_name](val)
        self.joint_groups[joint_name].move(*t).rotate(*r)

    def set_axis_values(self, val: list | np.ndarray) -> None:
        """Set all axes values by passing an array or list.

        Args:
            val: Array or list of joint values in order matching self.joint_names

        Note:
            This method is guarded - it will not update the robot during editing mode
            to prevent live updates from overwriting user manipulations.

            Joint transforms are flushed as a single WS frame via
            :func:`batch_scene` so three.js can't render a frame with only
            a subset of the 12 joint updates applied (which manifests as
            visible wrist "shake" at high update rates).
        """
        # Don't fight the user's manipulations while editing.
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            return
        if not self.scene:
            return

        with batch_scene(self.scene):
            for joint_name, q in zip(self.joint_names, val):
                joint_TF = self.joint_trafos[joint_name]
                joint_i = self.joint_groups[joint_name]
                t, r = joint_TF(q)
                joint_i.move(*t).rotate(*r)

    def _apply_joint_angles(self, angles_rad: list[float]) -> None:
        """Apply joint angles to the main robot joint groups.

        Internal method used by both live updates and editing mode.

        Args:
            angles_rad: Joint angles in radians, ordered by self.joint_names
        """
        for joint_name, q in zip(self.joint_names, angles_rad):
            if joint_name in self.joint_groups and joint_name in self.joint_trafos:
                t, r = self.joint_trafos[joint_name](q)
                self.joint_groups[joint_name].move(*t).rotate(*r)

    def set_editing_angles(self, angles: list[float]) -> None:
        """Set joint angles for editing mode (radians).

        Updates the robot visualization when in EDITING mode.

        Args:
            angles: List of joint angles in radians
        """
        n = len(self.joint_names)
        self._editing_angles = list(angles) + [0.0] * (n - len(angles))
        self._editing_angles = self._editing_angles[:n]

        if self._appearance_mode == RobotAppearanceMode.EDITING:
            self._apply_joint_angles(self._editing_angles)
            self._update_tcp_ball_position()
            # The status loop skips scene updates in EDITING, so the collision
            # highlight is driven from here (per scrub/pose change).
            self._update_collision_highlight()

    def get_editing_angles(self) -> list[float]:
        """Get current editing joint angles.

        Returns:
            List of joint angles in radians
        """
        return list(self._editing_angles)

    def get_joint_names(self) -> list[str]:
        """Get list of actuated joint names in order."""
        return list(self.joint_groups.keys())

    def set_tcp_pose(self, origin: Sequence[float], rpy: Sequence[float]) -> None:
        """Directly set TCP offset pose.

        Args:
            origin: Translation offset [x, y, z] in meters
            rpy: Rotation offset [roll, pitch, yaw] in radians
        """
        if not self.tcp_offset:
            logger.warning("TCP offset group not initialized; cannot set pose")
            return
        if len(origin) != 3 or len(rpy) != 3:
            raise ValueError("origin and rpy must each have exactly 3 elements")
        self.tcp_offset.move(*origin).rotate(*rpy)

    def apply_tool(self, tool_key: str, variant_key: str | None = None) -> None:
        """Update TCP, swap meshes, and invalidate FK cache for a tool change."""
        self.update_tcp_pose_from_tool(tool_key, variant_key=variant_key)
        self.swap_tool_mesh(tool_key, variant_key=variant_key)
        self.invalidate_fk_cache()

    def apply_tool_everywhere(
        self, tool_key: str, variant_key: str | None = None
    ) -> None:
        """Apply a tool to the robot model, scene meshes/TCP, and TCP ball.

        Caller is responsible for syncing to the controller (``select_tool``)
        — that's I/O and depends on the caller's async/sync context.
        """
        ui_state.active_robot.set_active_tool(tool_key, variant_key=variant_key)
        self.apply_tool(tool_key, variant_key=variant_key)
        self.refresh_tcp_ball()

    def update_tcp_pose_from_tool(
        self,
        tool: str,
        variant_key: str | None = None,
    ) -> None:
        """Move/rotate the TCP offset based on selected tool's TCP config.

        Args:
            tool: Tool identifier string
            variant_key: Optional variant key for per-variant TCP
        """
        if not self.tcp_offset:
            logger.warning("TCP offset group not initialized; cannot update from tool")
            return

        # Default: reset offsets.
        origin = [0.0, 0.0, 0.0]
        rpy = [0.0, 0.0, 0.0]

        tool_pose: ToolPose | None = None

        # Resolver takes precedence over the static map.
        if self.config.tool_pose_resolver is not None:
            tool_pose = self.config.tool_pose_resolver(tool, variant_key)

        if tool_pose is None and tool in self.config.tool_pose_map:
            tool_pose = self.config.tool_pose_map[tool]

        if tool_pose is not None:
            if tool_pose.origin and len(tool_pose.origin) == 3:
                origin = list(tool_pose.origin)
            if tool_pose.rpy and len(tool_pose.rpy) == 3:
                rpy = list(tool_pose.rpy)

        self.tcp_offset.move(*origin).rotate(*rpy)

        # Track tool offset for envelope calculations.
        self._current_tool = tool or "none"
        self._current_tool_offset_z = origin[2] if len(origin) > 2 else 0.0

        self._update_envelope_radius()

    def swap_tool_mesh(self, tool_key: str, variant_key: str | None = None) -> None:
        """Replace tool meshes in the 3D scene for the given tool.

        Loads STL files from the tool's ``meshes`` spec into the
        ``tool:meshes`` group.  Meshes with motion roles are tracked for animation.
        When *variant_key* is given, uses that variant's meshes and motions
        instead of the tool's defaults.
        """
        if not self.scene or not self._tool_meshes_group:
            return

        # Remove old tool meshes.
        old_tool = set(self._tool_meshes)
        for mesh in self._tool_meshes:
            self._safe_delete(mesh)
        self._tool_meshes.clear()
        self._tool_body_meshes.clear()
        self._tool_motions.clear()
        self._tool_motion_meshes.clear()
        self._tool_motion_origins.clear()
        self._tool_motion_rotations.clear()
        self._tool_motion_last = ()
        self._last_tool_engaged = None
        self._tool_has_motions = False
        # Drop collision bookkeeping for the deleted tool meshes; force a recompute
        # so new tool meshes re-tint if still colliding.
        self._colliding_meshes -= old_tool
        for m in old_tool:
            self._collision_saved.pop(id(m), None)
        self._last_collision_sig = None
        self._editing_collision_q = None

        try:
            tool_spec = ui_state.active_robot.tools[tool_key]
        except (KeyError, AttributeError):
            logger.debug("No tool spec for '%s'; tool meshes cleared", tool_key)
            return

        meshes = getattr(tool_spec, "meshes", ())
        motions = getattr(tool_spec, "motions", ())

        # Override with variant meshes/motions if specified.
        if variant_key:
            for v in getattr(tool_spec, "variants", ()):
                if v.key == variant_key:
                    meshes = v.meshes
                    motions = v.motions
                    break

        if not meshes:
            return

        self._tool_motions = list(motions)
        self._tool_has_motions = bool(motions)
        motion_roles = {m.role for m in self._tool_motions}

        body_color, moving_color, opacity = self._get_tool_colors()

        with self.scene:
            with self._tool_meshes_group:
                for mesh_spec in meshes:
                    filename = mesh_spec.file
                    if not filename:
                        continue
                    origin = mesh_spec.origin
                    rpy = mesh_spec.rpy
                    role = mesh_spec.role

                    url = self._stl_to_url(filename)
                    obj = (
                        ui.scene.stl(url)
                        .scale(self._stl_scale)
                        .move(*origin)
                        .rotate(*rpy)
                    )
                    is_moving = role in motion_roles
                    color = moving_color if is_moving else body_color
                    if color is not None:
                        obj.material(color, opacity)
                    self._tool_meshes.append(obj)
                    if not is_moving:
                        self._tool_body_meshes.append(obj)
                    if role in motion_roles:
                        self._tool_motion_meshes.setdefault(role, []).append(obj)
                        self._tool_motion_origins.setdefault(role, []).append(
                            (float(origin[0]), float(origin[1]), float(origin[2]))
                        )
                        self._tool_motion_rotations.setdefault(role, []).append(
                            (float(rpy[0]), float(rpy[1]), float(rpy[2]))
                        )

    def update_tool_animation(self) -> None:
        """Animate tool meshes based on ``ToolSpec.motions`` descriptors.

        Each motion reads its DOF position from ``robot_state.tool_status.positions``
        (0..1 fraction) and applies translation or rotation to meshes matching
        the motion's ``role``.

        Also applies activated color: moving-part meshes get the "moving" color
        only when ``tool_status.engaged`` is True.  Binary tools without motions
        apply activated color to all tool meshes.

        Mesh mutations (move/rotate/material across all moving meshes) are
        batched into one WS frame via :func:`batch_scene` so symmetric jaws
        can't render with one side updated and the other stale.
        """
        if not self.scene:
            return

        with batch_scene(self.scene):
            # Engaged color applies to all tools, not just those with motions.
            engaged = robot_state.tool_status.engaged
            if (
                engaged != self._last_tool_engaged
                and self._appearance_mode != RobotAppearanceMode.EDITING
            ):
                self._last_tool_engaged = engaged
                self._apply_tool_engaged_color(engaged)

            if not self._tool_motions:
                return

            positions = robot_state.tool_status.positions
            if positions == self._tool_motion_last:
                return
            self._tool_motion_last = positions

            for idx, motion in enumerate(self._tool_motions):
                meshes = self._tool_motion_meshes.get(motion.role)
                if not meshes:
                    continue

                frac = positions[idx] if idx < len(positions) else 0.0
                frac = max(0.0, min(1.0, frac))

                origins = self._tool_motion_origins.get(motion.role, [])

                if isinstance(motion, LinearMotion):
                    travel = motion.travel_m * -frac
                    ax = motion.axis
                    for i, mesh in enumerate(meshes):
                        sign = (
                            (1.0 if i % 2 == 0 else -1.0) if motion.symmetric else 1.0
                        )
                        ox, oy, oz = origins[i] if i < len(origins) else (0.0, 0.0, 0.0)
                        mesh.move(
                            ox + ax[0] * travel * sign,
                            oy + ax[1] * travel * sign,
                            oz + ax[2] * travel * sign,
                        )
                elif isinstance(motion, RotaryMotion):
                    angle = motion.travel_rad * frac
                    ax = motion.axis
                    rots = self._tool_motion_rotations.get(motion.role, [])
                    for i, mesh in enumerate(meshes):
                        sign = (
                            (1.0 if i % 2 == 0 else -1.0) if motion.symmetric else 1.0
                        )
                        r0x, r0y, r0z = rots[i] if i < len(rots) else (0.0, 0.0, 0.0)
                        mesh.rotate(
                            r0x + ax[0] * angle * sign,
                            r0y + ax[1] * angle * sign,
                            r0z + ax[2] * angle * sign,
                        )

    def _apply_tool_engaged_color(self, engaged: bool) -> None:
        """Apply activated color to tool meshes based on engaged state."""
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            return  # don't change colors in editing mode
        body_color, moving_color, opacity = self._get_tool_colors()

        if self._tool_has_motions:
            # Tools with motions: activated color only on moving parts.
            repainted = {m for ms in self._tool_motion_meshes.values() for m in ms}
            color = moving_color if engaged else body_color
            for mesh in repainted:
                mesh.material(color, opacity)
        else:
            # Binary tools without motions (vacuum, etc.): color the whole tool.
            repainted = set(self._tool_meshes)
            color = moving_color if engaged else body_color
            for mesh in repainted:
                mesh.material(color, opacity)

        # The repaint clobbered any red collision tint on these meshes and
        # their saved base color is now stale — drop the bookkeeping so the
        # next tick re-tints/restores from the new engaged/disengaged base.
        if repainted & self._colliding_meshes:
            self._colliding_meshes -= repainted
            for m in repainted:
                self._collision_saved.pop(id(m), None)
            self._last_collision_sig = None

    def set_appearance_mode(self, mode: RobotAppearanceMode) -> None:
        """Set robot appearance mode.

        Args:
            mode: The appearance mode to set (LIVE, SIMULATOR, or EDITING)
        """
        self._appearance_mode = mode

        body_color, moving_color, opacity = self._get_tool_colors()
        arm_color = {
            RobotAppearanceMode.LIVE: self.config.material,
            RobotAppearanceMode.SIMULATOR: self.config.sim_color,
        }.get(mode, self.config.edit_color)

        for mesh in self._robot_meshes:
            mesh.material(arm_color, opacity)

        for mesh in self._tool_body_meshes:
            mesh.material(body_color, opacity)

        moving_meshes = {m for ms in self._tool_motion_meshes.values() for m in ms}
        for mesh in moving_meshes:
            mesh.material(moving_color, opacity)

        # Shapes keep their own base color, but must be repainted with the
        # arm/tool so a colliding one isn't re-snapshotted with red as its base.
        program_hex = (
            SceneColors.SHAPE_DRAFT_HEX if self._shapes_draft else SceneColors.SHAPE_HEX
        )
        for name, obj in self._shape_objects.items():
            base = (
                SceneColors.SHAPE_INSTALL_HEX
                if name.startswith("install:")
                else program_hex
            )
            obj.material(base, SHAPE_OPACITY)

        logger.debug("Robot appearance mode set to %s", mode.value)
        # The repaint above clobbered any red collision tint; drop the
        # bookkeeping so the next tick re-applies it from the new mode base.
        self._colliding_meshes.clear()
        self._collision_saved.clear()
        self._last_collision_sig = None
        self._editing_collision_q = None
        if mode == RobotAppearanceMode.EDITING:
            # The status loop won't tick the scene in EDITING; show the current
            # editing pose's collisions immediately.
            self._update_collision_highlight()

    def set_simulator_appearance(self, active: bool) -> None:
        """Apply or remove simulator visual appearance (amber ghosting).

        Args:
            active: True to apply simulator appearance, False to restore default
        """
        # Don't change mode while EDITING.
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            logger.debug("Ignoring set_simulator_appearance while in EDITING mode")
            return

        if active:
            self.set_appearance_mode(RobotAppearanceMode.SIMULATOR)
        else:
            self.set_appearance_mode(RobotAppearanceMode.LIVE)

    def _get_tool_colors(self) -> tuple[str, str, float]:
        """Get (body_color, moving_color, opacity) for the current appearance mode."""
        if self._appearance_mode == RobotAppearanceMode.LIVE:
            return self.config.tool_body_material, self.config.tool_moving_material, 1.0
        elif self._appearance_mode == RobotAppearanceMode.SIMULATOR:
            return (
                self.config.tool_body_sim_color,
                self.config.tool_moving_sim_color,
                self.config.sim_opacity,
            )
        else:
            return (
                self.config.tool_body_edit_color,
                self.config.tool_moving_edit_color,
                self.config.edit_opacity,
            )

    # --------- Internal URDF building ---------

    def _get_next_joints(self, urdf, link_obj):
        """Get joints that have link_obj as parent."""
        return [j for j in urdf.joints if j.parent == link_obj.name]

    def _recursively_add_subtree(
        self, urdf, joint, scale_stls: float = 1, material=None
    ):
        """Recursively add joint and child link to scene."""
        t, r = get_transl_and_rpy(joint.origin)
        # Static transform from parent link to this joint frame.
        with ui.scene.group().move(*t).rotate(*r):
            # Inner group carries the dynamic joint value (q).
            with ui.scene.group() as joint_trafo:
                if joint.joint_type != "fixed":
                    self.joint_groups[joint.name] = joint_trafo

                    if joint.joint_type == "prismatic":
                        self.joint_trafos[joint.name] = (
                            lambda q, axis=joint.axis: transl_joint(axis, q)
                        )
                        self.joint_pos_limits[joint.name] = {
                            "min": joint.limit.lower,
                            "max": joint.limit.upper,
                        }
                    elif joint.joint_type in ("revolute", "continuous"):
                        self.joint_trafos[joint.name] = (
                            lambda q, axis=joint.axis: rot_joint(axis, q)
                        )
                        if joint.joint_type == "continuous":
                            self.joint_pos_limits[joint.name] = {
                                "min": None,
                                "max": None,
                            }
                        else:
                            self.joint_pos_limits[joint.name] = {
                                "min": joint.limit.lower,
                                "max": joint.limit.upper,
                            }
                    else:
                        raise NotImplementedError(
                            f"Unsupported joint type '{joint.joint_type}' for joint "
                            f"'{joint.name}'. Supported types: 'fixed', 'prismatic', "
                            f"'revolute', 'continuous'."
                        )

                child_link = next(
                    (link for link in urdf.links if link.name == joint.child), None
                )
                if child_link:
                    self._plot_stls(child_link, scale=scale_stls, material=material)

                    if child_link not in urdf.end_links:
                        joints = self._get_next_joints(urdf, child_link)
                        for sub_joint in joints:
                            self._recursively_add_subtree(
                                urdf,
                                sub_joint,
                                scale_stls=scale_stls,
                                material=material,
                            )
                    else:
                        # End link reached: place a TCP anchor
                        with joint_trafo:
                            anchor = ui.scene.group().with_name("tcp:anchor")
                            self.tcp_anchor = anchor
                            with anchor:
                                tool_grp = ui.scene.group().with_name("tool:meshes")
                                self._tool_meshes_group = tool_grp
                                offset = ui.scene.group().with_name("tcp:offset")
                                self.tcp_offset = offset
                        # Optional: small axes at TCP
                        if self.config.draw_tcp_axes:
                            self._draw_scene_cos(scale=0.05)

    def _plot_stls(self, link, scale: float = 1, material=None):
        """Add all visual STLs from a link to the scene."""
        for visual in link.visuals:
            obj = ui.scene.stl(
                self._stl_to_url(visual.geometry.geometry.filename)
            ).scale(scale)
            if visual.origin is not None:
                t, r = get_transl_and_rpy(visual.origin)
                if any(v != 0 for v in t):
                    obj.move(*t)
                if any(v != 0 for v in r):
                    obj.rotate(*r)
            if material is not None:
                obj.material(material)
            # Tracked for simulator appearance changes.
            self._robot_meshes.append(obj)
            # Tracked by link name so reported colliding links can be tinted red.
            self._link_to_meshes.setdefault(link.name, []).append(obj)

    def _stl_to_url(self, stl_path: str) -> str:
        """Convert STL file path to URL, preferring _simplified variants if they exist."""
        if stl_path.startswith("file://"):
            parsed = urlparse(stl_path)
            stl_path = url2pathname(parsed.path)

        # Resolve relative to meshes_dir.
        stl_full = Path(stl_path)
        if stl_full.is_absolute():
            try:
                rel_path = stl_full.relative_to(self.meshes_dir)
            except ValueError:
                rel_path = Path(stl_full.name)
        else:
            rel_path = stl_full

        # Prefer a _simplified variant (e.g. part.STL -> part_simplified.stl),
        # trying both the original extension case and lowercase .stl.
        for ext in [rel_path.suffix, ".stl"]:
            simplified_name = rel_path.stem + "_simplified" + ext
            simplified_path = rel_path.with_name(simplified_name)
            full_simplified = self.meshes_dir / simplified_path

            if full_simplified.exists():
                rel_path = simplified_path
                logger.debug("Using simplified mesh: %s", simplified_path)
                break

        return os.path.join(self.meshes_url, str(rel_path).replace("\\", "/"))

    def _draw_scene_cos(
        self, scale: float = 0.3, translate: Sequence[float] | None = None
    ):
        """Draw coordinate system axes at specified location."""
        scene = self.scene
        if scene is None:
            return
        tx, ty, tz = translate if translate is not None else (0.0, 0.0, 0.0)
        origin = [tx, ty, tz]
        scene.line(origin, [tx + scale, ty, tz]).material(SceneColors.AXIS_X_HEX)
        scene.line(origin, [tx, ty + scale, tz]).material(SceneColors.AXIS_Y_HEX)
        scene.line(origin, [tx, ty, tz + scale]).material(SceneColors.AXIS_Z_HEX)
