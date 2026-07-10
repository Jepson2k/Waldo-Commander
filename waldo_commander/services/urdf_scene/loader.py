"""
URDF loading utilities and static math helpers.

Contains:
- URDF loading with package:// resolution
- Mesh directory resolution
- Transformation math utilities (rotation, translation)
"""

import logging
import re
import tempfile
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation as R
from urchin import URDF

logger = logging.getLogger(__name__)


def load_urdf(
    path: Path,
    *,
    package_map: dict[str, Path] | None = None,
    lazy: bool = True,
) -> URDF:
    """Load a URDF file into memory, expanding package:// URIs to absolute paths.

    Args:
        path: Path to URDF file
        package_map: Optional mapping from package names to filesystem paths
        lazy: Whether to lazy-load meshes

    Returns:
        Loaded URDF object

    Raises:
        FileNotFoundError: If URDF file not found
        ValueError: If package:// URI found but package not in map and no fallback possible
    """
    package_map = package_map or {}

    if not path.exists():
        raise FileNotFoundError(f"URDF file not found: {path}")

    with open(path, "r") as file:
        content = file.read()

    package_names = set(re.findall(r"package://(\w+)", content))

    if not package_names:
        return URDF.load(str(path), lazy_load_meshes=lazy)

    modified_content = content
    for pkg_name in package_names:
        if pkg_name in package_map:
            pkg_path = package_map[pkg_name]
            replacement = pkg_path.as_uri()
        else:
            # Fall back to the URDF's own directory when a package isn't mapped.
            replacement = path.parent.as_uri()
            logger.warning(
                f"Package '{pkg_name}' not in package_map; using fallback: {replacement}"
            )

        modified_content = modified_content.replace(
            f"package://{pkg_name}", replacement
        )

    with tempfile.TemporaryDirectory() as tmpdirname:
        tmp_file_path = Path(tmpdirname) / path.name
        tmp_file_path.write_text(modified_content, encoding="utf-8")
        return URDF.load(str(tmp_file_path), lazy_load_meshes=lazy)


def resolve_meshes_dir(urdf_path: Path, configured_dir: Path | None = None) -> Path:
    """Resolve mesh directory from config or URDF location.

    Args:
        urdf_path: Path to the URDF file
        configured_dir: Explicitly configured meshes directory (optional)

    Returns:
        Path to the meshes directory

    Raises:
        NotADirectoryError: If meshes directory cannot be found
    """
    if configured_dir is not None:
        meshes_dir = Path(configured_dir)
        if not meshes_dir.exists():
            raise NotADirectoryError(
                f"Configured meshes_dir does not exist: {meshes_dir}"
            )
        return meshes_dir

    # Auto-discover a sibling "meshes" dir, then one level up.
    meshes_link = urdf_path.parent / "meshes"
    if meshes_link.exists():
        return meshes_link

    meshes_link = urdf_path.parent.parent / "meshes"
    if meshes_link.exists():
        return meshes_link

    raise NotADirectoryError(
        f"Could not find meshes directory. Checked: {urdf_path.parent}/meshes "
        f"and {urdf_path.parent.parent}/meshes"
    )


def get_transl_and_rpy(mat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return translation and Euler rpy from 4x4 homogeneous transformation.

    Args:
        mat: 4x4 homogeneous transformation matrix

    Returns:
        Tuple of (translation, rpy) where translation is [x,y,z] and rpy is [roll,pitch,yaw]
    """
    trans = mat[:3, 3]
    rpy = R.from_matrix(mat[:3, :3]).as_euler("xyz", degrees=False)
    return trans, rpy


def rot_joint(axis: np.ndarray, rot_rad: float) -> tuple[np.ndarray, np.ndarray]:
    """Transformation for rotatory joint around `axis` with value `rot_rad` [rad].

    Args:
        axis: Joint axis of rotation (3D vector)
        rot_rad: Rotation angle in radians

    Returns:
        Tuple of (translation, rpy) - translation is zero for revolute joints
    """
    norm_axis = axis / np.linalg.norm(axis)
    rpy = R.from_rotvec(rot_rad * norm_axis).as_euler("xyz", degrees=False)
    t = np.zeros_like(rpy)
    return t, rpy


def transl_joint(axis: np.ndarray, transl: float) -> tuple[np.ndarray, np.ndarray]:
    """Transformation for translational joint along `axis` with value `transl` [m].

    Args:
        axis: Joint axis of translation (3D vector)
        transl: Translation distance in meters

    Returns:
        Tuple of (translation, rpy) - rpy is zero for prismatic joints
    """
    norm_axis = axis / np.linalg.norm(axis)
    t = transl * norm_axis
    rpy = np.zeros_like(t)
    return t, rpy


def normalize_axis(axis) -> np.ndarray:
    """Normalize an axis-like value to a 3-vector (numpy array).

    Accepts list/tuple/ndarray of length 3 or an object with x,y,z attributes.
    Returns a unit vector. If invalid or near-zero, logs a warning and returns Z-axis.

    Args:
        axis: Axis value (list, tuple, ndarray, or object with x,y,z attributes)

    Returns:
        Normalized 3D numpy array
    """
    vec: np.ndarray | None = None

    if isinstance(axis, (list, tuple, np.ndarray)):
        try:
            a = np.asarray(axis, dtype=float).reshape(-1)
            if a.size >= 3:
                vec = a[:3].astype(float)
        except (ValueError, TypeError):
            vec = None
    else:
        try:
            x = getattr(axis, "x", None)
            y = getattr(axis, "y", None)
            z = getattr(axis, "z", None)
            if all(isinstance(v, (int, float)) for v in (x, y, z)):
                vec = np.array([x, y, z], dtype=float)
        except AttributeError:
            vec = None

    if vec is None or not np.all(np.isfinite(vec)):
        logger.warning("Invalid joint axis encountered; defaulting to Z-axis")
        return np.array([0.0, 0.0, 1.0], dtype=float)

    n = float(np.linalg.norm(vec))
    if n < 1e-9:
        logger.warning("Near-zero joint axis magnitude; defaulting to Z-axis")
        return np.array([0.0, 0.0, 1.0], dtype=float)

    return vec / n
