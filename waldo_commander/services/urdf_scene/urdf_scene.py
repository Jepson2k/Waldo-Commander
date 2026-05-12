"""
UrdfScene - Main class integrating all mixins.

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
from typing import Any, NamedTuple, Sequence
from urllib.parse import urlparse
from urllib.request import url2pathname

import numpy as np
from nicegui import ui, app

from waldoctl import LinearMotion, RotaryMotion, MeshRole, PartMotion

from waldo_commander.common.logging_config import TRACE_ENABLED, TraceLogger
from waldo_commander.common.theme import (
    PathColors,
    SceneColors,
    get_color_for_move_type,
)
from waldo_commander.constants import WAYPOINT_SIZE_LARGE, WAYPOINT_SIZE_SMALL
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
from .envelope_mixin import EnvelopeMixin
from .path_renderer_mixin import PathRendererMixin

logger: TraceLogger = logging.getLogger(__name__)  # type: ignore[assignment]  # ty: ignore[invalid-assignment]


def _lerp_hex(c1: tuple[int, int, int], c2: tuple[int, int, int], factor: float) -> str:
    """Linearly interpolate two RGB colors and return a hex string."""
    r = int(c1[0] + (c2[0] - c1[0]) * factor + 0.5)
    g = int(c1[1] + (c2[1] - c1[1]) * factor + 0.5)
    b = int(c1[2] + (c2[2] - c1[2]) * factor + 0.5)
    return f"#{r:02x}{g:02x}{b:02x}"


class RenderedSegment(NamedTuple):
    """Immutable record of a rendered path segment's scene objects and identity."""

    objects: list[Any]  # scene objects (polyline + cones)
    colors: list[str]  # display color per object
    uses_vc: bool  # polyline uses vertex colors?
    fingerprint: tuple  # identity for diff (visual properties only)
    line_number: int  # source line number (for cursor highlight map)


class RenderedItem(NamedTuple):
    """Immutable record of a rendered scene element (tool action or waypoint)."""

    objects: list[Any]  # scene objects
    fingerprint: tuple  # identity for diff
    segment_index: int  # associated segment index for playback opacity (-1 if N/A)


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
    # Compute neighbor validity signature within blend range
    neighbor_valid: list[bool] = []
    for d in range(1, blend_range + 1):
        before = seg_index - d
        after = seg_index + d
        neighbor_valid.append(before >= 0 and before < n and segments[before].is_valid)
        neighbor_valid.append(after >= 0 and after < n and segments[after].is_valid)
    return (
        tuple(pts[0]) if pts else (),
        tuple(pts[-1]) if pts else (),  # spatial identity
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
        # Bicone: rotate a diamond profile around Y axis
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
    EnvelopeMixin,
    PathRendererMixin,
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

        # Load URDF with package resolution
        self.urdf_model = load_urdf(path, package_map=self.config.package_map)

        # Store URDF path for IK solver
        self.urdf_path = path

        # Determine and mount mesh directory
        self.meshes_dir = resolve_meshes_dir(path, self.config.meshes_dir)
        self.meshes_url = f"{self.config.static_url_prefix}/{self.urdf_model.name}"
        self.joint_names = self.urdf_model.actuated_joint_names

        if self.config.mount_static:
            try:
                app.add_static_files(self.meshes_url, str(self.meshes_dir))
            except Exception as e:
                msg = str(e).lower()
                # Ignore duplicate registration across tests; re-raise other errors
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
        # Persist normalized joint axes (from URDF) by joint name
        self.joint_axes: dict[str, np.ndarray] = {}
        # Pre-populate joint axes directly from URDF so axes are available before scene build
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

        # STL scale
        self._stl_scale: float = 1.0

        # TCP anchoring: created on end-link under last joint
        self.tcp_anchor: Any | None = None
        self.tcp_offset: Any | None = None

        # Simulation visualization
        self.simulation_group: Any | None = None
        self.path_group: Any | None = None
        self.targets_group: Any | None = None
        # Per-segment rendering state (indexed by segment index)
        self._rendered_segments: list[RenderedSegment | None] = []
        # Cursor highlight: line_number -> list of segment indices
        self._line_to_segments: dict[int, list[int]] = {}
        # Per-item diff state for tool actions and waypoints
        self._rendered_tool_actions: list[RenderedItem | None] = []
        self._rendered_waypoints: list[RenderedItem | None] = []
        self._highlighted_line: int = 0
        self._rendered_playback_step: int = -1

        # Track robot mesh objects for material changes
        self._robot_meshes: list[ui.scene.stl] = []

        # Tool mesh state
        self._tool_meshes_group: Any | None = None
        self._tool_meshes: list[Any] = []
        self._tool_body_meshes: list[Any] = []  # static body parts (housing)
        # Generic tool motion state (driven by ToolSpec.motions)
        self._tool_motions: list[PartMotion] = []
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

        # Robot appearance mode (unified state machine)
        self._appearance_mode: RobotAppearanceMode = RobotAppearanceMode.LIVE

        # Editing mode state
        n = len(self.joint_names)
        self._editing_angles: list[float] = [0.0] * n  # Joint angles during editing
        self._pre_edit_angles: list[float] = [
            0.0
        ] * n  # Saved angles to restore on exit
        self._editing_target_type: str = "cartesian"  # "cartesian" or "joint"
        self._joint_ring_touched: bool = False  # True if user rotated any joint ring

        # Scene wrapper for proper positioning of overlays
        self._scene_wrapper: Any | None = None

        # Initialize mixin states
        self._init_editing_state()
        self._init_tcp_controls_state()
        self._init_envelope_state()

        # Register as listener for simulation state changes (event-driven updates)
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
        # Use config background color if not specified
        if background_color is None:
            background_color = self.config.background_color
        # Wrap scene in element to host context menu and edit bar
        self._scene_wrapper = ui.element("div").classes("relative w-full h-full")
        with self._scene_wrapper:
            # Create context menu - clear on hide so it doesn't auto-show with stale content
            self.context_menu = ui.context_menu()
            self.context_menu.on("hide", lambda: self.context_menu.clear())
            # Use polar grid sized to robot's approximate workspace (~536mm reach)
            default_radius = 0.55  # Workspace radius in meters
            with (
                ui.scene(
                    grid=False,  # Disable rectangular grid
                    polar_grid=(default_radius, 12, 6),  # (radius, sectors, rings)
                    raycaster_threshold=0.005,
                    background_color=background_color,
                    # Match the original feature-branch hoverable() visuals: a white,
                    # ~0.2 opacity halo at 2x the source geometry's footprint.
                    hover_color="#ffffff",
                    hover_opacity=0.2,
                    hover_scale=2.0,
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
                # Ground plane for contrast with background
                ui.scene.cylinder(
                    default_radius, default_radius, 0.001, radial_segments=64
                ).material(self.config.ground_color, opacity=0.5).rotate(
                    math.pi / 2, 0, 0
                )

                # Base link
                self._plot_stls(
                    self.urdf_model.base_link, scale=self._stl_scale, material=material
                )
                # Recursively add rest
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

                # Make simulation group
                with ui.scene.group().with_name("simulation:root") as sim_grp:
                    self.simulation_group = sim_grp
                    with ui.scene.group().with_name("simulation:paths") as path_grp:
                        self.path_group = path_grp
                    with ui.scene.group().with_name(
                        "simulation:targets"
                    ) as targets_grp:
                        self.targets_group = targets_grp

            # Position orientation inset
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

            # Add keyboard handler for ESC to deselect TransformControls
            ui.keyboard(on_key=self._handle_keyboard)

            # Register IK solved event listener for ghost robot IK
            self.scene.on("ik_solved", self._on_ik_solved)
            # Use on_transform_start for proper typed SceneTransformEventArguments
            self.scene.on_transform_start(self._handle_transform_start)

            # Register continuous transform event listener for live ghost robot updates
            self.scene.on_transform(self._handle_transform_continuous)

    def _handle_transform_continuous(self, e) -> None:
        """Handle continuous transform events for TCP ball and joint controls.

        This handler receives transform events during drag (not just at end)
        and is used for TCP ball movement (jogging or IK) and joint ring rotation.
        Does NOT handle pose targets - those use on_transform_end only.
        """
        object_name = getattr(e, "object_name", "") or ""

        # Check if this is the unified TCP ball being transformed
        # Behavior depends on appearance mode (jogging vs IK)
        if object_name in ("tcp:ball", "tcp:jog_ball", "tcp:offset"):
            self._handle_tcp_transform_for_jog(e)
            return

        # Check if this is a joint group being rotated (TransformControls on robot joints)
        if object_name.startswith("edit_joint_group:"):
            self._on_joint_group_transform(e)
            return

    def _handle_transform_start(self, e) -> None:
        """Handle TransformControls transform_start events to manage orbit and mutex."""
        object_name = getattr(e, "object_name", "") or ""
        if object_name in ("tcp:ball", "ghost:tcp_ball"):
            # Disable orbit controls as soon as TCP transform starts
            if self.scene:
                self.scene.set_orbit_enabled(False)
            self._tcp_ball_dragging = True
            if self._appearance_mode == RobotAppearanceMode.EDITING:
                # Suspend joint controls during TCP ball manipulation in editing mode
                if not self._joint_controls_suspended:
                    self._disable_joint_transform_controls()
                    self._joint_controls_suspended = True
            else:
                # Jogging mode: capture starting rotation (used by translate-only
                # cartesian streaming to keep rotation fixed) and notify the
                # consumer that a drag session has started. Doing this here —
                # rather than on the first on_transform event — is required
                # because on_transform_start already set _tcp_ball_dragging,
                # so the legacy "first event" detection inside
                # _handle_tcp_transform_for_jog never triggers.
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

        # Unified TCP ball - on transform_end re-enable orbit and joint controls
        if object_name in ("tcp:ball", "ghost:tcp_ball"):
            if event_type == "transform_end":
                self._tcp_ball_dragging = False
                # Clear drag start rotation
                self._tcp_drag_start_rot_deg = None
                # Re-enable orbit controls when TCP transform ends
                if self.scene:
                    self.scene.set_orbit_enabled(True)
                if self._joint_controls_suspended:
                    self._enable_joint_transform_controls()
                    self._joint_controls_suspended = False
                # In editing mode, snap TCP ball to FK position (in case
                # IK failed and the ball was dragged to an unreachable spot)
                if self._appearance_mode == RobotAppearanceMode.EDITING:
                    self._snap_tcp_to_fk()
                else:
                    # Notify drag-end to consumers (for jogging mode)
                    cb = getattr(self, "_tcp_cartesian_move_end_callback", None)
                    if callable(cb):
                        try:
                            cb()
                        except Exception as err:
                            logger.error(
                                "TCP cartesian move end callback error: %s", err
                            )
            return

        # Legacy jog ball name handling
        if object_name in ("tcp:jog_ball", "tcp:offset"):
            if event_type == "transform_end":
                self._tcp_ball_dragging = False
                self._tcp_drag_start_rot_deg = None
                # Notify drag-end to consumers
                cb = getattr(self, "_tcp_cartesian_move_end_callback", None)
                if callable(cb):
                    try:
                        cb()
                    except Exception as err:
                        logger.error("TCP cartesian move end callback error: %s", err)
            return

        # Joint rings handled by continuous handler - skip here
        if object_name.startswith("ghost_ring_group:"):
            return

        # Check if this is a target group being transformed
        if object_name.startswith("targetgroup:"):
            target_id = object_name.split("targetgroup:", 1)[1]
            target = self._find_target_by_id(target_id)
            if target:
                # Only update position if provided (translate mode)
                if e.x is not None:
                    target.pose[0] = e.x
                if e.y is not None:
                    target.pose[1] = e.y
                if e.z is not None:
                    target.pose[2] = e.z

                # Only update rotation if provided (rotate mode)
                if len(target.pose) >= 6:
                    if e.rx is not None:
                        target.pose[3] = e.rx
                    if e.ry is not None:
                        target.pose[4] = e.ry
                    if e.rz is not None:
                        target.pose[5] = e.rz

                # Only sync to editor on transform_end to avoid too many updates
                if event_type == "transform_end":
                    # Ensure pose has no None values before syncing
                    clean_pose = [v if v is not None else 0.0 for v in target.pose]
                    ui_state.editor_panel.sync_code_from_target(target_id, clean_pose)

                    # Quick IK check to update target validity and color
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

                # Check if clicked on ghost robot parts
                if (
                    name.startswith("ghost:")
                    or name.startswith("ghost_ring_")
                    or name.startswith("ghost_joint_group:")
                ):
                    clicked_ghost_part = True
                    continue

                # Check if clicked on TransformControls gizmo
                if object_id.startswith("transformcontrols:"):
                    clicked_transform_controls = True
                    continue

            # Handle target editing - clicking away does NOT auto-confirm
            if self._editing_unified_target:
                if not clicked_ghost_part and not clicked_transform_controls:
                    return  # Don't process other click handlers

        elif click_type == "contextmenu":
            # Record position and event when contextmenu fires
            # Menu only shows when populated - we decide on mouseup whether to populate
            self._right_click_start_pos = self._screen_pos(e)
            # Store event for populating menu on mouseup
            self._pending_context_menu_event = e

        elif click_type == "mouseup":
            button = getattr(e, "button", 0)
            if button == 2 and self._right_click_start_pos is not None:
                # Check if this was a drag
                screen_x, screen_y = self._screen_pos(e)
                start_x, start_y = self._right_click_start_pos
                distance = math.hypot(screen_x - start_x, screen_y - start_y)

                self._right_click_start_pos = None

                if distance <= self._right_click_drag_threshold:
                    # Was a simple click - populate the menu (which makes it show)
                    if self._pending_context_menu_event:
                        self._populate_context_menu(self._pending_context_menu_event)
                # If drag, don't populate - menu stays empty and won't show

                self._pending_context_menu_event = None

    def _update_simulation_view(self) -> None:
        """Update simulation visualization (paths, etc.) based on state."""
        if not self.scene or not self.simulation_group:
            return

        # Check if THIS scene's client still exists before modifying scene
        if self.scene.is_deleted:
            return

        # Check if event loop is still running
        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed():
                return
        except RuntimeError:
            return

        # Wrap all scene operations in try/except to handle client deletion during shutdown
        try:
            self._do_update_simulation_view()
        except RuntimeError as e:
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                return  # Client deleted during shutdown - safe to ignore
            raise

    def _do_update_simulation_view(self) -> None:
        """Internal implementation of simulation view update."""
        # Visibility check for paths and targets
        if not simulation_state.paths_visible:
            if self.path_group is not None:
                self.path_group.visible(False)
            if self.targets_group is not None:
                self.targets_group.visible(False)
            return

        if self.path_group is not None:
            self.path_group.visible(True)
        if self.targets_group is not None:
            self.targets_group.visible(True)

        all_segments = simulation_state.path_segments
        new_count = len(all_segments)
        old_count = len(self._rendered_segments)
        tool_actions = simulation_state.tool_actions
        targets = simulation_state.targets

        # Early-out: nothing to render and nothing rendered
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
        first_cmd_idx = -1  # first non-travel segment index
        last_cmd_idx = -1  # last non-travel segment index

        if new_count == 0 and old_count > 0:
            if TRACE_ENABLED:
                logger.trace("SCENE: Clearing all segments (count went to 0)")
            self._clear_path_state()
        elif new_count > 0 and self.path_group and self.scene:
            blend_range = int(self._BLEND_RANGE)
            rebuild_line_map = False

            # Diff existing slots and track first/last non-travel
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
                    # Fingerprint matches — check if line number shifted
                    if segment.line_number != old_ln and old_rs is not None:
                        self._rendered_segments[i] = old_rs._replace(
                            line_number=segment.line_number,
                        )
                        rebuild_line_map = True
                    continue

                # Fingerprint differs — delete old objects, render new
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

            # Append new slots
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

            # Remove trailing slots
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

            # Rebuild line_number -> segment indices map
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
                    # Update segment_index if it changed
                    if (
                        old_ri is not None
                        and action.segment_index != old_ri.segment_index
                    ):
                        self._rendered_tool_actions[i] = old_ri._replace(
                            segment_index=action.segment_index,
                        )
                    continue
                # Fingerprint differs — delete old, render new
                if old_ri is not None:
                    for obj in old_ri.objects:
                        self._safe_delete(obj)
                if self.path_group and self.scene:
                    with self.scene:
                        with self.path_group:
                            objs = self.render_tool_action(action)
                    self._rendered_tool_actions[i] = RenderedItem(
                        objects=objs,
                        fingerprint=fp,
                        segment_index=action.segment_index,
                    )
                else:
                    self._rendered_tool_actions[i] = None
            # Append new
            if new_ta_count > old_ta_count:
                for i in range(old_ta_count, new_ta_count):
                    action = tool_actions[i]
                    fp = _tool_action_fingerprint(action)
                    if self.path_group and self.scene:
                        with self.scene:
                            with self.path_group:
                                objs = self.render_tool_action(action)
                        self._rendered_tool_actions.append(
                            RenderedItem(
                                objects=objs,
                                fingerprint=fp,
                                segment_index=action.segment_index,
                            )
                        )
                    else:
                        self._rendered_tool_actions.append(None)
            # Remove trailing
            if new_ta_count < old_ta_count:
                for i in range(new_ta_count, old_ta_count):
                    old_ri = self._rendered_tool_actions[i]
                    if old_ri is not None:
                        for obj in old_ri.objects:
                            self._safe_delete(obj)
                del self._rendered_tool_actions[new_ta_count:]

        # Highlight path segments matching active cursor line
        if simulation_state.paths_visible and self._rendered_segments:
            self.update_cursor_line_highlight()

        # Waypoint markers at segment endpoints
        if self.targets_group and self.scene:
            self._update_waypoint_and_target_markers(
                all_segments,
                targets,
                first_cmd_idx,
                last_cmd_idx,
            )

        # Apply playback opacity AFTER all elements are rendered
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
                objs, obj_colors, uses_vc = self._render_path_segment(
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
        # Derive first/last command positions from segment indices
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

        # Build target lookup by line_number
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
            # Fingerprint differs — delete old, render new
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

        # Append new waypoints
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

        # Remove trailing waypoints
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

            # Derive segment_index from line_to_segments map
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
                # Detect shape or color change — recreate marker
                if td.get("shape_type") != shape or td.get("color") != color:
                    # Delete old group and recreate
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

        # Remove stale editable targets
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
            # Check if object ID still exists in scene before deleting
            if hasattr(obj, "id") and obj.id in self.scene.objects:
                obj.delete()
        except (KeyError, RuntimeError):
            # KeyError: Object was already deleted from scene
            # RuntimeError: Client was deleted (shutdown race condition)
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

        # Find closest invalid segment before and after
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

        # Skip if all colors are the original
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
        cursor_line = simulation_state.active_cursor_line
        if cursor_line == self._highlighted_line:
            return
        prev_line = self._highlighted_line
        self._highlighted_line = cursor_line

        # Restore previously highlighted segments to their display color
        if prev_line > 0:
            for i in self._line_to_segments.get(prev_line, ()):
                self._restore_segment_material(i)

        # Apply glow highlight to segments matching the new cursor line
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
        """Clear rendered paths and reset cache, forcing a full re-render on next update.

        Call this when switching tabs or when the path data has completely changed.
        """
        self._clear_path_state()

    def update_playback_opacity(self) -> None:
        """Fade completed elements to 50% opacity based on current_step_index.

        Applies opacity to ALL element types: segments, tool actions,
        non-editable waypoints, and editable targets.
        Only touches items whose opacity actually changed.
        """
        step = simulation_state.current_step_index
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
                # First update — apply all segments
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
            lo, hi = 0, n_rendered  # first update — touch all

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

        This method should be called directly from the status update loop
        in main.py to ensure reliable updates without context issues.
        """
        self._update_jog_ball_from_robot_state()
        self._update_envelope_from_robot_state()
        self.update_tool_animation()

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
        """
        # Don't update robot joints during editing mode - user is manipulating them
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            return

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

        # Only apply to robot if in editing mode
        if self._appearance_mode == RobotAppearanceMode.EDITING:
            self._apply_joint_angles(self._editing_angles)
            self._update_tcp_ball_position()

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

        # Default: reset offsets
        origin = [0.0, 0.0, 0.0]
        rpy = [0.0, 0.0, 0.0]

        tool_pose: ToolPose | None = None

        # Try resolver first
        if self.config.tool_pose_resolver is not None:
            tool_pose = self.config.tool_pose_resolver(tool, variant_key)

        # Fall back to map
        if tool_pose is None and tool in self.config.tool_pose_map:
            tool_pose = self.config.tool_pose_map[tool]

        # Apply pose if found
        if tool_pose is not None:
            if tool_pose.origin and len(tool_pose.origin) == 3:
                origin = list(tool_pose.origin)
            if tool_pose.rpy and len(tool_pose.rpy) == 3:
                rpy = list(tool_pose.rpy)

        self.tcp_offset.move(*origin).rotate(*rpy)

        # Track tool offset for envelope calculations
        self._current_tool = tool or "none"
        self._current_tool_offset_z = origin[2] if len(origin) > 2 else 0.0

        # Update envelope sphere if it exists
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

        # Remove old tool meshes
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

        # Look up tool spec
        try:
            tool_spec = ui_state.active_robot.tools[tool_key]
        except (KeyError, AttributeError):
            logger.debug("No tool spec for '%s'; tool meshes cleared", tool_key)
            return

        meshes = getattr(tool_spec, "meshes", ())
        motions = getattr(tool_spec, "motions", ())

        # Override with variant meshes/motions if specified
        if variant_key:
            for v in getattr(tool_spec, "variants", ()):
                if v.key == variant_key:
                    meshes = v.meshes
                    motions = v.motions
                    break

        if not meshes:
            return

        # Store motion descriptors from tool spec
        self._tool_motions = list(motions)
        self._tool_has_motions = bool(motions)
        # Collect which roles need mesh tracking
        motion_roles = {m.role for m in self._tool_motions}

        # Determine per-role appearance for new meshes
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

                    url = f"{self.meshes_url}/{filename}"
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
        """
        # Update engaged color state (applies to all tools, not just those with motions)
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
                    sign = (1.0 if i % 2 == 0 else -1.0) if motion.symmetric else 1.0
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
                    sign = (1.0 if i % 2 == 0 else -1.0) if motion.symmetric else 1.0
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
            # Tools with motions: activated color only on moving parts
            moving_meshes = {m for ms in self._tool_motion_meshes.values() for m in ms}
            color = moving_color if engaged else body_color
            for mesh in moving_meshes:
                mesh.material(color, opacity)
        else:
            # Binary tools without motions (vacuum, etc.): color the whole tool
            color = moving_color if engaged else body_color
            for mesh in self._tool_meshes:
                mesh.material(color, opacity)

    def set_appearance_mode(self, mode: RobotAppearanceMode) -> None:
        """Set robot appearance mode.

        Args:
            mode: The appearance mode to set (LIVE, SIMULATOR, or EDITING)
        """
        self._appearance_mode = mode

        # Get appearance settings based on mode
        body_color, moving_color, opacity = self._get_tool_colors()
        arm_color = {
            RobotAppearanceMode.LIVE: self.config.material,
            RobotAppearanceMode.SIMULATOR: self.config.sim_color,
        }.get(mode, self.config.edit_color)

        # Apply to arm meshes
        for mesh in self._robot_meshes:
            mesh.material(arm_color, opacity)

        # Apply to tool body meshes
        for mesh in self._tool_body_meshes:
            mesh.material(body_color, opacity)

        # Apply to tool moving part meshes
        moving_meshes = {m for ms in self._tool_motion_meshes.values() for m in ms}
        for mesh in moving_meshes:
            mesh.material(moving_color, opacity)

        logger.debug("Robot appearance mode set to %s", mode.value)

    def set_simulator_appearance(self, active: bool) -> None:
        """Apply or remove simulator visual appearance (amber ghosting).

        Args:
            active: True to apply simulator appearance, False to restore default
        """
        # Don't change mode if currently in EDITING mode
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
        # Static transform from parent link to this joint frame
        with ui.scene.group().move(*t).rotate(*r):
            # Dynamic transform for joint value (q)
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
                        # Continue recursion
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
            # Apply visual origin offset if present
            if visual.origin is not None:
                t, r = get_transl_and_rpy(visual.origin)
                if any(v != 0 for v in t):
                    obj.move(*t)
                if any(v != 0 for v in r):
                    obj.rotate(*r)
            if material is not None:
                obj.material(material)
            # Track mesh object for simulator appearance changes
            self._robot_meshes.append(obj)

    def _stl_to_url(self, stl_path: str) -> str:
        """Convert STL file path to URL, preferring _simplified variants if they exist."""
        # Handle file:// URIs
        if stl_path.startswith("file://"):
            parsed = urlparse(stl_path)
            stl_path = url2pathname(parsed.path)

        # Get path relative to meshes_dir
        stl_full = Path(stl_path)
        if stl_full.is_absolute():
            try:
                rel_path = stl_full.relative_to(self.meshes_dir)
            except ValueError:
                rel_path = Path(stl_full.name)
        else:
            rel_path = stl_full

        # Check for _simplified variant (e.g., part.STL -> part_simplified.stl)
        # Try both original extension case and lowercase .stl
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
