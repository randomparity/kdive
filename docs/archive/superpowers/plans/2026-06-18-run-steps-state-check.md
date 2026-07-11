# run_steps.state CHECK Constraint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Database-enforce the `run_steps.state` two-value state machine and make `claim_run_step` fail fast on an impossible state instead of polling forever (issue #562).

**Architecture:** A forward-only migration adds a validating `CHECK` to `run_steps.state` mirroring the private `_RunStepState` enum; `claim_run_step` gains a read-path guard that raises `RuntimeError` on any state that is neither `succeeded` nor `running`; two parity tests pin the enum and the SQL CHECK in both directions.

**Tech Stack:** Python 3.14, psycopg (async + sync), Postgres, pytest. Migrations are forward-only SQL files in `src/kdive/db/schema/` discovered by `kdive.db.migrate` (ADR-0015).

## Global Constraints

- Decision record: [ADR-0171](../../adr/0171-run-steps-state-check.md); spec: [docs/specs/run-steps-state-check.md](../../specs/run-steps-state-check.md).
- Migrations are forward-only and never edited once shipped; the runner verifies each file's SHA-256 checksum (ADR-0015). New migration filename: `0043_run_steps_state_check.sql`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict (whole tree). Run guardrails via `just`, not raw commands.
- Guardrail commands: `just lint`, `just type`, `just test` (full suite excludes the gated `live_vm` marker). Run a single test with `uv run python -m pytest <path>::<name> -q`.
- The db tests need a reachable Docker daemon (disposable Postgres via testcontainers); they skip when Docker is absent unless `KDIVE_REQUIRE_DOCKER=1`.
- Conventional commit messages; subject ≤72 chars; end every commit with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- `_RunStepState` (`src/kdive/db/idempotency.py:39-41`) stays private; tests import it within-package. Do not promote it.

---

### Task 1: Add the validating CHECK migration for `run_steps.state`

**Files:**
- Create: `src/kdive/db/schema/0043_run_steps_state_check.sql`
- Test: `tests/db/test_migrate.py` (modify)

**Interfaces:**
- Consumes: the existing `run_steps` table (`state text NOT NULL`, no CHECK) from `0001_init.sql`; the migration runner `kdive.db.migrate.apply_migrations`.
- Produces: a named constraint `run_steps_state_check` admitting exactly `running`, `succeeded`. Tasks 2 and 3 reference this constraint name.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/db/test_migrate.py`:

```python
def test_run_steps_state_check_admits_enum_values_and_rejects_others(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    run_id = _seed_run_for_steps(pg_conn)
    for state in ("running", "succeeded"):
        pg_conn.execute(
            "INSERT INTO run_steps (run_id, step, state) VALUES (%s, %s, %s)",
            (run_id, f"step-{state}", state),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO run_steps (run_id, step, state) VALUES (%s, 'bad', 'bogus')",
            (run_id,),
        )


def _seed_run_for_steps(conn: psycopg.Connection) -> str:
    """Insert the resource->allocation->system->investigation->run FK chain for run_steps."""
    resource_id = _seed_resource_row(conn)
    alloc = conn.execute(
        "INSERT INTO allocations (resource_id, state, principal, project) "
        "VALUES (%s, 'granted', 'alice', 'proj') RETURNING id",
        (resource_id,),
    ).fetchone()
    assert alloc is not None
    sysm = conn.execute(
        "INSERT INTO systems (allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, 'ready', '{}'::jsonb, 'alice', 'proj') RETURNING id",
        (alloc[0],),
    ).fetchone()
    assert sysm is not None
    inv = conn.execute(
        "INSERT INTO investigations (title, state, principal, project) "
        "VALUES ('t', 'open', 'alice', 'proj') RETURNING id"
    ).fetchone()
    assert inv is not None
    run = conn.execute(
        "INSERT INTO runs (investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) "
        "VALUES (%s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'alice', 'proj') RETURNING id",
        (inv[0], sysm[0]),
    ).fetchone()
    assert run is not None
    return str(run[0])
```

Note: `_seed_resource_row` already exists in `tests/db/test_migrate.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/db/test_migrate.py::test_run_steps_state_check_admits_enum_values_and_rejects_others -q`
Expected: FAIL — the third INSERT does NOT raise `CheckViolation` (no constraint yet), so `pytest.raises` fails.

- [ ] **Step 3: Create the migration**

Create `src/kdive/db/schema/0043_run_steps_state_check.sql`:

```sql
-- 0043_run_steps_state_check.sql — database-enforce the run_steps.state machine
-- (ADR-0171, #562). run_steps.state is the idempotency-ledger state machine with
-- exactly two values (_RunStepState: 'running', 'succeeded'), but 0001_init.sql left
-- the column an unconstrained text. A row with any other value makes claim_run_step
-- poll forever (it returns only on 'succeeded' and treats every other value as a live
-- 'running' claim to wait on). Add the named CHECK the durable lifecycle tables use.
--
-- Validating (not NOT VALID): the only writers (run_step / claim_run_step /
-- complete_run_step) only ever write 'running' or 'succeeded', so validation of
-- existing rows cannot fail. The CHECK mirrors _RunStepState exactly; the named
-- constraint is pinned to the enum by test_migrate.py (CHECK_ENUMS plus a dedicated
-- bidirectional test), which fails if it drifts.
ALTER TABLE run_steps
    ADD CONSTRAINT run_steps_state_check CHECK (state IN ('running', 'succeeded'));
```

- [ ] **Step 4: Run the new test plus the migration regression suite**

The migration count changed, so `test_rerun_is_a_noop` and `test_advisory_lock_serializes_migrators` (which assert the exact applied-version list) need `"0043"` appended to their expected lists. Add `"0043"` after `"0042"` in both lists in `tests/db/test_migrate.py`.

Run: `uv run python -m pytest tests/db/test_migrate.py -q`
Expected: PASS (including the new test and the two updated version-list tests).

- [ ] **Step 5: Run lint + type + commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/db/schema/0043_run_steps_state_check.sql tests/db/test_migrate.py
git commit -m "feat(db): add run_steps_state_check CHECK constraint (#562)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Pin the constraint to `_RunStepState` in both directions

**Files:**
- Modify: `tests/db/test_migrate.py`

**Interfaces:**
- Consumes: `run_steps_state_check` (Task 1); `kdive.db.idempotency._RunStepState`; the existing `CHECK_ENUMS` parametrized test `test_check_constraint_covers_every_enum_value`.
- Produces: enum⊆SQL coverage (via CHECK_ENUMS) and SQL⊆enum coverage (via the dedicated test) — exact bidirectional sync.

- [ ] **Step 1: Add the bidirectional failing test**

The enum⊆SQL direction comes for free by registering the constraint in `CHECK_ENUMS`. Add the entry to the `CHECK_ENUMS` list near the top of `tests/db/test_migrate.py`:

```python
    ("run_steps_state_check", idempotency._RunStepState),
```

and add the import at the top of the file:

```python
from kdive.db import idempotency, migrate
```

(Replace the existing `from kdive.db import migrate` line.)

Then add the dedicated SQL⊆enum test (the direction CHECK_ENUMS cannot check — an SQL-only extra value):

```python
def test_run_steps_state_check_admits_exactly_the_enum(pg_conn: psycopg.Connection) -> None:
    """The CHECK's admitted set equals _RunStepState exactly — no SQL-only extras."""
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'run_steps_state_check'"
    ).fetchone()
    assert row is not None, "run_steps_state_check constraint is missing"
    # pg renders the CHECK with the admitted values as single-quoted literals
    # (state = ANY (ARRAY['running'::text, ...]) or state IN ('running', ...)); the
    # ::text casts sit outside the quotes, so the quoted tokens are exactly the values.
    admitted = set(re.findall(r"'([^']+)'", row[0]))
    assert admitted == {s.value for s in idempotency._RunStepState}
```

Add `import re` to the imports if not already present (it is not in the current file).

- [ ] **Step 2: Run the tests to verify they pass**

Both directions should already be satisfied by Task 1's migration, so these tests pass once the constraint exists. Confirm they are real by checking they would fail without the constraint is covered by Task 1's red step; here, verify green:

Run: `uv run python -m pytest "tests/db/test_migrate.py::test_run_steps_state_check_admits_exactly_the_enum" "tests/db/test_migrate.py::test_check_constraint_covers_every_enum_value[run_steps_state_check-_RunStepState]" -q`
Expected: PASS (2 tests). If the parametrized id differs, run `uv run python -m pytest tests/db/test_migrate.py -k run_steps -q`.

- [ ] **Step 3: Run lint + type + commit**

Run: `just lint && just type`
Expected: clean (importing the private `_RunStepState` triggers no lint rule in `E,F,I,UP,B,SIM`).

```bash
git add tests/db/test_migrate.py
git commit -m "test(db): pin run_steps_state_check to _RunStepState both ways (#562)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Fail fast in `claim_run_step` on an unknown persisted state

**Files:**
- Modify: `src/kdive/db/idempotency.py:120-138`
- Test: `tests/db/test_idempotency.py` (modify)

**Interfaces:**
- Consumes: `claim_run_step(conn, run_id, step) -> StepClaim`; `_RunStepState`; `_STALE_RUNNING_INTERVAL = "30 minutes"`.
- Produces: `claim_run_step` raises `RuntimeError` on a state that is neither `succeeded` nor `running`; unchanged behavior for `succeeded` (replay) and `running` (wait/reclaim).

- [ ] **Step 1: Write the failing read-path + stale + replay tests**

Add to `tests/db/test_idempotency.py`. The module already has `_connect` (async, autocommit) and `_seed_run`. Import the claim functions and `_RunStepState`:

```python
from kdive.db.idempotency import (
    JsonValue,
    _RunStepState,
    claim_run_step,
    complete_run_step,
    run_step,
)
```

Tests:

Every `claim_run_step` call is wrapped in `asyncio.wait_for(..., timeout=5)`. The
pre-guard failure mode for an unknown state is an unbounded poll loop, and a
mis-staged stale row would also leave `claim_run_step` waiting forever; `wait_for`
turns either hang into a deterministic `TimeoutError` so the suite never stalls
(`pytest-timeout` / the `--timeout` flag is not a dependency of this repo).

```python
def test_claim_run_step_replays_succeeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            claim = await asyncio.wait_for(claim_run_step(conn, run_id, "s"), timeout=5)
            assert claim.claimed is True
            await complete_run_step(conn, run_id, "s", {"v": 1})
            replay = await asyncio.wait_for(claim_run_step(conn, run_id, "s"), timeout=5)
            assert replay.claimed is False
            assert replay.result == {"v": 1}

    asyncio.run(_run())


def test_claim_run_step_reclaims_stale_running(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            # Stage a running row aged past the 30-minute stale interval. The
            # run_steps_set_updated_at trigger fires BEFORE UPDATE only, so an INSERT
            # with an explicit old updated_at is not rewritten to now(). wait_for bounds
            # the call so a non-aged row surfaces as TimeoutError, not a hang.
            await conn.execute(
                "INSERT INTO run_steps (run_id, step, state, updated_at) "
                "VALUES (%s, 's', 'running', now() - interval '31 minutes')",
                (run_id,),
            )
            claim = await asyncio.wait_for(claim_run_step(conn, run_id, "s"), timeout=5)
            assert claim.claimed is True  # stale row deleted, freshly re-claimed

    asyncio.run(_run())


def test_claim_run_step_raises_on_unknown_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            run_id = await _seed_run(conn)
            # claim_run_step reads state straight from the DB with no injection seam,
            # so stage corrupt data by dropping the CHECK on this test's own freshly
            # migrated database (no leak into other tests).
            await conn.execute(
                "ALTER TABLE run_steps DROP CONSTRAINT run_steps_state_check"
            )
            await conn.execute(
                "INSERT INTO run_steps (run_id, step, state) VALUES (%s, 's', 'bogus')",
                (run_id,),
            )
            # Without the guard, claim_run_step polls forever -> wait_for raises
            # TimeoutError (not RuntimeError), so pytest.raises fails: the red signal.
            # With the guard it raises RuntimeError before the timeout.
            with pytest.raises(RuntimeError, match="unknown state"):
                await asyncio.wait_for(claim_run_step(conn, run_id, "s"), timeout=5)

    asyncio.run(_run())
```

- [ ] **Step 2: Run the tests to verify the unknown-state one fails**

Run: `uv run python -m pytest tests/db/test_idempotency.py::test_claim_run_step_raises_on_unknown_state -q`
Expected: FAIL in ~5s — without the guard, `claim_run_step` loops, so `asyncio.wait_for` raises `TimeoutError` and `pytest.raises(RuntimeError)` fails (it caught the wrong exception type). No hang, no `--timeout` flag needed. The replay and stale tests should already PASS (they exercise existing behavior).

- [ ] **Step 3: Add the read-path guard**

In `src/kdive/db/idempotency.py`, replace the tail of the `claim_run_step` loop body (the `if existing["state"] == _RunStepState.SUCCEEDED.value:` block, lines ~136-137) with:

```python
            state = existing["state"]
            if state == _RunStepState.SUCCEEDED.value:
                return StepClaim(False, _step_result(existing["result"], run_id=run_id, step=step))
            if state != _RunStepState.RUNNING.value:
                raise RuntimeError(
                    f"run_step ({run_id}, {step}) has unknown state {state!r}; "
                    f"expected one of {[s.value for s in _RunStepState]}"
                )
        await asyncio.sleep(_STEP_WAIT_POLL_SEC)
```

The `raise` sits inside `async with conn.transaction()`, so the transaction rolls back and the `RuntimeError` propagates out of the loop (it does not retry). A `running` state still falls through to the `await asyncio.sleep(...)` wait — unchanged.

- [ ] **Step 4: Run the idempotency suite to verify all pass**

Run: `uv run python -m pytest tests/db/test_idempotency.py -q`
Expected: PASS (all, including the three new tests).

- [ ] **Step 5: Run the adversarial idempotency suite (no regression)**

Run: `uv run python -m pytest tests/adversarial/test_idempotency_concurrency.py -q`
Expected: PASS — the guard does not touch the `running`-wait or concurrent-claim paths.

- [ ] **Step 6: Run lint + type + commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/db/idempotency.py tests/db/test_idempotency.py
git commit -m "fix(db): fail fast on unknown run_steps.state in claim_run_step (#562)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the full suite once: `just test`. Expected: PASS (the db/migration tests run against disposable Postgres; they skip only if Docker is absent).
- [ ] Run `just lint && just type`. Expected: clean.
- [ ] Confirm `git status` is clean and the three commits are present.
