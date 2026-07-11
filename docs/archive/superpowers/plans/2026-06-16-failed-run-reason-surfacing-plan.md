# Plan — Failed-run reason surfacing on `runs.get` (#486)

- **Spec:** [../../specs/2026-06-16-failed-run-reason-surfacing.md](../../specs/2026-06-16-failed-run-reason-surfacing.md)
- **ADR:** [0141](../../adr/0141-failed-run-reason-surfacing.md)
- **Branch:** `feat/failed-run-reason-surfacing-486`
- **Execution mode:** direct in-session (tasks are tightly coupled: model field ↔ migration ↔
  handler write ↔ read-path surface; not independent enough to fan out).

Guardrails before every commit (whole-tree where noted):
`just lint`, `just type` (whole tree), the focused tests, and for the final push `just ci`.

## Task 1 — Migration 0038 + `test_migrate` registration

**Where it fits:** the storage layer that backs the new Run field.

**Files:** `src/kdive/db/schema/0038_runs_failing_job_id.sql` (new),
`tests/db/test_migrate.py` (edit the applied-versions list + add a column-existence test).

**Steps (TDD):**
1. Add `test_runs_failing_job_id_column` to `tests/db/test_migrate.py` asserting the column
   exists with type `uuid` and is nullable (mirror `test_investigations_description_column`).
   Run it — fails (no migration).
2. Write `0038_runs_failing_job_id.sql`: `ALTER TABLE runs ADD COLUMN failing_job_id uuid;`
   Additive, forward-only, no FK (jobs are never deleted; ADR-0141). Header comment explains
   why no FK and why nullable.
3. Add `"0038"` to the expected applied-versions list in `test_migrate.py`
   (`test_creates_all_tables` / the explicit version list near line 133).
4. Run `just type` + the migrate tests (need Docker/`KDIVE_REQUIRE_DOCKER` — if Docker is
   absent locally, state that in the PR body and rely on CI).

**Acceptance:** migrate tests green; the column is `uuid`, nullable, no FK; no CHECK_ENUMS entry
(it is not an enum column).

**Rollback:** drop the SQL file + test edits; column is additive so no data migration to undo.

## Task 2 — `Run.failing_job_id` model field

**Where it fits:** the typed record the repository reads/writes.

**Files:** `src/kdive/domain/models.py`.

**Steps (TDD):**
1. Add a focused model test (in the appropriate existing models test, or a new one) asserting a
   `Run` round-trips with `failing_job_id` present and defaulting to `None` when absent. Run —
   fails (`extra="forbid"` rejects the key / attribute missing).
2. Add `failing_job_id: UUID | None = None` to `Run` with a one-line doc in the class docstring.
3. Run `just type` + the model test.

**Acceptance:** `Run(...)` accepts and round-trips `failing_job_id`; default `None`; whole-tree
`ty` clean.

**Rollback:** remove the field + test.

## Task 3 — `_fail_build` sets `failing_job_id`

**Where it fits:** the only path that flips a Run `running -> failed` on build failure.

**Files:** `src/kdive/jobs/handlers/runs.py`.

**Steps (TDD):**
1. Extend/author a handler test (`tests/jobs/...` for `_fail_build`/build failure) asserting that
   after a categorized build failure the Run row is `failed` **and** `failing_job_id == job.id`.
   Run — fails (column written as NULL).
2. Edit the `UPDATE runs SET state=…, failure_category=…` in `_fail_build` to also set
   `failing_job_id = %s` (job.id), keeping the `WHERE … state = RUNNING` guard and the
   `IllegalTransition` no-op path intact. The no-op (concurrent-cancel / already-terminal) path
   must NOT clear an existing link.
3. Run `just lint` + `just type` + the focused handler test.

**Acceptance:** first failing attempt records `failing_job_id`; a no-op second `_fail_build`
(already terminal) does not overwrite/clear it; concurrent-cancel path unchanged.

**Rollback:** revert the `UPDATE` to category-only.

## Task 4 — Surface on `runs.get`

**Where it fits:** the read path the agent calls.

**Files:** `src/kdive/mcp/tools/lifecycle/runs/view.py`,
`src/kdive/mcp/tools/lifecycle/runs/common.py`.

**Steps (TDD):**
1. Add MCP-level tests (`tests/mcp/lifecycle/test_runs_tools.py`):
   - a build-failed Run with a dead-lettered job → `runs.get` returns `detail` ==
     the job's redacted `failure_message` and `data["failing_job_id"]` == the job id;
   - a failed Run with `failing_job_id` NULL → no `detail`, no `failing_job_id` (today's shape);
   - a failed Run whose linked job has empty `failure_context` → link present, `detail=None`;
   - (no-leak) confirm `detail` routes through `suppressed_detail` — assert a suppressed-category
     failed Run would surface the seam constant, exercised via the existing failure-envelope path.
   Run — fail.
2. `common.py`: extend `envelope_for_run(run, *, required_cmdline=None, failing_job=None)` so the
   `RunState.FAILED` branch, when `failing_job` is provided, builds the failure envelope with
   `detail=failing_job.failure_context.get("failure_message")`, `data["failing_job_id"]=str(id)`,
   and copies `failure_detail_*` keys from `failure_context` into `data`. Pass `detail` via
   `ToolResponse.failure(..., detail=…)` so `suppressed_detail` governs it. Keep the existing
   category fallback.
3. `view.py`: in `get_run`, when `run.state is FAILED and run.failing_job_id is not None`, fetch
   `JOBS.get(conn, run.failing_job_id)` and pass it to `envelope_for_run`. Fetch only on the
   failed branch (one extra SELECT, cold path). Connection is already open in the `async with`.
4. Run `just lint` + `just type` + the focused MCP tests.

**Acceptance:** all four test scenarios pass; success path unchanged; no-leak seam intact.

**Rollback:** revert both files to category-only envelope.

## Task 5 — Full suite + branch review

1. `just ci` (full gate) — fix any whole-tree type/lint/test fallout (e.g. other `Run(...)`
   constructions, any `envelope_for_run` callers needing the new kwarg default).
2. Run the branch `/challenge` and `security-review` loops (workflow steps 6).
3. Confirm no generated tool-doc regen is needed (tool name/params/description unchanged).

**Acceptance:** `just ci` green locally (modulo Docker-gated tests if Docker is absent); branch
review approves.
