# 0812 — An authority activation records the boot step from the worker

## Status

Accepted (2026-11-04)

## Context

`debug.start_session` and `introspect.script` refuse a Run with no succeeded `boot` run step
(`boot_first`). The fixed-worker `boot` handler writes that step. An external-boot authority
activation is committed by a database function, and that function never touches `run_steps`, so
an authority-booted Run could not attach (#3135).

## Decision

After `queue.complete_external_boot` returns the job `succeeded` for an `activate` result, the
worker writes the `boot` step (`{system_id, boot_outcome: ready}`, the fixed handler's shape).
The write takes the System and Run advisory locks and, inside them, requires that the result's
activation is still the Run's `active` activation; an activation that was released or otherwise left `active` skips the
write with a warning. A failed write is retried three times, then logged, leaving the job
`succeeded`. The attach precondition is unchanged.

## Consequences

- A failed, queued, superseded or `deadline` result records no step.
- A worker crash between the commit and the write leaves the Run without a step, and the job is
  not redelivered. Attach keeps refusing with `boot_first`; releasing the activation and then
  `runs.boot` is the recovery (a forced re-boot is refused while the activation is `active`). No
  reconciler backfill exists.
- No schema, migration or public API change.

## Considered & rejected

- **Write the step inside the commit function.** verified: it needs a new migration to redefine
  the function, which the approved scope excluded; it would be the atomic option and could be
  adopted later.
- **Write it from the activate handler before the commit.** judgment: a refused or superseded
  commit would leave a step for a boot that never happened, opening the attach gate.
- **Relax the attach precondition for authority Systems.** judgment: it is a trust gate; the
  step is the evidence that a boot completed, so the evidence is produced, not waived.
- **Backfill from the reconciler.** judgment: it adds a new repair surface and database authority
  to close a crash window that release then `runs.boot` already recovers.
