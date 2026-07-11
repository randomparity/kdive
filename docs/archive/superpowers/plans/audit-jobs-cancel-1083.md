# Plan — audit successful `jobs.cancel` transitions (#1083)

- **Spec:** [`docs/specs/2026-07-10-audit-jobs-cancel-1083.md`](../../specs/2026-07-10-audit-jobs-cancel-1083.md)
- **Issue:** #1083 (follow-up to #1080 / PR#1082)
- **Branch:** `feat/audit-jobs-cancel-1083` (base `main`)
- **Guardrails:** `just lint`, `just type`, `just test` (CI runs each
  individually); `just ci` is the full gate. Single test:
  `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py::<name> -q`.

Single-file source change (`src/kdive/mcp/tools/jobs.py`) plus tests in
`tests/mcp/jobs/test_jobs_tools.py`. No schema, migration, or ADR. TDD:
write the failing audit-row tests first, then the handler change.

## Task 1 — Tests for the successful-cancel audit row (write first, RED)

**Where it fits:** the spec's first two acceptance criteria — a successful
cancel writes exactly one readable `audit_log` row attributing actor/tool/
object/project/transition-with-kind.

**File:** `tests/mcp/jobs/test_jobs_tools.py`. Reuse existing helpers:
`_pool`, `_enqueue` (BUILD job in `proj`), `_enqueue_system_job(kind, dedup)`
(SystemPayload job of a given kind), `OP_CTX`/`CONTRIB_CTX`. Import
`from kdive.security.audit import args_digest`.

Add a small helper to read the one audit row for a job:

```
async def _audit_rows(pool, job_id):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT principal, tool, object_kind, object_id::text, project, "
            "transition, args_digest FROM audit_log WHERE object_id = %s",
            (job_id,),
        )
        return await cur.fetchall()
```

Tests to add:

1. `test_cancel_running_leaseholder_job_writes_audit_row` — enqueue a
   contributor-cancelable job (use `_enqueue` BUILD, which is in
   `CONTRIBUTOR_CANCELABLE_JOB_KINDS`), force it to `running` with a raw
   `UPDATE jobs SET state = 'running' WHERE id = %s` (mirrors
   `_mark_failed_without_category`), then `cancel_job(pool, CONTRIB_CTX, job_id)`.
   Assert `resp.status == "canceled"`, exactly one audit row, and the row =
   `principal="user-1"`, `tool="jobs.cancel"`, `object_kind="jobs"`,
   `object_id == job_id`, `project="proj"`,
   `transition == "build:running->canceled"`,
   `args_digest == args_digest({"job_id": job_id, "kind": "build"})`.
2. `test_cancel_queued_destructive_job_by_operator_writes_audit_row` — enqueue a
   destructive-kind job (`_enqueue_system_job(JobKind.FORCE_CRASH, "d-fc")`;
   stays `queued` — this is the proven kind/payload pairing already used by
   `test_cancel_operator_gated_job_allowed_to_operator`), then
   `cancel_job(pool, OP_CTX, job_id)`. Assert one row with
   `transition == "force_crash:queued->canceled"` and
   `args_digest == args_digest({"job_id": job_id, "kind": "force_crash"})`.
3. `test_cancel_terminal_job_writes_no_audit_row` — enqueue, cancel once (now
   terminal), then cancel again; assert the second call is the
   `configuration_error`/`current_status` envelope **and** that exactly **one**
   audit row exists total (from the first successful cancel), not two.
4. Extend/confirm `test_cancel_job_requires_contributor_role` (or add a focused
   test): a `RoleDenied` from `cancel_job` writes **zero** rows via
   `_audit_rows` (denial auditing is the middleware's job, unchanged). Note:
   `cancel_job` re-raises `RoleDenied`, so wrap the call in
   `pytest.raises(RoleDenied)` and assert zero rows.

**Acceptance:** all four tests fail before Task 2 (no `audit` call exists yet):
tests 1–2 fail on "no row", test 3 currently passes trivially (zero rows) so
assert **one** row to make it meaningfully red, test 4 passes pre-change (still
assert it to lock the invariant).

**RED gate:** `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py -q`
shows tests 1–2 (and 3's one-row assertion) failing for the right reason.

## Task 2 — Emit the audit row in `cancel_job` (GREEN)

**Where it fits:** spec D1 + D4. **File:** `src/kdive/mcp/tools/jobs.py`.

1. Add `from kdive.security import audit` to the imports (keep import block
   sorted; ruff `I` will enforce order — run `just format` if needed).
2. In `cancel_job`, capture `prior_state = existing.state.value` after the
   `_in_scope`/role checks (D2: the pre-authz read is the annotation source).
3. Replace the bare `async with pool.connection() as conn:` around
   `JOBS.update_state` with `async with pool.connection() as conn,
   conn.transaction():` and, immediately after a successful `update_state`, call
   `audit.record(conn, ctx, audit.AuditEvent(...))` with:
   - `tool="jobs.cancel"`, `object_kind="jobs"`, `object_id=uid`,
   - `transition=f"{job.kind.value}:{prior_state}->canceled"`,
   - `args={"job_id": job_id, "kind": job.kind.value}`,
   - `project=_project(job)`.
   The `except ObjectNotFound`/`except IllegalTransition` handlers stay outside
   or inside such that a raised transition aborts before `audit.record` and no
   row is written (they currently wrap the `async with`; keep that structure so
   the exception unwinds the transaction). Verify the `IllegalTransition` branch
   still returns the `current_status` envelope and writes nothing.

**Conventions:** ≤100 lines/function, complexity ≤8, 100-char lines, absolute
imports, no banned prose words in comments. `audit.record` runs on `conn`
without opening its own transaction, so the outer `conn.transaction()` is the
commit boundary (ADR-0028).

**GREEN gate:** `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py -q`
all green, including the pre-existing cancel tests (161, 171, 202, 228, 482,
496, 513).

## Task 3 — Full guardrails

Run `just lint`, `just type`, then `just test` (or `just ci`). Fix every
warning. Confirm no unrelated tests regressed (especially
`test_cancel_job_member_overreach_is_audited_at_dispatch_boundary`, which must
still pass — the middleware denial path is untouched).

## Rollback / cleanup

Additive change on one handler plus tests. Rollback is reverting the branch; a
reverted build stops writing the new row and behaves as today. No migration or
persisted-state cleanup.
