# Bound the acknowledged-retry grant per budget — design

Issue: #2960 (parent #2951). Scope token: q2960-b487d068. Decision:
[ADR-0711](../../adr/0711-bound-acknowledged-retry-grant-per-budget.md), which amends ADR-0626.

## Problem

Migration 0150 (ADR-0626) gives an exhausted external-boot job one more claim for each exact,
unconsumed acknowledged no-mutation journal head. The consumption table is unique per proof and
per `(job_id, claimed_attempt)`, and the consume function returns `max_attempts + 1`. When the
authority refuses every attempt before provider admission, each attempt makes a new head, so
each lease lapse grants one more claim. The #2865 job went `3/3` to `6/6` and never ended.
Migration 0162 cannot dead-letter the job because its latest authority is `allocating` or
`current`.

## Scope

1. **Bound (criteria C1-C3).** Migration `0165_bound_acknowledged_retry_grant.sql` renames the
   0151 predicate to `has_acknowledged_external_boot_no_mutation_head(jobs)` and creates a new
   `has_acknowledged_external_boot_retry_proof(jobs)`. The new predicate is true only when no
   consumption row has `job_id = job.id` and `claimed_attempt = job.attempt`, and the renamed
   evidence predicate is true. `claim_worker_job`, `count_claimable_worker_jobs`, and
   `consume_acknowledged_external_boot_retry_proof` call the predicate by name at run time, so
   they get the bound without text changes. Both functions keep the 0150 privilege shape: no
   runtime role can execute them.
2. **Terminal state (C1).** The same migration does `CREATE OR REPLACE` on
   `dead_letter_unowned_external_boot_jobs()`. It keeps the 0162 body. A job that has an
   `allocating` or `current` authority is still skipped, unless all of these hold:
   - a consumption row claimed the job's current attempt;
   - after the job-row lock, the function takes
     `pg_advisory_xact_lock(hashtextextended('kdive:system:' || <marker system_id>, 2126))`;
   - after that lock, `has_acknowledged_external_boot_no_mutation_head(job)` is true.
   Then it sets that job's `allocating`/`current` authority rows to `superseded`
   (`superseded_at = clock_timestamp()`), and it fails the job and Run as 0162 does. The Python
   caller (`reconciler/repairs/jobs.py`) does not change.
3. **ADR (C4).** ADR-0711 records the bound, the terminal path, and the limit #2901 must use. The
   ADR-0626 `## Status` section gets one `Amended by` banner.
4. **Registration (C5).** Add 0165 to the pinned migration lists in `tests/db/test_migrate.py`,
   `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
   `tests/db/test_migration_0102_build_gc_cursors.py`, and
   `tests/db/test_migration_0115_capture_reap_state.py`, wherever each lists 0164.

Not in scope: the #2901 fix, plan-less teardown (#2961), activation reuse after release (#2968),
and carrier fixture cleanup (#2965).

## Failure model

1. **Actors and deployments**
   - the worker (`kdive_worker`) claims jobs and counts queue depth;
   - the reconciler (`kdive_reconciler`) runs `repair_abandoned_jobs`;
   - the authority service (`kdive_provider_authority`) advances journal heads;
   - deployment: a server stack migrated through 0165, including one that has a looping job.
2. **Invariants and assets at stake**
   - no provider mutation can be admitted or committed for a job after it is dead-lettered;
   - a job is never failed while its head shows an admitted or started mutation;
   - claim and queue depth agree on the jobs they count;
   - ADR-0626's one replacement for a crash at acknowledgement still works.
3. **Accepted failure classes**
   - a granted attempt that crashes before it allocates an authority stays `running`: the head
     does not bind the current attempt, so the proof is false. This is the pre-existing ADR-0620
     "waits for a newer allocation" case. Its cost is one stuck job, and it cannot loop.
   - a deadlock between the reconciler (job row, then Run row) and an allocation for the same job
     (Run row, then job row). This lock order already exists in 0162. The allocation needs a live
     lease that this job no longer has. Postgres aborts one side, and the next pass retries.
   - a `teardown` job past the bound stays `running` until a public teardown recycles it
     (ADR-0620). The recycle is an operator action, so the grants stay finite.
4. **Covered elsewhere**
   - the System release path for a `preparing` activation with no plan: #2961;
   - the `journal-conflict` trigger itself: #2968 and #2952;
   - generation churn within an ordinary budget: #2901, under the ADR-0711 limit.

## Success

- S1: a job claimed through a grant, with a new acknowledged head at its lapsed attempt, gives
  `count_claimable_worker_jobs = 0` and no row from `claim_worker_job`.
- S2: that job becomes `failed`/`lease_expired` in one `dead_letter_unowned_external_boot_jobs`
  call, and its authority becomes `superseded`.
- S3: an exhausted ordinary attempt with one acknowledged head still claims once (`3/3` to `4/4`).
- S4: a job with an `allocating`/`current` authority and no spent grant is still skipped by the
  dead-letter function.
- S5: neither predicate is executable by any runtime role.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| bound (S1) | focused-test | new `test_granted_attempt_cannot_earn_a_second_grant` in `tests/db/test_external_boot_authority_journal_migration.py`; red before 0165 (count 1) |
| terminal (S2) | focused-test | new `test_reconciler_dead_letters_a_job_past_the_grant_bound` in the same file: job `failed`, authorities `superseded`, head advance `superseded` |
| single grant kept (S3) | focused-test | existing `test_exhausted_acknowledged_authority_job_gets_one_recovery_claim` |
| skip kept (S4) | focused-test | existing `tests/db/test_exhausted_authority_job.py` cases, plus new `test_dead_letter_skips_a_job_without_a_spent_grant_and_proof` |
| privileges (S5) | focused-test | extend `test_acknowledged_retry_proof_helper_is_private` with the renamed function |
| migration registration | focused-test | `tests/db/test_migrate.py` and the 0091/0102/0115 lists |
| ADR text | task-test-not-applicable | prose record; `just records` validates its shape |
