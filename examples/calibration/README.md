# Arm calibration from Commander

`run.py` runs one `par6.calibration` routine against the runtime Commander
manages on port 5001. Home the arm first, and start Commander so that its
par6d records:

```sh
mkdir -p calibration-runs
PAR6_DIAGNOSTICS="$PWD/calibration-runs/capture.bin" \
PAR6_CONFIG="$PWD/examples/calibration/hardware/PAR6.toml" \
waldo-commander
```

Choose a new capture filename per launch. Then open `run.py`, set `ROUTINE`
(or `PAR6_CALIBRATION`) to `check`, `tune-feedback` (`PAR6_CALIBRATION_JOINT`
1–6), `gravity`, `verify-gravity` or `limits`, and run it. The same routines
are available as `par6-calibrate <routine>` from a terminal. The Diagnostics
panel's Calibration section runs them with progress and an apply/rollback
step when the par6 backend is active.

Each run leaves `calibration-runs/<time>-<routine>/` with the report, every
trial's capture offsets and metrics, and on success `profile/candidate/PAR6.toml`
plus a `rollback/` copy and `calibration-patch.toml` listing only what was
measured. Activate a candidate by restarting Commander with `PAR6_CONFIG`
pointing at it; run `verify-gravity` after applying a gravity candidate.

`hardware/PAR6.toml` and its `grippers/` are this test arm's configuration:
shoulder-first homing, the seek currents that homed it repeatably, the
validated J3 (Kpv 0.009 / Kiv 0.0009) and J5 (Kpv 0.012 / Kiv 0.0004)
velocity-loop gains, STREAM limits equal to EXEC limits (which removed the
50 Hz J2 stream pulses), and the measured tabletop 10 mm below the base plane
extended across the workspace. Calibration refuses an empty hardware obstacle
scene; a present scene still has to be dimensionally right.

See `par6/docs/calibration.md` for what each routine measures, its acceptance
rules and the limits of encoder-only evidence, and
`docs/development/2026-09-11-calibration.md` for the hardware observations
behind this baseline.
