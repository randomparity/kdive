# Teardown recycle policy (#2978) — implementation plan

Goal: implement [the design](../specs/2026-09-30-teardown-recycle-policy-2978-design.md).
Architecture: a required `recycle` argument on `enqueue_control_teardown` that never recycles an
authority-marked row; a break-glass refusal; a read-only warning lane in the reconciler catalog.
Stack: Python 3, psycopg async, pytest against a migrated Postgres (`migrated_url` fixture).

Expected implementation size: 140–200 changed lines (S) — three source files, one catalog entry,
the ADR amendment, and ~100 lines of tests across three test files.

## Global Constraints

- No migration (0165 is reserved by #2980), no new dependency, no new ADR number.
- Guardrails: `just lint`, `just type`, `just test-verbose <paths>`, `just records`, `just ci`.
- ADR-0435 is append-only: add one dated `### Amendment (2026-09-30)` section at the end.

## Task 1 — `enqueue_control_teardown` recycle and break-glass

Files: `src/kdive/services/systems/authority_owned.py`, `src/kdive/jobs/service_operations.py`,
`src/kdive/reconciler/repairs/systems.py` (call site and comment),
`src/kdive/mcp/tools/ops/security/breakglass.py`, `tests/mcp/ops/test_breakglass.py`,
`tests/reconciler/test_loop.py` (stub at `test_orphaned_system_failure_does_not_starve_sibling`).

Interfaces: `enqueue_control_teardown(conn, system, authorizing, *, recycle:
queue.JobRecyclePolicy) -> Job`; `queue` and `_dedup_job(conn, dedup_key) -> Job | None` already
exist in `authority_owned.py`.

Verification:
- Break-glass recycle — Mode: focused-test. `test_force_teardown_recycles_dead_lettered_job`
  (params `ready`, `failed`): call `force_teardown` on a `READY` System, then
  `UPDATE jobs SET state='failed', attempt=max_attempts, error_category='conflict'` (and
  `UPDATE systems SET state=...` for `failed`), call again; expect `status == "queued"`, same
  `object_id`, `attempt == 0`. Red: the second call returns `status == "failed"`.
- Marked row kept — Mode: focused-test. `test_force_teardown_keeps_failed_authority_marked_row`:
  as above, then also set `payload = payload || '{"authority_system_v1": {...}}'` copied from a
  preactivation teardown payload (reuse the file's `_mark_authority_owned` flow on a second System
  to obtain one); expect state and payload unchanged. Red after the recycle change, green after
  the marker guard.
- Refusal — Mode: focused-test. `test_force_teardown_refuses_reprovisioning` (params: no row,
  failed row): expect `error_category == "conflict"`, `data["current_status"] ==
  "reprovisioning"`, row unchanged or absent. Red: a job is queued/recycled.
- Green: `just test-verbose tests/mcp/ops/test_breakglass.py tests/reconciler/test_loop.py`.

Steps:
1. Write the tests; run; observe red.
2. `enqueue_control_teardown`: add `*, recycle`; on the ordinary branch, when `recycle` is not
   `NEVER`, read `prior = await _dedup_job(conn, key)` and use `NEVER` if `prior` carries
   `authority_system_v1` or `EXTERNAL_BOOT_AUTHORITY_MARKER_KEY` (`kdive.jobs.payloads`) in its
   payload; pass `recycle=` to `queue.enqueue`.
3. `service_operations.enqueue_teardown` and `repair_orphaned_systems` pass `NEVER`; rewrite the
   comment above `_ORPHAN_TEARDOWN_SKIPPED_STATE_VALUES` to say the lane leaves a failed row alone
   and `report_stranded_orphan_teardowns` reports it. The test_loop stub takes and forwards
   `**kwargs`.
4. `_teardown_locked`: after the `TORN_DOWN` branch, return
   `ToolResponse.failure(str(uid), ErrorCategory.CONFLICT, detail="System is mid-reprovision;
   retry ops.force_teardown once it settles", suggested_next_actions=["systems.get"],
   data={"current_status": system.state.value})` for `REPROVISIONING`; pass
   `recycle=queue.JobRecyclePolicy.FAILED`.
5. Green command, `just lint`, `just type`; commit.

## Task 2 — stranded-teardown warning lane and ADR amendment

Files: `src/kdive/reconciler/repairs/systems.py`, `src/kdive/reconciler/loop.py`,
`tests/reconciler/test_orphan_teardown_warning.py` (new),
`docs/adr/0435-reclaim-failed-provision-artifacts.md`.

Interfaces: `report_stranded_orphan_teardowns(conn: AsyncConnection) -> int`; module dict
`_warned_stranded_teardowns: dict[UUID, datetime]`; catalog entry `stranded_orphan_teardowns`
immediately after `abandoned_jobs`.

Verification:
- Orphan lane leaves a failed row — Mode: focused-test, characterization (no red phase: `NEVER`
  already holds; it guards a future `FAILED` at that call site).
  `test_orphan_lane_leaves_failed_teardown_row`: `seed_system(READY, RELEASED)`, run
  `repair_orphaned_systems`, mark the row `failed`, run again; `(state, attempt, updated_at)`
  unchanged.
- Warning and metric once per failure — Mode: focused-test.
  `test_stranded_teardown_warns_once_per_failure`: same seed; `reconcile_once(pool,
  FakeReaper(), config=make_reconcile_config())` three times, bumping the row's `updated_at` by
  one second before the third; `repair_counts["stranded_orphan_teardowns"]` is 1, 0, 1 and
  `caplog` holds exactly one matching WARNING per 1, naming the System and `systems.teardown`.
  Red: `KeyError` on the missing repair kind.
- `tearing_down` excluded — Mode: focused-test. Same seed with `TEARING_DOWN`, calling
  `report_stranded_orphan_teardowns` directly; returns 0.
- ADR amendment — Mode: task-test-not-applicable: prose record; `just records` checks its
  append-only shape.
- Green: `just test-verbose tests/reconciler/test_orphan_teardown_warning.py
  tests/reconciler/test_loop.py tests/mcp/ops/test_reconcile_now.py
  tests/observability/test_label_value_bounds.py`.

Steps:
1. Write the tests with an autouse fixture that clears `_warned_stranded_teardowns`; run; red.
2. Add the lane: one query joining `systems`, `allocations`, and `jobs` on
   `j.dedup_key = s.id::text || ':teardown'`, filtered by `s.state <> ALL(excluded)`,
   `a.state = ANY(_TERMINAL_ALLOCATION_STATE_VALUES)`, `j.state = 'failed'`, selecting the marker
   presence; warn for rows whose `updated_at` differs from the dict entry; replace the dict
   contents with the current rows; return the count warned.
3. `loop.py`: alias the function and add the catalog entry after `abandoned_jobs`.
4. Append the ADR-0435 amendment listing each ordinary enqueuer's policy (`systems.teardown`
   FAILED; `ops.force_teardown` FAILED on unmarked rows plus the reprovisioning refusal;
   `repair_orphaned_systems` NEVER plus the warning lane; investigation force-close NEVER;
   `repair_stalled_tearing_down_systems` TERMINAL), superseding the #2929 amendment's
   "`ops.force_teardown` keeps replaying" sentence.
5. Green command, `just lint`, `just type`, `just records`; commit.
