# A retained teardown requeue carries no failure category — design

Issue: #2916. Scope token: q2916-018ddfa2. No ADR: the change aligns one writer with an existing
invariant and picks no new policy.

## Problem

`public.finalize_external_boot_authority_teardown` (migration 0147, patched in place by 0149)
requeues the job on a `retained_quarantine` receipt with
`UPDATE public.jobs SET state = 'queued', ..., error_category = 'conflict'`. Every other requeue
clears the category: the generic recycle in `src/kdive/jobs/queue.py` and the authority-System
retained path in 0149. `ToolResponse.from_job` (`src/kdive/mcp/responses.py`) copies the stored
category for every state, and the `ToolResponse` validator rejects a category on a non-failure
status. So `systems.teardown`, when it replays that queued job, fails on the server.

## Scope

1. **Writer (criterion 1).** Migration `0163_retained_teardown_requeue_clears_category.sql`
   rewrites the finalizer with the in-place pattern 0149 and 0160 use: read
   `pg_get_functiondef`, replace the single text `error_category = 'conflict' WHERE id =
   p_job_id;` with `error_category = NULL WHERE id = p_job_id;`, raise if the old text is absent
   or the new text was not installed, and `EXECUTE` the result. Copying the 0147 body would drop
   the 0149 patch, so the migration does not copy it. Nothing else in the function changes;
   #2917 owns attempt and `max_attempts` handling in this branch of the function.
2. **Reader (criterion 2).** `from_job` passes `error_category` only when
   `job.state is JobState.FAILED`. This second layer covers rows already stored with a stale
   category, including the live fixture, and any `running` reclaim of such a row. The migration
   does not rewrite existing rows.
3. **Registration.** Add 0163 to the migration lists pinned in `tests/db/test_migrate.py`,
   `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
   `tests/db/test_migration_0102_build_gc_cursors.py`, and
   `tests/db/test_migration_0115_capture_reap_state.py`, wherever each one lists 0162.

## Failure model

1. **Actors and deployments**: the teardown worker (`kdive_worker`) finalizes the receipt; MCP
   callers of `systems.teardown`, `jobs.get` and `jobs.wait` read the envelope. The deployment is
   a server stack migrated through 0163.
2. **Invariants and assets at stake**:
   - the stored job row agrees with the queue's requeue contract;
   - a published envelope keeps its category on `failed` jobs, so `retryable` stays derived.
3. **Accepted failure classes**:
   - Existing queued rows keep `'conflict'` in the table until they next transition. This is
     bounded because the reader guard hides it, and a backfill would be a data migration that
     no criterion needs.
   - A `heartbeat_at` or `failure_context` left on the retained requeue is unchanged. No reader
     depends on it (`from_job` reads `failure_context` only for `failed`), and the dispatch
     requires the replacement to stay minimal.
4. **Covered elsewhere**:
   - exhausted-attempt stranding on this requeue: #2917;
   - retry churn: #2901;
   - authority fences (ADR-0584/0620): unchanged.

## Success

- After a `retained_quarantine` finalize, the job row is `queued` with `error_category IS NULL`.
- For each non-`failed` `JobState`, `from_job` on a job that carries a category returns an
  envelope with `error_category is None`. On a `failed` job it keeps the category.

## Validation

- Writer, `Mode: focused-test`: extend `test_0147_retains_quarantine_without_terminalizing_system`
  in `tests/db/test_migration_0147_external_boot_system_teardown.py` to assert
  `error_category IS NULL`. Without 0163 it is red with `('conflict',)`.
- Reader, `Mode: focused-test`: a new `tests/mcp/core/test_responses.py` case over
  `QUEUED`/`RUNNING`/`SUCCEEDED`/`CANCELED` carrying `CONFLICT`. Without the guard it is red
  with `ValueError: error_category set on non-failure status`.
- Registration, `Mode: focused-test`: the pinned-list tests. They are red until 0163 is listed.
