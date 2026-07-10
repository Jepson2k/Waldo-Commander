"""Pre-compiled numba pipelines for hot path operations.

These functions combine multiple operations into single @njit functions
to eliminate Python interpreter overhead between steps. They call existing
numba functions from pinokin.numba_se3 which get inlined by numba.
"""

import numpy as np
from numba import njit

# so3_rpy used in local njit functions
from pinokin import so3_rpy, warmup_numba_se3


@njit(cache=True)
def angle_pipeline(
    angles_in: np.ndarray,
    index_mapping: np.ndarray,
    signs: np.ndarray,
    offsets: np.ndarray,
    urdf_reorder: np.ndarray,
    angles_out: np.ndarray,
) -> bool:
    """Validate + Map + Sign + Offset + Deg2Rad + Reorder in ONE pass.

    Args:
        angles_in: Input angles in degrees (N elements, float64)
        index_mapping: Controller index mapping (N elements, int32)
        signs: Sign corrections per joint (N elements, float64)
        offsets: Angle offsets per joint in degrees (N elements, float64)
        urdf_reorder: URDF joint reorder mapping (N elements, int32)
        angles_out: Output buffer for angles in radians (N elements, float64)

    Returns:
        True if valid, False if any angle is non-finite
    """
    deg_to_rad = 0.017453292519943295
    n = len(angles_in)

    for i in range(n):
        if not np.isfinite(angles_in[i]):
            return False

    # Fused per-element transform: map -> sign -> offset -> deg2rad -> reorder.
    for i in range(n):
        src_idx = index_mapping[i]
        val = (angles_in[src_idx] * signs[src_idx] + offsets[src_idx]) * deg_to_rad
        dst_idx = urdf_reorder[i]
        angles_out[dst_idx] = val

    return True


@njit(cache=True)
def pose_extraction_pipeline(
    pose: np.ndarray,
    rot_buf: np.ndarray,
    rpy_buf: np.ndarray,
    result: np.ndarray,
) -> None:
    """Extract rotation/translation from flattened 4x4 matrix + so3_rpy + degrees.

    Args:
        pose: Flattened 4x4 homogeneous transform (16 elements, float64)
        rot_buf: Scratch buffer for 3x3 rotation matrix (3x3, float64)
        rpy_buf: Scratch buffer for RPY radians (3 elements, float64)
        result: Output buffer [x, y, z, rx_deg, ry_deg, rz_deg] (6 elements, float64)
    """
    rad_to_deg = 57.29577951308232

    # pose is row-major; these indices pick the 3x3 rotation block.
    rot_buf[0, 0] = pose[0]
    rot_buf[0, 1] = pose[1]
    rot_buf[0, 2] = pose[2]
    rot_buf[1, 0] = pose[4]
    rot_buf[1, 1] = pose[5]
    rot_buf[1, 2] = pose[6]
    rot_buf[2, 0] = pose[8]
    rot_buf[2, 1] = pose[9]
    rot_buf[2, 2] = pose[10]

    # Translation in mm.
    result[0] = pose[3]
    result[1] = pose[7]
    result[2] = pose[11]

    # Inlined by numba.
    so3_rpy(rot_buf, rpy_buf)

    result[3] = rpy_buf[0] * rad_to_deg
    result[4] = rpy_buf[1] * rad_to_deg
    result[5] = rpy_buf[2] * rad_to_deg


def warmup_pipelines() -> None:
    """Pre-compile all numba functions with dummy data.

    Call this during app startup to avoid JIT compilation lag
    during the first hot path execution.
    """
    dummy_angles = np.zeros(6, dtype=np.float64)
    dummy_pose = np.zeros(16, dtype=np.float64)
    dummy_rot = np.zeros((3, 3), dtype=np.float64)
    dummy_rpy = np.zeros(3, dtype=np.float64)
    dummy_result = np.zeros(6, dtype=np.float64)
    dummy_mapping = np.arange(6, dtype=np.int32)
    dummy_signs = np.ones(6, dtype=np.float64)
    dummy_offsets = np.zeros(6, dtype=np.float64)

    angle_pipeline(
        dummy_angles,
        dummy_mapping,
        dummy_signs,
        dummy_offsets,
        dummy_mapping,
        dummy_result,
    )
    pose_extraction_pipeline(dummy_pose, dummy_rot, dummy_rpy, dummy_result)

    # Warm up pinokin's zero-allocation SE3/SO3 functions
    warmup_numba_se3()
