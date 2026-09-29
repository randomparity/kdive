# Retained requeue clears its category — plan

Goal: implement `docs/workflow/specs/2026-09-29-retained-requeue-category-design.md` (#2916).
Architecture: one in-place function rewrite migration plus one guard in the envelope constructor.
Tech stack: PostgreSQL plpgsql migrations, Python 3 / pydantic, pytest.

Expected implementation size: 45–70 changed lines (S) — one ~30-line migration, a 1-line guard,
two tests, and four one-line registration edits.

## Global Constraints

- The migration number is exactly `0163`. Applied migrations are byte-immutable (ADR-0015).
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`. The pre-push hook
  runs `just ci`.

## Task 1 — writer: the retained requeue clears `error_category`

Files: create `src/kdive/db/schema/0163_retained_teardown_requeue_clears_category.sql`; modify
`tests/db/test_migration_0147_external_boot_system_teardown.py`, `tests/db/test_migrate.py`
(four lists), `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
`tests/db/test_migration_0102_build_gc_cursors.py`,
`tests/db/test_migration_0115_capture_reap_state.py`.
Interfaces: none consumed; nothing later relies on it.

Verification:

- Contract "the retained requeue stores NULL", `Mode: focused-test`. In
  `test_0147_retains_quarantine_without_terminalizing_system`, change the job-state assertion to
  `SELECT state, error_category FROM jobs WHERE id=%s` == `("queued", None)`. Red before the
  migration: `('queued', 'conflict')`. Green:
  `just test-verbose tests/db/test_migration_0147_external_boot_system_teardown.py`.
- Contract "0163 is registered in order", `Mode: focused-test`. Pinned lists: append
  `("0163", "0163_retained_teardown_requeue_clears_category.sql")` or `"0163"` after each 0162
  entry, and widen each `migrations[-N:]` slice by one. Green: `just test-verbose
  tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Steps:

1. Edit the 0147 test assertion and run it. Expect red with `'conflict'`.
2. Write the migration:

```sql
-- #2916 (ADR-0483: both retry seams agree): the retained_quarantine requeue in the 0147
-- teardown finalizer kept error_category = 'conflict' on a queued job, which every other requeue
-- clears and which ToolResponse forbids on a non-failure status.  Rewrite in place (as 0149 does)
-- so 0149's ownership patch survives; attempt accounting on this path is #2917's.
DO $$
DECLARE
    v_definition text;
    v_old constant text := 'error_category = ''conflict'' WHERE id = p_job_id;';
    v_new constant text := 'error_category = NULL WHERE id = p_job_id;';
BEGIN
    SELECT pg_get_functiondef(
        'public.finalize_external_boot_authority_teardown('
        'bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'::regprocedure
    ) INTO v_definition;
    IF strpos(v_definition, v_old) = 0 THEN
        RAISE EXCEPTION 'external boot teardown retained requeue shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    IF strpos(v_definition, v_new) = 0 THEN
        RAISE EXCEPTION 'external boot teardown retained requeue was not rewritten';
    END IF;
    EXECUTE v_definition;
END
$$;
```

3. Rerun the 0147 test and expect green. Update the pinned lists and run them green.
4. Commit: `fix(db): clear error_category on the retained teardown requeue`.

`pg_get_functiondef` emits `CREATE OR REPLACE FUNCTION`, so the existing grants and
`SECURITY DEFINER` are kept, as they were for 0149.

## Task 2 — reader: `from_job` surfaces a category only on `failed`

Files: modify `src/kdive/mcp/responses.py` (`from_job`) and `tests/mcp/core/test_responses.py`.
Interfaces: `ToolResponse.from_job(job, extra_next_actions=None)`, with its signature unchanged.

Verification: contract "no category on a non-failed envelope", `Mode: focused-test`:

```python
@pytest.mark.parametrize(
    "state", [JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.CANCELED]
)
def test_from_job_drops_stored_category_on_non_failure_state(state: JobState) -> None:
    job = _BUILD_JOB.model_copy(update={"state": state, "error_category": ErrorCategory.CONFLICT})
    resp = ToolResponse.from_job(job)
    assert resp.status == state.value
    assert resp.error_category is None
    assert resp.retryable is None
```

Red: `ValueError ... error_category set on non-failure status`. Green: `just test-verbose
tests/mcp/core/test_responses.py`. The existing `failed` case (line ~68) protects the kept
category.

Steps: add the test and see it red. In `from_job`, set
`error_category=job.error_category.value if job.error_category and job.state is JobState.FAILED
else None`, then see it green. Commit: `fix(mcp): surface a job's category only when it failed`.
