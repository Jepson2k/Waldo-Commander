# Physical homing success — 2026-09-10, 23:43 UTC

The user reauthorized physical homing after reconnecting the arm. One complete
physical homing run succeeded in **46.999 seconds**, using Commander MCP
`motion.home(wait=False)`. All six joints and the MSG gripper reported Done;
the runtime reported `homed=true`, no error, and an empty motion queue after Stop.

## Reproduce this configuration

Start `.venv/bin/waldo-commander --dev-mcp-autopilot` in this repository. The
running MCP endpoint is `http://127.0.0.1:8081/mcp` (the persisted GUI setting).
Commander currently resolves `/usr/local/bin/par6d`, which is the installed
0.3.0 runtime at commit `9d737bb61ab0ff7c0a9fe0a8813f7c8fff14578c`, using
`/etc/par6/PAR6.toml` and the MSG small-motor 150 mm rail gripper configuration.
This is distinct from both the experimental `par6/` source checkout and the
packaged Python runtime. No source changes, gain changes, or rebuilds were
needed for this successful run.

The native sequence matches the local vendor `rcb-runtime/config/PAR6_homing.xml`:
J1, then J2/J3, gripper calibration/reference, J2/J3 clearance positioning,
J4/J6, J6 positioning, J5, final ready positioning. Vendor gains were used,
including J1 Kpv 0.015 and J2/J3 seeking current 250 mA.

## Measured outcome

- J1 referenced by 6.04 s; J2/J3 completed by 10.08 s.
- Gripper referencing completed by 25.44 s.
- J2/J3 clearance positioning finished at 32.32 s, without timeout.
- J4/J6 completed by 36.74 s; J5 completed by 43.98 s.
- Final positioning completed at 47.00 s.
- Settled joint angles: approximately [89.988, -106.008, 163.240, -0.005,
  -28.735, 180.029] degrees. This is the vendor sequence's ready pose.
- Zero warnings and zero CAN transmit errors in the captured homing interval.
- Post-stop verification: homed, enabled, idle, zero queued segments, no drive
  faults, no warnings, and no encoder position variation over 100 fresh status
  samples. Gravity compensation was enabled by the completed homing routine.

The CAN interface initially reported ERROR-PASSIVE and the hardware mode switch
logged transient stale/lost nodes. It recovered to ERROR-ACTIVE before the home
command; all nodes supplied feedback and no faults were active at dispatch.
The earlier session's CAN loss and homing failures have **not** been root-caused;
this single success does not prove repeatability from arbitrary starting poses.

Commander MCP `status.get_connected` still reports false despite valid physical
feedback and successful command execution. That UI/status discrepancy remains
unresolved; physical mode, freshness, homed state and drive health above were
verified from the native status stream.

Raw 50 Hz status, the exact installed configuration and binary/config SHA256
manifest are retained locally under
`.git/hardware-recordings/2026-09-10-homing-success/` (owner-only files, not tracked
or published). Commander remains running with the arm homed and holding position.
