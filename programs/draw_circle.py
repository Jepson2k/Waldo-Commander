"""Curved motion commands: move_c, move_p, and move_s.

Draws three circles using progressively more flexible curve commands,
then connects their centers with a sine-wave spline:
  1. Full circle via single move_c (start = end)
  2. Two half-circle move_c arcs
  3. Computed 12-point waypoints via move_p
  4. Sine-wave spline (move_s) through all three centers
"""

import math
from parol6 import RobotClient

rbt = RobotClient()

RADIUS = 30
SPEED = 0.4
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


rbt.home()

# Circle 1: full circle with a single move_c (start = end)
cx, _, cz = CENTERS[0]
rbt.move_j(pose=circle_pt(cx, cz, 0), speed=0.5)
rbt.move_c(via=circle_pt(cx, cz, 180), end=circle_pt(cx, cz, 0), speed=SPEED)

# Circle 2: two half-circle move_c arcs
cx, _, cz = CENTERS[1]
rbt.move_l(circle_pt(cx, cz, 0), speed=SPEED)
rbt.move_c(via=circle_pt(cx, cz, 90), end=circle_pt(cx, cz, 180), speed=SPEED)
rbt.move_c(via=circle_pt(cx, cz, 270), end=circle_pt(cx, cz, 0), speed=SPEED)

# Circle 3: computed 12-point waypoints with move_p
cx, _, cz = CENTERS[2]
waypoints = [circle_pt(cx, cz, i * 30) for i in range(12)]
waypoints.append(waypoints[0])
rbt.move_l(waypoints[0], speed=SPEED)
rbt.move_p(waypoints, speed=SPEED)

# Sine-wave spline through all three circle centers (bottom to top)
SINE_POINTS = 36
z_min, z_max = CENTERS[2][2], CENTERS[0][2]
spline = []
for i in range(SINE_POINTS + 1):
    t = i / SINE_POINTS
    z = z_min + t * (z_max - z_min)
    x = RADIUS * math.cos(t * 3 * 2 * math.pi)
    spline.append([x, CIRCLE_Y, z] + ORIENTATION)
rbt.move_s(spline, speed=SPEED)

rbt.home()
print("Done!")
