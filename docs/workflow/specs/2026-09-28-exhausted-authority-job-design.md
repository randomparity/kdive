# Exhausted authority jobs no longer wedge `running` (#2889)

## Problem

An external-boot authority job whose final attempt dies stays `running` forever: nothing
reclaims or dead-letters an exhausted marked job. A public `systems.teardown` replays the dead
teardown job, and a stray `boot` job the authority refused never ends.

## Scope

Token `q2889-5dc4e3ed`; amends [ADR-0620](../../adr/0620-authority-owned-system-teardown.md).

- **Teardown recycle.** New `JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED`: enqueue's one
  `UPDATE` resets a `failed` row, or a `running` row with `attempt >= max_attempts` and
  `lease_expires_at < clock_timestamp()`. `_enqueue_authority_teardown` lets a `failed` or
  exhausted `running` prior reach the marker check and uses this policy. The lease is judged
  only in that `UPDATE`, so a live final attempt returns unchanged. `succeeded` is never reset:
  the server's System lock is not the key the commit function takes, so an old attempt can commit
  between the read and the `UPDATE`. `_RECYCLED` maps the policy to `{failed}`, its
  unconditional set.
- **Stray job terminalization.** Migration 0162 adds `dead_letter_unowned_external_boot_jobs()`
  (security definer, executable by `kdive_reconciler` only, which cannot read authority rows).
  For each `external_boot_authority_v1` job with `kind <> 'teardown'`, `running`, exhausted and
  lease-lapsed on the database clock, it locks the row, then in a later statement requires no
  authority row for the job in `allocating` or `current`, and sets `failed`/`lease_expired`.
  Every receipt path (commit, derived release, teardown finalize, exhausted-retry proof) needs
  such a row and a `running` job, and allocation locks the job row first, so no receipt can
  commit after it. `repair_abandoned_jobs` calls it. No Run compensation: an activate job's Run
  is already `succeeded`.
- **Rejected:** option 3 (reconciler terminalizes any lapsed marked job, splitting receipt
  ownership); option 2 (cancel, then recycle `canceled`); recycling `succeeded`.

### Failure model

1. **Actors and deployments** — admin, workers, reconciler, Postgres; libvirt authorities.
2. **Invariants and assets at stake** — reservation credited exactly once; host mutation only
   under the current authority generation; a live final attempt is never reset.
3. **Accepted failure classes** — a hung worker whose final lease lapsed loses its attempt; the
   recycled job's allocation supersedes its authority (ADR-0620). A stray job whose authority is
   still `allocating` waits until a newer allocation supersedes it. `authority_system_v1` jobs
   are untouched; their own repair owns them.
4. **Covered elsewhere** — `observe_system_teardown` `provider_conflict` on an older intent
   (follow-up); activate-versus-teardown policy (#2887 ADR follow-up).

## Success

- A public teardown recycles a lapsed exhausted authority teardown with an identical marker; a
  live final attempt, a non-exhausted one, `succeeded` and `canceled` replay.
- The recycled old attempt's heartbeat and finalize are refused; credit happens once, and a
  teardown after success replays.
- A stray marked non-teardown job is dead-lettered only when exhausted, lapsed and without an
  `allocating` or `current` authority.

## Validation

- Recycle policy and tool — focused-test: `tests/jobs/test_queue.py`,
  `tests/mcp/lifecycle/test_systems_tools.py`.
- Old attempt fence, credit, 0162, grant — focused-test: `tests/db/test_exhausted_authority_job.py`.
- Live settle — task-test-not-applicable: needs the retained lab fixture; runs after review.
