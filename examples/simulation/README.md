# Simulation cases

Run installed PAR6 model cases sequentially, without starting a robot runtime:

```sh
waldo-sim-cases examples/simulation/*.json --output results.json
# Equivalent without the installed console entry point:
python -m waldo_commander.services.simulation_scenarios examples/simulation/*.json
```

Each JSON file contains a Python program, initial joint angles in degrees,
reference state, simulated time budget, perturbation inputs, and expected stop
reason. `initial_tool` selects the tool the run starts with as
`["KEY", "variant"]`, the variant left empty for a tool that has none — which
is every PAR6 gripper today. `world` optionally contains a `waldoctl.world` snapshot of program
geometry. Installation geometry comes from the installed PAR6 model. Declare
attachments in Python using the new preview's attachment context.

Loading a file only reads data. Running it executes trusted Python in a disposable
worker, with the same client substitution and planning/physics passes as the
editor. Process isolation is for cancellation; it is not a security sandbox for
untrusted Python. `wall_timeout_s` also bounds programs that loop without issuing
robot commands. The runner uses the packaged model; it does not fetch a nearby
controller's configuration.

The examples cover normal idle, delayed/noisy observations, a latched encoder
fault, and explicit held-object geometry. The encoder-fault case passes only
when the native runtime reports joint fault 58. A failure with a different code
does not satisfy it. This validates declared geometry; it does not assert a
sensed grasp or add physical payload mass.

Perturbations use simulated seconds after model initialization:

| Input | Meaning |
|---|---|
| `seed` | Reproducible unsigned 64-bit noise seed |
| `observation_delay_s` | Host telemetry delay, 0–1 s |
| `encoder_noise_ticks` | Bounded motor-encoder observation noise, 0–16384 ticks |
| `dropout` | `start_s` and positive `duration_s` without replies |
| `driver_fault` | `at_s`, configured `node`, and `kind`: encoder, temperature, vbus, driver, velocity, current, or estop |
| `supply_loss` | `at_s` and `decay_s`; linear assumed supply reduction to zero |

Noise affects host observations, leaving the driver's local feedback intact.
The native runtime still reacts to stale observations and faults. Supply loss
removes powered support and motor torque; passive gravity, inertia, friction,
and joint limits remain. PAR6's capacitor bank is not electrically modeled.
These inputs are assumptions, not measured hardware responses or motor brakes.

The report includes outcome, command errors, recorded duration, final observed
joints, a replay digest, package versions, and hashes of the case and packaged
model. A matching digest is useful within the same build and platform; it is not
a promise of identical floating-point physics across platforms. The CLI exits
nonzero if any case fails, times out, or cannot be simulated.

Physics replays queued motion, tool actions, TCP/tool changes, world edits,
profile/payload/completion settings, and execution speed/pause controls.
Other system and streaming operations produce an explicit replay error.
Python conditionals follow the planning pass; physics does not execute the
Python program a second time or invent sensor observations for it.
