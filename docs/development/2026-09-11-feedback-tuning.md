# Feedback tuning and calibration timing — 2026-09-11

All new motion in this work used isolated `par6d --sim` instances on ephemeral
ports. The user unplugged the physical arm for the night. No hardware or
Commander-managed runtime was contacted or changed.

## J2 instability

The original moving-gravity verification trace shows a growing J2 oscillation
near 24 Hz while the command remains smooth. The 1-second measured velocity
residual rises from about 0.003 rad/s to 0.027 rad/s before the live gate rejects
it. Evidence: `calibration-runs/moving-verification-native-v2/`.

Reducing only J2's velocity integral gain to 40%, preserving its proportional
and position gains, makes both formerly failing sweeps pass:

| Direction | Velocity residual RMS | Peak tracking error |
| --- | ---: | ---: |
| negative | 0.00191 rad/s | 0.0236 degrees |
| positive | 0.00190 rad/s | 0.0266 degrees |

This is a simulator result, not a hardware tuning recommendation. The native
client regression applies the volatile gain change, observes successful motion,
restores the original tuple, and reproduces the rejected response. It does not
rely solely on an acknowledged write or a restoration flag. Full-arm gain
selection remains necessary: uniformly reducing all integral gains also caused
wrist tracking failures on return moves.

## Feedback calibration changes

- Integral gain can be reduced independently of proportional gain. Search first
  preserves proportional damping, then considers reducing both velocity gains.
- Default ±0.18 rad excursions sustain motion long enough to expose the growing
  oscillation; shorter excursions remain available where geometry requires them.
- Travel and hold have separate acceptance checks. Overlapping windows also
  cover the complete recording, including transitions. Every candidate must
  meet ordinary motion acceptance in all three views.
- Baseline/candidate comparisons establish matching initial poses before each
  trial, including independent validation. Diagnostic repositioning uses the same
  0.05 rad/s ceiling and permits the vibration being measured, while retaining
  tracking, current, readiness and Stop checks.
- A bounded rejected stimulus is recorded after confirmed Stop. A lost-ready
  state, controller fault or failed stop ends the experiment. Gain restoration
  remains protected from cancellation.

The reviewer found a concrete false pass in the earlier physical J3 recording:
`physical-20260911-4.bin`, rows 393639–394650. A quiet hold diluted the whole-trial
velocity residual to 0.02988 rad/s, below the 0.03 threshold. Current phase
analysis yields 0.05109 rad/s during travel and rejects velocity residual,
vibration and command ripple. The hold itself is quiet. The saved replay is
`calibration-runs/feedback-motion-phase-replay.json`.

## Command timing

Capture decoding was changed to bulk field conversion, retaining exact values.
Spectral evaluation runs outside the command producer with a 0.5-second deadline,
including the final outstanding result. Readiness/current checks also run during
settling. A telemetry task updates the latest checked status; the command producer
does not wait for a separate 50 Hz telemetry tick before every 50 Hz command.
Cached feedback is bounded by native client receipt age plus reported bus data
age, with a 100 ms limit. A local monotonic receipt timestamp survives GIL and
Python event-loop delays; the wire protocol is unchanged. The bound is checked
again immediately before emission and during Stop rest confirmation. Cancellation ends telemetry reception and confirms Stop before returning.

The first combined analysis/native regression run passed 17 tests in 144.08 s.
Subsequent timing changes still exposed intermittent scheduling failures in the
long native test. Instrumentation identified a 95.87 ms generation-2 garbage collection during a
failed motion interval. The first heap-freeze mitigation was ineffective when
pytest already had a nonempty frozen set; retaining that caller freeze skipped
the additional freeze. The native receipt-age fix exposed another stale packet
in this condition (three lifecycle cases passed, the long J2 case stopped).
Motion now defers automatic cyclic collection through Stop cleanup and restores
the caller's enabled/disabled state; existing frozen objects are unchanged.
Reference counting still releases acyclic capture data. The command and feedback
deadlines remain unchanged, and Python still has no hard real-time guarantee. The automatic J2 search subsequently passed the run described below. The full
moving-gravity verifier still requires a passing end-to-end run.

## Evidence and scripts

`calibration-runs/handoff-probes/` contains executable isolated reproductions.
The `j2-integral-0.4/` run records the successful pair. The three full-route
attempts distinguish timing failures from J4/J5 return tracking failures:
`moving-verification-integral-0.4/`, its `-v2/` repeat, and
`moving-verification-selected-integral/`. The failed automatic-search attempts
are `feedback-native-j2/` and `feedback-native-j2-v2/`.

## Follow-up verification

After native receipt-age enforcement and scoped GC deferral, all four native
calibration lifecycle tests passed in 139.57 seconds. They cover actual motion,
rejected gravity probes, gain-response restoration, cancellation, and preservation
of caller GC state. The delayed real-packet admission regression passed, as did
the native packet/receipt pairing regression. The full-process maximum GC pause
was 140 ms, with no collection while automatic GC was disabled; collection has
been deferred beyond the stimulus, not made bounded.

SYSTEM acknowledgement value 0 means unconfirmed, not success. Calibration now
requires value 1 for Stop, gravity-compensation switches, and feedback changes.
This also prevents unconfirmed gain restoration from being recorded as complete.

A separate session was building another checkout into the same Cargo target
directory, producing a stale dependency mismatch and lock contention. Subsequent
calibration native builds use `par6/target/calibration` to isolate Rust artifacts.
The other session's process and checkout were left untouched.

## Successful automatic J2 search

`calibration-runs/feedback-native-j2-v3/` completed the actual automatic search,
including six baseline trials, six candidate trials, and three separate
validation trials for each setting. It selected Kiv = 0.0008 (80% of baseline),
with Kpv = 0.01 and Kpp = 3 unchanged. Search follows preference order and stops
only after independent validation, avoiding exposure to less-preferred settings
when the preferred candidate already qualifies.

| Phase | Baseline moving RMS | Selected moving RMS |
| --- | ---: | ---: |
| Six comparison trials | 0.02235 rad/s | 0.001975 rad/s |
| Three independent validation trials | 0.01783 rad/s | 0.001991 rad/s |

Peak selected-joint tracking error during candidate validation was 0.0158 degrees.
The experiment restored the original tuple and completed with no Stop error. It
staged a candidate and rollback configuration under `run/feedback-profile/`.
This validates the selected joint at the recorded center and excursions in the
simulator. It is not physical tuning evidence or whole-workspace validation;
no physical-arm configuration was changed.

## Settling-vibration counterexample

The next adversarial check exposed a gap between the travel crop and final
one-second hold. A derivative-consistent analytic recording placed a 30 Hz burst
in that gap. Its 0.12 rad/s peak velocity produced only 0.02427 rad/s whole-trial
residual RMS, so even strict whole-trial acceptance missed it. Both the comparison
and validation gates accepted the bad signal. This is a signal-analysis
counterexample, not a demonstrated native-plant response.

Feedback quality now includes 1.2-second windows every 0.5 seconds, with a final
window aligned to the recording end. Their worst metrics enter both ordinary
acceptance and candidate scoring. One window measures 0.05180 rad/s residual
and 0.06167 rad/s vibration in the counterexample, rejecting it. The regression
exercises the production candidate gate for both comparison and validation and
also accepts a low-vibration control. The analysis suite passed 14 tests in
6.44 seconds. The original evidence is preserved in
`calibration-runs/adversarial-review/ringdown/REPORT.md`.

The automatic J2 run above preceded this window change. Reanalysis of all 18
native recorded trials through the current metrics and acceptance helpers still
passes both comparison and validation. Its separate result is
`calibration-runs/feedback-native-j2-v3/window-revalidation.json`; the original
run artifacts remain unchanged.

## Communication and build checks

The two lost-acknowledgement regressions use an opaque loopback UDP relay against
a real simulator. Dropping either the Stop reply or the gain-update reply during
restoration now raises and records `restored=false`. Both tests reproduced false
restoration against an isolated copy with the former `< 0` checks. Together with
the delayed-packet case, the fixed communication suite passed three tests in
17.12 seconds. The tests use the calibration feedback cadence (250 Hz native
sampling, 50 Hz STATUS), keeping the 100 ms freshness gate unchanged.

Final native checks passed: rustfmt and release clippy with `-D warnings` for
`par6-client` and `par6-py`, including their test targets. Python Ruff and diff
whitespace checks also passed.


## J3 and full-route follow-up

With only the independently validated J2 integral reduction loaded, the full
moving gravity verifier completed both J2 sweeps and then rejected J3 vibration.
The failure retained native evidence and confirmed Stop:
`calibration-runs/moving-verification-j2-validated/`.

The automatic J3 comparison at the failing center subsequently passed all six
comparison trials and three independent validation trials for its candidate.
It selected Kiv = 0.0012 (80% of baseline), retaining Kpv = 0.015 and Kpp = 5.
J2 retained its separately tested reduction throughout this run.

| J3 phase | Baseline moving RMS | Selected moving RMS |
| --- | ---: | ---: |
| Six comparison trials | 0.02293 rad/s | 0.002794 rad/s |
| Three independent validation trials | 0.02238 rad/s | 0.002762 rad/s |

Peak J3 candidate tracking error in validation was 0.01780 degrees. The run
restored the baseline tuple and completed without a Stop error. Unlike the
older J2 capture, this J3 run used the full overlapping-window checks live.
Evidence: `calibration-runs/feedback-native-j3-v2/`.

The first attempt, `feedback-native-j3-v1/`, stopped after two completed baseline
trials because feedback exceeded the unchanged 100 ms freshness bound. The next
run completed after timing diagnostics were added; that alone does not establish
the cause or eliminate scheduling failures. Future cached-feedback failures now
record both Python-cache and native-cache age/sequence to distinguish delivery
lag from missing native updates.


## Full recorded gravity verification and final regression checks

The run with validated J2/J3 integral settings completed 50 sweeps at five
retained poses plus all approaches/return (101 motion trials). Its original
report rejected the last J6 pair for insufficient matched speeds. Native catch-up
ticks exposed a measurement-clock error: fixed-timestep simulator positions were
being differentiated against host elapsed time. Hardware keeps monotonic time;
simulator derivatives now use the physics tick clock, while host liveness checks
remain independent.

Replaying every trial and overlapping acceptance window with the corrected clock
produced no motion failures. All required paired-torque checks passed on 113,806
steady samples; measured rank was 10 and weakest normalized singular value
0.1894. This is reanalysis of the complete actual native recording, not a fresh
hardware run. Evidence and the unchanged original result are retained in
`calibration-runs/moving-verification-j2-j3-validated/`.

The revised all-axis coupled test passed actual native motion and checked budget
continuation, confirmed rest and refusal before motion when the loaded limiter
would violate a proposed envelope. The final calibration regression selection
passed **25 tests in 185.57 seconds** (20 analysis/native-library tests and five
real-daemon lifecycle/coupled cases). Ruff and whitespace checks passed. Full
maximum-envelope search, gravity fitting/activation and physical validation are
still required; the native verifier's corrected recorded success does not prove
those separate stages.


The corrected physics-clock analysis also revalidated all 18 trials from each
J2 and J3 automatic search. Both comparison and independent validation still
pass for each selected 20% integral reduction. Separate results are
`feedback-native-j2-v3/physics-clock-revalidation.json` and
`feedback-native-j3-v2/physics-clock-revalidation.json` under `calibration-runs/`.
The native `servo_j(speed=..., accel=...)` interface can scale velocity separately
from acceleration/jerk; candidate limiter validation should investigate those
existing controls before adding a runtime configuration API. Current combined
validation intentionally refuses a lower candidate that violates bounds under
the actually loaded limiter.


## Native feedforward and evidence follow-up

An added recorded-command upper-bound gate exposed a native feedforward
reversal defect: choosing the larger magnitude of endpoint velocity and
interval-average velocity could produce jerk jumps. The native reversal test
reproduced 3.962 rad/s³ against a scaled 0.6 limit. `MotionStream` now uses
interval-average velocity consistently. Four native stream/braking tests and
29 analysis/isolated-daemon calibration tests passed with the rebuilt matching
preview and daemon. Correctly scaled all-axis trials reached their measured
excitation thresholds without exceeding 0.1 / 0.2 / 0.6 command bounds.

The same follow-up added signed-joint/travel evidence to gravity collection,
retained whole collision-free groups without changing the held-out split, and
fixed selection of stale Python Futures when genuinely newer native packets
are already available. Stale native packets and command deadlines still reject.
See `2026-09-11-calibration-adversarial-review.md` for preserved counterexamples
and limits of the evidence. The full gravity fit and physical validation are
still outstanding; earlier gain validation recordings predate this feedforward
change and must not be presented as a fresh physical comparison of it.
