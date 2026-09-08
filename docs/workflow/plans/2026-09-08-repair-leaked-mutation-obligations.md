# Repair leaked System mutation obligations

**Goal.** Give the reconciler a lane that discharges mutation obligations left open on Systems
already in `torn_down`, so those attempts leave the retained-owner set and their volumes stop being
held out of the module-volume reaper.

**Architecture.** One new async repair function in the reconciler's System-repair module, selecting
candidates with a single SQL predicate and discharging each under the System advisory lock through
ADR-0629's existing `SECURITY DEFINER` function. One entry in the reconciler's repair catalog
registers it, which is all the wiring the loop needs — the catalog is the loop's only extension
point and `ALL_REPAIR_KINDS` derives from it.

**Tech stack.** Python 3.14, `uv`, `psycopg` (async) against PostgreSQL, `pytest` with disposable
Postgres via testcontainers. Design record: `docs/adr/0632-leaked-mutation-obligations-repaired-by-reconciler.md`.
Specification: `docs/workflow/specs/2026-09-08-repair-leaked-mutation-obligations-design.md`.

Expected implementation size: 190–260 changed lines (M) — the file map below: ~55 added lines in
`repairs/systems.py`, ~10 in `loop.py`, and one ~160-line test module; design artifacts excluded.

## Global Constraints

- Ruff line length **100**; lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults over the
  **whole tree**, `src` and `tests` alike.
- **Never invent an `ErrorCategory` string.** This change records no error category at all; the
  repair reports a count and logs, and the reconciler's plan runner already isolates a raise.
- **Never write a data migration in this change.** Migration number `0153` was reserved for it and
  is deliberately left unused; the ADR records why. Do not add a file under
  `src/kdive/db/schema/`.
- **No commit may cite a `Proposed` ADR from `src/` or `tests/`.**
  `scripts/guards/check_adr_status.py` fails any such commit, and no pre-commit hook runs it, so the
  failure appears only in CI or a bare `just ci`
  (`docs/solutions/2026-09-04-adr-status-flip-must-share-the-first-citation-commit.md`). This change
  satisfies the guard by writing the ADR **Accepted** in the design commit, which carries no
  citation; the later implementation commit then adds the first citations from both trees against an
  already-Accepted record. Never open this ADR as `Proposed`.
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
| `docs/adr/0632-leaked-mutation-obligations-repaired-by-reconciler.md` | created | The decision: a reconciler lane rather than a data migration, and one exclusion rather than two. |
| `docs/workflow/specs/2026-09-08-repair-leaked-mutation-obligations-design.md` | created | Problem, scope, success criteria, threat model, validation. |
| `src/kdive/reconciler/repairs/systems.py` | changed | The candidate predicate and the per-System discharge. |
| `src/kdive/reconciler/loop.py` | changed | Registering the repair in the catalog, after `abandoned_jobs`. |
| `tests/reconciler/test_leaked_mutation_obligation_repair.py` | created | Every success criterion that is a runtime behaviour of the lane. |

## Task 1 — the reconciler lane that discharges leaked mutation obligations

Creates: `tests/reconciler/test_leaked_mutation_obligation_repair.py`.
Modifies: `src/kdive/reconciler/repairs/systems.py`, `src/kdive/reconciler/loop.py`.
Reads, already committed by the design phase:
`docs/adr/0632-leaked-mutation-obligations-repaired-by-reconciler.md`,
`docs/workflow/specs/2026-09-08-repair-leaked-mutation-obligations-design.md`.
Tests: `tests/reconciler/test_leaked_mutation_obligation_repair.py`,
`tests/reconciler/test_loop.py` (existing, must stay green).

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
- `kdive.domain.capacity.state.SystemState.TORN_DOWN` — value `"torn_down"`.
- `_ACTIVE_JOB_STATE_VALUES` — module-private tuple already defined at
  `src/kdive/reconciler/repairs/systems.py:27`, equal to
  `(JobState.QUEUED.value, JobState.RUNNING.value)`.
- `_RepairCatalogEntry(name: str, factory: Callable[[InfraReaper, ReconcileConfig, timedelta], _AnyRepairFn | None], report_field: str | None = None)`
  — frozen dataclass at `src/kdive/reconciler/loop.py:236-240`.

Relied on by nothing later in this plan; the public name this task adds is
`kdive.reconciler.repairs.systems.repair_leaked_mutation_obligations`, with signature
`async def repair_leaked_mutation_obligations(conn: AsyncConnection) -> int`, and the repair-kind
name `"leaked_mutation_obligations"`.

### Verification

- **Contract: a torn-down System with an open obligation and no active teardown job is discharged
  with reason `terminal_escape`, and the repair counts it.**
  Mode: focused-test. Test: `tests/reconciler/test_leaked_mutation_obligation_repair.py::test_leaked_obligation_on_torn_down_system_is_discharged`.
  Expected red before the implementation: `AttributeError: module 'kdive.reconciler.repairs.systems'
  has no attribute 'repair_leaked_mutation_obligations'` at import.
  Green: `uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py::test_leaked_obligation_on_torn_down_system_is_discharged -q`
- **Contract: an active (`queued` or `running`) job at dedup key `'<system_id>:teardown'` defers
  the repair, which then counts 0 and leaves the row open.**
  Mode: focused-test. Test: `...::test_active_teardown_job_defers_the_repair` (parametrized over
  `queued` and `running`).
  Expected red with the `NOT EXISTS` clause omitted: `assert 0 == 1` on the count, then
  `assert None is None` failing on the discharged timestamp.
  Green: `uv run python -m pytest "tests/reconciler/test_leaked_mutation_obligation_repair.py::test_active_teardown_job_defers_the_repair" -q`
- **Contract: a System that is not `torn_down` is never discharged.**
  Mode: focused-test. Test: `...::test_non_terminal_system_is_untouched`.
  Expected red with the `s.state = %s` clause omitted: `assert 0 == 1` on the count.
  Green: `uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py::test_non_terminal_system_is_untouched -q`
- **Contract: first-write-wins is preserved — a second pass changes neither
  `mutation_discharged_at` nor `mutation_discharge_reason`.**
  Mode: focused-test. Test: `...::test_second_pass_is_a_noop`.
  Expected red if the repair re-wrote discharged rows: the second read differs from the first.
  Green: `uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py::test_second_pass_is_a_noop -q`
- **Contract: the discharge runs under real `kdive_reconciler` grants, through the ADR-0629
  function and not a table privilege.**
  Mode: focused-test. Test: `...::test_repair_runs_under_the_reconciler_role`.
  Expected red if the repair issued a direct `UPDATE`: `psycopg.errors.InsufficientPrivilege:
  permission denied for table remote_module_attempt_obligations`.
  Green: `uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py::test_repair_runs_under_the_reconciler_role -q`
- **Contract: only the selected System is discharged — a deferred second System keeps its open
  obligation in the same pass.**
  Mode: focused-test. Covered by the bystander assertion inside
  `...::test_leaked_obligation_on_torn_down_system_is_discharged`.
  Expected red if the discharge dropped its `system_id` bound: the bystander's read returns a
  non-`None` timestamp.
  Green: same command as the first contract.
- **Contract: the repair is registered in the reconciler catalog, immediately after
  `abandoned_jobs`, and a fully populated plan emits it.**
  Mode: focused-test. Test: `...::test_repair_is_registered_after_abandoned_jobs`, asserting both
  that `"leaked_mutation_obligations"` is in `loop.ALL_REPAIR_KINDS` and that its index is exactly
  one past `"abandoned_jobs"`. This is the idiom `tests/reconciler/test_loop.py:1994-1996` and
  `tests/reconciler/test_capture_reaping_wiring.py:38` already use for wiring.
  `test_all_repair_kinds_matches_a_fully_populated_plan` cannot serve here: both sides of its
  assertion derive from `_REPAIR_CATALOG`, so a missing entry leaves it green.
  Expected red with the function added and the catalog entry omitted:
  `assert 'leaked_mutation_obligations' in ('expired_allocations', …)`.
  Green: `uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py::test_repair_is_registered_after_abandoned_jobs -q`
  The existing `tests/reconciler/test_loop.py::test_all_repair_kinds_matches_a_fully_populated_plan`
  still guards the separate property that no catalog entry's factory returns `None` in a fully
  populated plan, and stays green.

### Steps

**Step 1 — confirm the ADR is already `Accepted`.** The design phase commits
`docs/adr/0632-leaked-mutation-obligations-repaired-by-reconciler.md` with its `## Status` body set
to exactly `Accepted (2026-09-08)`, before any citation exists. Read it back and confirm that
keyword before writing a line of source. It must never be `Proposed`, because steps 2 and 4 add the
first citations from `src/` and `tests/`, and
`scripts/guards/check_adr_status.py` fails every commit where a `Proposed` ADR is cited from either
tree. Run, bare:

```
just adr-status-check
```

Expect exit 0 and no output.

**Step 2 — add the repair function.** In `src/kdive/reconciler/repairs/systems.py`, add this import
beside the existing `kdive.db.locks` import:

```python
from kdive.db.remote_module_attempt_obligations import RemoteModuleAttemptObligationRepository
```

Add these two module-level constants after `_ORPHANED_SYSTEM_TERMINAL_STATE_VALUES`:

```python
# Both teardown families enqueue at `_teardown_dedup_key(system_id)` — the ordinary
# `enqueue_control_teardown` and the authority-owned `enqueue_preactivation_teardown`
# (services/systems/authority_owned.py:137-138, 196-199, 223-229) — so one key covers both.
_LEAKED_MUTATION_CANDIDATES_SQL = (
    "SELECT DISTINCT o.system_id AS system_id "
    "FROM remote_module_attempt_obligations o "
    "JOIN systems s ON s.id = o.system_id "
    "WHERE o.mutation_discharged_at IS NULL "
    "  AND s.state = %s "
    "  AND NOT EXISTS ( "
    "    SELECT 1 FROM jobs j "
    "    WHERE j.dedup_key = s.id::text || ':teardown' "
    "      AND j.state = ANY(%s) "
    "  )"
)
_LEAKED_MUTATION_RECHECK_SQL = (
    "SELECT 1 FROM systems s "
    "WHERE s.id = %s AND s.state = %s "
    "  AND NOT EXISTS ( "
    "    SELECT 1 FROM jobs j "
    "    WHERE j.dedup_key = s.id::text || ':teardown' "
    "      AND j.state = ANY(%s) "
    "  )"
)
```

Add this function after `repair_orphaned_systems`:

```python
async def repair_leaked_mutation_obligations(conn: AsyncConnection) -> int:
    """Discharge a mutation obligation left open on a `torn_down` System (ADR-0632, #2326).

    A System can reach `torn_down` with its mutation obligation still open: the teardown handler
    commits the terminal state in its own transaction before the discharge that follows it
    (`jobs/handlers/systems.py:700-706`), and `systems.teardown` then returns `torn_down` from its
    terminal short-circuit without enqueueing a job, so no retry reaches the discharge. The row
    keeps `mutation_discharged_at IS NULL`, so `retained_owners` holds that attempt's volumes out
    of the module-volume sweep indefinitely, and nothing logs at a level an operator sees.

    One exclusion, not the two #2326 proposes: a teardown in flight. That window sits entirely
    inside a live `teardown` job, and both teardown families enqueue at the same dedup key. The
    other candidate exclusion is unnecessary — migration 0147 discharges in the same statement
    group that sets `torn_down` (0147:440-443), and 0149 refuses with `cleanup-required` over an
    open obligation (0149:1154-1157) — so no path leaves one legitimately open on a torn-down
    System.

    The predicate is not a fence: the teardown handler releases the System lock before its
    provider call, so re-reading it inside the per-System locked transaction narrows the race to
    the width every other reconciler repair carries, and no narrower. Runs after
    `repair_abandoned_jobs`, which dead-letters a lease-lapsed teardown job and so is what makes a
    stranded candidate visible. The write goes through ADR-0629's SECURITY DEFINER function: the
    reconciler holds `EXECUTE` on it and no `UPDATE` on the table.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _LEAKED_MUTATION_CANDIDATES_SQL,
            (SystemState.TORN_DOWN.value, list(_ACTIVE_JOB_STATE_VALUES)),
        )
        candidates: list[UUID] = [row["system_id"] for row in await cur.fetchall()]
    obligations = RemoteModuleAttemptObligationRepository()
    repaired = 0
    for system_id in candidates:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            async with conn.cursor() as cur:
                await cur.execute(
                    _LEAKED_MUTATION_RECHECK_SQL,
                    (system_id, SystemState.TORN_DOWN.value, list(_ACTIVE_JOB_STATE_VALUES)),
                )
                if await cur.fetchone() is None:
                    continue
            discharged = await obligations.worker_discharge_system_mutation_obligations(
                conn, system_id
            )
        if discharged:
            repaired += 1
            _log.info(
                "reconciler: torn-down system %s had %d leaked mutation obligation(s) discharged",
                system_id,
                discharged,
            )
    return repaired
```

**Step 3 — register it in the catalog.** In `src/kdive/reconciler/loop.py`, add this module-level
alias beside `_repair_orphaned_systems`:

```python
_repair_leaked_mutation_obligations = system_repairs.repair_leaked_mutation_obligations
```

and insert this entry into `_REPAIR_CATALOG` immediately after the `"abandoned_jobs"` entry:

```python
    # Runs after abandoned_jobs, which dead-letters a lease-lapsed teardown job — and an active
    # teardown job is exactly what defers this repair's candidate (ADR-0632, #2326). No report
    # field: the count reaches operators through repair_counts, as the stalled-state repairs do.
    _RepairCatalogEntry(
        "leaked_mutation_obligations", lambda _r, _c, _g: _repair_leaked_mutation_obligations
    ),
```

**Step 4 — write the test module.** Create
`tests/reconciler/test_leaked_mutation_obligation_repair.py` with the cases named in the
Verification inventory, following the shape of `tests/reconciler/test_stalled_crashing_recovery.py`:
seed on an autocommit connection, run the repair through a real pool, assert on the autocommit
connection.

Seed with helpers that already exist:

- `tests.reconciler.conftest.connect(migrated_url) -> psycopg.AsyncConnection` — autocommit.
- `tests.reconciler.conftest.seed_system(conn, *, system_state=SystemState.READY, alloc_state=AllocationState.ACTIVE) -> UUID`
  — pass `system_state=SystemState.TORN_DOWN`.
- `tests.reconciler.conftest.seed_run(conn, system_id, *, run_state=RunState.RUNNING) -> UUID` —
  the obligations table's `remote_module_attempt_run_system_fk` needs the run row.
- `kdive.db.remote_module_attempt_obligations.ModuleAttempt(system_id=…, run_id=…, operation_nonce=…)`
  — `operation_nonce` must be 32 lowercase hex characters or `__post_init__` raises; use `"0" * 32`.
- `RemoteModuleAttemptObligationRepository().open_mutation_obligation(conn, attempt) -> bool` —
  returns `True` when it inserted the row.

Insert a deferring job with a local helper, parametrized on state, because
`tests.reconciler.conftest.seed_running_job` only writes `running`:

```python
async def _seed_teardown_job(
    conn: psycopg.AsyncConnection, system_id: UUID, *, state: str
) -> None:
    """A teardown job at the dedup key both teardown families use, in ``state``."""
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key) "
        "VALUES ('teardown', %s, %s, 1, 3, %s, %s)",
        (
            Jsonb({"system_id": str(system_id)}),
            state,
            Jsonb({"principal": "alice", "project": "proj"}),
            f"{system_id}:teardown",
        ),
    )
```

Run the repair through
`tests.reconciler.conftest.run_repair(pool, repair_leaked_mutation_obligations)` on an
`AsyncConnectionPool(migrated_url, min_size=1, open=False)`, exactly as
`test_stalled_crashing_recovery.py:79-81` does.

For `test_repair_runs_under_the_reconciler_role`, take the DSN from the existing
`authority_role_dsns` fixture — re-export it in this module the way
`tests/db/test_worker_system_mutation_discharge.py:33-35` does — and run
`run_repair` over an `AsyncConnectionPool(authority_role_dsns("kdive_reconciler"), min_size=1, open=False)`,
so the whole lane executes with the reconciler's real grants and no others.

For `test_repair_is_registered_after_abandoned_jobs`, import `kdive.reconciler.loop as loop` and
assert:

```python
kinds = loop.ALL_REPAIR_KINDS
assert "leaked_mutation_obligations" in kinds
assert kinds.index("leaked_mutation_obligations") == kinds.index("abandoned_jobs") + 1
```

**Step 5 — prove each new test fails for the stated reason, then passes.** For each contract in the
Verification inventory, make the named controlled fault (omit the clause named in its expected-red
line), run its green command, observe the stated failure, revert the fault, and re-run it to green.

**Step 6 — run the focused suite.** Bare:

```
uv run python -m pytest tests/reconciler/test_leaked_mutation_obligation_repair.py tests/reconciler/test_loop.py -q
```

Expect `0 failed`, no `skipped` for the new module (the db tier skips only without a reachable
Docker daemon; set `KDIVE_REQUIRE_DOCKER=1` to turn that skip into a failure and prove they ran).

**Step 7 — format and commit the implementation.** This commit carries exactly three paths:
`src/kdive/reconciler/repairs/systems.py`, `src/kdive/reconciler/loop.py`, and
`tests/reconciler/test_leaked_mutation_obligation_repair.py`. It is Python-only, so run
`just format`, then `git add --` those three paths, then commit. The ADR is already committed and
already `Accepted`, so this commit — which adds the first citation from both trees — passes
`adr-status-check`; run it bare once more to confirm.

Suggested subject: `fix(reconciler): repair mutation obligations leaked on torn-down Systems`.

**Step 8 — run the full gate.** From the worktree root, capture rather than pipe:

```
just ci > /tmp/ci-2326.log 2>&1 < /dev/null
```

Expect exit 0. A fresh worktree needs `just install-mermaid-deps` first, or `check-mermaid` fails
with `Mermaid checker dependencies are missing` — a missing local prerequisite, not a defect in this
change.

### Acceptance criteria

1. `repair_leaked_mutation_obligations` exists in `src/kdive/reconciler/repairs/systems.py` with the
   signature in *Interfaces*, and discharges only through
   `worker_discharge_system_mutation_obligations`. No direct `UPDATE` of
   `remote_module_attempt_obligations` appears anywhere in the diff.
2. `"leaked_mutation_obligations"` appears in `ALL_REPAIR_KINDS`, positioned after
   `"abandoned_jobs"`, and `tests/reconciler/test_loop.py` is green.
3. Every case in the Verification inventory passes, and each was observed red for the reason stated
   there before it passed.
4. No file is added under `src/kdive/db/schema/`; migration `0153` is unused.
5. The ADR reads `Accepted (2026-09-08)` and was never committed as `Proposed`, so
   `just adr-status-check` exits 0 on both the design commit and the implementation commit that
   first cites it.
6. `just ci` exits 0.

### Rollback

The change is one function, one catalog entry, one ADR, one spec, and one test module, all additive.
`git revert` of the single commit removes the lane entirely and restores the prior catalog; no
schema object, grant, or persisted row shape is touched, so there is nothing to migrate back. Rows
this lane already discharged stay discharged, which is the intended terminal state and is what the
teardown path would have written.
