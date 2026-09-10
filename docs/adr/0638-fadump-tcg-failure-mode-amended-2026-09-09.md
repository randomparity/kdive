# 0638 — ADR-0349's TCG failure-mode claim is amended by the 2026-09-09 observation

## Status

Accepted (2026-09-09)

- **Issue:** #2398
- **Amends:** [0349](0349-ppc64le-fadump-opt-in.md) (does not supersede
  it — the QEMU 10.2 floor and the native-POWER requirement both still hold)

## Context

[ADR-0349](0349-ppc64le-fadump-opt-in.md)'s live-proof outcome paragraph
(2026-07-14) attributes the TCG capture failure to one named mechanism:

> The fadump guest then hit a recurring `rtas_event_scan` RTAS `Oops` (dispatch into the
> fadump-reserved region) under TCG and never reached readiness ... Because the crash→capture path
> rides the same Oopsing RTAS, no boot-window tuning recovers it under emulation.

`docs/design/2026-09-09-ppc64le-emulated-power-live-proof-2383-proof-record.md` (#2383) re-ran the
fadump driver on the current stack — QEMU 10.2.2, the same version that satisfies ADR-0349's
floor — and observed a different failure. fadump registered cleanly at 12 s of guest time:

```
[    0.000000] fadump: Reserved 512MB of memory at 0x00000020000000 (System RAM: 4096MB)
[   11.947384] rtas fadump: Registration is successful!
```

19 minutes later, systemd PID 1 froze during early boot, well before the crash→capture phases:

```
[ 1137.236766] systemd[1]: Failed to fork off sandboxing environment for executing generators:
                           Protocol error
[!!!!!!] Failed to start up manager.
[ 1143.046156] systemd[1]: Freezing execution.
```

No `rtas_event_scan` Oops is recorded in this run. The proof record excludes memory exhaustion as
the cause (two boots froze at the identical line at both 2048 MiB/`crashkernel=256M` and 4096
MiB/`crashkernel=512M`), and the mechanism behind the `EPROTO` is undiagnosed. Because the driver
never reached the `ppc64le-fadump:attribute`, `crash`, or `capture` phases, no fadump verdict —
positive or negative — could be read from this run; it failed as a test with `drain_timeout`
after 2 h 51 m (`tests.integration.live_stack.spine.SpinePhaseError: phase
'ppc64le-fadump:boot' failed: drain_timeout`).

A second, load-bearing fact this record does not change: the test that ran was gated on
`platform.machine() != "ppc64le"`, so an emulated ppc64le host with no `/dev/kvm` passed the
guard and paid the full 2 h 51 m before failing. #2398 (companion code change, same PR) regates
the driver on the resolved accelerator (`expected_accel("ppc64le") != "kvm"`, ADR-0352) so this
case skips instead of burning three hours; that fix is orthogonal to which RTAS mechanism is at
fault and is recorded here only for cross-reference.

Per this repository's ADR convention, an accepted record is never edited in place — a changed
observation is recorded as a new, amending record.

## Decision

ADR-0349's live-proof outcome paragraph is amended, not replaced: its two confirmed facts —
fadump *registers* under QEMU 10.2 TCG (`rtas fadump: Registration is successful!`), and the
verdict that fadump end-to-end capture requires native-POWER (KVM) validation — stand unchanged.
Its stated failure *mechanism* (`rtas_event_scan` Oops) is no longer the only mechanism observed
blocking readiness under TCG: the 2026-09-09 run blocked earlier, at a systemd generator-sandbox
`EPROTO` freeze, with no Oops recorded. A reader relying on ADR-0349's outcome paragraph to say
*why* a TCG fadump attempt fails should cross-reference this record: the current stack's failure
mode is an unresolved systemd freeze that pre-empts the RTAS path entirely, not (or not only) the
Oops ADR-0349 documented.

The undiagnosed `EPROTO` mechanism is out of this record's scope. The proof record names the
first diagnostic step (`systemd.log_level=debug` on an otherwise identical boot); whether that
investigation is worth running is a separate decision, owned by a future issue if the operator
opens one.

## Consequences

- ADR-0349's QEMU **10.2** floor and its native-POWER-required verdict remain authoritative and
  unchanged by this record.
- ADR-0349's outcome paragraph no longer stands as a complete account of TCG fadump failure: a
  reader needs both records — ADR-0349 for the 2026-07-14 Oops observation and this record for the
  2026-09-09 EPROTO observation — to know that the failure mode under TCG is not settled to one
  mechanism.
- fadump-under-TCG remains unproven positively or negatively by either run; #1204's crash-to-capture
  proof is still owed and still deferred to native-POWER hardware (#2383).
- No code, schema, or behavior changes as a consequence of this record alone; the accelerator-gate
  fix that stops the next run from paying 2 h 51 m to rediscover this is tracked as ordinary code
  in the same change, not as a decision this record makes.

## Considered & rejected

- **Edit ADR-0349 in place to replace the Oops mechanism with the EPROTO observation.** Rejected:
  the repository convention never edits an accepted record in place, and doing so would also
  overwrite the 2026-07-14 Oops observation, which remains true for that run and may recur — the
  two are not known to be mutually exclusive.
- **Mark ADR-0349 Superseded by this record.** Rejected: nothing ADR-0349 decided is reversed. Its
  floor, its capture-method modeling, and its native-POWER verdict all still hold; only the
  *why-TCG-fails* explanation gained a second, unreconciled data point. Superseding would overstate
  this record's scope.
- **Fold this observation into the #2383 proof-record doc only, with no ADR.** Rejected: #2398's
  acceptance criteria require ADR-0349 to be amended by a new record, and a proof-record doc is
  historical evidence (per its own banner), not a decision surface a future reader checking
  ADR-0349 would think to consult.
