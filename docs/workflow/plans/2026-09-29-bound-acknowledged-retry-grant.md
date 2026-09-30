# Bound the acknowledged-retry grant per budget — plan

Goal: an attempt that an ADR-0626 grant claimed cannot earn a second grant in the same budget.
The reconciler fails a `boot` job that is past that bound (#2960).

Architecture: migration 0165 renames the 0151 proof predicate to an evidence-only function and
puts a bound-checking wrapper under the old name. The claim, queue-depth, and consume functions
already call that name. The same migration replaces the 0162 dead-letter function, so it also
ends a job past the bound, under the journal-head lock. Spec:
`docs/workflow/specs/2026-09-29-bound-acknowledged-retry-grant-design.md`. Decision: ADR-0711.

Tech stack: PostgreSQL SQL and PL/pgSQL migrations (`src/kdive/db/schema/`). pytest against a
real Postgres (fixtures `migrated_url` and `authority_role_dsns`).

Expected implementation size: 220–290 changed lines (M) — derived from the file map below:
migration about 95, new tests about 170, registration lists 8, docstring 6, ADR-0626 banner 3.

## Global Constraints

- The assigned numbers are migration `0165` and ADR `0711`.
- Do not edit an applied migration (0150, 0151, 0162). `just schema-guard` enforces this.
- Each new function sets `SET search_path = ''` and has the 0150 REVOKE list:
  `FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
  kdive_provider_authority`. `CREATE OR REPLACE` keeps the 0162 `kdive_reconciler` grant on
  the dead-letter function.
- Lines are at most 100 characters.
- On macOS, run gates bare with Homebrew bash 4.4+ and gnubin on `PATH`.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/db/schema/0165_bound_acknowledged_retry_grant.sql` | create | the bound predicate; the dead-letter path past the bound |
| `tests/db/test_external_boot_authority_journal_migration.py` | modify | the ADR-0626/0711 claim and dead-letter proofs |
| `src/kdive/reconciler/repairs/jobs.py` | modify docstring and log text | the description of `repair_abandoned_jobs` |
| `tests/db/test_migrate.py`, `tests/db/test_migration_0091_system_object_sweep_cursors.py`, `tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py` | modify | the pinned migration lists |
| `docs/adr/0626-recover-exhausted-acknowledged-authority-claims.md`, `docs/adr/0620-authority-owned-system-teardown.md` | modify the Status section only | the `Amended by` banners |

## Task 1 — The bound and the terminal path

Interfaces:

- Consumes:
  - `public.external_boot_acknowledged_retry_consumptions(job_id, claimed_attempt,
    consumed_at, ...)` from 0150;
  - `public.has_acknowledged_external_boot_retry_proof(public.jobs)` with its 0151 body.
- Produces:
  - `public.has_acknowledged_external_boot_no_mutation_head(public.jobs) RETURNS boolean`;
  - a new `public.has_acknowledged_external_boot_retry_proof(public.jobs) RETURNS boolean`;
  - `public.dead_letter_unowned_external_boot_jobs() RETURNS SETOF uuid`, with the same
    signature as in 0162.

Test helpers already in the test module or its support modules, with their signatures confirmed:

- `_seed_exhausted_acknowledged_job(url, dsns, suffix, *, promote, attempt=3)`, which returns
  `(case, authority, ack)`;
- `_register_worker(url, prefix, credential) -> str`;
- `_allocate(worker, case)`;
- `_record(case, authority, sequence, previous_digest, phase, **changes)`;
- `_advance_raw(conn, case, authority, expected_sequence, expected_digest, payload)`;
- `_payload(record)`;
- `_promote(url, case, authority, ack)`.

Verification. Each focused command is `uv run pytest -q -p no:cacheprovider
tests/db/test_external_boot_authority_journal_migration.py -k <expr>`.

- S1, bound. Mode: focused-test. Test: `test_granted_attempt_cannot_earn_a_second_grant`,
  parametrized over `promote`.
  - Red before the migration: the query of `has_acknowledged_external_boot_no_mutation_head`
    fails because that function does not exist.
  - Green: `-k second_grant` gives `2 passed`.
- S2, terminal. Mode: focused-test. Test:
  `test_reconciler_dead_letters_a_job_past_the_grant_bound`, parametrized over `promote`.
  - Red: the dead-letter call returns `[]`.
  - Green: `-k past_the_grant_bound` gives `2 passed`.
- S4, skip kept. Mode: focused-test. Test:
  `test_dead_letter_skips_a_job_without_a_spent_grant_and_proof`, with shapes `unspent-grant`
  and `unacknowledged-granted-attempt`. It passes both before and after the change, so it guards
  against an over-wide bypass. Its docstring says that the second shape pins a known residual.
  `tests/db/test_exhausted_authority_job.py` also stays green.
- S6, per budget. Mode: focused-test. Test: `test_recycled_job_earns_a_grant_in_its_new_budget`.
  - Red when the `consumed_at >= created_at` clause is removed: `has_..._retry_proof` is false,
    and the count is 0.
  - Green: `-k new_budget` gives `1 passed`.
- S3 and S5. Mode: focused-test.
  - The existing `test_exhausted_acknowledged_authority_job_gets_one_recovery_claim` stays
    green.
  - `test_acknowledged_retry_proof_helper_is_private` gains
    `"has_acknowledged_external_boot_no_mutation_head(jobs)"`.

Steps:

1. Add a helper to the test module:
   `_granted_attempt_head(url, dsns, suffix, *, promote, acknowledge=True) -> (case, authority,
   head)`. It does these steps in order:
   1. Seed an exhausted job with `_seed_exhausted_acknowledged_job(..., promote=True)`.
   2. Claim the grant as a new registered worker and assert `(job_id, 4, 4)`.
   3. `_allocate` G2 with `replace(case, worker_id=..., credential=..., attempt=4)`.
   4. Advance a `WATERMARK_INSTALLED` record at sequence 3 from the first acknowledgement.
   5. When `acknowledge` is true, advance a `TAKEOVER_ACKNOWLEDGED` record at sequence 4 with
      `watermark_sequence=3`. Both records use `attempt_id=str(G2.authority_id)`.
   6. `_promote` G2 when both `acknowledge` and `promote` are true.
   7. Lapse the job's lease.
   Also add `_dead_letter_unowned(dsns) -> list[UUID]`, which calls
   `SELECT * FROM dead_letter_unowned_external_boot_jobs()` as `kdive_reconciler`.
2. Write the four tests. Each test body is described here:
   - `second_grant`:
     - as the owner, check that `(no_mutation_head(j), retry_proof(j))` is `(True, False)`;
     - as a new worker, check that the count is 0 and that the claim returns `None`.
   - `past_the_grant_bound`:
     - the dead-letter call returns `[job_id]`;
     - the job is `('failed', 'lease_expired')`;
     - the authority states, by generation, are `['superseded', 'retired' if promote else
       'superseded']`;
     - an `ADMITTED` advance at sequence 5 under G2 returns `superseded`;
     - a second dead-letter call returns `[]`.
   - `skips`: the dead-letter call returns `[]`, and the job is still `running`.
   - `new_budget`:
     1. After a past-the-bound dead-letter with `promote=True`, emulate the `queue.enqueue` failed
        recycle plus the new budget's ordinary claims:
        `UPDATE jobs SET state='running', attempt=4, max_attempts=4, worker_id=<new worker>,
        lease_expires_at=now()+interval '1 minute', created_at=clock_timestamp()`.
     2. `_allocate` G3 at attempt 4.
     3. Advance the watermark and the acknowledgement at sequences 5 and 6 from G2's head.
     4. Lapse the lease.
     5. Assert that `retry_proof(j)` is `True` and that the worker count is 1.
3. Run the focused commands. S1 and S2 fail as stated.
4. Create `src/kdive/db/schema/0165_bound_acknowledged_retry_grant.sql`:
   1. Add a header comment that cites ADR-0711 and #2960.
   2. Run `ALTER FUNCTION public.has_acknowledged_external_boot_retry_proof(public.jobs) RENAME
      TO has_acknowledged_external_boot_no_mutation_head;`.
   3. Create the new predicate with `LANGUAGE sql STABLE RETURNS NULL ON NULL INPUT SET
      search_path = ''`, then REVOKE it. The body is:

      ```sql
      SELECT NOT EXISTS (
          SELECT 1 FROM public.external_boot_acknowledged_retry_consumptions AS consumed
          WHERE consumed.job_id = p_job.id AND consumed.claimed_attempt = p_job.attempt
            AND consumed.consumed_at >= p_job.created_at
      ) AND public.has_acknowledged_external_boot_no_mutation_head(p_job)
      ```

   4. `CREATE OR REPLACE FUNCTION public.dead_letter_unowned_external_boot_jobs()`. Copy the
      0162 body and replace its `CONTINUE WHEN EXISTS (live authority)` with the block below:

      ```sql
      IF EXISTS (SELECT 1 FROM public.external_boot_authorities AS authority
                 WHERE authority.job_id = v_job.id
                   AND authority.state IN ('allocating', 'current')) THEN
          CONTINUE WHEN NOT EXISTS (<the consumption clause above, over v_job>);
          PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:'
              || (v_job.payload #>> '{external_boot_authority_v1,system_id}'), 2126));
          CONTINUE WHEN NOT public.has_acknowledged_external_boot_no_mutation_head(v_job);
          BEGIN
              PERFORM 1 FROM public.external_boot_authorities AS authority
              WHERE authority.job_id = v_job.id
                AND authority.state IN ('allocating', 'current')
              FOR UPDATE NOWAIT;
          EXCEPTION WHEN lock_not_available THEN
              CONTINUE;
          END;
          UPDATE public.external_boot_authorities
          SET state = CASE WHEN state = 'current' THEN 'retired' ELSE 'superseded' END,
              retired_at = CASE WHEN state = 'current' THEN clock_timestamp() END,
              superseded_at = CASE WHEN state = 'allocating' THEN clock_timestamp() END
          WHERE job_id = v_job.id AND state IN ('allocating', 'current');
      END IF;
      ```

5. Run the focused commands. S1, S2, S4, and S6 pass. Then run
   `uv run pytest -q -p no:cacheprovider tests/db/test_external_boot_authority_journal_migration.py
   tests/db/test_exhausted_authority_job.py tests/db/test_worker_fence_authority.py`. Every test
   passes.
6. In `repair_abandoned_jobs` (`src/kdive/reconciler/repairs/jobs.py`):
   - extend the docstring sentence about `dead_letter_unowned_external_boot_jobs` to name the
     ADR-0711 past-the-bound case;
   - change the log line from `unowned external-boot job` to `external-boot job`.
7. Commit: `fix(db): bound the acknowledged-retry grant per budget (#2960)`.

Rollback: migrations only move forward (ADR-0015). A fix lands as a later migration.

## Task 2 — Registration and the ADR-0626 banner

Verification:

- Pinned migration lists. Mode: focused-test.
  - Command: `uv run pytest -q -p no:cacheprovider tests/db/test_migrate.py
    tests/db/test_migration_0091_system_object_sweep_cursors.py
    tests/db/test_migration_0102_build_gc_cursors.py
    tests/db/test_migration_0115_capture_reap_state.py`.
  - Red: after 0165 exists, the list comparisons fail.
  - Green: after the edit, every test passes.
- The ADR-0626 banner. Mode: task-test-not-applicable. It is one prose line in the Status
  section, and `just records` accepts a change to that region only.

Steps:

1. In `tests/db/test_migrate.py`:
   - after each `"0164",` (near lines 290, 1112, and 1550), add `"0165",`;
   - after the 0164 tuple (near line 364), add
     `("0165", "0165_bound_acknowledged_retry_grant.sql"),`.
   Add the same tuple after the 0164 tuple in the 0091, 0102, and 0115 test files.
2. In the `## Status` section of ADR-0626, after the date, add an `Amended by` blockquote that
   links ADR-0711 and cites #2960: a claim that this ADR granted cannot earn another grant in
   the same budget. Add the matching banner to ADR-0620.
3. Run the registration command. Then run these gates, and confirm that each one exits 0:
   - `git fetch origin main`, then `just records`;
   - `just schema-guard`;
   - `just migration-order-check`;
   - `just lint`;
   - `just type`.
4. Commit the registration and the banner as two separate commits.

## Spec coverage

| Spec item | Task |
|---|---|
| Scope 1 (bound), S1, S3, S5, S6 | Task 1 |
| Scope 2 (terminal), S2, S4, and the jobs.py docstring | Task 1 |
| Scope 3 (ADR banner) | Task 2. ADR-0711 is already on the branch. |
| Scope 4 (registration) | Task 2 |
