# Implementation plan — Recycle a terminally-failed install/boot step (#603)

- **Spec:** [docs/specs/2026-06-19-retry-terminal-failed-step.md](../../specs/2026-06-19-retry-terminal-failed-step.md)
- **ADR:** [ADR-0185](../../adr/0185-retry-terminal-failed-step.md)
- **Branch:** `feat/retry-terminal-failed-step-603`

## Context

`queue.enqueue` returns a terminal-`failed` step job forever (upsert-then-fetch), wedging the Run.
Fix: add opt-in `retry_terminal_failed` to `queue.enqueue` that resets a `failed` row for the dedup
key back to `queued`/`attempt=0`; `_enqueue_step` passes it for install/boot. Tightly coupled
(queue helper + its sole step caller + tests), so implemented directly in this session.

## Guardrails (run before every commit)

- `just lint`, `just type`
- Focused: `uv run python -m pytest tests/jobs/test_queue.py tests/adversarial/test_queue_concurrency.py tests/mcp/lifecycle/test_runs_tools.py tests/jobs/handlers/test_runs_install.py -q`
- Before push: full `just ci` (DB tests need Docker/testcontainers; `migrated_url` fixture).

## Task 1 — `queue.enqueue` opt-in terminal-failed recycle

**File:** `src/kdive/jobs/queue.py` (+ `tests/jobs/test_queue.py`, `tests/adversarial/test_queue_concurrency.py`)

TDD order — failing tests first in `test_queue.py` (use the `migrated_url` fixture and the existing
`_insert_running_job` / `queue.fail` helpers to build a terminal-`failed` job):

1. `test_enqueue_retry_terminal_failed_resets_failed_job`: build a job, dequeue+fail it to `failed`
   at `attempt == max_attempts`; call `enqueue(..., same dedup_key, retry_terminal_failed=True)`;
   assert the returned job is `JobState.QUEUED`, `attempt == 0`, `worker_id is None`,
   `lease_expires_at is None`, `error_category is None`, `failure_context == {}`, **same `id`**; then
   `dequeue` claims it (proving it is no longer wedged).
2. `test_enqueue_retry_terminal_failed_preserves_in_flight`: a `queued` job + retry flag → unchanged
   (same id, still `queued`, `attempt` unchanged); a `running` (leased) job + retry flag → unchanged
   (`worker_id`/lease intact). Confirms the `state='failed'` fence preserves in-flight dedup.
3. `test_enqueue_retry_terminal_failed_does_not_resurrect_succeeded`: a `succeeded` job + retry flag
   → unchanged (still `succeeded`).
4. `test_enqueue_default_leaves_failed_job_untouched`: failed job + `enqueue` **without** the flag
   (default) → returned unchanged (`failed`) — pins that provision/build callers are unaffected.
5. Adversarial (`test_queue_concurrency.py`): two concurrent `enqueue(retry_terminal_failed=True)` on
   the same failed dedup key → exactly one queued row, `attempt == 0`, no duplicate row (mirror the
   existing `test_concurrent_enqueue_same_dedup_key_makes_one_row`).

Implementation: add `retry_terminal_failed: bool = False` to `enqueue`. Inside the existing
`conn.transaction()`, after the `INSERT … ON CONFLICT DO NOTHING` and before the final `SELECT`, when
the flag is set run one fenced statement (parameterize the state values via `JobState.*.value`):

```python
if retry_terminal_failed:
    await cur.execute(
        "UPDATE jobs SET state = %s, attempt = 0, worker_id = NULL, "
        "lease_expires_at = NULL, heartbeat_at = NULL, error_category = NULL, "
        "failure_context = '{}'::jsonb, result_ref = NULL "
        "WHERE dedup_key = %s AND state = %s",
        (JobState.QUEUED.value, dedup_key, JobState.FAILED.value),
    )
```

Update the `enqueue` docstring to describe the opt-in recycle and the `state='failed'` fence.

**Acceptance:** all five tests pass; existing enqueue/dequeue/fail tests still pass; `ty`/`lint`
clean.

## Task 2 — `_enqueue_step` passes the flag

**File:** `src/kdive/mcp/tools/lifecycle/runs/steps.py` (+ `tests/mcp/lifecycle/test_runs_tools.py`)

TDD order:

1. **Failing test** in `test_runs_tools.py` (or `tests/jobs/handlers/test_runs_install.py` if that is
   the closer boundary — pick the one whose harness can build a `SUCCEEDED` Run with a bound System
   and a terminal-`failed` `<run_id>:install` job): a built (`SUCCEEDED`, bound) Run whose
   `<run_id>:install` job is terminal-`failed`; call `install_run`; assert it returns a `running`
   job envelope for a `queued`/fresh job (`attempt == 0`), not the old failed job, and that the
   `runs` row state is unchanged (no rebuild). Add the in-flight case: an existing `queued` install
   job → `install_run` returns the same job (deduped, not double-enqueued).
2. Implementation: in `_enqueue_step`, pass `retry_terminal_failed=True` to `queue.enqueue`. (One
   line; both install and boot route through this single helper.)

**Acceptance:** the new tests pass; existing install/boot admission tests pass.

## Task 3 — stale-run_steps-row tolerance (spec criterion 6)

**File:** a focused test — `tests/jobs/handlers/test_runs_install.py` or `tests/db/test_idempotency.py`
(whichever owns `claim_run_step` coverage).

This is a **guard test only** (no production change — claim_run_step already self-heals):

1. Seed a `run_steps` row for `(run_id, "install")` in `state='running'` with `updated_at` older than
   `_STALE_RUNNING_INTERVAL`. Call `claim_run_step(conn, run_id, "install")` and assert it returns
   `StepClaim(claimed=True, ...)` (the stale row is reaped and re-claimed) — pinning that a recycled
   re-run is not blocked by a lingering row. If `tests/db/test_idempotency.py` already covers stale
   reaping, extend/point at it rather than duplicating.

**Acceptance:** the guard test passes, documenting the relied-upon self-heal.

## Verification (step 5 / 7)

- Full `just ci`.
- Grep every `queue.enqueue(` caller to confirm only `_enqueue_step` passes `retry_terminal_failed`
  (provision/build/teardown/reprovision/power/force_crash/capture/image-build untouched).

## Rollback / cleanup

Pure code + tests, no DDL/migration/schema. Revert is a straight `git revert`. The new parameter
defaults off, so reverting cannot strand any persisted state (no jobs depend on the flag).
