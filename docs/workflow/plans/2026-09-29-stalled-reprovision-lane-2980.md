# Plan: reconciler settles a stalled `reprovisioning` System (#2980)

Goal: a new reconciler lane moves a `reprovisioning` System that no reprovision job can finish to
`failed`, records `reprovision_incomplete`, and writes an audit row.
Spec: [`../specs/2026-09-29-stalled-reprovision-lane-2980-design.md`](../specs/2026-09-29-stalled-reprovision-lane-2980-design.md).

Architecture: this is a fifth stalled-state lane in `reconciler/repairs/systems.py`. It is shaped
like `repair_stalled_restoring_systems` and uses the `_TEARDOWN_SETTLE` window. A new
`ErrorCategory` member ships with migration 0166.
Tech stack: Python 3.14, psycopg 3 async, PostgreSQL, pytest.

Expected implementation size: 220–300 changed lines (M) — two new test modules of about 150
lines, 70 lines of lane code, 60 lines of migration, and one-line edits to seven migration lists.

## Global Constraints

- Migration number `0166` is reserved for this change. It is forward-only (ADR-0015) and drops and
  recreates the constraints under their existing names.
- ADR sections are append-only. The amendment is appended to ADR-0435 (`just records`).
- Guardrails: `just lint`, `just type`, `just test-verbose <paths>`, `just records`,
  `just docs-check`, `just resources-docs-check`, and `just ci` before push.
- Keep the diff to the new lane. #2978 edits the same module next.

## File map

| File | Change | Criterion |
|---|---|---|
| `src/kdive/domain/errors.py` | `REPROVISION_INCOMPLETE` + retryable `False` | category |
| `src/kdive/db/schema/0166_reprovision_incomplete_category.sql` | widen the four CHECKs | category |
| `src/kdive/jobs/handlers/external_boot/runner.py` | docstring counts 24→25, 7→8 uncommittable | category |
| `src/kdive/reconciler/repairs/systems.py` | lane + `_limbo_category` generalization | lane |
| `src/kdive/reconciler/loop.py` | alias + catalog entry after `stalled_restoring_systems` | lane |
| `docs/guide/errors.md` → `just resources-docs` | new section | category |
| `docs/adr/0435-reclaim-failed-provision-artifacts.md` | appended amendment | record |
| `tests/domain/test_errors.py`, `tests/mcp/core/test_responses.py` | enum + retryable pins | category |
| `tests/db/test_migrate.py` + three `test_migration_0091/0102/0115_*.py` | `0166` in tail lists | migration |
| `tests/reconciler/test_stalled_reprovision_recovery.py` (new) | lane arms | lane |
| `tests/mcp/lifecycle/test_systems_tools.py` | settle → teardown end to end | teardown reachable |

## Task 1 — `reprovision_incomplete` category and migration 0166

**Verification**
- `Mode: focused-test`. Contract: every CHECK admits the whole `ErrorCategory`
  (`tests/db/test_migrate.py::test_check_constraint_covers_every_enum_value`, parametrized over `CHECK_ENUMS`).
  Red: after the enum is added but before 0166 exists, the four cases fail. Green:
  `just test-verbose tests/db/test_migrate.py`.
- `Mode: focused-test`. Contract: the category is non-retryable. Red: add
  `test_reprovision_incomplete_is_not_retryable` to `tests/mcp/core/test_responses.py` (a copy of
  `test_restore_incomplete_is_not_retryable`) and add the entry to the expected table; both fail
  until `errors.py` changes. Green: `just test-verbose tests/mcp/core/test_responses.py
  tests/domain/test_errors.py`.
- `Mode: focused-test`. Contract: the migration tails list 0166. Green:
  `just test-verbose tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Steps:
1. In `tests/domain/test_errors.py`, add `"reprovision_incomplete"` to a
   `REPROVISION_ADDED` set and union it into `M0_ALL`. In `test_responses.py`, add the pin and the
   test above. Run them and expect red.
2. In `errors.py`, after `RESTORE_INCOMPLETE`, add a comment citing #2980 and ADR-0435, then
   `REPROVISION_INCOMPLETE = "reprovision_incomplete"`, and add
   `ErrorCategory.REPROVISION_INCOMPLETE: False` to `RETRYABLE_BY_CATEGORY`.
3. Create `0166_reprovision_incomplete_category.sql`. Copy the header and four `DROP`/`ADD` pairs
   from `0086_restore_incomplete_category.sql` exactly, and append `'reprovision_incomplete'` after
   `'restore_incomplete'` in each list. The header names #2980 and ADR-0435 and says only
   `systems` can hold the value.
4. Append `"0166"` / `("0166", "0166_reprovision_incomplete_category.sql")` after every `0165`
   entry in `tests/db/test_migrate.py` (four lists) and in the three `test_migration_0*` tails.
5. Change the `runner.py` docstring: `ErrorCategory` has 25 members, eight are not committable,
   and `reprovision_incomplete` joins the list. Make the matching count change in the
   `tests/jobs/handlers/external_boot/test_runner.py` docstring (line ~1350).
6. Run the green commands. Commit `feat(errors): add reprovision_incomplete failure category`.

## Task 2 — the lane

Interfaces: consumes `ErrorCategory.REPROVISION_INCOMPLETE` (Task 1). Produces
`repair_stalled_reprovisioning_systems(conn: AsyncConnection) -> int` and the catalog name
`stalled_reprovisioning_systems`. Also produces
`_limbo_category(conn, system_id: UUID, kind: JobKind, verdict: ErrorCategory) -> ErrorCategory | None`,
which replaces `_restore_limbo_category`; its only caller is `repair_stalled_restoring_systems`.

**Verification** (`Mode: focused-test`, new file `tests/reconciler/test_stalled_reprovision_recovery.py`,
green: `just test-verbose tests/reconciler/test_stalled_reprovision_recovery.py tests/reconciler/test_snapshot_repairs.py`).
Seed with `seed_system(conn, system_state=REPROVISIONING)` and a `_seed_job(conn, sid, state=,
error_category=None, age_seconds=0)` helper. The helper INSERTs `kind='reprovision'`, payload
`{"system_id", "profile_digest": "d"}`, a unique dedup key, and a backdated `updated_at` in the
INSERT, because the `jobs_set_updated_at` trigger overwrites an UPDATE. Run each arm through
`run_repair` on an `AsyncConnectionPool`. Before the lane exists, collection fails with
`ImportError`.
- no job → 1 settled, `failed`, `reprovision_incomplete`, one `audit_log` row
  `systems/reprovisioning->failed`;
- `failed` + NULL category, age 0 → settled with the category (no window);
- `failed` + `configuration_error` → settled, category NULL (ADR-0513 §1a);
- `failed` + `infrastructure_failure` at `attempt = 1`, age 0 → settled (no window);
- parametrized over `canceled`, `failed`/`lease_expired`, and `failed`/`infrastructure_failure` at
  `attempt = 3`: at age 0 → 0 settled and still `reprovisioning`, with category NULL; at age
  16 min → settled (`lease_expired` → category stamped). `_seed_job` takes an `attempt=1`
  keyword;
- parametrized over `queued` and `running` at age 16 min → untouched;
- an old canceled job beside a fresh `running` job → untouched;
- a `ready` System with a failed reprovision job → untouched;
- race: monkeypatch `repairs_systems.advisory_xact_lock` so that, once acquired, it inserts a
  `running` job over a second autocommit connection → 0 settled (the pattern in
  `test_leaked_mutation_obligation_repair.py::test_teardown_job_appearing_under_the_lock_defers_the_candidate`);
- reconciler role: run through `authority_role_dsns("kdive_reconciler")` → settled;
- ordering: `ALL_REPAIR_KINDS.index("abandoned_jobs") < index("stalled_reprovisioning_systems")`.

Controlled faults, each run after committing and reverted by hand-editing the lane module only,
never with `git checkout`: drop the `canceled` clause, drop the `lease_expired` clause, drop the
`attempt > 1` clause, drop the locked recheck, and stamp the category unconditionally. Each must turn at least one arm red.

Steps:
1. Write the test file; expect `ImportError`.
2. In `systems.py`, below `_STALLED_TEARING_DOWN_REPAIR_LIMIT`, add the lane's constants: the
   shared `_REPROVISION_BLOCKING` `EXISTS` fragment
   `j.kind = %s AND j.payload->>'system_id' = s.id::text AND (j.state = ANY(%s) OR ((j.state = %s
   OR (j.state = %s AND (j.error_category = %s OR j.attempt > 1))) AND j.updated_at > now() - %s))`
   with its six parameters (kind, active states, `canceled`, `failed`, `lease_expired`,
   `_TEARDOWN_SETTLE`), the candidate SQL
   (`s.state = %s AND NOT <fragment> ORDER BY s.id LIMIT %s`), the recheck SQL
   (`SELECT s.project … WHERE s.id = %s AND s.state = %s AND NOT <fragment>`),
   `_REPROVISION_BLOCKING_PARAMS`, and `_STALLED_REPROVISIONING_REPAIR_LIMIT = 100`. Add a comment
   explaining why canceled and lease-lapsed rows wait: the handler keeps running (spec §Why).
3. Add `repair_stalled_reprovisioning_systems` after `repair_stalled_restoring_systems`. It selects
   candidates in one transaction, then handles each under
   `conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, id)`: recheck (`continue` when
   there is no row), `SYSTEMS.update_state(conn, id, SystemState.FAILED)`,
   `_limbo_category(conn, id, JobKind.REPROVISION, ErrorCategory.REPROVISION_INCOMPLETE)` (record
   when not `None` via `record_system_failure_category`), then
   `audit.record_system(... tool="systems.reprovision", transition="reprovisioning->failed",
   args={"system_id": str(id)}, project=row["project"])`. Wrap each candidate in
   `try/except Exception` with a `noqa: BLE001` justification and a warning log, as
   `repair_stalled_tearing_down_systems` does. Return the settled count.
4. Rename `_restore_limbo_category` to `_limbo_category` with `kind`/`verdict` parameters and
   generalize its docstring and log line. Update the restoring caller to pass
   `(JobKind.RESTORE, ErrorCategory.RESTORE_INCOMPLETE)`.
5. In `loop.py`, add `_repair_stalled_reprovisioning_systems = system_repairs.repair_stalled_reprovisioning_systems`
   and a `_RepairCatalogEntry("stalled_reprovisioning_systems", …)` right after
   `stalled_restoring_systems`, with a comment that it runs after `abandoned_jobs` (#2980).
6. Run green, including `tests/reconciler/test_loop.py` (the `ALL_REPAIR_KINDS` plan equality).
   Commit `feat(reconciler): settle a stalled reprovisioning System`, then run the controlled
   faults.

## Task 3 — teardown reachable, docs, ADR amendment

**Verification**
- `Mode: focused-test`. Contract: after the lane settles a System, `systems.teardown` enqueues
  and tears down. Add `test_teardown_after_stalled_reprovision_settles` to the #2928 block of
  `tests/mcp/lifecycle/test_systems_tools.py`. It seeds `_seed_teardown_system(pool, alloc,
  REPROVISIONING)` and a `failed` reprovision job, asserts `_teardown` returns `conflict`, runs
  `repair_stalled_reprovisioning_systems`, asserts `_teardown` returns `queued`, and runs
  `teardown_handler`. The System stays `failed`, and `prov.torn_down == [f"kdive-{sys_id}"]`.
  Red: with the lane call removed, the second `_teardown` is `conflict`. Green:
  `just test-verbose tests/mcp/lifecycle/test_systems_tools.py -k reprovision`.
- `Mode: task-test-not-applicable`. Surface: the prose of the `docs/guide/errors.md` section and
  the ADR amendment. Reason: no executable consumer reads the wording. The snapshot copy is
  checked by `just resources-docs-check`, and the ADR structure by `just records`.

Steps:
1. Write the test and see the second assertion fail when the repair call is commented out. Restore
   the call.
2. In `docs/guide/errors.md`, after the restore section, add `## An incomplete reprovision`.
   `reprovision_incomplete` means the reconciler found a System stuck `reprovisioning` with no
   reprovision job able to finish it, and no more specific job category. The System is `failed`,
   its disk may be indeterminate, and the category is not retryable. One cause is a
   `systems.reprovision` with a previously applied profile, which replays that profile's old job
   instead of running a new one. Recovery: `systems.teardown` reclaims provider resources and
   leaves the System `failed`; then use `allocations.release` and `allocations.request`. In the
   restore section, replace "and ordinary `systems.teardown` cannot complete from that state" with
   the same teardown description, so the two sections agree. Run `just resources-docs`.
3. Append `### Amendment (2026-09-29): the reconciler settles a stalled reprovision (#2980)` to
   ADR-0435. Summarize the spec's design: the blocking predicate, the category and its §1a
   precedence, why `lease_expired` gets the window, no auto-teardown, and a link to the spec.
4. Run `just records`, `just docs-check`, and `just resources-docs-check`. Commit
   `docs: record the stalled-reprovision lane`.

## Rollback

Before reverting the code, run
`UPDATE systems SET failure_category = NULL WHERE failure_category = 'reprovision_incomplete'`.
Otherwise the reverted `ErrorCategory` cannot validate those rows, and every `SYSTEMS.get` of them
fails. The ADR-0454 job fallback then reports them. Then revert the commits. Migration 0166 is
forward-only, and its widened CHECKs stay in place.
