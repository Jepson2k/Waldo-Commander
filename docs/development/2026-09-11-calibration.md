# Calibration development and physical measurements — 2026-09-11

The calibration implementation lives in `par6/python/par6/calibration`, with
native gravity model/configuration bindings and an opt-in native telemetry
recorder. `examples/calibration/run.py` runs the protocols against Commander's
managed runtime. See `par6/docs/calibration.md` for operation and acceptance rules.

## Homing

**Physical correction:** after the 45.139 second run with the J3 feedback
profile, the user observed that J1 was incorrectly homed. Controller completion
and the displayed ready angles did not prove the base reference was correct.
The sequence is now changed to complete J2/J3 before starting J1; the initial
wrist clearance and later wrist/gripper phases are preserved. The configuration
under `examples/calibration/hardware/` includes this ordering correction.
The earlier exact configurations remain with the raw run evidence.


A complete physical home succeeded in **42.760 seconds** at 01:20 UTC, with the
MSG gripper attached, using the local release runtime. The six arm joints and
gripper finished referencing. Final angles were approximately
[89.961, -105.996, 163.297, 0.038, -28.641, 180.046] degrees. Stop was acknowledged
and fresh encoder feedback confirmed rest.

The baseline configuration and gripper files, with the ordering correction above, are retained in
`examples/calibration/hardware/`. This is a configuration for this tested arm,
not a claim that every assembly needs these homing currents. Relative to the
installed baseline, the successful changes were:

| Setting | J2 | J3 |
| --- | ---: | ---: |
| Seeking current | 900 mA | 650 mA |
| Seeking speed | 15000 ticks/s | 6000 ticks/s |
| Seeking timeout | 20 s | 10 s |
| Backoff duration | 0.6 s | 1.5 s |

Normal running gains/current limits were unchanged, including J1 Kpv 0.015.
The two-pass stop-repeatability check remains enabled. Earlier 250/650 mA J2
attempts stopped at inconsistent positions and were correctly rejected;
900 mA at 6000 ticks/s was moving but exceeded the existing seek timeout.
This success establishes one measured run, not arbitrary-start repeatability.

A CAN startup stall required releasing hardware ownership and cycling can0
before fresh feedback returned. Its underlying cause remains unresolved.

## Initial motion and feedback checks

The short shoulder/elbow check recorded J2 tracking peaks around 0.153 degrees
without current saturation. J3 reached its target but failed the powered-hold
settling requirement: position excursion was approximately 0.228 degrees, with
native reported speed RMS 0.551 rad/s. A spectral peak near 102.5 Hz is an
observed sampled signal; aliasing and drive velocity estimation prevent calling
that a measured table resonance.

A subsequent feedback experiment reproduced position-dependent J3 oscillation:
some holds were quiet, while two nearby configurations oscillated by 0.179 and
0.187 degrees. Reducing Kpv/Kiv in an experimental comparison reduced these
holds to 0.001–0.0024 degrees. Electrical gains and current limits were preserved,
and the starting gains were restored. These preliminary comparisons alone do
not establish a validated operating profile.

CAN gain writes provide controller acknowledgements, not motor parameter
readback. The observed drive serial numbers are all 1 and are not unique unit
identifiers. No motor electrical calibration was performed in this work.

## Repeated J3 comparison

The repeat run `20260911T014409Z-feedback` accepted **Kpv 0.009, Kiv 0.0009**
against baseline 0.015/0.0015. Kpp stayed 5.0; current-loop/electrical gains,
current/velocity/voltage limits, and torque constants were unchanged. A 20%
reduction was rejected because one training hold still oscillated. The 40%
reduction passed six training excursions and three independent validation
excursions. The routine restored the baseline after testing and staged a
candidate plus rollback profile for explicit activation.

| Independent hold | Baseline excursion | Candidate excursion |
| --- | ---: | ---: |
| 1 | 0.19671° | 0.00243° |
| 2 | 0.00121° | 0° at encoder resolution |
| 3 | 0.17607° | 0.00121° |

Candidate J3 tracking peaks on these small movements were 0.16514°, 0.10545°,
and 0.11169°. This establishes the tested local operating improvement; it is
not whole-workspace feedback validation or a measurement of table vibration.

## Requested homing order

Following the user's physical observations, the configured order is now:
J2/J3, J1, wrist clearance and wrist references, arm ready pose, then gripper
firmware calibration, gripper motor reference and final closing move.

The run at 02:08:53 UTC completed in **47.800 seconds**. Fresh status established:

- J2 finished at 12.219 s; J1 started at 12.240 s.
- All six arm references were complete by 35.679 s.
- The gripper started at 38.719 s, after the arm reached its ready pose.
- Gripper referencing finished at 45.279 s; the closing move finished the sequence.
- Stop was acknowledged. The user independently confirmed that J1 finished in the correct physical position.

The native recorder for this run is `physical-20260911-7.bin`, with status in
`homing-20260911T020853Z.jsonl`. The active configuration is
`calibration-runs/j2-first-gripper-last/PAR6.toml`; the tracked example contains
the same operating gains and homing sequence. Native regression checks cover
shoulder-before-base ordering and gripper-after-arm readiness.

## Local evidence

Raw captures and trial reports are private, untracked files in `calibration-runs/`:

- `physical-20260911-4.bin`: native 250 Hz capture, PAR6CAP2 format.
- `homing-20260911T012052Z.jsonl`: the 42.760 second homing run.
- `20260911T012402Z-check/`: the shoulder/elbow check and J3 settling failure.
- `20260911T013334Z-feedback/`: the initial feedback comparison and restoration.

The recorder's bounded file budget is one hour at 250 Hz. Evidence includes
configuration fingerprints and offsets into the raw recording. These are local
measurements; they do not yet demonstrate completed whole-arm gravity or
maximum-motion calibration.

## J5 diagnosis during gravity collection

The gravity run `20260911T022421Z-gravity` completed eight sweeps, then
stopped when J5 would not settle near -25.798 degrees. The final recorded
hold had about 0.088 degrees of encoder excursion and drew approximately
438 mA against its 2100 mA configured limit. Stop and encoder rest were
confirmed. No gravity correction was fitted or applied from that run.

A diagnostic centered on the relaxed, stopped pose did not reproduce the
problem. The feedback routine now accepts an explicit center so a recorded
failing target can be tested directly. Run `20260911T022846Z-feedback`
reproduced the oscillation at that target and accepted J5 Kpv 0.012 and
Kiv 0.0004 (80% of the baseline). Kpp 5, electrical gains, torque constants,
and the 2100 mA current limit were preserved. Maximum training hold excursion
fell from 0.055 to 0.027 degrees; the three independent candidate holds were
0.016, 0.022 and 0.016 degrees. The independent baseline holds were already
quiet, so these establish no regression there rather than a second large
oscillation reduction.

The candidate was activated through a Commander restart. An initial CAN
startup stall prevented homing; releasing the bus and cycling can0 restored
fresh feedback. The subsequent full home completed in 47.939 seconds with
the requested order. Capture: `physical-20260911-8.bin`. The tracked hardware
example now includes this J5 profile alongside the earlier J3 adjustment.

## J2 vibration while moving: streamed command excitation

The user reported strong J2 vibration during gravity run
`20260911T024046Z-gravity` and clarified it occurred while moving. The run
was interrupted during sweep 21; Commander MCP acknowledged Stop and fresh
encoder feedback confirmed rest. No gravity fit was promoted.

Native traces showed roughly 50 Hz velocity ripple in the commanded J2
trajectory. Although the input quintic requested at most 0.07 rad/s, the
stream planner repeatedly accelerated and braked toward individual 50 Hz
position targets under the much larger stream acceleration/jerk limits.
Recorded command acceleration approached 10 rad/s² and jerk exceeded
2000 rad/s³. Current saturation was absent. This supports a command-side
excitation diagnosis; motor encoders do not measure table vibration.

An offline native limiter comparison preserved the sweep speed while reducing
command energy above 20 Hz by about 99.99% when stream limits matched the
execution limits. A new calibration preflight runs the actual native limiter
and wire feedforward conversion, checks its derivatives against the experiment
envelope, and checks the resulting path for collisions before sending motion.
A regression reproduces the bad output from gentle input targets. The expanded
Python calibration suite passes 9 tests, including the real simulator lifecycle.

The candidate `20260911-smooth-stream-profile` was activated and homing
completed in 49.740 seconds, retaining J2-before-J1 and gripper-last ordering.
The short physical J2 test `20260911T030030Z-check` passed both directions:

| Measurement | Negative direction | Positive direction |
| --- | ---: | ---: |
| Peak tracking error | 0.0434° | 0.0408° |
| Velocity residual RMS | 0.0121 rad/s | 0.0147 rad/s |
| Command peak acceleration | 0.328 rad/s² | 0.436 rad/s² |
| Command peak jerk | 17.10 rad/s³ | 17.50 rad/s³ |
| Peak measured current | 504 mA | 375 mA |
| Current saturation | none | none |

The J2 current limit stayed 2500 mA. These short checks have a smaller excursion
than the earlier gravity sweeps, so the summary is not a matched full-trajectory
A/B measurement. Stop/rest was confirmed. The user independently confirmed that J2 looked "much smoother" during the
short comparison. Broader physical validation remains separate from this check.
Native capture: `physical-20260911-9.bin`.

### Full-length J2 comparison

The following gravity run, `20260911T030301Z-gravity`, repeats the original
0.24 rad J2 sweeps near home. Other joint poses differ slightly. Native
tracking peaks fell from 0.1566/0.1622 degrees to 0.0336/0.0345 degrees.
Velocity residual RMS fell from 0.0528/0.0589 rad/s to 0.0124/0.0131 rad/s.
Commanded velocity energy above 20 Hz fell by about 99.999%; measured motor
velocity energy in that band fell by about 88%. This remains encoder evidence,
not a table accelerometer measurement. Both directions stopped cleanly.

`stream-ripple-study/physical-comparison.json` retains the capture offsets and
normalized spectral measurements. The tracked hardware configuration now
includes the active smoother stream limits. Gravity fitting is still pending
completion of the whole collection and independent validation.


## Wider approach and table contact

After the user requested larger movements, the narrower run was stopped cleanly
with 26,145 training and 7,930 validation samples retained. A proposed addition
used three broader configurations and 0.36 rad (20.6°) sweeps. Its joint-window
and self-collision preflight passed against the connected scene, which had empty
installation and program layers. This was insufficient for the physical setup.

Run `20260911T031925Z-gravity` stopped during the first approach on sustained
J5 current saturation. Peak measured J5 current was 2120 mA against a 2100 mA
limit. The user reported that the gripper touched the table. The first 95%-limit
sample was near [97.85, -68.97, 148.76, 19.82, -8.74, 162.44] degrees. No sweep
from this run entered gravity fitting. Stop was acknowledged and fresh feedback
confirmed zero speeds and an empty queue. No automatic retry was made.

Offline replay with the source configuration's floor rejects the requested
approach for jaw/floor collision. That source floor is 20 mm below the mounting
plane, an installation assumption which must be checked against this table;
it does not detect the exact observed contact pose. The active hardware profile
contained no such geometry. Physical work is paused until the work surface is
located correctly. Calibration now rejects empty hardware obstacle scenes and
retains scene readback in `world.json`. A simulator integration check exercises
refusal of this wider approach before motion is submitted.


The user measured the base as 10 mm above the tabletop. The hardware example now
places the table top at z = -0.010 m, extending across the workspace. Offline
replay rejects the requested target with gripper/jaw table pairs, but the model
still predicts clearance at the first current-limit sample. That remaining
geometry/reference/contact-timing discrepancy is unresolved, so no further arm
motion has been issued. The calibration suite passes all 9 tests, including
refusal of the wider approach before any simulated movement.


The user identified the contacting part as the gripper fingers. Transforming the
actual packaged jaw mesh vertices at the first 95%-current sample places the
lowest finger at z = +0.0419 m: 51.9 mm above the measured tabletop. Near the end
of the approach this falls to about 38.6 mm. This is a geometry/reference mismatch,
not evidence that more motor current is needed. Motor following error during
the approach peaked at 0.076/0.164/0.124/0.356/1.392/0.084 degrees (J1–J6).
`table-contact-geometry.json` records mesh-height sensitivity to joint references.

The live controller acknowledged the measured table as a program obstacle and
reported scene epoch 1, zero joint speeds, homed/enabled status, and no fault.
`table-applied-readback.json` retains the scene. Commander MCP's shape command
initially failed because the installed waldoctl emits an eighth attachment field
while this par6 branch expects seven fields. The client now accepts ordinary
unattached shapes from either format and explicitly rejects unsupported attached
shapes. The real simulator test verifies shape readback and preservation after
attachment rejection; it passes. A fresh local client applied the live table.
The running Commander process picks up that client code change on its next
restart; no restart or additional physical movement was performed for this fix.

## Why the earlier calibration missed vibration and gravity drift

The earlier ordinary acceptance gate checked faults, tracking error and current
saturation. Gravity collection discarded the returned motion metrics; it could
retain steady-looking samples from an oscillating trial. Settling kept position
feedback active, then Stop checked only 0.3 seconds of low reported speed in
idle. Neither established gravity-only balance. No completed physical gravity
fit had been applied.

The calibration implementation adds explicit live and whole-stimulus motion
acceptance. The initial 20-second gravity-only assay was subsequently broken
by the adversarial reviewer: static friction concealed torque error, and a
software drift threshold did not bound stopping travel. That assay is now
restricted to simulation and is absent from automatic calibration.

Gravity identification and `verify-gravity` retain active feedback throughout,
including stops. They disable gravity feedforward once before collecting
current-derived torque, leaving active support engaged afterward. The latter
protocol compares the loaded model against matched opposite-direction sweeps
at six independent shoulder/elbow/wrist configurations, checks measured speed
and pose overlap, torque error/spread, and observable-direction coverage. It
does not refit the loaded model. Passing means torque consistency under the
stated odd-friction assumption at the measured poses and paths. It does not
certify inertial gravity parameters, arbitrary joint-coordinate combinations,
or operation after enabling feedforward; `applied_validation_complete` stays
false. See the adversarial review for a concrete directional-friction ambiguity.

Prior samples and envelope checkpoints must match acceptance policy version 7
and the recorded process/reference interval. Recollecting prior gravity data
retains the physical centers' original training/validation assignment; it no
longer renumbers a subset into the opposite split.

Replay of the actual J2 comparisons through the new gates gives:

| J2 sweep | Velocity residual RMS | Measured velocity RMS above 20 Hz | Result |
| --- | ---: | ---: | --- |
| Before, negative | 0.05276 rad/s | 0.04610 rad/s | rejected |
| Before, positive | 0.05888 rad/s | 0.05121 rad/s | rejected |
| After, negative | 0.01239 rad/s | 0.01575 rad/s | accepted |
| After, positive | 0.01314 rad/s | 0.01800 rad/s | accepted |

Both earlier sweeps also exceed the new command-ripple threshold. Evidence:
`validation-physical-replay.json`. These are retrospective checks, not new
hardware trials.

Capture 11 contains a 165.31-second homed idle interval with 1.968° J3 excursion
and fitted drift -0.01515°/s. A later 141.99-second idle interval was nearly
stationary, illustrating why one quiet pose/hold is insufficient.
`validation-historical-idle-drift.json` retains the intervals. The old capture
did not encode the new gravity-enable flag, so it is historical drift evidence,
not a passing run of the new gravity-verification protocol.

The J3 feedforward trim implementation supports a proposed 30% increase based
on roughly 550 mA gravity-only current versus 730 mA in a powered position hold.
This is a provisional diagnostic estimate, not an identified full-arm model.
The live robot has not been restarted to apply that trim during this validation
work. A readback still showed it stationary in completed EXEC position hold,
with no error or queued motion. Server-restart holding is not implemented.


Before the adversarial review, the calibration suite passed **14 tests** (11 analysis/configuration
regressions and three real `par6d --sim` workflows). The simulator confirms a
nominal gravity model passes the sustained hold, deliberately reducing J3's
gravity scale to 0.1 fails with a measured drift abort, and both paths finish
with active zero-velocity holding. Native config/gravity trim tests, rustfmt
and clippy with warnings denied also passed. These do not constitute physical
validation of the new routines; the user unplugged the arm for the night and
requested an adversarial reviewer to search for false acceptance cases.
