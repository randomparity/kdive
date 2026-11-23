# 0841 — Teardown warning history belongs to the reconciler

## Status

Accepted (2026-11-23)

## Context

The stranded-orphan and stalled-teardown repairs keep two warning maps at module scope.
The stranded map remembers a failed job's `updated_at`; the stalled map remembers a skip cause.
Each repair replaces or prunes its map against the current query result. A real database proof
with two reconcilers sharing a pool and configuration produced warning counts `[1, 0, 0]`
instead of `[1, 1, 0]`: the second reconciler inherited the first one's suppression.
An empty pass against another database can also erase a runner's warning history.

[ADR-0839](0839-external-boot-repair-keyset-progress.md) already gives each reconciler its
own cursor map and passes it through the repair catalog's fourth factory argument.
Configuration describes dependencies; sharing it must not share observation history.

## Decision

Add `TeardownWarningHistory` in the System repair module with separate stranded and stalled
maps. Each `Reconciler` constructs its own history. `reconcile_once` and the two repair functions
accept explicit history for repeat standalone use; omission creates one-shot history.
Remove both global maps and the test fixtures that clear them.

Preserve the existing cursor argument and owner. A private frozen `_RepairState` groups the
cursor and warning-history references passed as the existing fourth catalog factory argument.
The external-boot factory uses its cursor reference; the two warning factories capture its
history. All other factories ignore that argument. No fifth argument or name-based dispatch.

Keep the current warning keys, pruning, messages, counts and transaction ordering. SQL,
selection, locking, authority checks, job admission and the rest of the catalog stay unchanged.
Warning history is observational, never a cleanup authority or a durable retry allowance.

## Consequences

Independent runners can each report a stop once. Restarts and one-shot calls can warn again.
History remains bounded by each repair's existing current-result pruning, independently per
owner and warning family. Normal runner passes remain serial; this adds no overlapping-pass
synchronization or persistent suppression guarantee.

## Considered & rejected

- **Keep globals keyed by pool.** judgment: pool identity does not identify a runner, and a
  process-wide map requires another lifetime owner and cleanup policy.
- **Put history in configuration.** verified: the reproduction deliberately shares configuration.
- **Add another factory parameter.** judgment: the existing runtime-state seam can carry both
  references without increasing callback arity or adding a second execution path.
