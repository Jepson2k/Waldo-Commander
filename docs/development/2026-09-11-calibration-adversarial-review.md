# Adversarial calibration review — 2026-09-11

The user unplugged the arm for the night and requested an adversarial reviewer
that tries to make a bad model pass calibration. The reviewer used the native
GravityModel and isolated real `par6d --sim` processes. No hardware connection
or motion was used. Full evidence and executable reproductions are retained in
`calibration-runs/adversarial-review/REVIEW.md` and its adjacent files.

## Confirmed false acceptance

| Check | Adversarial result | Evidence |
| --- | --- | --- |
| Fitting and profile export | Accepted a candidate with 0.1930 Nm held-out J3 residual and 0.1160 Nm J6 residual. Relative improvement concealed substantial remaining error. | `fit-counterexample.json`, `reproduce_fit.py`, exported `bad-fit-profile/` |
| Gravity-only stationary hold | J3 scale 0.9 passed 20.176 seconds with zero excursion, despite an independently known 0.2641 Nm support deficit. Ordinary Coulomb friction masked it; additional holding friction was zero. | `static-friction.json`, actual native recording and trial report |
| Verification pose coverage | A correction invisible at all three verifier poses passed all three 20-second hold gates, but produced 0.3904 Nm J6 error at a fourth collision-checked wrist pose. The original pose regressor spans 8 of 10 observable directions. | `independent-holds.json`, actual native recording and four trial reports |

The fitting counterexample uses deterministic analytic observations from a known
native gravity model, positive dissipative friction, and periodic elastic torque.
The stationary counterexamples use the real simulator plant. These are distinct
claims: the reviewer did not demonstrate that the normal expanded fitting
trajectory itself produces the pose-nullspace correction.

The full three-pose verifier was also blocked by a nominal controller transition
failure after its first hold. Therefore the pose-coverage result establishes that
all three constituent hold gates accept, without claiming an overall verifier
success that never occurred.

## Implemented correction

Gravity fits now require an absolute held-out residual ceiling for every joint,
in addition to relative improvement and plausibility checks. The commissioning
default is 0.05 Nm, recorded in the acceptance policy and fit report. The saved
adversarial fit now fails for J2, J3 and J6, and profile export is refused. The
regression uses the same deterministic model-mismatch construction and also
checks that the correctly modeled control case still passes.

The analysis/configuration calibration suite passed 12 tests after this change.
Original accepted artifacts and source snapshots remain preserved beside the
new `fit-counterexample-after-fix.json` result.

## Follow-up corrections and numerical review

The automatic gravity workflow now keeps feedback support engaged. Its old
20-second gravity-only release is restricted to isolated simulation: the
reviewer demonstrated 4.2913 degrees of wrist excursion despite a 0.25-degree
abort threshold and 0.5-degree checked margin. This does not implement a runtime
stopping-distance guard; physical release remains unavailable until one is
validated.

`verify-gravity` now compares current-derived torque from matched forward and
reverse travel against the loaded model, without refitting that model. It
requires measured motion on every loaded joint, matching speeds/poses,
sufficient travel, bounded torque residual/spread, and measured regressor
coverage. Six independent wrist/shoulder/elbow centers replace the three static
poses. Complete groups with colliding approaches, sweeps or return paths are
excluded before motion; at least three groups and adequate coverage must remain.
The report records exclusions and the retained pose region. Shared-regressor
rank cannot substitute for missing observations of an independently scaled joint.

The reviewer challenged this implementation using native-model analytic data:

| Case | Result |
| --- | --- |
| Omit J3 while its scale is 0.5 | Initially accepted with 1.32 Nm J3 error. Both the analyzer and workflow now require the missing loaded-joint evidence. |
| Poorly conditioned local pose set | A correction passed locally but had 0.231 Nm error at an untested combination of individually exercised coordinates. The new independent centers reject this correction and improve the weakest normalized singular value from 0.0547 to 0.2046. Accuracy claims remain confined to measured poses/paths. |
| Load-dependent directional friction | A strictly dissipative friction law makes a model with gravity parameters 5% low pass fitting and moving checks, leaving 0.132 Nm J3 error at a tested pose. This is a physical identifiability ambiguity, not fixed by more equivalent current samples. |

The last example remains open. Reports explicitly state the odd-friction
assumption, distinguish effective torque consistency from identified inertial
parameters, and leave `applied_validation_complete` false. Independent known-load,
physical mass/COM, torque-reference, or other constrained evidence is needed for
stronger claims. Full reproductions and limitations are in
`calibration-runs/adversarial-review/moving-validation/REPORT.md`.

Prior-data continuation also keeps each physical center in its original
training/validation split. The previous subset-renumbering could turn a trained
center into a nominally held-out one. Acceptance policy version 4 prevents older
recordings from silently entering the revised protocol.

## Validation and remaining implementation work

The analysis/configuration suite passed **13 tests**, including native-model
regressions for the friction-hidden J3 deficit, the old verifier's wrist blind
direction, missing-joint data and insufficient travel. The three real
`par6d --sim` lifecycle tests passed in 70.87 seconds; they cover active support,
recording, cancellation, restoration and the simulation-only gravity probe.
Ruff and whitespace checks passed. These results do not establish a successful
end-to-end moving gravity calibration.

The integrated native verifier run excluded group 3 because an approach would
collide with the installed floor, retained five groups with rank 10 and weakest
normalized singular value 0.1774, then rejected excessive J2 velocity residual
and vibration during its first collection sweep. Its report remained invalid
and incomplete, with confirmed cleanup. Evidence:
`calibration-runs/moving-verification-native-v2/run/` and its adjacent capture.

Independent nominal simulator probes fail tracking/velocity gates even without
a preceding gravity-only hold. Preserving feedforward, disabling it, and reducing
velocity gains do not by themselves provide a fully passing forward/reverse
workflow. The locally cached vendor firmware confirms that `rstinit` defaults
to zero and conditional integral resets are not automatic mode-change resets;
the simulator was not altered to invent such behavior. Exact evidence is under
`calibration-runs/handoff-probes/` and the corresponding native recordings.

The full moving verifier still requires a passing native-plant run and physical
validation. No hardware connections, commands, configuration changes or fitted
corrections were applied during this review. The user unplugged the arm for the
night. Physical reference accuracy, gravity transfer over the intended workspace,
full motion-envelope calibration and table vibration remain unverified.

## Moving-vibration and execution follow-up

The reviewer subsequently reproduced a quiet-hold dilution false pass in the
historical physical J3 capture. Travel and hold now have separate acceptance
checks. Integral gain can be tuned independently, and diagnostic approaches use
the same slow limits while retaining tracking/current/readiness checks. A full
automatic J2 simulator search passed independent validation, selecting a 20%
integral reduction and reducing validation velocity residual from 0.01783 to
0.001991 rad/s. Scope, artifacts and remaining full-arm work are recorded in
[the feedback-tuning report](2026-09-11-feedback-tuning.md).

The review also caught native-to-Python delivery time missing from feedback age.
Status now carries the original client receipt timestamp; admission, emission
and Stop-rest confirmation include that age. Four native calibration lifecycle
tests passed with this enforcement. Python cyclic collection is deferred through
Stop cleanup while reference counting continues and caller GC state is preserved.
Unconfirmed SYSTEM acknowledgements can no longer count as successful gain
updates or restoration.

The reviewer then constructed settling vibration omitted by both the travel and
final-hold crops. At 30 Hz and 0.12 rad/s peak velocity, quiet portions also
diluted the whole-trial RMS below the limit. Feedback acceptance now checks
overlapping windows throughout the recording, including the final partial
interval. The production candidate gate rejects this analytic counterexample
while accepting the quiet control. All 18 recordings from the successful native
J2 search also pass the revised comparison and validation checks. The analysis
suite now passes 14 tests. The saved before-fix functions and reproducer are in
`calibration-runs/adversarial-review/ringdown/REPORT.md`.


## Derivative excitation and simulator-clock follow-up

The limits review reproduced command-only acceleration/jerk acceptance despite
an 80 ms response lag. A first measured-velocity derivative fix still accepted a
bad jerk response after applying the real virtual driver's encoder-difference
velocity estimator: true peak jerk was 0.6851 rad/s³, while the noisy derivative
estimate reached 22.506 rad/s³. Ordinary tracking/vibration checks passed.

The final excitation gate uses position divided differences at several time
spacings and subtracts the worst-case half-count rounding contribution. The
quantized bad response now proves only 0.4543 rad/s³ against a required 0.9.
Coarse encoders and short pulses can be inconclusive; those remain rejected.
Coupled validation exercises all six joints and all three derivatives, with a
shared resumable budget and native preflight against the loaded configuration.
Original code, numerical counterexamples and regression evidence are retained in
`calibration-runs/adversarial-review/envelope/REPORT.md`.

A complete native gravity run with the independently validated J2/J3 integral
reductions performed all 50 sweeps and returned to its starting pose. Its last
J6 pair failed only the speed-matching check. The simulator analysis had used
host elapsed time to differentiate positions advanced by fixed native timesteps;
catch-up ticks inflated one direction's estimated speed. The corrected plant
clock retains host timestamps and deadline checks, while hardware analysis
continues to use monotonic time.

Reanalysis of all 101 recorded motion trials, including every sweep and approach,
produced no whole-trial or overlapping-window motion failures. Its 113,806 steady
samples passed every required joint/pose torque check and achieved rank 10 with
weakest normalized singular value 0.1894. Original artifacts remain unchanged:
`calibration-runs/moving-verification-j2-j3-validated/analysis-after-clock-fix.json`.
The clock regression fails with the former differentiation rule and passes with
the correction; it also verifies that a 200 ms host stall still rejects data.
This does not resolve friction identifiability or establish physical validation.


Final regression selection after these corrections: **25 tests passed in
185.57 seconds**, including the real-daemon lifecycle and all-axis coupled trial
with budget continuation. Ruff and diff whitespace checks passed. The positional
excitation bound still assumes correctly timed position acquisition; drive-side
sample/transport timing uncertainty and physical-arm validation are outstanding.


## Collection labels, native fractions, and delayed delivery

The reviewer constructed a passing collection-completeness counterexample:
two sweeps labelled J6 actually contained only J5 movement. Both independent
coverage checks had rank 10 and the mathematical fit passed, because other
pose groups supplied the missing J6 fit observations. Signed measured speed,
minimum sample count, and measured travel now determine whether each requested
sweep supplied evidence. The saved case rejects exactly the two mislabelled
sweeps while retaining the other 58. Physical center identities survive
continuation, and protocol 3 rejects prior recordings without these checks.
Evidence: `calibration-runs/adversarial-review/gravity-collection/REPORT.md`.
This demonstrates an evidence-completeness defect, not an inaccurate fitted
torque model or a full motion-control bypass in that particular example.

Gravity collection itself reproduced a table-colliding group from the normal
simulator start. The shared group planner now excludes that complete group,
retains the original held-out split, and checks both splits' coverage. A native
run completed 28 of 50 sweeps before status delivery exceeded 100 ms; Stop was
confirmed and the run remained incomplete. Native ticks during the failure
peaked at 4.695 ms, with no stale-drive flags. Raw evidence is preserved under
`calibration-runs/gravity-fitting-j2-j3-v2/`.

A separate deterministic native-client test reproduced stale Python delivery
while newer packets continued arriving. Before the fix, readiness rejected a
132.4 ms-old Future. It now selects a genuinely newer native packet without
refreshing any timestamp. The same test closes the listener and confirms that
a cached stale packet still rejects. Before/fixed results are in
`calibration-runs/fresh-packet-before-fix.txt` and `fresh-packet-fixed.txt`.
This reproduces a delivery failure mode; it does not prove the exact scheduling
cause of every earlier long-run interruption.

The reviewer's native-library experiments found 20 compatible lower-envelope
cases using existing speed/acceleration fractions with the actual loaded caps.
Other requested tuples remained unproven. These fractions now match preview,
stream commands, and settling. A real native test deliberately dropping them
passed ordinary motion quality but must fail the newly added recorded-command
bound check. Removing that check in an isolated test process reproduced false
acceptance (`recorded-bounds-before-fix.txt`).


The recorded-command gate then rejected a real scaled trial: J5 feedforward
jerk reached 2.384 rad/s³ during an encoder-sized reversal near the start.
Native `MotionStream` selected whichever was larger in magnitude: interval
average velocity or endpoint velocity. Switching those definitions at a
reversal produced a discontinuity even though the underlying position OTG
respected its limits. A deterministic native regression reproduced 3.962
rad/s³ with configured scaled jerk 0.6. Feedforward now consistently uses
interval-average velocity; landing intervals still retain their commanded
advance. All four stream/stop tests passed, including the new reversal case
at <=0.6 rad/s³. Preflight and recorded command checks now use the configured
bounds directly, without the old 1.5x jerk allowance. Evidence:
`stream-reversal-before-fix.txt`, `stream-reversal-fixed.txt`, and the original
real-simulator failure `recorded-bounds-fixed.txt` under `calibration-runs/`.


With the matching fixed daemon and extension, the complete relevant Python
selection passed **29 tests in 202.57 seconds**, including six isolated-daemon
cases. The coupled test rejected deliberately lost native fractions: measured
command peaks reached 0.1010 / 0.2928 / 1.2000 against requested
0.1 / 0.2 / 0.6. Two correctly scaled coupled trials passed all six joints and
all three measured-excitation requirements, with maximum commanded speed about
0.09636, acceleration 0.20000, and jerk 0.60000 (floating-point tolerance only).
Budget continuation retained those accepted trials without duplication. The
full output and captures are in `calibration-runs/stream-consistency-regressions*`.
The four native adapter/braking tests also passed. No Commander or hardware
connection was made. The complete gravity-fitting collection has not yet been
rerun after the status-delivery and native-feedforward fixes; the earlier
28-sweep interrupted collection remains incomplete.

Final Ruff, Rust formatting, diff-whitespace checks, and isolated release
Clippy for par6d/par6-py with all targets and `-D warnings` passed.


## Terminal vibration and stopping-projection follow-up

The adversarial reviewer constructed a further passing bad motion trace from
actual native coupled-stream commands and an analytic, derivative-consistent
J2 oscillation. A 30 Hz burst lasting 0.1 seconds starts after the last live
analysis window. Native target/velocity data still permit the real 0.3-second
settling condition before the next live check. Every scheduled live window,
the old whole-trial acceptance, command bounds, and all 18 excitation checks
pass. The final 1.2-second window fails the existing vibration and velocity
residual bounds. This is a reproduced final-assessment defect, not proof that
the physical plant produces that particular response.

Final assessment now uses overlapping windows including the tail after Stop.
Feedback tuning reuses the same window calculation. Acceptance policy 6 rejects
older checkpoint evidence without these checks. The regression retains a
smooth native positive control and verifies that diagnostic allowance still
cannot hide tracking errors. Restoring the old final expression in an isolated
test process makes the new regression fail. Reviewer artifacts are under
`calibration-runs/adversarial-review/envelope-v4-tail/`.

The complete gravity-fitting attempt v3 collected 40 of 50 sweeps, then the
controller refused the approach to sweep 41. The recorded actual, commanded,
and requested positions were collision-free. Replaying native stopping travel
on the requested targets reproduces `[jaw1, install:floor]` at capture offset
157809. Collision mesh hashes and joint transforms match between the packaged
and repository assets; packaged jaws are fixed while native collision kinematics
hold their passive coordinates at zero. The refusal is explained by the omitted
stopping projection, not a demonstrated difference in table or finger geometry.
The run confirmed Stop and retained an incomplete outcome, with no fitted profile.

Offline ServoPreview now exposes the same gate's nominal stopping projections.
Calibration checks both target and commanded projections before motion and
tries a bounded set of slower approaches. The recorded approach collides at its
original planning speed; 75% clears that nominal projection. Actual tracking may
differ, so the runtime measured-velocity guard remains authoritative. Full
collection and physical validation remain outstanding.

The initial envelope planner also could not establish its starting limits with
the actual loaded stream caps: it sized short quintics using much larger EXEC
limits. Protocol 4 uses bounded constant-jerk pulses and the actual native stream
fractions. The native-library regression covers all six joints, all three
derivatives, and both directions. Its old-planner mutation fails, while all eight
envelope tests passed before the stopping-projection integration. These results
establish offline excitation feasibility, not the arm's mechanical limits.


Validation after this follow-up: **26 offline tests passed in 39.89 seconds**,
including native-preview stopping geometry, terminal vibration, measured
excitation, fitting evidence, clock handling, and earlier regressions. **Seven
isolated-daemon tests passed in 205.23 seconds**, covering calibration capture,
cancellation/Stop, incorrect gravity rejection, feedback tuning/restoration,
accepted limit probes with budget continuation, coupled motion, and lost Stop
or gain-restoration acknowledgements. No daemon contacted Commander or hardware.
Outputs and captures are in `calibration-runs/adversarial-{offline,sim}-regressions*`.

The old positions-only preflight mutation fails the new stopping regression
with `DID NOT RAISE`; the fixed planner admits a slower clear approach and
continues to refuse a colliding endpoint. Its output is retained in
`calibration-runs/stopping-preview-before-fix.txt`. The rebuilt extension and
daemon use the isolated `par6/target/calibration` directory. Ruff, Python/Rust
formatting, and whitespace checks passed.


## Complete collection, numerical fitting, and recorder identity

The next full simulator collection completed all 50 sweeps plus the return
motion, with confirmed Stop and no lifecycle error. Independent fitting and
validation coverage both have rank 10; 54,321 training samples and 36,210
withheld samples pass the per-sweep direction/travel checks. The original
`gravity-fitting-j2-j3-v4/run/gravity-fit.json` rejected an implausible coefficient:
column 11 received approximately 9.23 billion while contributing only about
0.0003 Nm. No profile was staged by that original run.

The regressor column is a numerical zero. Its norm crossed the former fixed
1e-12 cutoff as sample count grew; per-column normalization amplified it into
an apparent fit parameter. Repeating the existing native-model regression's
same 100 observations 100 times reproduced the rejection. A relative numerical
zero threshold now preserves the fit under that repetition. The before/fixed
regression outputs are `gravity-null-column-before-fix.txt` and
`gravity-null-column-fixed.txt`.

Reanalysis of the unchanged complete recording now stages a plausible candidate.
Withheld J2/J3 residuals fall from approximately 0.300 Nm to 0.0113/0.0116 Nm.
Against the simulator's known nominal model, 512 additional algebraic poses
show a maximum torque difference of 0.00617 Nm. They are not motion targets or
physical measurements. Original rejected results remain untouched; the candidate
and revised report are under `gravity-fitting-j2-j3-v4/reanalysis/`. The source
collection used policy 6 and the dedicated harness; it predates CaptureInfo.
Independent activation and motion verification remain separate requirements.

The review found that configuration fingerprints and file freshness alone do
not bind a recording to the connected controller process. CaptureInfo is an
additive read-only query (command 63, response 24) carrying the exact CAP2 header
identity, or nil when no recorder exists. Existing ConfigInfo/ConfigBundle
layouts are unchanged. Policy 7 verifies that identity at entry and around each
completed trial, retaining it for in-trial freshness checks. This guards wrong
local recording sources, not deliberate file forgery by its owner.

The full codec suite passed 24 tests, including independent golden bytes,
absence/presence, malformed fields, truncation and trailing data. The matching
native build succeeded and enum stubs were regenerated. A real two-runtime
regression rejects a foreign live file, accepts its own, rejects switching the
file after entry, and rejects a runtime with recording disabled. The old-source
mutation initially failed because its test fixture read before the first capture
flush. A condition-based readiness barrier resolves that setup issue; the
mutation then fails with `DID NOT RAISE` at the foreign-file assertion.
Artifacts: `capture-source-{fixed,before-fix-ready,proto-tests}.txt` and
`adversarial-review/gravity-activation/REPORT.md` under `calibration-runs/`.


After the recorder-query and numerical-fit changes, the combined calibration
selection and generated-stub checks passed **36 tests in 261.34 seconds**.
Strict release Clippy for par6-proto, par6-server, par6-client, par6d, and par6-py
(all targets, `-D warnings`) passed in 35.08 seconds. The full collection contains
101 motion records, all with accepted quality and no trial error. These checks
remain distinct from the pending fresh-runtime verification of the fitted model.


## Independent validation setup and native model loading

The first fresh-runtime attempt to validate the fitted candidate stopped during
session entry, before any sweep, on a 158.9 ms-old status packet. The retained
capture has no stale-drive flags and no native host-tick interval above 6.3 ms.
Evidence remains in `gravity-fitted-v4-independent/`; it is not a completed
validation. A separate real-client diagnostic reproduced a stale native cache
of 1.84 seconds during a preview-model constructor (`model-setup-status-before.txt`).

GravityModel, Preview, and CollisionWorld performed native file/mesh loading
while holding the Python GIL. Their constructors now release it, and calibration
runs their loading in an asyncio worker so status delivery can also run on the
Python event loop. The 100 ms admission and moving-feedback age gates are unchanged.
A real blocked-file regression requires status delivery before an external
writer releases the model read; all three old constructors failed that barrier
in 18.70 seconds (`model-loading-before-v2.txt`). The first test-harness attempt
used unsupported FIFO paths for Preview and a multiply opened URDF; it was
terminated and is not counted as regression evidence. The corrected test gates
ordinary gripper/SRDF reads, supports parser reopen, and bounds its external writer.

The final bounded motion-envelope source audit found no new false completion
under exclusive-control/startup-gain assumptions. It does not establish a full
executed envelope. Same-runtime gain changes by another client remain outside
the checkpoint identity contract. See
`calibration-runs/adversarial-review/envelope-completion/REPORT.md`.


The corrected native binding built successfully in 1m47s. All **eight focused
checks passed in 36.07 seconds**, including the three blocked-model loads,
recorder source switching, stale-status rejection, native preflight, and enum
stub consistency. Strict par6-py release Clippy passed with `-D warnings`;
Ruff, rustfmt, and whitespace checks passed. Results are retained in
`model-loading-{fixed,clippy,build}.txt`. Fresh-runtime candidate verification
now uses the separate `gravity-fitted-v4-independent-v2/` output directory.


## Independent fitted-model simulator validation completed

`gravity-fitted-v4-independent-v2/` loaded the staged fitted candidate in a new
isolated native runtime, verified exact configuration/drive readback and its own
CaptureInfo identity, and performed no parameter refit. All 50 moving verification
sweeps plus approaches/return completed: 101 motion records, no rejected quality
check, no trial error, and confirmed Stop/cleanup. The report is complete and
`torque-consistent`, with rank 10 and weakest normalized singular value 0.18945.
One of six proposed groups was excluded before motion for a finger/floor collision.

Across the 25 retained joint/pose combinations, the largest paired-torque residual
was 0.00366545 Nm, compared with the 0.05 Nm limit. Worst RMS residual was
0.00237391 Nm. This completes an independent simulator fit/load/moving-verification
path; it does not establish physical mass/COM identification or arm validation.
The odd-friction assumption, measured-pose scope, and
`applied_validation_complete=false` remain explicit in the result. No hardware
configuration or motion was involved.

The subsequent source-only smoothness audit found identical A/B settings under
ordinary STREAM<EXEC caps, pooling of held-out observations into training means,
and export of untested JOG increases. New regressions use real native preview
commands with an explicitly analytic, position/velocity-consistent mechanical
response. Their source audit is
`calibration-runs/adversarial-review/smoothness-completion/REPORT.md`.


## Smoothness false acceptance reproduced and corrected

The first native-command/analytic-response regression run produced exactly seven
failures and one passing improvement control in 12.24 seconds
(`smoothness-before.txt`). It demonstrated acceptance of a 20-fold held-out RMS
regression despite pooled improvement, identical quiet settings, missing/duplicate
or incorrectly labeled observations, identical selected caps, and a probe unable
to exercise the proposed derivative change. These are numerical mechanical
responses on real native preview commands, not measured arm transfer functions.

Protocol 2 now chooses realizable global stream fractions, halves candidate
acceleration/jerk at the tested speed, and uses those fractions in preview and
execution. It requires matched evidence and independent training/held-out
assessment. JOG limits are preserved because no JOG motion was measured.
Interrupted/error paths retain an invalid, incomplete report; their status cannot
remain `improved` after a return/export failure.

Follow-up review exposed another pooling gap: forward and other-joint improvement
hid a 20-fold J1 backward-stroke regression within every tested pose. The numerical
reproduction failed the first fix (`smoothness-direction-before.txt`). Acceptance
now checks each (pose, requested joint, direction) separately across repeats,
including affected-joint vibration and latency. All nine offline regressions then
passed in 15.46 seconds (`smoothness-direction-fixed.txt`).

An actual-native workflow regression deliberately restored the former unscaled
execution behavior. It failed before the candidate moved: J1 commanded jerk was
1.2 rad/s³ against a 0.6 rad/s³ candidate bound (`smoothness-unscaled-before.txt`).
This confirms that the new native scaling matters in the integrated routine.
A complete physical smoothness calibration, JOG calibration, and full executed
motion-envelope search remain outstanding.


The corrected real workflow test passed in 27.18 seconds
(`smoothness-workflow-fixed.txt`). It executed the first candidate through the
ordinary native client, confirmed recorded command peaks within its scaled caps,
then cancelled the unfinished protocol. Stop and encoder rest were confirmed;
the retained report is invalid/incomplete with failed status and no exported
profile. This is a bounded integration check, not a full smoothness calibration.

Final checks for this continuation: eight loading/source/freshness/preflight/stub
checks passed, nine adversarial smoothness checks passed, and the bounded native
smoothness workflow passed. The independent gravity run completed all 50 sweeps
and 101 motion records without refitting or a quality rejection. Required strict
par6-py Clippy, Ruff, rustfmt, and patch whitespace checks passed. All simulators
started by this continuation have stopped. Commander and hardware were untouched.


## Recording capacity and envelope completion

A source audit found that ordinary envelope continuation can outlast the fixed
one-hour capture. The production trajectory generator plans 1,224 isolated
trials and 12 coupled trials for STREAM caps (0.2, 0.4, 1.2). Command duration plus
minimum settling/Stop time alone is 4,125.36 seconds, before approach travel,
setup or computation. `calibration-runs/envelope-duration-budget.json` preserves
that all-pass workload estimate; it is not an executed completion claim.

Two real regressions exposed the recording limits. An isolated daemon ignored an
80-sample requested cap and kept writing (`capture-budget-before.txt`). A native
capture-channel/disk-writer test retained only 900,000 of 900,001 submitted frames
(`capture-capacity-native-before.txt`). The latter passes with the configurable
budget (`capture-capacity-native-fixed.txt`). The bounded default is unchanged;
`PAR6_DIAGNOSTICS_MAX_SAMPLES` or the overriding `--diagnostics-max-samples` flag
selects a positive sample count before startup. No wire/header format changes
are involved. Recording includes idle time; exhaustion leaves the controller
running but calibration refuses stale evidence. Restarting invalidates the
same-runtime/reference continuation, so capacity must cover the whole search.

The reviewer also reproduced stale successful envelope JSON after a failed
final return, failed export, or early argument rejection. The regression uses a
real isolated CalibrationSession, native return motion and profile validation,
with real path/filesystem errors. Its phase report tests finalization only; it
is not evidence that the complete search has passed. Failed-before results are
retained in `calibration-runs/envelope-finalization-before.txt`.

STREAM evidence alone does not certify the normal queued EXEC path, which has
separate planning, torque feedforward and settling. Envelope reports must name
the tested mode and retain `applied_validation_complete=false` until a separately
recorded activated-profile comparison exercises that path. JOG likewise remains
outside the current envelope validation.


The finalization fix passed the same regression in 17.73 seconds
(`envelope-finalization-after.txt`); failed-before took 26.72 seconds. Entry
invalidates an older report, return/export errors persist invalid and incomplete
status with their reason, and an existing motion-profile prevents same-session
reuse before motion while retaining its original manifest. STREAM scope and
pending applied validation are explicit. This test's daemons exited; scoped
Ruff checks passed.


The rebuilt isolated daemon passed both recording-budget end-to-end checks in
7.72 seconds (`capture-budget-fixed.txt`): exact 80-sample cutoff with continued
live status and stale-evidence rejection, plus invalid CLI/environment values
and CLI precedence. The final native >900,000-frame test passed in 2.40 seconds;
strict par6d release Clippy for all targets passed in 13.29 seconds. Rustfmt,
scoped Ruff and patch whitespace checks passed.

A complete configured search is now running under
`calibration-runs/full-envelope-v1/`, using the independently verified fitted
simulator candidate, a 3,600,000-sample recording budget, and 180-trial invocations
with same-runtime/reference continuation. The initial J1 observations pass.
The long run is pending; this entry does not claim a completed envelope or
activated EXEC validation. Its console is `full-envelope-v1-console.txt` and
its harness is `handoff-probes/full_envelope_v1.py` in that ignored evidence tree.


## Queued execution and shutdown review (in progress)

The complete STREAM search has passed its first 180-trial invocation and resumed
in the same isolated simulator. It has exported no profile. The current observed
checkpoint is recorded in `calibration-runs/full-envelope-v1/monitoring.json`;
that file is an observation, not a live health assertion. This run must retain
its process, reference and recorder identity. No hardware or Commander was used.

A separate queued-execution verifier is drafted in
`par6/python/par6/calibration/execution.py`. It is not yet exposed by the public
calibration module or example runner and has not passed numerical/native tests.
It plans 108 single-axis and 12 coupled trials through actual RUCKIG MoveJ,
requires the exact command acknowledgement and COMPLETE verdict, continuous
EXEC capture, measured derivative excitation, endpoint hold quality, and
confirmed Stop/rest. Its scope is the tested RUCKIG trajectories with the empty
configured gripper; it does not identify inertial parameters or validate JOG.

Adversarial source review found three concrete draft gaps:

* The offline Preview inherited the configuration's initial profile, which can
  differ from the runtime-selected RUCKIG planner. The draft now explicitly
  selects RUCKIG in Preview before planning.
* Allowing gravity compensation to remain disabled could validate performance
  without exercising the gravity model used by ordinary startup. The draft
  requires enabled gravity compensation and refuses changes during verification.
  Feedback can still mask model error; this remains a performance check, not
  independent gravity identification.
* Examining only the final two seconds of a 2.5-second post-COMPLETE observation
  can miss a smooth 0.2-degree droop over the first 0.4 seconds, followed by a
  stationary offset. This is an analytic counterexample inferred from the
  thresholds, not a recorded physical result. The draft now checks excursion
  over the entire terminal commanded-target plateau as well as the final hold,
  and tracks live post-COMPLETE excursion. The full plateau includes settling
  and may conservatively reject it. A buffered capture cursor is explicitly
  not an exact native completion tick. Numerical regressions are being prepared.

The reviewer also prepared a native client shutdown fix. A real Python client
and silent loopback UDP sink reproduced a Tokio consumed-oneshot panic after
close returned; the probe did not finish recording a late retry datagram, so that
part remains source-established. Pending request removal now uses an RAII guard,
backoff wakes on closure, and joined close waits for entered roundtrips. Three
virtual-time tests over actual UDP sockets are prepared, with exact before/after
source snapshots in `calibration-runs/adversarial-review/client-close/`. Rust
builds/tests are deferred while the timing-sensitive envelope capture runs.
Already-sent datagrams cannot be recalled by this client fix.

Queued cleanup preserves pending submissions while draining them, attempts a
latched software EStop when acceptance is uncertain, and still attempts final
Stop if an earlier cleanup step fails. These draft semantics require native
integration tests before exposure. A separate native preflight regression is
prepared for approaches after EXEC limits are activated below STREAM limits;
its production scaling fix has not yet been applied. Scoped Ruff and whitespace
checks passed; these are not substitutes for the deferred behavior tests.


The early-droop numerical test is now prepared in
`par6/python/tests/test_calibration_execution_hold.py`: native RUCKIG positions,
explicitly reconstructed interval-average velocity, healthy quantized control,
and analytic 0.2-degree droop/rebound responses. It requires ordinary motion
quality, derivative excitation and final-two-second quality to pass before
asserting rejection of the early excursion. The unchanged assessor snapshot is
`calibration-runs/adversarial-review/execution-hold/execution-before.py`.
Neither before nor after numerical execution has run yet.

`par6/python/tests/test_calibration_execution_e2e.py` also prepares a real isolated
controller workflow: healthy queued completion/hold, actual Stop immediately
after COMPLETE, invalid mask admission, and refusal with gravity disabled.
It remains unrun. All three execution source/test files pass scoped Ruff.
The client close/retry fix remains unbuilt; no shared daemon or native extension
was restarted/replaced during this continuation.


## Continuation audit and remaining motion modes

A read-only audit of the active envelope continuation found no normal-flow
incomplete-export path. The two completed invocations remained invalid and
incomplete. Isolated acceptance requires two poses, both directions and three
repeats per candidate; all 18 joint/derivative entries must be measured. All
12 coupled holdout trials and successful final return are required before
staging. The running process subsequently reached 403 observations with no
rejection. These are progress observations, not completed-envelope evidence.

Two qualified limits remain. Resume trusts stored `accepted` flags without
reconciling their numeric metrics; current production checkpoints were consistent,
but an inconsistent or edited checkpoint could bypass admission. The exact
80%-reduced matrix is tested only at the coupled holdout pose. A pose-dependent
resonance at that matrix could therefore remain untested at the training poses.
The exported result remains a staged candidate with applied validation false.
The queued verifier and physical validation must not be omitted.

JOG is a distinct remaining protocol. Source inspection confirms that JogJ
fractions use the JOG limiter and integrated position/velocity targets, with a
duration watchdog. Native preview retains ramp state across consecutive JogJ
submissions. A finite preview duration is the amount integrated; the live duration
is the watchdog, so they cannot be substituted without accounting for command
refresh timing and braking. `law_jog` does emit both position and velocity commands,
so native CAP2 quality analysis can retain their actual values. A JOG calibration
must exercise that limiter, releases/reversals and measured stopping travel in
its own mode; existing STREAM comparisons cannot supply that evidence. No JOG
limits or public JOG calibration API were changed during this audit.

The operating documentation now reflects the retained passing independent
native gravity run: 50 verification sweeps, 101 motion records, maximum paired
residual 0.00367 Nm, one preflight-excluded pose group, and active-feedback /
compensation-disabled scope. Compensation-enabled EXEC, JOG, table vibration,
and physical arm validation remain separate outstanding work.


## Compensation-enabled Stop support gap

A subsequent source audit found that the queued verifier's ordinary Stop would
enter gravity-only IDLE. Both the shipped configuration and fitted simulator
candidate disable freedrive drift lock. Native IDLE then omits q/qd commands
while applying G(q), bypassing the physical-release restriction on the former
gravity-only assay. The new unexposed verifier now explicitly refuses hardware
until a continuously supported entry/exit protocol is validated. This is an
interim gate, not completion of the requested physical calibration routine.
The public AsyncRobotClient.stop documentation was corrected: enabled idle
support follows gravity/freedrive settings and is not always position holding.

The existing APIs offer a source-supported sequence for simulator testing:
start with IDLE/G=False active feedback, acknowledge pause, submit the actual
MoveJ, confirm fresh EXEC/paused, enable G while remaining paused in EXEC, then
resume. Native EXEC entry preserves a pre-entry pause. Disabling G while still
EXEC also preserves feedback; active_support can then confirm G=False and Stop.
Pause persists through queue flushing, so cleanup must restore it after the
queue is cleared. This avoids a designed G=True IDLE phase without adding a
wire command, but the feedback/feedforward transients still require measurement.
A real isolated native test is being prepared; the sequence is unvalidated.
Drift lock alone is not an immediate workaround because it waits its settling
interval in gravity-only support before arming.


The supported-handoff experiment is prepared in
`par6/python/tests/test_calibration_execution_support_e2e.py`; it remains unrun.
It retains 1.5 seconds of actual captured EXEC/G=False position support before
Stop, measures both gravity transitions with ordinary quality gates, and rejects
any gravity-only IDLE tick in the controlled interval. It deliberately pauses
again before Stop to prove queue clearing preserves pause, then confirms explicit
restoration. Wire `executing_index` comes from server PlanStarted, independently
of the RT ring's active metadata; the test waits for matching mode/pause/gravity
and command identity together and checks the same identity after resume. Exact
COMPLETE remains required. This avoids assuming an ordering between status and
planner events. Ruff and whitespace checks passed; all behavior tests remain
queued behind the active envelope run.

A bounded second continuation audit checked interruption between accepted stimulus and checkpoint persistence, the isolated-to-coupled handoff, and partial coupled resumption. It found no new normal-flow false export: unsaved trials are rerun and coupled evidence remains required. This does not resolve the previously documented editable-checkpoint trust limitation. The supported-handoff test artifact now stays invalid through session exit and owned client/daemon teardown; assertion or cleanup failures persist their errors. Its scoped Ruff checks pass, but the test still awaits the simulator slot.

## Public STREAM support and provenance (native verification pending)

The public example caller previously entered check, smoothness and envelope routines without disabling gravity. Their trial cleanup and context exit could therefore select gravity-only IDLE. The same runtime could also resume an envelope after a gravity toggle because that state was absent from the continuation identity. The reviewer preserved exact before sources and prepared a real default-G-enabled `check_motion` regression under `calibration-runs/adversarial-review/public-stream-support/`. It has not run yet.

The draft fix establishes active support before these protocols, requires gravity-disabled STREAM both in live status and native capture, and labels reports/trials. Envelope protocol 5 binds that condition and refuses old unlabeled checkpoints; smoothness protocol 3 records it. Cleanup establishes G=False before Stop, attempts latched EStop and fresh ACTIVE_ERROR confirmation if support setup fails, and always attempts Stop/rest. Its outcome is written to `active-support.json`; failures remain errors and the latch is never cleared automatically. The fault path still needs actual transport-failure coverage. Scoped Ruff passes after formatting.

The already-running full envelope process retains its previously imported protocol 4 throughout; it has not been restarted or mixed with protocol 5. Its raw capture remains the evidence for its compensation state. The first queued-support experiment is scheduled in exec session 23315 (supervisor PID 1189746) and starts only after both the full-envelope harness and its owned simulator exit. No other numerical test/build may overlap it.

## Executed adversarial checks and terminal long-run result

The full configured envelope stopped at 862 accepted observations in run-04. At the next approach admission, feedback age was 118.6 ms while the controller reported IDLE, enabled/homed, healthy link and no error. The 100 ms gate rejected it; outcome confirms no Stop error. No envelope/profile was completed or exported. The owned simulator and harness exited with failure; the old monitor is terminal. The failure and raw native capture remain under `calibration-runs/full-envelope-v1/`; do not restart from an old-reference checkpoint or claim a completed envelope. Waiting for a fresh packet before an idle approach, without weakening supervision while moving, remains to be investigated.

The automatic supported-EXEC experiment then ran and failed its real measured quality gates in 19.17 seconds. It did confirm the exact command COMPLETE, matching paused/resumed command identity, zero gravity-enabled IDLE samples, restored pause and confirmed Stop. Both gravity switches nevertheless caused J5 tracking/velocity residual failures: 0.8020 degrees excursion on enable and 0.7690 degrees on disable. Keeping position feedback engaged alone is not a sufficiently smooth handoff. This failure remains preserved; the hardware admission gate stays in place.

The prepared early-droop numerical regression now has executed before/after evidence: the original assessor accepted both analytic 0.2-degree droop and rebound while the final two seconds were quiet (2.63-second failed-before run). The corrected assessor rejected both with a 0.19914-degree measured terminal excursion and retained the healthy control (2.59-second passing run). These are explicit analytic responses to a real native RUCKIG position plan, not measured arm behavior.

The actual default-G-enabled public `check_motion` regression failed before on gravity-only IDLE/omitted velocity support, and passed after on both unchanged-quality J1 sweeps with active support (22.50/22.52 seconds). Native approach preflight reproduced J1 jerk 1.2 exceeding the activated 0.96 bound; `position_settings()` now supplies the same realizable fractions/bounds to planning and execution, and both native geometry/scaling tests pass in 9.81 seconds.

Real opaque-UDP reply loss reproduced the unsupported cleanup after a lost gravity-disable ACK (5.05 seconds), and the fixed helper passed in 3.73 seconds. An added lost-Stop-ACK case reproduced the remaining missing latch (5.04 seconds). Cleanup now also fences a failed Stop and attempts confirmed Stop/rest again. Both actual ACK-loss variants pass together in 7.23 seconds, preserving the original errors and the software latch. No hardware was used.

The first native client test run reproduced pending-registration leakage, but its normal-retry control failed against both old/fixed code and its old close case did not reach backoff. The reviewer corrected the virtual-timer barrier using Tokio's actual deadline rounding and driver-processing semantics. Re-execution is pending; the native client patch is not yet runtime-verified or installed.
