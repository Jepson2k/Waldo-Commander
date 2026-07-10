"""Path renderer.

Standalone class held by ``UrdfScene`` via composition. Stateless — all
methods rely on the caller having an active ``ui.scene`` context (i.e.
``with urdf_scene.scene:`` block surrounding the call).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy.spatial.transform import Rotation as ScipyRotation
from nicegui import ui

if TYPE_CHECKING:
    from waldo_commander.state import PathSegment, ToolAction

_DEFAULT_CONE_AXIS = np.array([0.0, 1.0, 0.0], dtype=np.float64)
_PI_ROTATION_RPY = [math.pi, 0.0, 0.0]
_ZERO_ROTATION_RPY = [0.0, 0.0, 0.0]


def _hex_to_rgb(hex_color: str) -> list[float]:
    """Convert '#RRGGBB' to [r, g, b] floats in 0-1 range."""
    h = hex_color.lstrip("#")
    return [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]


def _extract_tcp_rotation(tcp_pose: list[float]) -> ScipyRotation | None:
    """Extract a ScipyRotation from a TCP pose ``[x, y, z, rx, ry, rz]`` if rotation present."""
    if len(tcp_pose) < 6:
        return None
    return ScipyRotation.from_euler("XYZ", [tcp_pose[3], tcp_pose[4], tcp_pose[5]])


class PathRenderer:
    """Renders path segments and tool action arrows inside an active scene context."""

    def render_path_segment(
        self,
        segment: PathSegment,
        point_pair_colors: list[str] | None = None,
        *,
        opacity: float = 1.0,
        force_dashed: bool = False,
    ) -> tuple[list[Any], list[str], bool]:
        """Render a path segment as a single polyline plus direction arrow cones.

        Args:
            segment: PathSegment object with points, color, is_dashed, show_arrows
            point_pair_colors: Optional per-point-pair colors (one per consecutive
                point pair). If None, uses segment.color for all objects.
            opacity: Material opacity (1.0 = fully opaque).
            force_dashed: Force dashed rendering regardless of segment.is_dashed.

        Returns:
            Tuple of (scene objects, display color per object, uses_vertex_colors).
            The first object is always the polyline (if any points exist).
        """
        objects: list[Any] = []
        object_colors: list[str] = []
        points = segment.points
        default_color = segment.color
        is_dashed = segment.is_dashed or force_dashed
        show_arrows = segment.show_arrows
        uses_vertex_colors = False

        if len(points) < 2:
            return objects, object_colors, uses_vertex_colors

        arrow_distance = 0.020  # Arrow every 20mm of TCP arc-length
        arrow_scale = 0.003  # Arrow cone size

        pts = np.asarray(points, dtype=np.float64)
        pts_list = pts.tolist()

        vertex_colors = None
        if point_pair_colors is not None:
            uses_vertex_colors = True
            # N points → N-1 pairs; each vertex gets its pair's color, final
            # vertex reuses the last pair's color.
            vertex_colors = []
            for i, hex_c in enumerate(point_pair_colors):
                vertex_colors.append(_hex_to_rgb(hex_c))
            vertex_colors.append(_hex_to_rgb(point_pair_colors[-1]))

        line = ui.scene.polyline(
            pts_list,
            colors=vertex_colors,
            dashed=is_dashed,
            dash_size=0.008,
            gap_size=0.004,
        )
        if vertex_colors:
            # color=None tells Three.js to use the per-vertex colors.
            line.material(None, opacity)
        else:
            line.material(default_color, opacity)
        objects.append(line)
        object_colors.append(default_color)

        if show_arrows:
            accum = 0.0
            for i in range(1, len(points)):
                d = float(np.linalg.norm(pts[i] - pts[i - 1]))
                accum += d
                if accum >= arrow_distance:
                    seg_vec = pts[i] - pts[i - 1]
                    seg_len = float(np.linalg.norm(seg_vec))
                    if seg_len < 1e-6:
                        continue
                    direction = seg_vec / seg_len
                    midpoint = (pts[i - 1] + pts[i]) * 0.5
                    color = (
                        point_pair_colors[min(i - 1, len(point_pair_colors) - 1)]
                        if point_pair_colors
                        else default_color
                    )
                    cone = self._create_direction_cone(
                        midpoint, direction, arrow_scale, color, opacity
                    )
                    objects.append(cone)
                    object_colors.append(color)
                    accum = 0.0

        return objects, object_colors, uses_vertex_colors

    def _create_direction_cone(
        self,
        position: np.ndarray,
        direction: np.ndarray,
        scale: float,
        color: str,
        opacity: float = 0.9,
    ) -> Any:
        """Create a small cone pointing in the given direction."""
        cross = np.cross(_DEFAULT_CONE_AXIS, direction)
        dot = np.dot(_DEFAULT_CONE_AXIS, direction)

        if np.linalg.norm(cross) < 1e-6:
            if dot > 0:
                rpy = _ZERO_ROTATION_RPY
            else:
                rpy = _PI_ROTATION_RPY
        else:
            angle = math.acos(max(-1.0, min(1.0, float(dot))))
            axis = cross / np.linalg.norm(cross)
            rot = ScipyRotation.from_rotvec(angle * axis)
            rpy = rot.as_euler("xyz", degrees=False).tolist()

        cone = ui.scene.cylinder(
            top_radius=0.0,
            bottom_radius=scale,
            height=scale * 2,
            radial_segments=24,
            height_segments=1,
            wireframe=False,
        )
        cone.move(*position)
        cone.rotate(rpy[0], rpy[1], rpy[2])
        cone.material(color, opacity)

        return cone

    def _create_arrow(
        self,
        direction: list[float],
        origin: list[float],
        length: float,
        color: str,
        head_length: float,
        head_width: float,
        opacity: float = 1.0,
    ) -> list[Any]:
        """Create an arrow from separate line + cone (supports .material()).

        Unlike ArrowHelper, each sub-object can have its opacity updated
        individually via .material().
        """
        d = np.asarray(direction, dtype=np.float64)
        d_norm = float(np.linalg.norm(d))
        if d_norm < 1e-9:
            return []
        d = d / d_norm

        o = np.asarray(origin, dtype=np.float64)
        shaft_end = o + d * (length - head_length)
        head_pos = o + d * (length - head_length * 0.5)

        line = ui.scene.line(o.tolist(), shaft_end.tolist())
        line.material(color, opacity)

        cone = self._create_direction_cone(
            head_pos, d, head_width * 0.5, color, opacity
        )

        return [line, cone]

    def render_tool_action(
        self,
        action: ToolAction,
        color: str = "#FF9800",
    ) -> list[Any]:
        """Render a tool action as arrows at the TCP position(s).

        If the action has a tcp_path (multiple TCP samples over the action
        duration), renders cascading jaw-tip arrows that show the gripper
        closing while the arm moves.  Otherwise falls back to a single
        pair of arrows at the final TCP position.
        """
        objects: list[Any] = []
        if action.tcp_pose is None or len(action.tcp_pose) < 3:
            return objects

        if action.tcp_path and len(action.tcp_path) >= 2:
            return self._render_cascading_tool_action(action, color)

        return self._render_single_tool_action(action, color)

    def _render_single_tool_action(
        self,
        action: ToolAction,
        color: str,
    ) -> list[Any]:
        """Render arrows at a single TCP position (fallback when no tcp_path)."""
        objects: list[Any] = []
        assert action.tcp_pose is not None
        px, py, pz = action.tcp_pose[0], action.tcp_pose[1], action.tcp_pose[2]

        tcp_rot = _extract_tcp_rotation(action.tcp_pose)

        for idx, motion in enumerate(action.motions):
            target = (
                action.target_positions[idx]
                if idx < len(action.target_positions)
                else 0.0
            )
            local_axis = np.asarray(motion.get("axis", (0, 0, 1)), dtype=np.float64)
            if tcp_rot is not None:
                world_axis = tcp_rot.apply(local_axis)
            else:
                world_axis = local_axis

            if motion.get("type") == "linear":
                travel = motion.get("travel_m", 0.01)
                symmetric = motion.get("symmetric", True)
                jaw_travel = travel if symmetric else travel * 0.5
                arrow_length = travel * 0.5
                head_length = arrow_length * 0.5
                head_width = head_length * 1.5
                start_val = (
                    action.start_positions[idx]
                    if idx < len(action.start_positions)
                    else (0.0 if target > 0.5 else 1.0)
                )
                closing = target > start_val
                jaw_offset = jaw_travel * (1.0 - start_val)

                if symmetric:
                    for sign in [1.0, -1.0]:
                        d = world_axis * sign
                        if closing:
                            d = -d
                        origin_dist = (
                            jaw_offset + arrow_length if closing else jaw_offset
                        ) * sign
                        arrow_origin = [
                            px + world_axis[0] * origin_dist,
                            py + world_axis[1] * origin_dist,
                            pz + world_axis[2] * origin_dist,
                        ]
                        objects.extend(
                            self._create_arrow(
                                d.tolist(),
                                arrow_origin,
                                arrow_length,
                                color,
                                head_length,
                                head_width,
                            )
                        )
                else:
                    d = -world_axis if closing else world_axis
                    origin_dist = jaw_offset + arrow_length if closing else jaw_offset
                    origin = [
                        px + world_axis[0] * origin_dist,
                        py + world_axis[1] * origin_dist,
                        pz + world_axis[2] * origin_dist,
                    ]
                    objects.extend(
                        self._create_arrow(
                            d.tolist(),
                            origin,
                            arrow_length,
                            color,
                            head_length,
                            head_width,
                        )
                    )

        return objects

    def _render_cascading_tool_action(
        self,
        action: ToolAction,
        color: str,
    ) -> list[Any]:
        """Render cascading jaw-tip arrows along the TCP path.

        Arrows are placed at consistent time intervals along tcp_path.
        Each arrow pair shows where the jaw tips physically are in 3D space
        at that time step — both the arm translation and jaw closing are
        interpolated simultaneously.
        """
        objects: list[Any] = []
        assert action.tcp_pose is not None and action.tcp_path is not None
        path = action.tcp_path
        n = len(path)

        tcp_rot = _extract_tcp_rotation(action.tcp_pose)

        for idx, motion in enumerate(action.motions):
            if motion.get("type") != "linear":
                continue

            target = (
                action.target_positions[idx]
                if idx < len(action.target_positions)
                else 0.0
            )
            local_axis = np.asarray(motion.get("axis", (0, 0, 1)), dtype=np.float64)
            if tcp_rot is not None:
                world_axis = tcp_rot.apply(local_axis)
            else:
                world_axis = local_axis

            travel = motion.get("travel_m", 0.01)
            symmetric = motion.get("symmetric", True)

            jaw_travel = travel if symmetric else travel * 0.5
            arrow_scale = travel * 0.3
            head_length = arrow_scale * 0.5
            head_width = head_length * 1.5

            # Position 0=open (offset=jaw_travel), 1=closed (offset=0).
            start_val = (
                action.start_positions[idx]
                if idx < len(action.start_positions)
                else (0.0 if target > 0.5 else 1.0)
            )

            # Space arrows by combined TCP + jaw movement distance.
            # Minimum spacing = 3× arrow scale so arrows don't overlap.
            min_spacing = arrow_scale * 3
            last_px, last_py, last_pz = path[0][0], path[0][1], path[0][2]
            last_jaw = jaw_travel * (1.0 - start_val)
            accum = min_spacing  # place first arrow immediately
            moving_inward = target > start_val

            for i in range(n):
                t = i / max(1, n - 1)  # 0.0 → 1.0

                pos = start_val * (1.0 - t) + target * t
                jaw_offset = jaw_travel * (1.0 - pos)

                pt = path[i]
                px, py, pz = pt[0], pt[1], pt[2]

                tcp_dist = (
                    (px - last_px) ** 2 + (py - last_py) ** 2 + (pz - last_pz) ** 2
                ) ** 0.5
                jaw_dist = abs(jaw_offset - last_jaw)
                accum += tcp_dist + jaw_dist
                last_px, last_py, last_pz = px, py, pz
                last_jaw = jaw_offset

                is_first = i == 0
                is_last = i == n - 1
                if not is_first and not is_last and accum < min_spacing:
                    continue
                accum = 0.0

                if symmetric:
                    for sign in [1.0, -1.0]:
                        d = world_axis * sign
                        if moving_inward:
                            d = -d  # point inward
                        # Offset the origin so the arrow HEAD lands at the jaw
                        # position: inward arrows shift the base outward by
                        # arrow_scale.
                        origin_offset = (
                            jaw_offset + arrow_scale if moving_inward else jaw_offset
                        )
                        tip_pos = [
                            px + world_axis[0] * origin_offset * sign,
                            py + world_axis[1] * origin_offset * sign,
                            pz + world_axis[2] * origin_offset * sign,
                        ]
                        objects.extend(
                            self._create_arrow(
                                d.tolist(),
                                tip_pos,
                                arrow_scale,
                                color,
                                head_length,
                                head_width,
                            )
                        )
                else:
                    d = world_axis if not moving_inward else -world_axis
                    origin_offset = (
                        jaw_offset + arrow_scale if moving_inward else jaw_offset
                    )
                    tip_pos = [
                        px + world_axis[0] * origin_offset,
                        py + world_axis[1] * origin_offset,
                        pz + world_axis[2] * origin_offset,
                    ]
                    objects.extend(
                        self._create_arrow(
                            d.tolist(),
                            tip_pos,
                            arrow_scale,
                            color,
                            head_length,
                            head_width,
                        )
                    )

        return objects
