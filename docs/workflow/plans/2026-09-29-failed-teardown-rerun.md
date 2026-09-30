# Plan: `systems.teardown` re-runs a dead-lettered teardown job (#2929)

Goal: the ordinary `systems.teardown` path resets a `failed` `{uid}:teardown` row to a fresh attempt.
Architecture: a `FAILED` recycle policy in `kdive.jobs.queue`, mirrored in `_idempotency._RECYCLED`,
passed by `teardown_system`'s ordinary path. Spec:
[`2026-09-29-failed-teardown-rerun-design.md`](../specs/2026-09-29-failed-teardown-rerun-design.md).
Tech: Python 3.14, psycopg async, pytest with disposable Postgres (`migrated_url` fixture).

Global Constraints: no migration, no new ADR (amend ADR-0435 in place), no new dependency.
Guardrails: `just lint`, `just type`, `just test-verbose <paths>`, `just ci` before push.

Expected implementation size: 60–110 changed lines (S) — one enum value and its two branches,
two call-site arguments, three DB-backed tests, one ADR amendment paragraph.

## Task 1: `JobRecyclePolicy.FAILED`

Files: `src/kdive/jobs/queue.py`, `src/kdive/mcp/tools/lifecycle/support/_idempotency.py`,
`tests/jobs/test_queue.py`. Interfaces: produces `queue.JobRecyclePolicy.FAILED` (value
`"failed"`), consumed by Task 2.

Verification:
- `Mode: focused-test` — contract: `enqueue(..., recycle=FAILED)` resets a `failed` row in place
  and returns a `succeeded` or `canceled` row unchanged. Test:
  `tests/jobs/test_queue.py::test_enqueue_recycle_failed_resets_only_a_failed_job`. Red:
  `AttributeError: FAILED`. Green: `just test-verbose tests/jobs/test_queue.py -k recycle_failed`.

Steps:
1. Add the test beside `test_enqueue_recycle_canceled_reclaims_only_when_opted_in`: build a failed
   row with `_terminal_failed_job(conn, "dk-failed")`, enqueue with `recycle=FAILED`, assert same
   id, `QUEUED`, `attempt == 0`; then for `succeeded` and `canceled`, enqueue a row on a fresh key,
   `UPDATE jobs SET state = ...`, enqueue again with `recycle=FAILED`, assert id and state
   unchanged; assert `await _count_jobs(conn) == 3`. Run: red as above.
2. In `JobRecyclePolicy` add `FAILED = "failed"` after `NEVER`, and one docstring sentence:
   ``FAILED`` resets only a ``failed`` row, so a ``succeeded`` job still replays (#2929).
3. In `enqueue_with_status` replace the `SUCCEEDED` branch with
   `if recycle in (JobRecyclePolicy.TERMINAL, JobRecyclePolicy.TERMINAL_OR_CANCELED):`.
4. In `_RECYCLED` add `queue.JobRecyclePolicy.FAILED: frozenset({JobState.FAILED}),`.
5. Run green; `just lint`; `just type`; commit `feat(jobs): add a failed-only recycle policy`.

## Task 2: ordinary `systems.teardown` recycles a failed row

Files: `src/kdive/mcp/tools/lifecycle/systems/admin.py`,
`tests/mcp/lifecycle/test_systems_tools.py`, `docs/adr/0435-reclaim-failed-provision-artifacts.md`.
Interfaces: consumes `queue.JobRecyclePolicy.FAILED`; `dedup_replay(conn, key, *, recycle)`
and `queue.enqueue(..., recycle=...)` exist with those signatures.

Verification:
- `Mode: focused-test` — contract: a `failed` row (any category) is reset for a `failed` or
  `ready` System. Test: `test_teardown_recycles_dead_lettered_ordinary_job[failed|ready]`
  (sets the row `state='failed', attempt=max_attempts, error_category='infrastructure_failure'`
  and asserts `response.status == "queued"`, same `object_id`, row `("queued", 0, payload)`).
  Red: status `"failed"`. Green:
  `just test-verbose tests/mcp/lifecycle/test_systems_tools.py -k ordinary_job`.
- `Mode: focused-test` — contract: `queued`, `running`, `succeeded`, `canceled` rows replay
  unchanged for a `failed` System. Test: `test_teardown_replays_live_or_settled_ordinary_job`
  parametrized on those four; asserts `_JOB_COLUMNS` row before == after and
  `response.status == prior`. Must pass before and after (guards against `TERMINAL`; a controlled
  fault swapping in `TERMINAL` must turn the `succeeded` case red).
- `Mode: task-test-not-applicable` — ADR-0435 amendment prose; no executable consumer reads it
  beyond `just records`, which runs.

Steps:
1. Add both tests after `test_teardown_admin_enqueues_job`; seed with
   `_seed_teardown_system(pool, alloc_id, state)`, enqueue the first job via `_teardown`, then
   `UPDATE jobs` the row. Run: the recycle test is red.
2. In `teardown_system` pass `recycle=queue.JobRecyclePolicy.FAILED` to the ordinary
   `dedup_replay` and `queue.enqueue` calls; rewrite the comment above them: an unkeyed repeat
   replays a live, succeeded, or canceled job; a dead-lettered `failed` job is reset (#2929).
3. Append to ADR-0435 `### Amendment (2026-09-29): systems.teardown re-runs a dead-lettered
   teardown (#2929)`: the #2908 residual that such a job is not re-run no longer holds for the
   public tool; the policy, the replayed states, no reconciler lane (ADR-0441 unchanged), and a
   link to the spec.
4. Run green; controlled fault (commit first); `just records`; `just lint`; `just type`; commit
   `fix(systems): re-run a dead-lettered ordinary teardown job`.
