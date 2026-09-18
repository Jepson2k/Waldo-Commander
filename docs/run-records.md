# Run records

Open **Run records** (the bug icon in the program toolbar) and enable
**Record future program runs**. Recording is off when Commander starts. Changing
the checkbox affects the next launch; it does not change a running program.

Select a recent run to inspect its events. Filter by skill or command name and
click a row to inspect local values. The record includes:

- A hash of the executed source and installed package versions.
- Bounded readbacks of the tool, TCP, payload, profile, world and execution speed
  before launch. Unsupported or unavailable queries are identified. Readbacks
  have their own observation times; they are not one atomic controller snapshot.
- Skill identity, version, parent invocation, named arguments, progress, result,
  failure and cancellation. Arguments are captured before the function mutates
  them. The supplied client is excluded.
- Managed command arguments, dispatch return values and exceptions. A returned
  command index acknowledges dispatch. `command_completed` requires the existing
  backend completion wait; `command_unconfirmed` means that wait expired.
- The named setup snapshots actually loaded by the program, rather than every
  setup stored on the computer.
- Controller session, publication sequence and timestamp, joint readings and
  health. Status is sampled at up to 2 Hz, with additional entries for reference,
  enablement, collision or session changes. Sequence gaps include intentionally
  skipped publications; this is not a full-rate telemetry recording.
- Pause/resume/step actions and the process's terminal outcome. A stopped process
  with an unconfirmed controller stop is reported separately.

Managed Python calls keep the usual stepping and pause behavior. Recording does
not freeze arbitrary Python, change backend deadlines, or restore Python locals.
Standalone Python can opt into the same skill metadata with
`observe_skills(callback, capture_values=True)`; persistence remains the caller's
responsibility.

## Storage and sharing

Local journals live in `~/.waldo-commander/runs` (override with
`WALDO_RUN_RECORD_DIR`). They contain no source code, console output, environment
dump, camera frames or unrelated files. Credential-named fields are redacted.
Other strings in explicitly captured arguments/results can contain personal
data. Each journal is limited to 4 MiB; truncation is marked, and terminal outcome
space is reserved. Complete entries remain readable after a partial final write.
The dialog lists the most recent 20 files; older records remain on disk.

**Export debugging data** produces a separate JSON document. It keeps numeric
and structural values, timing, standard command names and package versions.
Free-text values, custom skill names, custom mapping keys and error messages are
omitted; invocation identifiers are replaced with local aliases. This makes the
export deliberately less detailed than the private journal. No files are
uploaded automatically.

Captured values are bounded and lossy: unsupported objects, large arrays, deep
structures and excess values carry omission/truncation markers. They are never
an input format for executing a program or restoring a robot state. If the GUI
falls behind the bounded IPC history, the record explicitly reports lost events.
