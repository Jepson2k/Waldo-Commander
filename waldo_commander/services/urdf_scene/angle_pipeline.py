"""Angle pipeline: maps controller joint angles to URDF scene joint values.

Owns pre-allocated numpy buffers and delegates to the numba angle_pipeline
kernel. Call init_buffers() once at startup, then update_urdf_angles() at 50Hz.
"""

import logging

import numpy as np

from waldo_commander.numba_pipelines import angle_pipeline
from waldo_commander.state import ui_state

logger = logging.getLogger(__name__)

# Pre-allocated buffers (module-level, resized by init_buffers)
_angles_ordered_buffer: np.ndarray = np.zeros(6, dtype=np.float64)
_angle_signs_array: np.ndarray = np.ones(6, dtype=np.float64)
_angle_offsets_array: np.ndarray = np.zeros(6, dtype=np.float64)
_index_mapping_array: np.ndarray = np.arange(6, dtype=np.int32)
_urdf_reorder_array: np.ndarray = np.arange(6, dtype=np.int32)
_config_valid: bool = False


def init_buffers(num_joints: int) -> None:
    """Resize pipeline buffers to match the robot's joint count."""
    global _angles_ordered_buffer, _angle_signs_array, _angle_offsets_array
    global _index_mapping_array, _urdf_reorder_array
    _angles_ordered_buffer = np.zeros(num_joints, dtype=np.float64)
    _angle_signs_array = np.ones(num_joints, dtype=np.float64)
    _angle_offsets_array = np.zeros(num_joints, dtype=np.float64)
    _index_mapping_array = np.arange(num_joints, dtype=np.int32)
    _urdf_reorder_array = np.arange(num_joints, dtype=np.int32)


def _init_config() -> None:
    """Precompute the index/sign/offset mappings the numba kernel needs, once after URDF scene init."""
    global _config_valid

    if not ui_state.urdf_scene:
        _config_valid = False
        return

    try:
        config = ui_state.urdf_scene.config
        index_mapping = ui_state.urdf_index_mapping
        joint_name_order = config.joint_name_order
        urdf_joint_names = ui_state.urdf_scene.joint_names

        num_joints = ui_state.active_robot.joints.count

        # For each output position: which input index to read, plus its sign/offset.
        for i in range(num_joints):
            if i < len(index_mapping) and index_mapping[i] < num_joints:
                controller_idx = index_mapping[i]
                _index_mapping_array[i] = controller_idx

                if controller_idx < len(config.angle_signs):
                    _angle_signs_array[i] = (
                        1.0 if config.angle_signs[controller_idx] >= 0 else -1.0
                    )
                else:
                    _angle_signs_array[i] = 1.0

                if controller_idx < len(config.angle_offsets):
                    _angle_offsets_array[i] = config.angle_offsets[controller_idx]
                else:
                    _angle_offsets_array[i] = 0.0
            else:
                _index_mapping_array[i] = i
                _angle_signs_array[i] = 1.0
                _angle_offsets_array[i] = 0.0

        # Build URDF reorder mapping
        for i, joint_name in enumerate(urdf_joint_names[:num_joints]):
            try:
                urdf_idx = joint_name_order.index(joint_name)
                _urdf_reorder_array[i] = urdf_idx if urdf_idx < num_joints else i
            except ValueError:
                _urdf_reorder_array[i] = i

        _config_valid = True
        logger.debug("Angle pipeline config initialized")

    except (AttributeError, IndexError, TypeError) as e:
        logger.debug("Failed to init angle pipeline config: %s", e)
        _config_valid = False


def update_urdf_angles(angles_deg: np.ndarray) -> None:
    """Update URDF scene with new joint angles (degrees -> radians)."""
    global _config_valid

    if not ui_state.urdf_scene or len(angles_deg) < ui_state.active_robot.joints.count:
        return

    if not _config_valid:
        _init_config()

    # Pass numpy array directly to numba pipeline (no copy needed)
    if not angle_pipeline(
        angles_deg,
        _index_mapping_array,
        _angle_signs_array,
        _angle_offsets_array,
        _urdf_reorder_array,
        _angles_ordered_buffer,
    ):
        return

    ui_state.urdf_scene.set_axis_values(_angles_ordered_buffer)
