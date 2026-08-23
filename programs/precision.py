"""Precision demo: TCP offset, TRF moves, and orientation rotations.

Picks up a pencil, offsets the TCP to the pencil tip, then demonstrates
linear and rotational moves in the Tool Reference Frame (TRF). The pencil
tip stays at a fixed point while the wrist rotates around it.
"""

from parol6 import RobotClient

rbt = RobotClient()

HOME_ANGLES = [90.0, -90.0, 180.0, 0.0, 0.0, 180.0]
HOME_TOLERANCE_DEG = 2.0

# Select tool, and home only if not already near the home pose
rbt.select_tool("SSG-48")
rbt.tool.calibrate()
current = rbt.angles()
if (
    current is None
    or max(abs(a - h) for a, h in zip(current, HOME_ANGLES)) > HOME_TOLERANCE_DEG
):
    rbt.home()

PRECISION_POSE = [0, -250, 350, -90, 0, -90]
rbt.move_j(pose=PRECISION_POSE, speed=0.5)

# Test gripper: two quick close/open cycles
rbt.tool.close(speed=1.0)
rbt.tool.open(speed=1.0)
rbt.tool.close(speed=1.0)
rbt.tool.open(speed=1.0)

# Approach pencil: move_j to 100mm above, descend linearly, grab, retract
PENCIL_ABOVE = [-90, -81.6, 161.8, 0, -69.4, 180]
rbt.move_j(angles=PENCIL_ABOVE, speed=0.8)
rbt.move_l([0, 0, -93, 0, 0, 0], rel=True, speed=0.4)
rbt.tool.close(wait=True)
rbt.move_l([0, 0, 93, 0, 0, 0], rel=True, speed=0.4)
rbt.move_j(pose=PRECISION_POSE, speed=0.8)

# Offset TCP to pencil tip (~100mm exposed below gripper). The pencil is
# clamped perpendicular to the gripper's jaw-closing direction, hanging
# along tool -X — that's the axis the offset goes on, not Z.
rbt.set_tcp_offset(-100, 0, 0)

# Pencil tip traces straight lines (linear precision demo)
rbt.move_l([0, 0, 100, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
rbt.move_l([0, 0, -200, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
rbt.move_l([0, 0, 100, 0, 0, 0], speed=0.8, frame="TRF", rel=True)

# Precision TRF rotations — pencil tip stays stationary while wrist rotates.
# 40° is the largest sweep that keeps every axis IK-reachable from this pose
# with the 100mm pencil offset.
SWEEP = 40
for axis in range(3):
    delta = [0, 0, 0, 0, 0, 0]
    delta[3 + axis] = -SWEEP
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
    delta[3 + axis] = SWEEP
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
    delta[3 + axis] = -SWEEP
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)

# Place pencil back: descend linearly, release, retract
rbt.set_tcp_offset(0, 0, 0)
rbt.move_j(angles=PENCIL_ABOVE, speed=0.8)
rbt.move_l([0, 0, -93, 0, 0, 0], rel=True, speed=0.4)
rbt.tool.open(wait=True)
rbt.move_l([0, 0, 93, 0, 0, 0], rel=True, speed=0.4)

# Return to home position (joint move, not the full homing sequence)
rbt.move_j(pose=PRECISION_POSE, speed=0.8)
rbt.move_j(angles=HOME_ANGLES, speed=0.8)
print("Done!")
