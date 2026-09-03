# 0008 — the release's run-scoped job arm scans every job under the System lock

## Status

Open
review-by: 2027-03-02

## Concern

`runs.release_external_boot` refuses while any queued or running job holds the System
(ADR-0583). `_active_job_ids_for_system`
(`src/kdive/mcp/tools/external_boot/recovery_requests.py`) answers that question in two arms,
and it runs inside `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`.

The `system_id` arm is index-served. The `run_id` arm is not, and cannot be: no index covers
`payload->>'run_id'`. `jobs_live_install_run_id_idx` (migration 0101) is partial on
`kind = 'install'`, so it does not answer the general question, and `jobs_payload_system_id_idx`
(migration 0082) indexes only the other key. The arm's own `LIMIT` can stop the scan early only
when rows actually match — and the ordinary case, where nothing blocks the release, is exactly
the case where nothing matches and the whole table is read.

Measured on PostgreSQL 17 with 200,000 `jobs` rows over 5,000 `runs`, both named indexes
present and `ANALYZE`d, with no row matching the System:

| form | plan | buffers |
| --- | --- | --- |
| single statement, `OR` + global `ORDER BY j.id` | `Index Scan using jobs_pkey`, `Rows Removed by Filter: 200000` | 201041 |
| `system_id` arm alone | `Index Scan using jobs_payload_system_id_idx` | 3 |
| `run_id` arm alone | `Parallel Seq Scan on jobs`, `Rows Removed by Filter: 100000` × 2 workers | 2861 |

The single-statement form was this branch's original shape and its comment claimed the
`system_id` arm rode the expression index; the plan above refutes that, because an `OR` across
an indexed and an unindexed expression is planned as neither. Splitting the arms fixed that
half and is what ships. The residual is the third row: while the release holds the System-wide
advisory lock, the `run_id` arm reads every job, blocking `runs.create`, `runs.bind`,
`runs.install`, `runs.boot`, `control.power`, the snapshot tools and `systems.teardown` on that
System for the duration. Any project `CONTRIBUTOR` can call the tool, and a denied caller's own
suggested next action is to call it again.

Exposure today is nil: the tool returns `configuration_error` with
`reason=recovery_executor_unavailable` before it can be reached in anger, and nothing on this
branch creates an activation. The cost becomes real when #2118 lands the executor.

## Why deferred

Closing it needs an index on `jobs((payload->>'run_id'))` — a new migration. Issue #2117's
frozen surface covers the admission matrix, its call sites, and the three agent contracts; it
does not cover schema changes, and adding a migration to reach an index is exactly the kind of
surface expansion the charter excludes. The index also belongs with the work that makes the
path reachable: #2118 owns the recovery executor, and it is the change that turns this from a
measured cost into an observable one.

Moving the read outside the advisory lock was considered and rejected. It would remove the
liveness cost today, but the check is a release precondition: once #2118 makes the release
commit a transition, deciding it against a job set read outside the lock is a race. Keeping the
read where correctness needs it, and recording the cost, leaves #2118 a sound structure to
build on rather than one it must first undo.

## Non-regression boundary

This change must not make the scan worse or reintroduce the false claim:

- `_ACTIVE_JOBS_SQL` stays two separately-planned arms. Recombining them into one `OR` returns
  the `system_id` arm to a full `jobs_pkey` walk — 201041 buffers against 3.
- No global `ORDER BY` returns. It is what stopped the `LIMIT` from ending the original
  statement early, and the refusal needs existence, one page of ids, and whether the cap bit,
  never a particular page.
- The comment at `_ACTIVE_JOBS_SQL` and the design spec both state what the planner actually
  does per arm, including that the `run_id` arm scans. Neither may claim the arm is bounded.

Held by the comment in the source, the corrected paragraph in
`docs/workflow/specs/2026-08-29-external-boot-admission-agent-contracts-design.md`, and this
record. No test asserts a query plan; a plan assertion would be tied to row counts and planner
version and would be a flake generator, which is why the boundary is documentary.

## What would resolve it

A migration adding an index on `jobs((payload->>'run_id'))`, after which the `run_id` arm
plans as an index scan and the whole query is bounded by matches rather than by table size.

Done when the arm's `EXPLAIN (ANALYZE, BUFFERS)` on a `jobs` table of the size above, with no
matching row, reports an index scan and buffer counts in the same order as the `system_id`
arm's 3 rather than the 2861 measured here — and the comment and the spec paragraph are
updated to say so.

## Provenance

target: src/kdive/mcp/tools/external_boot/recovery_requests.py
target: docs/workflow/specs/2026-08-29-external-boot-admission-agent-contracts-design.md

Raised as a `high` finding in the #2117 branch review (gauntlet run
`gauntlet-2117-c9a6271-20260902T1935Z-7b1d4e`, 2026-09-02), which refuted the index claim the
branch was carrying. The query-shape half was fixed in that round; this record owns the
residual.

tracker: #2118
