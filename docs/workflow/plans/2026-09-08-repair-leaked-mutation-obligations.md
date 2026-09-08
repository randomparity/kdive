# Repair leaked System mutation obligations

**Goal.** Give the reconciler a lane that discharges mutation obligations left open on Systems
already in `torn_down`, so those attempts leave the retained-owner set and their volumes stop being
held out of the module-volume reaper.

**Architecture.** One new async repair function in the reconciler's System-repair module, selecting
candidates with a single SQL predicate and discharging each under the System advisory lock through
ADR-0629's existing `SECURITY DEFINER` function. One entry in the reconciler's repair catalog
registers it — the catalog is the loop's only extension point, and `ALL_REPAIR_KINDS` derives from
it.

**Tech stack.** Python 3.14, `uv`, `psycopg` (async) against PostgreSQL, `pytest` with disposable
Postgres via testcontainers. Design record:
`docs/adr/0634-leaked-mutation-obligations-repaired-by-reconciler.md`. Specification:
`docs/workflow/specs/2026-09-08-repair-leaked-mutation-obligations-design.md`.

Expected implementation size: 200–280 changed lines (M) — the file map below: ~65 added lines in
`repairs/systems.py`, ~10 in `loop.py`, and one ~185-line test module; design artifacts excluded.

## Global Constraints

- Ruff line length **100**; lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults over the
  **whole tree**, `src` and `tests` alike.
- **Never invent an `ErrorCategory` string.** This change records no error category at all; the
  repair reports a count and logs.
- **This change writes no migration: add no file under `src/kdive/db/schema/`.**
- **No commit may cite a `Proposed` ADR from `src/` or `tests/`.**
  `scripts/guards/check_adr_status.py` fails any such commit, and no pre-commit hook runs it, so the
  failure appears only in CI or a bare `just ci`
  (`docs/solutions/2026-09-04-adr-status-flip-must-share-the-first-citation-commit.md`). ADR-0634 is
  already committed as `Accepted (2026-09-08)` with no citation, so the implementation commit adds
  the first citations against an already-Accepted record. Never set it back to `Proposed`.
- **Run guardrails bare.** No `| tail`, no `| head`, no `>/dev/null`, no `|| true`, and never a
  trailing `; echo $?` — a pipeline reports the last command's status, so the gate's own status is
  lost. To capture output, redirect: `just ci > <file> 2>&1 < /dev/null`.
- **Before `git commit`**, for a Python-only change run `just format`; for anything else stage the
  paths, run `prek run`, then `git add --` exactly the paths that were staged (never `git add -A`).
- Commits follow Conventional Commits 1.0.0, imperative, subject ≤72 characters.
- Prose rule, project-wide: use **Milestone**, never "Sprint"; avoid "critical", "robust",
  "comprehensive", "elegant" in ADRs, specs, commit messages, and code comments.

## File map

| Path | Created / changed | Answerable for |
|---|---|---|
| `src/kdive/reconciler/repairs/systems.py` | changed | The candidate predicate, the settle window, and the per-System discharge. |
| `src/kdive/reconciler/loop.py` | changed | Registering the repair in the catalog, after `abandoned_jobs`. |
| `tests/reconciler/test_leaked_mutation_obligation_repair.py` | created | Every success criterion that is a runtime behaviour of the lane. |

## Task 1 — the reconciler lane that discharges leaked mutation obligations

Creates: `tests/reconciler/test_leaked_mutation_obligation_repair.py`.
Modifies: `src/kdive/reconciler/repairs/systems.py`, `src/kdive/reconciler/loop.py`.
Reads, already committed by the design phase:
`docs/adr/0634-leaked-mutation-obligations-repaired-by-reconciler.md`,
`docs/workflow/specs/2026-09-08-repair-leaked-mutation-obligations-design.md`.

**Where this fits.** This is the whole change. There is no earlier or later task; the repair
function and its catalog entry are one deliverable, because the function without the entry is code
the reconciler never runs.

### Interfaces

Consumed from the existing codebase, each confirmed to exist with this signature:

- `kdive.db.remote_module_attempt_obligations.RemoteModuleAttemptObligationRepository` — class with
  `async def worker_discharge_system_mutation_obligations(self, conn: AsyncConnection, system_id: UUID) -> int`
  (`src/kdive/db/remote_module_attempt_obligations.py:327-343`). It takes
  `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)` itself and returns the number of rows
  discharged.
- `kdive.db.locks.advisory_xact_lock(conn: AsyncConnection, scope: LockScope, key: UUID | str)` —
  an `asynccontextmanager` that acquires `pg_advisory_xact_lock` and **does not release on block
  exit**; the lock is released by the surrounding transaction (`src/kdive/db/locks.py:94-121`). It
  is therefore safe to hold it here and let the repository method acquire the same key again, which
  is exactly what `src/kdive/jobs/handlers/system_reclaim.py:107-115` already does.
- `kdive.domain.capacity.state.SystemState.TORN_DOWN` — value `"torn_down"`, and terminal: it has an
  empty outgoing edge set, which is why the locked re-read below checks only the job half.
- `_ACTIVE_JOB_STATE_VALUES` — module-private tuple already defined at
  `src/kdive/reconciler/repairs/systems.py:27`, equal to
  `(JobState.QUEUED.value, JobState.RUNNING.value)`.
- `jobs.dedup_key` is `NOT NULL UNIQUE` (`src/kdive/db/schema/0001_init.sql:166,169`) and the table
  carries the `jobs_set_updated_at` trigger (`:171-172`), so one row holds a System's whole teardown
  history and its `updated_at` moves only when that teardown is written.
- `_RepairCatalogEntry(name: str, factory: Callable[[InfraReaper, ReconcileConfig, timedelta], _AnyRepairFn | None], report_field: str | None = None)`
  — frozen dataclass at `src/kdive/reconciler/loop.py:236-240`.

The public names this task adds are
`kdive.reconciler.repairs.systems.repair_leaked_mutation_obligations`, with signature
`async def repair_leaked_mutation_obligations(conn: AsyncConnection) -> int`, and the repair-kind
name `"leaked_mutation_obligations"`.

### Verification

Every entry is `Mode: focused-test` in
`tests/reconciler/test_leaked_mutation_obligation_repair.py` unless it names another file. Each
green command is `uv run python -m pytest <node-id> -q`; run the fault, observe the stated red,
revert it, and re-run to green.

1. **A torn-down System with an open obligation and no teardown job is discharged with reason
   `terminal_escape`, and no bystander is touched.**
   `::test_leaked_obligation_on_torn_down_system_is_discharged`. Red before the implementation:
   `AttributeError: module 'kdive.reconciler.repairs.systems' has no attribute
   'repair_leaked_mutation_obligations'` at import. Red if the discharge loses its `system_id`
   bound: the bystander's `mutation_discharged_at` read is not `None`.
2. **An active teardown job defers the repair.**
   `::test_active_teardown_job_defers_the_repair`, parametrized over `queued` and `running`. Red
   with the `j.state = ANY(%s)` term removed: `assert 0 == 1` on the count.
3. **A terminal-but-recent teardown job defers the repair** — the operator-cancel window of ADR-0634
   Context. `::test_recently_terminal_teardown_job_defers_the_repair`, parametrized over `canceled`
   and `failed`, each inserted with a fresh `updated_at`. Red with the
   `j.updated_at > now() - %s` term removed: `assert 0 == 1` on the count, and the row's
   `mutation_discharged_at` is not `None`.
4. **A teardown job terminal for longer than the settle window does not defer it.**
   `::test_settled_terminal_teardown_job_does_not_defer_the_repair`, forcing the job's `updated_at`
   an hour into the past. Red if the window is applied to every terminal job regardless of age:
   `assert 0 == 1` on the count. This is the arm that keeps entry 3's fix from disabling the lane.
5. **A System that is not `torn_down` is never discharged.**
   `::test_non_terminal_system_is_untouched`. Red with the `s.state = %s` term removed:
   `assert 0 == 1` on the count.
6. **First-write-wins is preserved.** `::test_second_pass_is_a_noop`. Red if the repair re-wrote
   discharged rows: the second `(mutation_discharged_at, mutation_discharge_reason)` read differs
   from the first.
7. **The count is obligation rows, not Systems.** `::test_count_is_obligation_rows`, seeding one
   System with two open obligations (two distinct `operation_nonce` values on the same run). Red
   with `repaired += 1`: `assert 1 == 2`.
8. **One raising candidate does not starve the batch.**
   `::test_one_failing_candidate_does_not_starve_the_rest`, seeding two candidates and making the
   first raise by monkeypatching
   `RemoteModuleAttemptObligationRepository.worker_discharge_system_mutation_obligations` to raise
   on its first call and delegate afterwards. Red without the per-candidate `except`: the
   `RuntimeError` propagates out of `run_repair` instead of the assertion running.
9. **The discharge runs under real `kdive_reconciler` grants, through the ADR-0629 function.**
   `::test_repair_runs_under_the_reconciler_role`. Red if the repair issued a direct `UPDATE`:
   `psycopg.errors.InsufficientPrivilege: permission denied for table
   remote_module_attempt_obligations`.
10. **The repair is registered in the catalog, after `abandoned_jobs`.**
    `::test_repair_runs_after_abandoned_jobs`. Red with the catalog entry omitted:
    `ValueError: 'leaked_mutation_obligations' is not in list`. The existing
    `tests/reconciler/test_loop.py::test_all_repair_kinds_matches_a_fully_populated_plan` cannot
    serve here — both sides of its assertion derive from `_REPAIR_CATALOG`, so a missing entry
    leaves it green — but it must stay green, and it guards the separate property that no entry's
    factory returns `None` in a fully populated plan.

### Steps

**Step 1 — confirm ADR-0634 is already `Accepted`.** Read
`docs/adr/0634-leaked-mutation-obligations-repaired-by-reconciler.md` and confirm its `## Status`
body is exactly `Accepted (2026-09-08)` before writing a line of source. Then run, bare:

```
just adr-status-check
```

Expect exit 0 and no output.

**Step 2 — add the repair function.** In `src/kdive/reconciler/repairs/systems.py`, add
`from datetime import timedelta` to the stdlib imports and this import beside the existing
`kdive.db.locks` import:

```python
from kdive.db.remote_module_attempt_obligations import RemoteModuleAttemptObligationRepository
```

Add these three module-level constants after `_ORPHANED_SYSTEM_TERMINAL_STATE_VALUES`:

```python
# Pacing with a stated limit, not a fence (ADR-0634). An operator `jobs.cancel` takes a teardown
# job out of `queued`/`running` while its handler keeps running, so job state alone does not bound
# the window; the teardown job's `updated_at` does, because nothing but that teardown writes it.
_TEARDOWN_SETTLE = timedelta(minutes=15)
# Both teardown families enqueue at `_teardown_dedup_key(system_id)`, and `jobs.dedup_key` is
# UNIQUE, so one row carries a System's whole teardown history.
_TEARDOWN_IN_FLIGHT_SQL = (
    "SELECT 1 FROM jobs j "
    "WHERE j.dedup_key = %s "
    "  AND (j.state = ANY(%s) OR j.updated_at > now() - %s)"
)
_LEAKED_MUTATION_CANDIDATES_SQL = (
    "SELECT DISTINCT o.system_id AS system_id "
    "FROM remote_module_attempt_obligations o "
    "JOIN systems s ON s.id = o.system_id "
    "WHERE o.mutation_discharged_at IS NULL "
    "  AND s.state = %s "
    "  AND NOT EXISTS ( "
    "    SELECT 1 FROM jobs j "
    "    WHERE j.dedup_key = s.id::text || ':teardown' "
    "      AND (j.state = ANY(%s) OR j.updated_at > now() - %s) "
    "  )"
)
```

Add this function after `repair_orphaned_systems`:

```python
async def repair_leaked_mutation_obligations(conn: AsyncConnection) -> int:
    """Discharge mutation obligations left open on a `torn_down` System (ADR-0634, #2326).

    The teardown handler commits the terminal state before the discharge that follows it, and
    `systems.teardown` then short-circuits on `torn_down` without enqueueing a job, so nothing
    else ever reaches that discharge and `retained_owners` holds the attempt's volumes out of the
    module-volume sweep. Returns the number of obligation rows discharged.

    A candidate is deferred while its teardown job is active *or* terminal within
    `_TEARDOWN_SETTLE`; ADR-0634 carries why job state alone is not enough and what the window
    does not cover.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _LEAKED_MUTATION_CANDIDATES_SQL,
            (
                SystemState.TORN_DOWN.value,
                list(_ACTIVE_JOB_STATE_VALUES),
                _TEARDOWN_SETTLE,
            ),
        )
        candidates: list[UUID] = [row["system_id"] for row in await cur.fetchall()]
    obligations = RemoteModuleAttemptObligationRepository()
    discharged_rows = 0
    for system_id in candidates:
        try:
            async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
                # Only the job half is re-read: `torn_down` is terminal, so the state half cannot
                # change between the candidate query and this lock.
                async with conn.cursor() as cur:
                    await cur.execute(
                        _TEARDOWN_IN_FLIGHT_SQL,
                        (
                            f"{system_id}:teardown",
                            list(_ACTIVE_JOB_STATE_VALUES),
                            _TEARDOWN_SETTLE,
                        ),
                    )
                    if await cur.fetchone() is not None:
                        continue
                discharged = await obligations.worker_discharge_system_mutation_obligations(
                    conn, system_id
                )
        except Exception:  # noqa: BLE001 - one System must not starve the rest of the batch
            _log.warning(
                "reconciler: leaked mutation obligation discharge failed for system %s; "
                "retrying next pass",
                system_id,
                exc_info=True,
            )
            continue
        if discharged:
            discharged_rows += discharged
            _log.info(
                "reconciler: torn-down system %s had %d leaked mutation obligation(s) discharged",
                system_id,
                discharged,
            )
    return discharged_rows
```

**Step 3 — register it in the catalog.** In `src/kdive/reconciler/loop.py`, add this module-level
alias beside `_repair_orphaned_systems`:

```python
_repair_leaked_mutation_obligations = system_repairs.repair_leaked_mutation_obligations
```

and insert this entry — the comment, the call, and a trailing comma — into `_REPAIR_CATALOG`
immediately after the `"abandoned_jobs"` entry, indented one level to match its siblings:

```python
# Runs after abandoned_jobs, which dead-letters a lease-lapsed teardown job — and a teardown
# job that is active or recently terminal is what defers this repair's candidate (ADR-0634,
# #2326). No report field: the count reaches operators through repair_counts, as the
# stalled-state repairs do.
_RepairCatalogEntry(
    "leaked_mutation_obligations", lambda _r, _c, _g: _repair_leaked_mutation_obligations
)
```

The fence shows the entry at top level without its trailing comma because `ruff format` formats
Python inside Markdown fences in this repo, and an indented `X(...),` fragment is not valid
standalone Python — the formatter rewrites it into a one-element tuple, which is not what to paste
(`docs/solutions/2026-09-04-ruff-format-rewrites-python-in-markdown-fences.md`).

**Step 4 — write the test module.** Create
`tests/reconciler/test_leaked_mutation_obligation_repair.py` with the ten cases in the Verification
inventory, following `tests/reconciler/test_stalled_crashing_recovery.py`: seed on an autocommit
connection, run the repair through a real pool, assert on the autocommit connection.

Helpers that already exist:

- `tests.reconciler.conftest.connect(migrated_url) -> psycopg.AsyncConnection` — autocommit.
- `tests.reconciler.conftest.seed_system(conn, *, system_state=SystemState.READY, alloc_state=AllocationState.ACTIVE) -> UUID`
  — pass `system_state=SystemState.TORN_DOWN`.
- `tests.reconciler.conftest.seed_run(conn, system_id, *, run_state=RunState.RUNNING) -> UUID` —
  the obligations table's `remote_module_attempt_run_system_fk` needs the run row.
- `kdive.db.remote_module_attempt_obligations.ModuleAttempt(system_id=…, run_id=…, operation_nonce=…)`
  — `operation_nonce` must be 32 lowercase hex characters or `__post_init__` raises; use `"0" * 32`
  and, for the two-obligation arm, `"1" * 32`.
- `RemoteModuleAttemptObligationRepository().open_mutation_obligation(conn, attempt) -> bool` —
  returns `True` when it inserted the row.
- `tests.reconciler.conftest.run_repair(pool, repair)` on an
  `AsyncConnectionPool(migrated_url, min_size=1, open=False)`, as
  `test_stalled_crashing_recovery.py:79-81` does.

Insert a teardown job with a local helper, because `tests.reconciler.conftest.seed_running_job`
only writes `running`. The `age` parameter is what entries 3 and 4 vary:

The age is supplied in the `INSERT` column list, never by a later `UPDATE`: `set_updated_at()`
assigns `NEW.updated_at := now()` unconditionally (`src/kdive/db/schema/0001_init.sql:5-11`) and
`jobs_set_updated_at` is `BEFORE UPDATE` only (`:171-172`), so an `UPDATE` that backdates the column
is silently overwritten while an `INSERT` that supplies it is not.

```python
async def _seed_teardown_job(
    conn: psycopg.AsyncConnection, system_id: UUID, *, state: str, age_seconds: int = 0
) -> None:
    """A teardown job at the dedup key both teardown families use, in ``state``.

    ``age_seconds`` backdates ``updated_at`` so an arm can put a terminal job outside the settle
    window. It goes in the INSERT because the BEFORE UPDATE trigger would overwrite it.
    """
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "
        "    updated_at) "
        "VALUES ('teardown', %s, %s, 1, 3, %s, %s, now() - make_interval(secs => %s))",
        (
            Jsonb({"system_id": str(system_id)}),
            state,
            Jsonb({"principal": "alice", "project": "proj"}),
            f"{system_id}:teardown",
            age_seconds,
        ),
    )
```

For entry 9, run `run_repair` over an
`AsyncConnectionPool(authority_role_dsns("kdive_reconciler"), min_size=1, open=False)`, re-exporting
the `authority_role_dsns` fixture the way `tests/db/test_worker_system_mutation_discharge.py:33-35`
does, so the whole lane executes with the reconciler's real grants and no others.

For entry 10, follow `tests/reconciler/test_capture_reaping_wiring.py:47` — assert on ordering, not
adjacency, so a sibling lane registered between them does not fail this:

```python
kinds = loop.ALL_REPAIR_KINDS
assert kinds.index("abandoned_jobs") < kinds.index("leaked_mutation_obligations")
```

**Step 5 — prove each arm bites, then run the focused suite.** For each Verification entry, make the
named fault, run its green command, observe the stated red, revert, and re-run to green. Then, bare:

```
uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py tests/reconciler/test_loop.py -q
```

Expect `0 failed` and no `skipped` for the new module; `KDIVE_REQUIRE_DOCKER=1` turns a
missing-daemon skip into a failure and is how these are proven to have run.

**Step 6 — format and commit the implementation.** This commit carries exactly three paths:
`src/kdive/reconciler/repairs/systems.py`, `src/kdive/reconciler/loop.py`, and
`tests/reconciler/test_leaked_mutation_obligation_repair.py`. It is Python-only, so run
`just format`, `git add --` those three paths, and commit. Run `just adr-status-check` bare once
more: this commit adds the first citation of ADR-0634 from both trees.

Suggested subject: `fix(reconciler): repair mutation obligations leaked on torn-down Systems`.

**Step 7 — run the full gate.** From the worktree root, capture rather than pipe:

```
just ci > /tmp/ci-2326.log 2>&1 < /dev/null
```

Expect exit 0. A fresh worktree needs `just install-mermaid-deps` first, or `check-mermaid` fails
with `Mermaid checker dependencies are missing` — a missing local prerequisite, not a defect in this
change.

### Acceptance criteria

1. `repair_leaked_mutation_obligations` exists with the signature in *Interfaces* and discharges
   only through `worker_discharge_system_mutation_obligations`. No direct `UPDATE` of
   `remote_module_attempt_obligations` appears anywhere in the diff.
2. The candidate predicate and the locked re-read both defer on an active **or** recently terminal
   teardown job, and Verification entries 3 and 4 both pass.
3. `"leaked_mutation_obligations"` appears in `ALL_REPAIR_KINDS` after `"abandoned_jobs"`, and
   `tests/reconciler/test_loop.py` is green.
4. Every Verification entry passes, and each was observed red for the reason stated there.
5. `just adr-status-check` exits 0 on the implementation commit.
6. `just ci` exits 0.

### Rollback

The change is one function, one catalog entry, and one test module, all additive on top of an
already-committed design record. `git revert` of the implementation commit removes the lane and
restores the prior catalog; no schema object, grant, or persisted row shape is touched, so there is
nothing to migrate back. Rows the lane already discharged stay discharged, which is the intended
terminal state and what the teardown path would have written.
