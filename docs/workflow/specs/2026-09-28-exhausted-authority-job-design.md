# Exhausted authority jobs no longer wedge `running` (#2889)

## Problem

Nothing reclaims or dead-letters an exhausted authority-marked job, so a dead teardown job is
replayed by every public `systems.teardown` and a refused stray `boot` job never ends.

## Scope

Token `q2889-5dc4e3ed`; amends [ADR-0620](../../adr/0620-authority-owned-system-teardown.md).

- **Teardown recycle.** New `JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED`: one `UPDATE` resets a
  `failed` row to attempt 0, or requeues a `running` row with `attempt >= max_attempts` and
  `lease_expires_at < clock_timestamp()`, keeping its attempt and adding `max_attempts` to its
  budget so no `(worker, attempt)` pair of the dead attempt recurs. `_enqueue_authority_teardown`
  lets a `failed` or exhausted `running` prior reach the marker check; a `running` prior whose
  payload cannot be rebuilt or whose marker differs replays. Only the `UPDATE` judges the lease,
  so a live final attempt returns unchanged. `succeeded` is never reset (the server's System
  lock is not the commit function's key). `dedup_replay` refuses this clock-dependent policy;
  `_RECYCLED` lists only policies it can answer.
- **Stray job terminalization.** Migration 0162 adds `dead_letter_unowned_external_boot_jobs()`
  (security definer; only `kdive_reconciler`, which cannot read authority rows, may execute it).
  For each `external_boot_authority_v1` `boot` job (public teardown owns `teardown` ones),
  `running`, exhausted and lease-lapsed on the database clock, it locks the row, then in
  a later statement requires no authority row for the job in `allocating` or `current`, and sets
  `failed`/`lease_expired`. Every receipt path needs such a row and a `running` job, and
  allocation rechecks the job under its own row lock, so no receipt can commit after it.
  `repair_abandoned_jobs` calls it; the marker's `created`/`running` Run fails with the job.
- **Rejected:** option 3 (splits receipt ownership), option 2 (cancel, recycle `canceled`).

### Failure model

1. **Actors and deployments** — admin, workers, reconciler, Postgres; libvirt authorities.
2. **Invariants and assets at stake** — reservation credited exactly once; host mutation only
   under the current authority generation; a live final attempt is never reset.
3. **Accepted failure classes** — a hung old attempt keeps its authority until the recycled job
   allocates (ADR-0620 fence). A stray whose authority is `allocating` or `current` (commit has
   no lease check) waits for a newer allocation to supersede it. `authority_system_v1` jobs are
   untouched; their own repair owns them.
4. **Covered elsewhere** — follow-ups: `observe_system_teardown` `provider_conflict` on an older
   intent; a `queued` exhausted teardown left by `retained_quarantine`; exhausted repair-lane
   teardown jobs. Activate-versus-teardown policy: #2887 ADR follow-up.

## Success

- A public teardown recycles a lapsed exhausted authority teardown with an identical marker; a
  live final attempt, a non-exhausted one, `succeeded` and `canceled` replay.
- The dead attempt's heartbeat and finalize are refused; credit happens once, and a teardown
  after success replays.
- A stray marked non-teardown job is dead-lettered only when exhausted, lapsed and without an
  `allocating` or `current` authority.
- Live fixture: System `torn_down`, domain absent, one release row, stray boot job `failed`.

## Validation

- Recycle policy and tool — focused-test: `tests/jobs/test_queue.py`,
  `tests/mcp/lifecycle/test_systems_tools.py`.
- Dead attempt, credit, 0162, grant, reconciler call — focused-test:
  `tests/db/test_exhausted_authority_job.py`.
- Live settle — task-test-not-applicable: retained lab fixture, supported calls only, post-review.
