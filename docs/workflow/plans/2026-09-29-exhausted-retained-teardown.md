# Exhausted retained teardown implementation plan (#2917)

**Goal:** a System-teardown job whose final attempt commits a `retained_quarantine` receipt ends
`failed`, which the public teardown recycles, instead of `queued` and unclaimable.
**Architecture:** migration 0163 patches `finalize_external_boot_authority_teardown`'s retained
branch and repairs rows that are already stranded; spec
[`2026-09-29-exhausted-retained-teardown-design.md`](../specs/2026-09-29-exhausted-retained-teardown-design.md).
**Tech stack:** PostgreSQL PL/pgSQL migration, pytest DB tests (testcontainers PostgreSQL).

Expected implementation size: 170–240 changed lines (M) — migration 25–35, new test file
110–160, helper parameter 3–5, migration-list updates 6–10, ADR amendment 20–25.

## Global Constraints

- Migration number is exactly `0163` (orchestrator-assigned). Applied migrations are immutable
  (ADR-0015); change a function only through a new migration.
- Do not edit `src/kdive/mcp/responses.py` (#2916 is changing it) or `src/kdive/jobs/queue.py`.
- Ruff line length 100; `ty` whole tree; prose avoids "critical", "robust", "comprehensive".

## File map

- create `src/kdive/db/schema/0163_dead_letter_exhausted_retained_teardown.sql` — finalizer
  patch plus one-time repair.
- create `tests/db/test_exhausted_retained_teardown.py` — DB-backed proofs.
- modify `tests/db/external_boot_journal_support.py` — `_make_current` gains a keyword
  `category: str = "absent"` for the head record's observation category (a retained finalize
  needs a non-`absent` category).
- modify `tests/db/test_migrate.py` (three version lists and the `(version, filename)` list),
  `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
  `tests/db/test_migration_0102_build_gc_cursors.py`,
  `tests/db/test_migration_0115_capture_reap_state.py` — add `0163`.
- modify `docs/adr/0620-authority-owned-system-teardown.md` — dated amendment (already written
  in the design commit).

## Task 1 — failing DB tests

**Verification**

- `Mode: focused-test` — final-attempt retained finalize dead-letters.
  `test_final_attempt_retained_teardown_is_dead_lettered`; red on main with job state
  `queued`; green: `just test-verbose tests/db/test_exhausted_retained_teardown.py`.
- `Mode: focused-test` — non-final retained finalize requeues.
  `test_non_final_retained_teardown_still_requeues`; passes before and after (a guard).
- `Mode: focused-test` — stranded-row repair.
  `test_migration_0163_dead_letters_stranded_retained_teardown`; red before the migration file
  exists (`StopIteration`/state `queued`).
- `Mode: focused-test` — recycle after dead-letter credits once.
  `test_dead_lettered_retained_teardown_recycles_and_credits_once`; red on main because the job
  is `queued` and `enqueue` with `FAILED_OR_LAPSED_EXHAUSTED` does not recycle it.

Steps:

1. In `tests/db/external_boot_journal_support.py`, change the `_make_current` signature to
   `def _make_current(conn, case, authority, proof, sequence, *, category: str = "absent") -> str`
   and write `"category": category` in the head record's `observation`.
2. Create `tests/db/test_exhausted_retained_teardown.py` using `_ready_teardown_case`,
   `_allocate`, `_proof(case, "retained_quarantine")`, `_make_current(..., category="conflict")`,
   `_finalize`, and the `migrated_url`, `authority_role_dsns`, `pg_conn` fixtures:
   - final attempt: `UPDATE jobs SET max_attempts = attempt` before finalize; assert finalize
     returns `"retained"`, job `(state, error_category, attempt, max_attempts)` is
     `("failed", "conflict", 1, 1)`, and the authority is `superseded`.
   - non-final: no `max_attempts` change; finalize returns `"retained"`; job state `queued`.
   - repair: on `pg_conn`, apply migrations through `0162` with
     `tests.db.external_boot_authority_support._apply_through`, record them in
     `schema_migrations` as `migrate.apply_migrations` expects (the insert pattern in
     `tests/db/test_migration_0070_resolved_cpu.py`), seed one stranded marked teardown row, one
     marked teardown row `queued` with `attempt < max_attempts`, and one unmarked `teardown` row
     that is `queued` and exhausted; run `migrate.apply_migrations(pg_conn)`; assert only the first
     row is `failed`/`conflict`.
   - recycle: final-attempt retained finalize, then `queue.enqueue(..., recycle=
     JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED)` with the job's own marker gives `queued`,
     attempt 0; a successor incarnation claims attempt 1, allocates, makes current with a
     `complete_ready` proof, and finalizes `"applied"`; one `external_boot_reservation_releases`
     row; a second finalize and a second `enqueue` replay add no row, and the job is `succeeded`.
3. Run `just test-verbose tests/db/test_exhausted_retained_teardown.py`; expect the final-attempt,
   repair, and recycle tests to fail.

## Task 2 — migration 0163

**Verification**

- `Mode: focused-test` — the Task 1 tests turn green; the existing
  `tests/db/test_migration_0147_external_boot_system_teardown.py` retained test stays green.
- `Mode: focused-test` — migration lists: `just test-verbose tests/db/test_migrate.py
  tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Steps:

1. Create the migration:

   ```sql
   -- ADR-0620 amendment (#2917): a retained_quarantine receipt on the job's final attempt ...
   DO $$
   DECLARE
       v_definition text;
       v_old constant text := $old$RETURN 'retained';$old$;
       v_new constant text := $new$UPDATE public.jobs SET state = 'failed', error_category = 'conflict'
           WHERE id = p_job_id AND attempt >= max_attempts;
           RETURN 'retained';$new$;
   BEGIN
       SELECT pg_get_functiondef(
           'public.finalize_external_boot_authority_teardown('
           'bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'::regprocedure
       ) INTO v_definition;
       IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
          OR position('attempt >= max_attempts' IN v_definition) <> 0 THEN
           RAISE EXCEPTION 'external boot System teardown retained branch shape changed';
       END IF;
       EXECUTE replace(v_definition, v_old, v_new);
   END
   $$;

   UPDATE public.jobs SET state = 'failed', error_category = 'conflict'
   WHERE kind = 'teardown' AND state = 'queued' AND attempt >= max_attempts
     AND jsonb_typeof(payload -> 'external_boot_authority_v1') = 'object';
   ```

2. Add `0163` to the migration lists named in the file map.
3. Run the focused commands above; expect all green. Then `just lint`, `just type`.

## Verification

`just test-verbose tests/db/test_exhausted_retained_teardown.py
tests/db/test_migration_0147_external_boot_system_teardown.py tests/db/test_exhausted_authority_job.py
tests/db/test_migration_0161_teardown_takeover.py`, then `just lint`, `just type`,
`just test-changed`, `just records` (after `git fetch origin main`); the pre-push hook runs
`just ci`. Rollback: revert the commits before merge; after deploy, a forward migration is the
only rollback (ADR-0015).
