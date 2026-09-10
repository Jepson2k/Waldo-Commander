# Preserved session evidence — 2026-09-10

The debugging session ended without reliable full-arm homing or a validated autonomous arm-calibration routine. See [the final handoff](../2026-09-10-handoff.md) for the final status and recorded validation.

The local directory `.git/cleanup-archives/2026-09-10/hardware-evidence/` preserves the motor calibration notes and raw UART calibration/save transactions, compact simulator and motion analyses, the experimental hardware startup configuration, and the last homing program source. These files remain on this dev box and are not published in the GitHub branch. The trial configuration and program are historical evidence, not validated defaults. They are not loaded by the application from the recovery directory.

Individual notes are contemporaneous snapshots: statements about the arm being unplugged, checks still running, or recordings under `bench/` describe the time those notes were written. The final handoff supersedes those status statements. J4, J5, J6 and the gripper completed motor calibration and acknowledged Save; this does not establish successful assembled-arm homing. J1–J3 were not electrically recalibrated during the session.

The user requested removal of `bench/`, the project `.local/`, and `~/waldo-migration`. Large motion captures and build caches were deleted. `cleanup.json` lists the removed directories and disk space recovered during deletion; it excludes the earlier removal of the native linked worktree.

The two preserved-head manifests map transferred Git worktree heads to durable local archive refs. These archive refs are local recovery material, not additional published branches. The Commander and par6 WIP branches were pushed separately.

Older uncommitted source and other small historical files are preserved locally under `.git/cleanup-archives/2026-09-10/`:

- `par6-held-uncommitted.patch`: the six-file simulator patch from the old transfer checkout.
- `agent-a613cd1ad31ec1360.patch`: staged source changes from an older Commander worktree.
- `recap-uncommitted.patch` and `source-pi-uncommitted-files.tar.gz`: earlier RECAP work and untracked source/research files.
- `orphan-worktree-source.tar.gz`: source from two old worktrees whose original Git metadata was unavailable.
- `diagnostic-source-and-transfer-notes.tar.gz`: small diagnostic scripts and transfer/session notes.

The hardware evidence and the other local recovery files were not published. Automatic approval review rejected publishing raw calibration evidence; the cleanup commit therefore contains only this index, manifests and handoff documentation. Their parent histories are retained through archive refs; they can be inspected without applying anything to the current branch.
