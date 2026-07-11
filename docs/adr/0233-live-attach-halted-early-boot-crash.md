# ADR 0233 — Live attach to a halted early-boot crash (#747)

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Supersedes (narrowly):** [ADR-0064](0064-expected-boot-failures-artifact-search.md) — only
  its assertion that a crashed boot "is not a live-debuggable guest." That remains true for the
  *declared expected-crash* (A/B) flow; it no longer holds for a System provisioned with
  `gdbstub` whose boot leaves a reachable stub.
- **Builds on (does not supersede):** [ADR-0049](0049-crash-capture-tiers.md) (capture-method
  vocabulary + the `preserve_on_crash`/`gdbstub` debug flags),
  [ADR-0032](0032-connect-plane-gdbstub-debugsession.md) (the gdbstub transport + RSP probe),
  [ADR-0210](0210-local-libvirt-live-debug-introspection.md) (the `<qemu:commandline> -gdb`
  passthrough + bind-probe), [ADR-0185](0185-retry-terminal-failed-step.md) (the `run_steps`
  running/succeeded ledger that deletes a failed step row).
- **Issue:** [#747](https://github.com/randomparity/kdive/issues/747) (part of #746).
- **Spec:** [`../superpowers/specs/2026-06-23-live-attach-halted-early-boot-crash-design.md`](../archive/superpowers/specs/2026-06-23-live-attach-halted-early-boot-crash-design.md).

## Context

`debug.start_session(run, "gdbstub")` denies a Run whose boot ended in an early-boot panic, the
one class where a live gdb session is most valuable and where the other capture tiers are
weakest: kdump cannot capture a panic that precedes its kexec capture kernel, and host_dump
yields only a post-mortem core. The QEMU `-gdb` stub, by contrast, is reachable at the earliest
boot instant (a host-side facility, nothing in-guest) — a focused spike confirmed gdb attaches
to the halted vCPU inside the panic path with full symbol resolution.

Two gaps block this today. First, an unexpected early-boot panic raises `READINESS_FAILURE`, the
boot handler deletes the `boot` `run_steps` row (`abandon_run_step_best_effort`), and the Run
goes `FAILED`; `_attach_preconditions` keys on a *succeeded* boot step and returns a misleading
`boot_first` rejection for a VM that is actually halted with a live stub. Second,
`preserve_on_crash` is a phantom flag: its docstring claims it adds a pvpanic device +
`<on_crash>preserve</on_crash>`, but `render_domain_xml` emits neither, so the deterministic
halt the scenario needs does not exist. A separate, intentional gate routes a *declared*
expected crash (`expected_crash_observed`) to post-mortem; that A/B kernel-testing flow
(ADR-0064) is correct and stays.

## Decision

When a Run's boot ends in an early-boot panic that leaves a **reachable gdbstub**, the boot
worker records a **succeeded** `boot` step with `boot_outcome = "crashed_halted_live"` (Run →
`SUCCEEDED`), and `debug.start_session(…, "gdbstub")` admits it.

1. **Render `preserve_on_crash` for real.** `render_domain_xml` emits a `pvpanic` device +
   `<on_crash>preserve</on_crash>` when the flag is set (independent of the gdbstub/SSH
   passthroughs). This makes the panic-halt deterministic.
2. **Probe stub reachability at boot.** On `READINESS_FAILURE` the worker reuses the existing
   bounded `rsp_reachable` probe (`providers/shared/debug_common/rsp.py`, ADR-0032/0083) — one
   read-only RSP `?` exchange over a loopback socket. It is **reachability-only**: connecting an
   RSP client to a QEMU `-gdb` stub halts the vCPU and may resume it on disconnect, so the probe
   is not treated as a passive crash signal. `open_transport` re-probes authoritatively at attach.
3. **Record the outcome, gated on panic evidence + a reachable stub.** Record
   `boot_outcome = "crashed_halted_live"` (succeeded `boot` step, redacted console evidence, an
   `available_capture` list) only when `gdbstub` is provisioned, the captured console matches a
   generic kernel-panic signature, **and** `rsp_reachable` succeeds. The **console-panic
   signature is the crash signal** (distinguishing a real panic from a slow-but-healthy boot that
   the probe's halt-on-connect could otherwise freeze); the gate is **not** `preserve_on_crash`
   and **not** `capture_method`, so live-gdb is a fallback that works alongside a kdump- or
   host_dump-primary System. Otherwise the existing abandon → `FAILED` path is unchanged.
4. **Admit at the gate.** `_attach_preconditions` admits `crashed_halted_live` for the
   `gdbstub` transport and rejects it for `drgn-live` (a halted guest has no sshd). The Run is
   already `SUCCEEDED`, the boot step `succeeded`, and the System remains `READY` (the boot
   handler never transitions it), so the existing checks pass.
5. **Surface options, not a knob.** The crashed-boot result and `runs.get` enumerate the
   physically-available follow-ups (`available_capture`); no on-panic action parameter is added.

`boot_outcome` is a schemaless value in `run_steps.result`, so there is **no migration** and no
state-machine change.

## Consequences

- A Run provisioned with `gdbstub` that ends in an early-boot panic can start a live gdbstub
  session against the halted stub — the issue's primary acceptance — including when kdump or
  host_dump was the primary method but could not capture the early panic.
- `preserve_on_crash` stops being a phantom flag: the pvpanic + `<on_crash>preserve</on_crash>`
  instrumentation it always claimed is now actually rendered, making the host_dump tier and the
  new live-attach tier deterministic.
- A new terminal boot outcome (`crashed_halted_live`) joins `ready` and `expected_crash_observed`
  on the `boot` step; `runs.get` (which already reads `boot_outcome`) reports it, and the
  `available_capture` field rides in the free-form `run_steps.result`/`data` (no committed
  snapshot invalidated).
- Files touched: `providers/local_libvirt/lifecycle/xml.py`, `jobs/handlers/runs_boot.py`, a
  bounded RSP probe helper, and `mcp/tools/debug/sessions_lifecycle.py`, plus unit tests at each
  boundary and a `live_vm` proof. No port, schema, migration, dependency, or auth-model change.
- Rollback is removing the four edits; no persisted state requires reversal (the new outcome
  string simply stops being written).

## Considered & rejected

- **A new run state (e.g. `CRASHED_DEBUGGABLE`).** Rejected as premature and heavy: it needs new
  state-machine edges, a migration, and handling in every tool that switches on run state.
  Modeling the halted-debuggable boot as a succeeded step with a distinct `boot_outcome` reuses
  the existing `expected_crash_observed` precedent and touches no schema.
- **Keep the Run `FAILED` and admit a FAILED-but-halted run.** Rejected: it fights the gate's
  `run_state == SUCCEEDED` check, needs extra branches, and overloads `FAILED` (some attachable,
  some not). The succeeded-step model keeps the precondition's existing checks intact.
- **A new agent-facing "on panic, do X" action knob.** Rejected as a second declaration surface
  overlapping the ADR-0049 provisioning flags (the two-config-formats anti-pattern). The
  provisioned instruments are the intent; the available paths are surfaced as next-actions.
- **Gate the recording on `preserve_on_crash` (issue-literal) or on `capture_method == GDBSTUB`.**
  Rejected: gating on the live stub instead lets live-gdb rescue a kdump- or host_dump-primary
  System whose early-boot panic the primary method could not capture, which is the more useful
  contract. `preserve_on_crash` is still rendered (for determinism) but is not required.
- **Reverse the declared expected-crash (A/B) flow too.** Rejected: `expected_crash_observed`
  is a deliberate ADR-0064 workflow that keeps the System reusable and routes to post-mortem
  evidence; an operator running A/B kernel tests wants that, not a live session. Only the
  undeclared/unmatched crash path records `crashed_halted_live`.
- **Admit `drgn-live` to a halted crash.** Rejected: the drgn-live transport reaches the guest
  over SSH (ADR-0218); a panicked or halted guest has no running sshd, so the attach cannot
  succeed. Only the host-side gdbstub transport is admitted.
- **Treat the RSP probe as the crash signal** ("stub reachable ⇒ crashed-and-halted"). Rejected:
  connecting an RSP/gdb client to a QEMU `-gdb` stub stops the vCPU and may resume it on
  disconnect, so a reachability probe is not passive — it could freeze a slow-but-healthy boot
  that merely tripped the readiness timeout and mislabel it a crash. The crash signal is the
  console kernel-panic signature; the probe only confirms reachability.
- **Attach-before-boot / halt-at-start** (start the VM paused so breakpoints precede initcalls).
  Rejected from this change as a distinct capability (a provisioning/boot-sequencing feature),
  deferred to future work.
