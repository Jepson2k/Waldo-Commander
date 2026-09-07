# Writing Programs

This guide builds a single script from scratch, adding capabilities section by section. By the end you'll have a program that demos joint and Cartesian moves, curved paths, scan patterns, tool control, TCP offset, and precision TRF rotations — a tour of what the robot can do.

For the full method reference, see the [API Reference](api-reference.md).
For reusable motion functions, see [Python skills](skills.md).

## Connect and home

Every program starts by importing the backend's sync client and connecting to the controller. The editor pre-fills the host and port from your configuration.

```python
from parol6 import RobotClient

rbt = RobotClient(host='127.0.0.1', port=5001)

HOME_ANGLES = [90.0, -90.0, 180.0, 0.0, 0.0, 180.0]
HOME_TOLERANCE_DEG = 2.0

# Select tool, and home only if not already near the home pose
rbt.select_tool("SSG-48")
rbt.tool.calibrate()
current = rbt.angles()
if current is None or max(abs(a - h) for a, h in zip(current, HOME_ANGLES)) > HOME_TOLERANCE_DEG:
    rbt.home()
```

`select_tool` activates the end-effector and updates the TCP. `tool.calibrate()` runs the gripper's one-time-per-session calibration routine — required before any `tool.open` / `tool.close` / `tool.set_position` calls. `home()` blocks until all joints reach the home position. All motion commands block by default. Skipping the home when already there saves time on repeated runs.

## move_j vs move_l

`move_j` interpolates in joint space — each joint takes the shortest path to its target angle. `move_l` moves the TCP in a straight line through Cartesian space. The difference is visible: move_j produces a curved TCP path, move_l produces a straight one.

To show this, we'll move to a pose with move_j, then to another with move_l. Watch the TCP path in the 3D view — the move_j arc vs the move_l straight line.

```python
# move_j — TCP follows a curved arc through joint space
rbt.move_j(pose=[100, 340, 334, 90, 0, 90], speed=0.5)

# move_l — TCP travels in a straight Cartesian line
rbt.move_l([-50, 340, 334, 90, 0, 90], speed=0.5)
```

`speed` is normalized 0.0–1.0. You can also use `duration` (seconds) or `accel` (0.0–1.0). Relative moves offset from the current position with `rel=True`, and `frame="TRF"` switches to the Tool Reference Frame for Cartesian moves.

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/move_j_vs_move_l.mp4" type="video/mp4">
</video>

## Curved motion

`move_c` draws a circular arc through a via-point to an end-point. `move_p` follows a series of waypoints at constant TCP speed. `move_s` fits a smooth spline through waypoints.

We'll draw three circles stacked vertically, each with a different command, then thread a sine-wave spline through all of them.

```python
import math

RADIUS = 30
SPEED = 0.8
CIRCLE_Y = 340
ORIENTATION = [90, 0, 90]
CENTERS = [(0, CIRCLE_Y, 280), (0, CIRCLE_Y, 210), (0, CIRCLE_Y, 140)]


def circle_pt(cx, cz, angle_deg):
    """Circle in the XZ plane (vertical) at fixed Y."""
    a = math.radians(angle_deg)
    return [cx + RADIUS * math.cos(a), CIRCLE_Y, cz + RADIUS * math.sin(a)] + ORIENTATION


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
```

These commands raise `NotImplementedError` on backends that don't support them.

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/smooth_motion.mp4" type="video/mp4">
</video>

## Zig-zag scan with blend radius

Now we rotate to a new workspace area and do a raster scan. The blend radius `r` parameter rounds the corners so the robot doesn't stop at each turn — it blends the end of one move into the start of the next.

Setting `wait=False` queues moves so the controller can blend them. `wait_motion()` blocks until the queue drains.

```python
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
```

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/zig_zag_with_blend.mp4" type="video/mp4">
</video>

## Tool control and precision

Rotate to a third area and demo precision moves with the gripper we already selected at the start. `select_tool` selects the active end-effector and updates the TCP. The `tool` object gives you `open()`, `close()`, and `set_position()`.

### Gripper basics

```python
PRECISION_POSE = [0, -250, 350, -90, 0, -90]
rbt.move_j(pose=PRECISION_POSE, speed=0.5)

# Quick close/open cycles
rbt.tool.close(speed=1.0)
rbt.tool.open(speed=1.0)
rbt.tool.close(speed=1.0)
rbt.tool.open(speed=1.0)
```

Electric grippers accept `speed` and `current` keyword arguments for finer control.

### Pencil pickup

Use joint-space angles to reach 100mm above the pickup, then descend linearly for a precise approach:

```python
# Approach: move_j to 100mm above, descend linearly, grab, retract
PENCIL_ABOVE = [-90, -81.6, 161.8, 0, -69.4, 180]
rbt.move_j(angles=PENCIL_ABOVE, speed=0.8)
rbt.move_l([0, 0, -100, 0, 0, 0], rel=True, speed=0.4)
rbt.tool.close(wait=True)
rbt.move_l([0, 0, 100, 0, 0, 0], rel=True, speed=0.4)
rbt.move_j(pose=PRECISION_POSE, speed=0.8)
```

### TCP offset and TRF moves

`set_tcp_offset` shifts the tool center point. With a pencil gripped vertically, offsetting by -100mm in the tool X axis puts the TCP at the pencil tip. All subsequent moves reference this new TCP.

`frame="TRF"` with `rel=True` makes moves relative to the current tool frame — the robot moves in the tool's local axes regardless of its world-frame orientation.

```python
# Offset TCP to pencil tip (~100mm exposed below gripper)
rbt.set_tcp_offset(-100, 0, 0)

# Pencil tip traces straight lines in tool frame
# (tool Z = world -Y at this pose)
rbt.move_l([0, 0, 100, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
rbt.move_l([0, 0, -200, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
rbt.move_l([0, 0, 100, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
```

### Tool rotations

TRF rotations keep the TCP stationary while the wrist rotates around it — the pencil tip stays fixed and the robot pivots. This is useful for inspection, welding, or any task where the tool tip stays put but the approach angle changes.

```python
SWEEP = 20
for axis in range(3):
    delta = [0, 0, 0, 0, 0, 0]
    delta[3 + axis] = -SWEEP
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
    delta[3 + axis] = SWEEP
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
    delta[3 + axis] = -SWEEP
    rbt.move_l(delta, speed=0.8, frame="TRF", rel=True)
```

### Cleanup

Reset the TCP offset, return the pencil, and go home:

```python
# Place pencil back: descend linearly, release, retract
rbt.set_tcp_offset(0, 0, 0)
rbt.move_j(angles=PENCIL_ABOVE, speed=0.8)
rbt.move_l([0, 0, -100, 0, 0, 0], rel=True, speed=0.4)
rbt.tool.open(wait=True)
rbt.move_l([0, 0, 100, 0, 0, 0], rel=True, speed=0.4)

# Return to home position (joint move, not the full homing sequence)
rbt.move_j(pose=PRECISION_POSE, speed=0.8)
rbt.move_j(angles=HOME_ANGLES, speed=0.8)
print("Done!")
```

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/tool_control_and_rotations.mp4" type="video/mp4">
</video>

## The complete script

Here's everything together — one continuous program that exercises all motion types across three sides of the workspace:

```python
"""Showcase script demonstrating all motion types.

Exercises move_j, move_l, move_c, move_p, move_s, blended zig-zag,
tool actions, TCP offset, and precision TRF rotations.
"""

import math
from parol6 import RobotClient

rbt = RobotClient(host='127.0.0.1', port=5001)

HOME_ANGLES = [90.0, -90.0, 180.0, 0.0, 0.0, 180.0]
HOME_TOLERANCE_DEG = 2.0

# Select tool, and home only if not already near the home pose
rbt.select_tool("SSG-48")
rbt.tool.calibrate()
current = rbt.angles()
if current is None or max(abs(a - h) for a, h in zip(current, HOME_ANGLES)) > HOME_TOLERANCE_DEG:
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
    return [cx + RADIUS * math.cos(a), CIRCLE_Y, cz + RADIUS * math.sin(a)] + ORIENTATION


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
rbt.move_l([0, 0, -100, 0, 0, 0], rel=True, speed=0.4)
rbt.tool.close(wait=True)
rbt.move_l([0, 0, 100, 0, 0, 0], rel=True, speed=0.4)
rbt.move_j(pose=PRECISION_POSE, speed=0.8)

# Offset TCP to pencil tip (~100mm exposed below gripper)
rbt.set_tcp_offset(-100, 0, 0)

# Pencil tip traces straight lines (linear precision demo)
# Forward/back (tool Z = world -Y at this pose)
rbt.move_l([0, 0, 100, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
rbt.move_l([0, 0, -200, 0, 0, 0], speed=0.8, frame="TRF", rel=True)
rbt.move_l([0, 0, 100, 0, 0, 0], speed=0.8, frame="TRF", rel=True)

# Precision TRF rotations — pencil tip stays stationary while wrist rotates
SWEEP = 20
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
rbt.move_l([0, 0, -100, 0, 0, 0], rel=True, speed=0.4)
rbt.tool.open(wait=True)
rbt.move_l([0, 0, 100, 0, 0, 0], rel=True, speed=0.4)

# Return to home position (joint move, not the full homing sequence)
rbt.move_j(pose=PRECISION_POSE, speed=0.8)
rbt.move_j(angles=HOME_ANGLES, speed=0.8)
print("Done!")
```

<video controls width="100%">
  <source src="https://github.com/Jepson2k/Waldo-Commander/releases/download/docs-assets/demo_showcase.mp4" type="video/mp4">
</video>
