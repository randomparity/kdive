# Teardown recycle policy (#2978) — implementation plan

Goal: implement [the design](../specs/2026-09-30-teardown-recycle-policy-2978-design.md).
Architecture: a required `recycle` argument on `enqueue_control_teardown`; a break-glass refusal;
an orphan-lane skip plus a read-only warning lane in the reconciler catalog. Stack: Python 3,
psycopg async, pytest against a migrated Postgres (`migrated_url` fixture).

Expected implementation size: 150–230 changed lines (S) — three source files, one catalog line,
the ADR amendment, and ~110 lines of tests across two test files.

## Global Constraints

- No migration (0165 is reserved by #2980), no new dependency, no new ADR number.
- Guardrails: `just lint`, `just type`, `just test-verbose <paths>`, `just records`, `just ci`.
- ADR-0435 is append-only: add one dated `### Amendment (2026-09-30)` section at the end.

## Task 1 — `enqueue_control_teardown` recycle and break-glass

Files: `src/kdive/services/systems/authority_owned.py`, `src/kdive/jobs/service_operations.py`,
`src/kdive/reconciler/repairs/systems.py` (call site only),
`src/kdive/mcp/tools/ops/security/breakglass.py`, `tests/mcp/ops/test_breakglass.py`.

Interfaces: `enqueue_control_teardown(conn, system, authorizing, *, recycle:
queue.JobRecyclePolicy) -> Job` (`queue` is already imported in `authority_owned.py`).

Verification:
- Break-glass recycle — Mode: focused-test. New
  `test_force_teardown_recycles_dead_lettered_job[ready|failed]`: seed a System, call
  `force_teardown`, set the row `state='failed', attempt=max_attempts,
  error_category='conflict'`, call again; expect `status == "queued"`, same `object_id`, row
  `attempt == 0`. Red: the second call returns `status == "failed"`.
- Break-glass refusal — Mode: focused-test. New
  `test_force_teardown_refuses_reprovisioning[none|queued|failed]`: seed `REPROVISIONING`, seed
  the row per param, call; expect `error_category == "conflict"`,
  `data["current_status"] == "reprovisioning"`, row unchanged (or absent). Red: a job is queued.
- Green: `just test-verbose tests/mcp/ops/test_breakglass.py`.

Steps:
1. Write both tests (seed the prior row by calling `force_teardown` on a `READY` System, then
   `UPDATE systems SET state=...` directly, since `update_state` enforces the transition graph).
2. Run them; expect the red observations above.
3. Add `*, recycle: queue.JobRecyclePolicy` to `enqueue_control_teardown`, pass
   `recycle=recycle` to `queue.enqueue`, and state in the docstring that the preactivation branch
   ignores it.
4. `service_operations.enqueue_teardown`: pass `recycle=queue.JobRecyclePolicy.NEVER` with a
   comment that investigation force-close replays by decision (#2978). The reconciler call site
   passes `NEVER` too (Task 2 makes it unreachable for a failed row).
5. In `_teardown_locked`, after the `TORN_DOWN` branch:
   ```python
   if system.state is SystemState.REPROVISIONING:
       return ToolResponse.failure(
           str(uid),
           ErrorCategory.CONFLICT,
           detail="System is mid-reprovision; retry ops.force_teardown once it settles",
           suggested_next_actions=["systems.get"],
           data={"current_status": system.state.value},
       )
   ```
   and pass `recycle=queue.JobRecyclePolicy.FAILED` to `enqueue_control_teardown`.
6. Run the green command, `just lint`, `just type`; commit `fix(ops): force_teardown re-runs a
   dead-lettered teardown (#2978)`.

## Task 2 — orphan lane skips a failed row and the stranded-teardown warning lane

Files: `src/kdive/reconciler/repairs/systems.py`, `src/kdive/reconciler/loop.py`,
`tests/reconciler/test_orphan_teardown_warning.py` (new),
`docs/adr/0435-reclaim-failed-provision-artifacts.md`.

Interfaces: `report_stranded_orphan_teardowns(conn: AsyncConnection) -> int`; catalog name
`stranded_orphan_teardowns`, placed immediately after `abandoned_jobs`.

Verification:
- Orphan lane leaves a failed row — Mode: focused-test.
  `test_orphan_lane_leaves_failed_teardown_row`: `seed_system(READY, RELEASED)`, insert the row
  via `repair_orphaned_systems`, mark it `failed`; a second run returns 0 and the row
  (`state, attempt, updated_at`) is unchanged.
- Warning once per failure — Mode: focused-test. `test_stranded_teardown_warns_once_per_failure`
  with `caplog` at WARNING on `kdive.reconciler.repairs.systems`: counts 1, 0, then after
  `UPDATE jobs SET updated_at = now() + interval '1 second'` 1 again; one warning line per `1`,
  containing the System id and `systems.teardown`. Red: `AttributeError` (no function).
- `tearing_down` excluded — Mode: focused-test. Same seed with `TEARING_DOWN`; returns 0.
- Metric wiring — Mode: focused-test. `test_stranded_lane_is_cataloged_after_abandoned_jobs`:
  `"stranded_orphan_teardowns" in loop.ALL_REPAIR_KINDS` and its index is greater than
  `abandoned_jobs`'. The existing `test_reconcile_now` / label-bound tests cover the counter.
- ADR amendment — Mode: task-test-not-applicable: prose record; `just records` validates only its
  append-only shape, which is run below.
- Green: `just test-verbose tests/reconciler/test_orphan_teardown_warning.py
  tests/reconciler/test_loop.py tests/mcp/ops/test_reconcile_now.py
  tests/observability/test_label_value_bounds.py`.

Steps:
1. Write the four tests; a fixture clears `system_repairs._warned_stranded_teardowns` around each.
2. Run; expect the red observations.
3. In `repair_orphaned_systems`: add to the candidate query
   `AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dedup_key = s.id::text || ':teardown'
   AND j.state = %s)` with `JobState.FAILED.value`; replace the recheck's `SELECT 1` with
   `SELECT state FROM jobs ...`, `continue` when it is `failed`, and keep `already_queued` for
   the log. Rewrite the comment above `_ORPHAN_TEARDOWN_SKIPPED_STATE_VALUES` to say a failed
   row is left alone and reported by `report_stranded_orphan_teardowns`.
4. Add the lane:
   ```python
   _STRANDED_TEARDOWN_EXCLUDED_STATE_VALUES = (
       *_ORPHAN_TEARDOWN_SKIPPED_STATE_VALUES,
       SystemState.TEARING_DOWN.value,
   )
   _warned_stranded_teardowns: dict[UUID, datetime] = {}


   async def report_stranded_orphan_teardowns(conn: AsyncConnection) -> int:
       async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
           await cur.execute(
               "SELECT s.id, j.id AS job_id, j.error_category, j.updated_at FROM systems s "
               "JOIN allocations a ON a.id = s.allocation_id "
               "JOIN jobs j ON j.dedup_key = s.id::text || ':teardown' "
               "WHERE s.state <> ALL(%s) AND a.state = ANY(%s) AND j.state = %s",
               (
                   list(_STRANDED_TEARDOWN_EXCLUDED_STATE_VALUES),
                   list(_TERMINAL_ALLOCATION_STATE_VALUES),
                   JobState.FAILED.value,
               ),
           )
           rows = await cur.fetchall()
       new = [r for r in rows if _warned_stranded_teardowns.get(r["id"]) != r["updated_at"]]
       _warned_stranded_teardowns.clear()
       _warned_stranded_teardowns.update({r["id"]: r["updated_at"] for r in rows})
       for r in new:
           _log.warning(
               "reconciler: orphaned system %s has a failed teardown job %s (%s); "
               "it needs an operator systems.teardown",
               r["id"],
               r["job_id"],
               r["error_category"],
           )
       return len(new)
   ```
5. `loop.py`: alias `_report_stranded_orphan_teardowns` and add
   `_RepairCatalogEntry("stranded_orphan_teardowns", lambda _r, _c, _g:
   _report_stranded_orphan_teardowns)` right after the `abandoned_jobs` entry.
6. Append the ADR-0435 amendment: the policy of each ordinary enqueuer (`systems.teardown`
   FAILED; `ops.force_teardown` FAILED plus the reprovisioning refusal; `repair_orphaned_systems`
   never enqueues over a failed row and the stranded lane warns once per failure with the
   counter; investigation force-close `NEVER`; `repair_stalled_tearing_down_systems` TERMINAL).
   It supersedes the #2929 amendment's "`ops.force_teardown` keeps replaying" sentence.
7. Run the green command, `just lint`, `just type`, `just records`; commit.
