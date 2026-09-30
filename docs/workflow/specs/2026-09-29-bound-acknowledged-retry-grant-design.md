# Bound the acknowledged-retry grant per budget — design

Issue: #2960 (parent #2951). Scope token: q2960-b487d068. Decision:
[ADR-0711](../../adr/0711-bound-acknowledged-retry-grant-per-budget.md), which amends ADR-0626.

## Problem

ADR-0711's Context gives the full story. Migration 0150 grants one more claim for each new
acknowledged no-mutation head. A job that the authority refuses on every attempt before provider
admission therefore re-claims at every lease lapse. Migration 0162 cannot dead-letter it,
because its latest authority is still live.

## Scope

1. **Bound (criteria C1-C3).** Migration `0165_bound_acknowledged_retry_grant.sql` renames the
   0151 predicate to `has_acknowledged_external_boot_no_mutation_head(jobs)`. It then creates a
   new `has_acknowledged_external_boot_retry_proof(jobs)`. The new predicate is true only when
   both of these hold:
   - no consumption row has `job_id = job.id`, `claimed_attempt = job.attempt`, and
     `consumed_at >= job.created_at`;
   - the renamed evidence predicate is true.
   `claim_worker_job`, `count_claimable_worker_jobs`, and
   `consume_acknowledged_external_boot_retry_proof` call the predicate by name at run time, so
   they apply the bound with no text change. No runtime role can execute either function.
2. **Terminal state (C1).** The same migration runs `CREATE OR REPLACE` on
   `dead_letter_unowned_external_boot_jobs()` and keeps the 0162 body. The function still skips a
   job with an `allocating` or `current` authority, unless all of these hold:
   - a current-budget consumption row, as in item 1, claimed the job's current attempt;
   - after the job-row lock, it takes
     `pg_advisory_xact_lock(hashtextextended('kdive:system:' || <marker system_id>, 2126))`;
   - after that lock, `has_acknowledged_external_boot_no_mutation_head(job)` is true;
   - `FOR UPDATE NOWAIT` on the job's live authority rows succeeds. On `lock_not_available`,
     the function skips the job until the next pass.
   When all of these hold, it retires a `current` row (`retired_at = clock_timestamp()`),
   supersedes an `allocating` row (`superseded_at`), and fails the job and Run as 0162 does. In
   `reconciler/repairs/jobs.py`, only the `repair_abandoned_jobs` docstring and one log line
   change to name this case.
3. **ADR (C4).** ADR-0711 records the bound, the terminal path, and the limit that #2901 must
   use. The `## Status` section of ADR-0626 gets one `Amended by` banner.
4. **Registration (C5).** Add 0165 to the pinned migration lists in `tests/db/test_migrate.py`,
   `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
   `tests/db/test_migration_0102_build_gc_cursors.py`, and
   `tests/db/test_migration_0115_capture_reap_state.py`, in each place where 0164 is listed.

Not in scope: the #2901 fix, plan-less teardown (#2961), activation reuse after release (#2968),
and carrier fixture cleanup (#2965).

## Failure model

1. **Actors and deployments**
   - the worker (`kdive_worker`): claims jobs, counts queue depth, and commits receipts;
   - the reconciler (`kdive_reconciler`): runs `repair_abandoned_jobs`;
   - the authority service (`kdive_provider_authority`): advances journal heads;
   - deployment: a server stack migrated through 0165. This includes a stack that has a looping
     job.
2. **Invariants and assets at stake**
   - after a job is dead-lettered, no provider mutation can be admitted or committed for it;
   - a job is never failed while its head shows an admitted or started mutation;
   - claim and queue depth count the same set of jobs;
   - the activation keeps a `current` or `retired` dispatch route, if it had one before;
   - ADR-0626 still gives one replacement per budget for a crash at acknowledgement.
3. **Accepted failure classes**
   - a granted attempt that lapses before its own acknowledged head stays `running`. This covers
     a granted attempt with no allocation, with an allocation only, or with a watermark only. It
     is the existing ADR-0620 case of an exhausted job whose authority is still live. It costs
     one stuck job and cannot loop.
   - a deadlock between the reconciler and a same-Run worker call over the job and Run rows. The
     job-row-then-Run-row order already exists in 0162. Postgres aborts one side, the
     reconciler's pass rolls back, and the next pass retries. The new authority-row step uses
     `NOWAIT`, so it adds no wait.
   - a `teardown` job past the bound stays `running` until a public teardown recycles it
     (ADR-0620). The recycle is an operator action, so the number of grants stays finite.
4. **Covered elsewhere**
   - a release path for a `preparing` activation that has no plan: #2961;
   - the `journal-conflict` trigger itself: #2968 and #2952;
   - generation churn within an ordinary budget: #2901, under the limit that ADR-0711 sets.

## Success

- S1: take a job that a grant claimed, with a new acknowledged head at its lapsed attempt. For
  that job, `count_claimable_worker_jobs = 0`, and `claim_worker_job` returns no row.
- S2: one `dead_letter_unowned_external_boot_jobs` call makes that job
  `failed`/`lease_expired`. Its `current` authority becomes `retired`, or its `allocating`
  authority becomes `superseded`. A later journal-head advance returns `superseded`.
- S3: an exhausted ordinary attempt with one acknowledged head still claims once (`3/3` to
  `4/4`).
- S4: the dead-letter function still skips a job that has an unspent grant, and a granted job
  that has no new acknowledged head.
- S5: no runtime role can execute either predicate.
- S6: a job recycled after a grant gets one grant again at the last attempt of its new budget.

## Validation

All new tests are in `tests/db/test_external_boot_authority_journal_migration.py`.

| Contract | Mode | Evidence |
|---|---|---|
| bound (S1) | focused-test | `test_granted_attempt_cannot_earn_a_second_grant`; red before 0165 |
| terminal (S2) | focused-test | `test_reconciler_dead_letters_a_job_past_the_grant_bound` |
| single grant kept (S3) | focused-test | existing `test_exhausted_acknowledged_authority_job_gets_one_recovery_claim` |
| skip kept (S4) | focused-test | `test_dead_letter_skips_a_job_without_a_spent_grant_and_proof`, `tests/db/test_exhausted_authority_job.py` |
| privileges (S5) | focused-test | `test_acknowledged_retry_proof_helper_is_private` gains the renamed function |
| per budget (S6) | focused-test | `test_recycled_job_earns_a_grant_in_its_new_budget`; red when keyed on attempt only |
| registration | focused-test | `tests/db/test_migrate.py` and the 0091/0102/0115 lists |
| ADR, docstring | task-test-not-applicable | prose only; `just records` checks the ADR shape |
