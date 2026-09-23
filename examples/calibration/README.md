# Arm calibration

A PAR6 has two calibrations.

**The arm: `par6-selfcal`.** It homes the arm, measures each joint's inertia and
friction, and identifies the arm's own link masses. Stop `par6d` and take the
tool off first: the binary drives the CAN bus directly, and it identifies the
bare arm. Its source is `par6/crates/par6d/src/bin/par6-selfcal.rs`. From the
`par6/` directory:

```sh
pixi run cargo run -p par6d --release --bin par6-selfcal -- --help
```

**The tool: `estimate_payload()`.** With the tool fitted and the runtime up, the
client call fits the mass and centre of mass the arm is carrying and declares
them to the runtime. Run it whenever the tool or the load changes.

`hardware/PAR6.toml` and its `grippers/` are this test arm's configuration:
shoulder-first homing, the seek currents that homed it repeatably, the
validated J3 (Kpv 0.009 / Kiv 0.0009) and J5 (Kpv 0.012 / Kiv 0.0004)
velocity-loop gains, STREAM limits equal to EXEC limits (which removed the
50 Hz J2 stream pulses), and the measured tabletop 10 mm below the base plane
extended across the workspace. These files preserve the September 11 hardware
baseline; `docs/development/2026-09-11-calibration.md` records the hardware
observations behind it.
