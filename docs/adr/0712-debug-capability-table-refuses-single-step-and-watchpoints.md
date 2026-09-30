# ADR-0712: a per-arch and accelerator table refuses single-step and watchpoints before resume (#2963)

- Status: Accepted
- Date: 2026-09-29
- Amends: [ADR-0379](0379-gdb-source-and-instruction-stepping.md),
  [ADR-0277](0277-gdb-watchpoints.md). Cites: [ADR-0463](0463-consolidate-debug-step-variants.md).

## Context

ADR-0379 added source and instruction stepping, and ADR-0463 folded the step verbs into
`debug.advance` modes `into`, `over`, `instruction` and `out`. ADR-0277 added hardware write
watchpoints. Both assume the gdbstub can single-step the vCPU and insert a hardware watchpoint.

The native POWER9 proof ([#2739 record](../design/2026-09-29-ppc64le-gdbstub-proof-record-2739.md),
sections 3-5) shows that the pseries gdbstub under KVM can do neither. `stepi`, `nexti`, `step`
and `next` resume the vCPU and it does not stop again. The engine then reports `transport_stall`
after about 21 s, or the MCP path returns `timed_out: True`. `-break-watch` succeeds on the
halted guest, and the next resume fails with `Could not insert hardware watchpoint 2.` `finish`,
software breakpoints and `advance` work.

## Decision

1. A static table in `kdive.providers.shared.debug_common.gdbmi.policy.capabilities` names the
   debug capabilities a target lacks, keyed on `(arch, accel)`. The one row is
   `("ppc64le", "kvm") -> {single_step, hw_watchpoint}`. A key that is not in the table, or
   that has an unknown arch or accelerator, lacks nothing.
2. The key comes from the session's System: `provisioning_profile["arch"]` and the persisted
   `accel` (ADR-0339). The Debug-plane op runner reads it when an op declares a required
   capability. It makes the decision after the live-session gate and before it resolves the
   engine runtime, attaches, or sends any gdb/MI command.
3. `debug.advance` modes `into`, `over` and `instruction` require `single_step`. Mode `out`
   requires nothing. `debug.set_watchpoint` requires `hw_watchpoint`.
4. A refusal is a failure envelope with category `debug_attach_failure`, the category of the
   existing `watchpoint_unsupported` code, and `data` of `code`, `arch` and `accel` (plus
   `mode` for `debug.advance`):
   - `single_step_unsupported`: the detail names `debug.advance` mode `out` and a breakpoint
     with `debug.continue`; the next actions are `debug.advance`, `debug.set_breakpoint` and
     `debug.continue`.
   - `watchpoint_unsupported`: the detail names a breakpoint with `debug.continue`; the next
     actions are `debug.set_breakpoint` and `debug.continue`.
   A refused op writes no audit row, as for every other failed op.

## Consequences

- On a ppc64le KVM System the guest stays halted and no watchpoint is armed. The refusal
  returns without a gdb round trip.
- x86_64 has no row, so its behavior does not change. A ppc64le TCG System has no row either:
  no proof covers stepping or watchpoints there, so it keeps the pre-change behavior.
- `kvm` does not tell KVM-HV from KVM-PR. The row refuses both; only KVM-HV is proven.
- A System with a NULL `accel` keeps the pre-change behavior. The resume-time classification
  of a failed step or watchpoint insert is #2964's work.
- A new row needs native proof evidence, the same as the #2739 record.

## Considered & rejected

- **Probe the stub at attach.** verified: the only observable probe is a step. On the #2739
  host it left the vCPU running, and the next command printed `Cannot execute this command
  while the target is running.` (proof record section 3, gdb 17.1, QEMU 10.2.1).
- **Reclassify the failure after the resume only.** verified: the engine reports
  `transport_stall` after 21.5 s with the vCPU still running (proof record section 4). This
  change must leave the guest halted, and #2964 owns the reclassification.
- **Infer the accelerator in the engine (native arch means KVM).** judgment: a native guest
  without `/dev/kvm` runs under TCG, and the System's persisted `accel` is already the authority.
- **Do nothing.** verified: the #2740 proofs skip the ppc64le single-step modes because the
  guest does not stop (`tests/mcp/debug/test_debug_gdbmi_live_smoke.py` at `e914b66f4`).
