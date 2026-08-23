"""Speed and motion profile comparison.

Runs the same L-shaped path at different speeds and with different motion
profiles, timing each run to show the effect of these parameters.
"""

import time
from parol6 import RobotClient

rbt = RobotClient()

ORIENTATION = [90, 0, 90]

# L-shaped path
START = [0, 280, 300] + ORIENTATION
PATH = [
    [80, 280, 300] + ORIENTATION,  # right
    [80, 280, 150] + ORIENTATION,  # down
    [-80, 280, 150] + ORIENTATION,  # left
]


def run_path(speed):
    """Run the L-shaped path and return elapsed time."""
    rbt.move_l(START, speed=1.0, wait=True)
    t0 = time.time()
    for wp in PATH:
        rbt.move_l(wp, speed=speed, wait=True)
    return time.time() - t0


print("Homing...")
rbt.home(wait=True)

# ── Speed comparison (TOPPRA profile) ────────────────────────────
print("\n--- Speed Comparison (TOPPRA) ---\n")
rbt.select_profile("TOPPRA")

speed_results = []
for speed in [0.3, 0.6, 1.0]:
    elapsed = run_path(speed)
    speed_results.append((speed, elapsed))
    print(f"  speed={speed:.1f}  time={elapsed:.2f}s")

# ── Profile comparison (speed=0.5) ───────────────────────────────
print("\n--- Profile Comparison (speed=0.5) ---\n")

profile_results = []
for profile in ["TOPPRA", "TRAPEZOID"]:
    rbt.select_profile(profile)
    elapsed = run_path(0.5)
    profile_results.append((profile, elapsed))
    print(f"  {profile:12s}  time={elapsed:.2f}s")

# Reset to default
rbt.select_profile("TOPPRA")

# ── Summary ──────────────────────────────────────────────────────
print("\n--- Summary ---")
ratio_speed = (
    speed_results[0][1] / speed_results[2][1] if speed_results[2][1] > 0 else 0
)
ratio_profile = (
    profile_results[0][1] / profile_results[1][1] if profile_results[1][1] > 0 else 0
)
print(f"  Speed 0.3 vs 1.0: {ratio_speed:.1f}x slower")
print(f"  TOPPRA vs TRAPEZOID: {ratio_profile:.2f}x ratio")

print("\nHoming...")
rbt.home(wait=True)
print("Done!")
