"""Configuration dataclasses for UrdfScene."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from waldo_commander.common.theme import SceneColors

#: Render prefix for a shape proposed for the installation layer. Unlike
#: waldoctl's ``install:``/``shape:``/``tool:``, which are the vocabulary a
#: backend reports collisions in, this one is the frontend's alone — a
#: proposal has no backend geometry to be reported as.
DRAFT_PREFIX = "draft:"


class RobotAppearanceMode(Enum):
    """Robot visual appearance modes.

    LIVE: Normal robot view showing real-time joint angles from robot_state
    SIMULATOR: Amber/ghost appearance, still shows real-time angles
    EDITING: Grey semi-transparent appearance for target editing, shows editing angles
    """

    LIVE = "live"
    SIMULATOR = "simulator"
    EDITING = "editing"


@dataclass
class ToolPose:
    """TCP offset and orientation for a tool."""

    origin: Sequence[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rpy: Sequence[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


@dataclass
class UrdfSceneConfig:
    """Configuration for UrdfScene behavior, appearance, and kinematics."""

    meshes_dir: Path | None = None
    """Directory containing mesh files. If None, auto-discover from URDF location."""

    static_url_prefix: str = "/meshes"
    """URL prefix for serving static mesh files."""

    package_map: dict[str, Path] = field(default_factory=lambda: {})
    """Mapping from package:// names to filesystem paths."""

    mount_static: bool = True
    """Whether to automatically mount meshes as static files."""

    scale_stls: float = 1.0
    """Scale factor for all STL files (e.g., 1e-1 if designed in mm)."""

    gizmo_scale: float | None = None
    """Override gizmo size. If None, scales with STL scale."""

    draw_tcp_axes: bool = True
    """Whether to draw coordinate axes at TCP location."""

    tool_pose_map: dict[str, "ToolPose"] = field(default_factory=lambda: {})
    """Mapping from tool names to TCP poses."""

    tool_pose_resolver: Callable[[str, str | None], "ToolPose | None"] | None = None
    """Function to resolve tool name to TCP pose dynamically.

    Signature: ``(tool_key, variant_key) -> ToolPose | None``.
    """

    # Colors from theme.py SceneColors
    material: str = SceneColors.MATERIAL_DARK_HEX
    """Default material color for robot meshes."""

    background_color: str = SceneColors.BACKGROUND_DARK_HEX
    """Scene background color."""

    ground_color: str = SceneColors.GROUND_DARK_HEX
    """Ground plane color (contrasts with background)."""

    sim_color: str = SceneColors.SIM_AMBER_HEX
    """Color for robot in simulator mode (amber ghost)."""

    sim_opacity: float = 0.9
    """Opacity for robot in simulator mode."""

    edit_color: str = SceneColors.EDIT_GRAY_HEX
    """Color for robot in editing mode (grey ghost)."""

    edit_opacity: float = 0.4
    """Opacity for robot in editing mode."""

    tool_body_material: str = SceneColors.TOOL_BODY_HEX
    """Color for tool body meshes in live mode."""

    tool_body_sim_color: str = SceneColors.TOOL_BODY_SIM_HEX
    """Color for tool body meshes in simulator mode."""

    tool_body_edit_color: str = SceneColors.TOOL_BODY_EDIT_HEX
    """Color for tool body meshes in editing mode."""

    tool_moving_material: str = SceneColors.TOOL_MOVING_HEX
    """Color for tool moving parts in live mode."""

    tool_moving_sim_color: str = SceneColors.TOOL_MOVING_SIM_HEX
    """Color for tool moving parts in simulator mode."""

    tool_moving_edit_color: str = SceneColors.TOOL_MOVING_EDIT_HEX
    """Color for tool moving parts in editing mode."""

    joint_name_order: list[str] = field(
        default_factory=lambda: ["L1", "L2", "L3", "L4", "L5", "L6"]
    )
    """Order of joint names for mapping controller angles to URDF joints."""

    deg_to_rad: bool = True
    """Whether to convert angles from degrees to radians."""

    angle_signs: list[int] = field(default_factory=lambda: [1, 1, 1, 1, 1, 1])
    """Sign corrections for each joint angle."""

    angle_offsets: list[float] = field(
        default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    )
    """Offset corrections for each joint angle (in degrees if deg_to_rad is True)."""
