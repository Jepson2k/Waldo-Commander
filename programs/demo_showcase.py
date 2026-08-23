"""Showcase script demonstrating all motion types.

Exercises move_j, move_l, move_c, move_p, move_s, blended zig-zag,
tool actions, TCP offset, and precision TRF rotations.
"""

import math
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

# move_j vs move_l (joint-space then linear-cartesian to nearby pose)
rbt.move_j(pose=[100, 340, 334, 90, 0, 90], speed=0.5)
rbt.move_l([-50, 340, 334, 90, 0, 90], speed=0.5)


# ── Curved motion: three vertical circles + sine-wave spline ──────────
RADIUS = 30
SPEED = 0.8
CIRCLE_Y = 340
ORIENTATION = [90, 0, 90]
CENTERS = [(0, CIRCLE_Y, 280), (0, CIRCLE_Y, 210), (0, CIRCLE_Y, 140)]


def circle_pt(cx, cz, angle_deg):
    """Circle in the XZ plane (vertical) at fixed Y."""
    a = math.radians(angle_deg)
    return [
        cx + RADIUS * math.cos(a),
        CIRCLE_Y,
        cz + RADIUS * math.sin(a),
    ] + ORIENTATION


# Circle 1: full circle with a single move_c (start = end)
cx, _, cz = CENTERS[0]
rbt.move_j(pose=circle_pt(cx, cz, 0), speed=0.5)
rbt.move_c(via=circle_pt(cx, cz, 180), end=circle_pt(cx, cz, 0), speed=SPEED)

# Circle 2: two half-circle move_c arcs
cx, _, cz = CENTERS[1]
rbt.move_l(circle_pt(cx, cz, 0), speed=SPEED)
rbt.move_c(via=circle_pt(cx, cz, 90), end=circle_pt(cx, cz, 180), speed=SPEED)
rbt.move_c(via=circle_pt(cx, cz, 270), end=circle_pt(cx, cz, 0), speed=SPEED)

# Circle 3: computed waypoints with move_p
cx, _, cz = CENTERS[2]
waypoints = [circle_pt(cx, cz, i * 30) for i in range(12)]
waypoints.append(waypoints[0])
rbt.move_l(waypoints[0], speed=SPEED)
rbt.move_p(waypoints, speed=SPEED)

# Sine wave through all three circle centers (bottom to top) using move_s
SINE_POINTS = 36
z_min, z_max = CENTERS[2][2], CENTERS[0][2]
spline = []
for i in range(SINE_POINTS + 1):
    t = i / SINE_POINTS
    z = z_min + t * (z_max - z_min)
    x = RADIUS * math.cos(t * 3 * 2 * math.pi)
    spline.append([x, CIRCLE_Y, z] + ORIENTATION)
rbt.move_s(spline, speed=SPEED)

# ── Zig-zag scan ─────────────────────────────────────────────────────
ZZ_ORI = [-180, -90, -180]
ROWS = 6
Y_MIN, Y_MAX = 0, 160
Z_MIN, Z_MAX = 200, 300
X = 280
BLEND = 15

rbt.move_j(pose=[X, 0, 334] + ZZ_ORI, speed=0.5)
rbt.move_l([X, Y_MIN, Z_MAX + 30] + ZZ_ORI, speed=1.0)
z_step = (Z_MAX - Z_MIN) / (ROWS - 1)
for row in range(ROWS):
    z = Z_MAX - row * z_step
    is_last = row == ROWS - 1
    y_start, y_end = (Y_MIN, Y_MAX) if row % 2 == 0 else (Y_MAX, Y_MIN)
    rbt.move_l([X, y_start, z] + ZZ_ORI, speed=1.0, r=BLEND, wait=False)
    rbt.move_l([X, y_end, z] + ZZ_ORI, speed=1.0, r=0 if is_last else BLEND, wait=False)
rbt.wait_motion()

# ── Precision demo: pencil pick-up and TCP-offset rotations ──────────
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
