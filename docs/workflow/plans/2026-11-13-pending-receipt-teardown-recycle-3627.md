# Pending absence receipt keeps its preactivation teardown — plan (#3627)

Goal: `enqueue_preactivation_teardown` stops restarting a teardown whose current authority
attempt holds an unconsumed `preactivation-absent` receipt, leaving it to the reconciler repair.

Architecture: a server-only `SECURITY DEFINER` predicate (migration 0196) answers under the SQL
System lock; the service downgrades a non-`NEVER` policy to `NEVER` when it is true. Spec:
`docs/workflow/specs/2026-11-13-pending-receipt-teardown-recycle-3627-design.md`.

Expected implementation size: 270–330 changed lines (M) — a 40-line migration, a 20-line service
change, ~210 lines of tests across two files, six window-list edits, a comment, runbook prose.

## Global Constraints

- Migrations are forward-only (ADR-0015); the assigned number is `0196`; do not edit earlier
  migrations.
- `src/kdive/jobs/queue.py` is not changed.
- Docs prose: plain and factual; do not use "critical", "robust", "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, `just test-changed`, `prek run`; full gate `just ci`.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/db/schema/0196_authority_system_receipt_pending.sql` | create | the predicate and its grants |
| `src/kdive/services/systems/authority_owned.py` | modify `enqueue_preactivation_teardown` | the policy downgrade |
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | one comment line | caller note |
| `tests/db/test_migration_0196_authority_system_receipt_pending.py` | create | S3, S4, S5 |
| `tests/services/systems/test_authority_admission.py` | add one parametrized test | S1 (S2 is the existing `[lapsed]` case) |
| `tests/db/test_migrate.py`, `tests/db/test_migration_0091_system_object_sweep_cursors.py`, `tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py` | append the 0196 entry to every list ending at 0195, widen each slice by one | migration window |
| `docs/operating/runbooks/exhausted-authority-system-provision.md` | reduce the reconciler-log step | operator procedure |

## Task 1 — the predicate function

**Interfaces.** Produces SQL
`public.authority_system_preactivation_receipt_pending(p_system_id uuid, p_job_id uuid)
RETURNS boolean`, EXECUTE for `kdive_server` only. Task 2 calls it.

**Verification.**
- Contract "true only for the repair's receipt clauses" — Mode: focused-test;
  `test_pending_receipt_matches_only_the_repair_predicate` (parametrized); red: `UndefinedFunction`
  before the migration exists; green: `uv run python -m pytest
  tests/db/test_migration_0196_authority_system_receipt_pending.py -q`.
- Contract "server-only EXECUTE" — Mode: focused-test; `test_only_the_server_may_execute`;
  red/green as above.
- Contract "reads under the SQL System lock" — Mode: focused-test;
  `test_answers_after_the_system_lock_holder_commits`; red/green as above (it also fails, reading
  `(True,)`, if the lock is taken after the read).
- Contract "migration window" — Mode: focused-test; the four window tests; red: list mismatch
  once the file exists; green: `uv run python -m pytest tests/db/test_migrate.py
  tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py -q`.

Steps:

1. Write `tests/db/test_migration_0196_authority_system_receipt_pending.py`:

```python
"""Real-Postgres proofs of the pending absence-receipt predicate (#3627, ADR-0620)."""

from __future__ import annotations

import threading
import time
from typing import Any, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db import migrate
from tests.db.authority_system_support import (
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)
from tests.db.external_boot_authority_support import _RoleDsns

_PENDING = "SELECT authority_system_preactivation_receipt_pending(%s,%s)"
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('kdive:system:' || %s::text, 2125))"
_DEVIATIONS: dict[str, LiteralString | None] = {
    "pending": None,
    "other-job": None,
    "consumed": "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() "
    "WHERE system_id=%s",
    "not-teardown-requested": "UPDATE authority_system_ownership SET state='provisioning' "
    "WHERE system_id=%s",
    "head-moved": "UPDATE authority_system_ownership SET journal_sequence=3 WHERE system_id=%s",
    "not-current": "UPDATE authority_system_ownership SET current_attempt_id=NULL "
    "WHERE system_id=%s",
}


def _case(migrated_url: str, disposition: str = "preactivation-absent") -> dict[str, Any]:
    with psycopg.connect(migrated_url) as admin:
        case: dict[str, Any] = _seed_direct_authority_case(
            admin,
            operation="preactivation-teardown",
            ownership_state="teardown-requested",
            attempt=3,
        )
        _seed_direct_terminal_attempt(admin, case, disposition=disposition)
        admin.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 minute' WHERE id=%s",
            (case["job_id"],),
        )
    return case


def test_migration_0196_is_registered_once() -> None:
    assert [
        (migration.version, migration.filename)
        for migration in migrate.discover_migrations()
        if migration.version == "0196"
    ] == [("0196", "0196_authority_system_receipt_pending.sql")]


@pytest.mark.parametrize("deviation", list(_DEVIATIONS))
def test_pending_receipt_matches_only_the_repair_predicate(
    migrated_url: str, authority_role_dsns: _RoleDsns, deviation: str
) -> None:
    case = _case(migrated_url)
    statement = _DEVIATIONS[deviation]
    if statement is not None:
        with psycopg.connect(migrated_url) as admin:
            admin.execute(statement, (case["system_id"],))
    job_id = uuid4() if deviation == "other-job" else case["job_id"]
    with psycopg.connect(authority_role_dsns("kdive_server")) as server:
        assert server.execute(_PENDING, (case["system_id"], job_id)).fetchone() == (
            deviation == "pending",
        )


def test_other_disposition_and_unknown_system_are_not_pending(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url, disposition="retained-quarantine")
    with psycopg.connect(authority_role_dsns("kdive_server")) as server:
        assert server.execute(_PENDING, (case["system_id"], case["job_id"])).fetchone() == (False,)
        assert server.execute(_PENDING, (uuid4(), uuid4())).fetchone() == (False,)


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("kdive_server", True),
        ("kdive_reconciler", False),
        ("kdive_worker", False),
        ("kdive_provider_authority", False),
    ],
)
def test_only_the_server_may_execute(
    authority_role_dsns: _RoleDsns, role: str, allowed: bool
) -> None:
    with psycopg.connect(authority_role_dsns(role), autocommit=True) as conn:
        if allowed:
            assert conn.execute(_PENDING, (uuid4(), uuid4())).fetchone() == (False,)
            return
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(_PENDING, (uuid4(), uuid4()))


def _await_lock_wait(migrated_url: str, pid: int) -> None:
    with psycopg.connect(migrated_url, autocommit=True) as observer:
        for _ in range(200):
            row = observer.execute(
                "SELECT count(*) FROM pg_locks WHERE pid=%s AND NOT granted", (pid,)
            ).fetchone()
            if row == (1,):
                return
            time.sleep(0.05)
    raise AssertionError("the server call never waited on the System lock")


def test_answers_after_the_system_lock_holder_commits(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _case(migrated_url)
    params = (case["system_id"], case["job_id"])
    answers: list[Any] = []
    with (
        psycopg.connect(migrated_url) as holder,
        psycopg.connect(authority_role_dsns("kdive_server")) as server,
    ):
        holder.execute(_LOCK, (case["system_id"],))
        waiter = threading.Thread(
            target=lambda: answers.append(server.execute(_PENDING, params).fetchone())
        )
        waiter.start()
        _await_lock_wait(migrated_url, server.info.backend_pid)
        holder.execute(
            "UPDATE authority_system_attempts SET consumed_at=clock_timestamp() WHERE system_id=%s",
            (case["system_id"],),
        )
        holder.commit()
        waiter.join(timeout=10)
    assert answers == [(False,)]
```

2. Run the focused command; expect every case to fail with `UndefinedFunction` (or the
   registration test with `[] != [...]`).
3. Create `src/kdive/db/schema/0196_authority_system_receipt_pending.sql`:

```sql
-- 0196_authority_system_receipt_pending.sql — #3627, ADR-0620 (2026-11-13 amendment).
-- Forward-only (ADR-0015). `systems.teardown` restarts an exhausted preactivation teardown whose
-- final lease lapsed (#3617). When that attempt already recorded a `preactivation-absent` receipt,
-- the reconciler teardown repair owns the row; a restart would leave a queued row behind a
-- `torn-down` owner. The server cannot read `authority_system_attempts`, so this definer answers
-- the repair candidate's receipt clauses for one System.

CREATE FUNCTION public.authority_system_preactivation_receipt_pending(
    p_system_id uuid, p_job_id uuid
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member') THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    -- Every receipt writer and the teardown repair take this lock first; holding it to commit
    -- keeps the answer true for the caller's recycle statement.
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || p_system_id::text, 2125));
    RETURN EXISTS (
        SELECT 1
        FROM public.authority_system_attempts AS attempt
        JOIN public.authority_system_ownership AS ownership
          ON ownership.current_attempt_id = attempt.id
        WHERE ownership.system_id = p_system_id AND attempt.job_id = p_job_id
          AND attempt.operation = 'preactivation-teardown'
          AND attempt.state = 'terminal' AND attempt.consumed_at IS NULL
          AND attempt.receipt_disposition = 'preactivation-absent'
          AND ownership.state = 'teardown-requested'
          AND ownership.journal_sequence = attempt.terminal_head_sequence
          AND ownership.journal_digest = attempt.terminal_head_digest
    );
END
$$;

REVOKE ALL ON FUNCTION public.authority_system_preactivation_receipt_pending(uuid, uuid)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.authority_system_preactivation_receipt_pending(uuid, uuid)
TO kdive_server;
```

4. Append the 0196 entry after every `0195` entry: the tuple
   `("0196", "0196_authority_system_receipt_pending.sql"),` in the `migrations[-91:]` list of
   `tests/db/test_migrate.py` (slice → `[-92:]`), the `[-87:]` lists of the 0091 and 0102 tests
   (→ `[-88:]`) and the `[-74:]` list of the 0115 test (→ `[-75:]`); and the string `"0196",` in
   the two version-string lists of `tests/db/test_migrate.py` that end with `"0195",`.
   `rg -n '"0195"' tests/db` must then show a `0196` line after each hit.
5. Run both focused commands; expect all pass. Commit
   `feat(db): add the pending absence-receipt predicate (#3627)`.

## Task 2 — the service downgrade, its end-to-end proof, and the runbook

**Interfaces.** Consumes Task 1's function. `enqueue_preactivation_teardown` keeps its
signature.

**Verification.**
- Contract S1 — Mode: focused-test;
  `test_exhausted_preactivation_teardown_keeps_a_pending_absence_receipt[running|failed]`; red: the
  row comes back `("queued", 3, 6)` instead of `(<state>, 3, 3)`; green: `uv run python -m pytest
  tests/services/systems/test_authority_admission.py -q -k "pending_absence_receipt or
  recycles_only_after_lease_lapse"` (the second selector is S2).
- Contract "runbook procedure" — Mode: task-test-not-applicable; prose, no executable consumer.

Steps:

1. Add to `tests/services/systems/test_authority_admission.py` (add imports `from typing import
   Any, cast` and `from kdive.reconciler.repairs.jobs import repair_terminal_authority_system_attempts`):

```python
class _NoArtifacts:
    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


@pytest.mark.parametrize("state", ["running", "failed"])
def test_exhausted_preactivation_teardown_keeps_a_pending_absence_receipt(
    migrated_url: str, request: pytest.FixtureRequest, state: str
) -> None:
    """#3627, ADR-0620: the repair, not a restart, settles a pending absence receipt."""
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    first = systems_support.ctx(role=Role.ADMIN)
    second = replace(first, principal="user-2")

    async def provision_and_request_teardown() -> tuple[UUID, str]:
        async with systems_support.pool(migrated_url) as pool:
            system_id = await _provision_authority_system(pool)
            queued = await teardown_system(pool, first, str(system_id))
            assert queued.status == "queued"
            return system_id, str(queued.object_id)

    async def operator_retry(system_id: UUID) -> str:
        async with systems_support.pool(migrated_url) as pool:
            response = await teardown_system(pool, second, str(system_id))
        return str(response.object_id)

    async def reconcile() -> int:
        async with await psycopg.AsyncConnection.connect(role_dsns("kdive_reconciler")) as conn:
            return await repair_terminal_authority_system_attempts(conn, cast(Any, _NoArtifacts()))

    system_id, job_id = asyncio.run(provision_and_request_teardown())
    worker, credential = _register_worker(migrated_url, "receipt-teardown")
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state=%s,attempt=3,max_attempts=3,worker_id=%s,"
            "heartbeat_at=now(),lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (state, worker, job_id),
        )
        marker = conn.execute(
            "SELECT payload->'authority_system_v1' FROM jobs WHERE id=%s", (job_id,)
        ).fetchone()
        assert marker is not None
        case: dict[str, object] = {
            "system_id": system_id,
            "job_id": UUID(job_id),
            "worker": worker,
            "credential": credential,
            "marker": marker[0],
        }
        _seed_direct_terminal_attempt(conn, case, disposition="preactivation-absent")

    assert asyncio.run(operator_retry(system_id)) == job_id
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state,attempt,max_attempts FROM jobs WHERE id=%s", (job_id,)
        ).fetchone() == (state, 3, 3)
        assert conn.execute(
            "SELECT count(*) FROM audit_log WHERE transition='teardown:recycled' AND object_id=%s",
            (system_id,),
        ).fetchone() == (0,)

    # The repair marks only a `running` job `succeeded`; a `failed` one stays `failed`.
    assert asyncio.run(reconcile()) >= 1
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=%s", (job_id,)).fetchone() == (
            "succeeded" if state == "running" else "failed",
        )
        assert conn.execute("SELECT state FROM systems WHERE id=%s", (system_id,)).fetchone() == (
            "torn_down",
        )
```

2. Run the focused command; expect both cases to fail at `("queued", 3, 6) == (<state>, 3, 3)`.
3. In `src/kdive/services/systems/authority_owned.py` `enqueue_preactivation_teardown`, between
   the marker-conflict `raise` and `queue.enqueue_with_status`, insert:

```python
    if (
        prior is not None
        and recycle is not queue.JobRecyclePolicy.NEVER
        and await _preactivation_receipt_pending(conn, system.id, prior.id)
    ):
        # The reconciler teardown repair owns an unconsumed absence receipt (ADR-0620, #3627).
        recycle = queue.JobRecyclePolicy.NEVER
```

   and add above the function:

```python
async def _preactivation_receipt_pending(
    conn: AsyncConnection, system_id: UUID, job_id: UUID
) -> bool:
    """Whether the job's current attempt holds an unconsumed absence receipt, under the SQL lock."""
    cursor = await conn.execute(
        "SELECT authority_system_preactivation_receipt_pending(%s,%s)", (system_id, job_id)
    )
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("authority System receipt-pending check returned no row")
    return bool(row[0])
```

   In the docstring, replace "whose final attempt's lease lapsed without a receipt" with "whose
   final attempt's lease lapsed, unless that attempt holds an unconsumed absence receipt the
   reconciler repair owns (#3627)". In `src/kdive/mcp/tools/lifecycle/systems/admin.py`
   `_enqueue_preactivation_authority_teardown`, change the comment "A failed job, or one whose
   final attempt's lease lapsed, restarts (ADR-0620, #3617)." to "A failed job, or one whose final
   attempt's lease lapsed, restarts unless an absence receipt is pending (ADR-0620, #3617, #3627)."
4. Run the focused command and `uv run python -m pytest
   tests/services/systems/test_teardown_recycle_policy.py -q`; expect all pass.
5. In `docs/operating/runbooks/exhausted-authority-system-provision.md`, Limits → "Authority
   unavailable", replace the sentences from "Also check the reconciler log" through "a restarted
   teardown cannot finish while such a receipt is pending." with: "If the authority already
   proved absence and the worker died before finalizing, `systems.teardown` returns the same
   job without restarting it and it stays `running`; the reconciler's teardown repair finishes it.
   If it stays `running` across reconciler passes, check the reconciler log for
   `authority System teardown attempt <id> still has core cleanup` or
   `authority System teardown repair failed for attempt <id>` and fix that cleanup; a
   `authority System teardown attempt <id> was superseded` line means the repair will not finish
   it. The reconciler must be running. The `<id>` is an authority attempt, not the System, and no
   MCP tool maps it to a System."
6. Run `just lint`, `just type`; commit
   `fix(system-authority): keep a pending absence receipt's teardown (#3627)`.
