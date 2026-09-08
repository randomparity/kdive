"""The reconciler repair for mutation obligations leaked on torn-down Systems (ADR-0634, #2326).

The teardown handler commits `torn_down` before the discharge that follows it, and
`systems.teardown` then short-circuits on that terminal state without enqueueing a job, so a
teardown that ends between the two leaves the obligation open with nothing to reach it again.
`retained_owners` then holds the attempt's volumes out of the module-volume sweep forever.

Two things these arms exist to hold in place:

- **The exclusion is not job state alone.** `RUNNING -> CANCELED` is legal, `jobs.cancel` does not
  fence an ordinary teardown, and the worker never aborts a running handler — so an operator cancel
  takes the job out of `queued`/`running` while the teardown keeps running. The settle-window arms
  are what stop the repair acting inside that window, and the settled arm is what stops the window
  from disabling the repair outright.
- **The write is a function grant, never a table grant.** The reconciler-role arm drives the whole
  lane over a real `kdive_reconciler` LOGIN principal, so a direct `UPDATE` would fail with
  `permission denied for table` (ADR-0629, migration 0152).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope
from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.capacity.state import JobState, SystemState
from kdive.reconciler import loop
from kdive.reconciler.repairs import systems as repairs_systems
from kdive.reconciler.repairs.systems import repair_leaked_mutation_obligations
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system

_NONCE = "0" * 32
_SECOND_NONCE = "1" * 32
_READ_DISCHARGE = (
    "SELECT mutation_discharged_at, mutation_discharge_reason "
    "FROM remote_module_attempt_obligations WHERE system_id = %s ORDER BY operation_nonce"
)


async def _seed_open_obligation(
    conn: psycopg.AsyncConnection,
    *,
    system_state: SystemState = SystemState.TORN_DOWN,
    nonces: tuple[str, ...] = (_NONCE,),
) -> UUID:
    """A System in ``system_state`` carrying one open mutation obligation per nonce."""
    system_id = await seed_system(conn, system_state=system_state)
    run_id = await seed_run(conn, system_id)
    repository = RemoteModuleAttemptObligationRepository()
    for nonce in nonces:
        assert await repository.open_mutation_obligation(
            conn, ModuleAttempt(system_id=system_id, run_id=run_id, operation_nonce=nonce)
        )
    return system_id


async def _seed_teardown_job(
    conn: psycopg.AsyncConnection, system_id: UUID, *, state: str, age_seconds: int = 0
) -> None:
    """A teardown job at the dedup key both teardown families use, in ``state``.

    ``age_seconds`` backdates ``updated_at`` so an arm can put a terminal job outside the settle
    window. It goes in the INSERT because `jobs_set_updated_at` is a BEFORE UPDATE trigger that
    reassigns the column unconditionally, so a later UPDATE that backdates it is overwritten.
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


async def _discharges(conn: psycopg.AsyncConnection, system_id: UUID) -> list[tuple[Any, Any]]:
    rows = await (await conn.execute(_READ_DISCHARGE, (system_id,))).fetchall()
    return [(row[0], row[1]) for row in rows]


async def _run(url: str) -> int:
    """Run one repair pass over a real (non-autocommit) pool, as the reconciler does."""
    async with AsyncConnectionPool(url, min_size=1, open=False) as pool:
        await pool.open()
        return await run_repair(pool, repair_leaked_mutation_obligations)


def test_leaked_obligation_on_torn_down_system_is_discharged(migrated_url: str) -> None:
    """The reported leak, plus the bystander that pins the discharge to one System.

    Without the bystander, dropping the function's `WHERE system_id = …` bound would leave this
    arm green.
    """

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)
        bystander_id = await _seed_open_obligation(conn)
        await _seed_teardown_job(conn, bystander_id, state=JobState.RUNNING.value)

        discharged = await _run(migrated_url)

        assert discharged == 1
        repaired = await _discharges(conn, system_id)
        assert len(repaired) == 1
        assert repaired[0][0] is not None
        assert repaired[0][1] == "terminal_escape"
        assert await _discharges(conn, bystander_id) == [(None, None)]
        await conn.close()

    asyncio.run(run())


@pytest.mark.parametrize("job_state", [JobState.QUEUED.value, JobState.RUNNING.value])
def test_active_teardown_job_defers_the_repair(migrated_url: str, job_state: str) -> None:
    """A teardown still queued or running is the window the issue's exclusion 1 names."""

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)
        await _seed_teardown_job(conn, system_id, state=job_state)

        assert await _run(migrated_url) == 0
        assert await _discharges(conn, system_id) == [(None, None)]
        await conn.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "job_state",
    [JobState.CANCELED.value, JobState.FAILED.value, JobState.SUCCEEDED.value],
)
def test_recently_terminal_teardown_job_defers_the_repair(
    migrated_url: str, job_state: str
) -> None:
    """The operator-cancel window: the job is terminal while its handler keeps running.

    `RUNNING -> CANCELED` is legal, `jobs.cancel` does not fence an ordinary teardown, and the
    worker never aborts the handler, so job state alone would let the repair act mid-teardown.
    """

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)
        await _seed_teardown_job(conn, system_id, state=job_state)

        assert await _run(migrated_url) == 0
        assert await _discharges(conn, system_id) == [(None, None)]
        await conn.close()

    asyncio.run(run())


def test_settled_terminal_teardown_job_does_not_defer_the_repair(migrated_url: str) -> None:
    """The settle window must not disable the repair — this is what bounds the arm above."""

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)
        await _seed_teardown_job(conn, system_id, state=JobState.SUCCEEDED.value, age_seconds=3600)

        assert await _run(migrated_url) == 1
        assert (await _discharges(conn, system_id))[0][1] == "terminal_escape"
        await conn.close()

    asyncio.run(run())


def test_non_terminal_system_is_untouched(migrated_url: str) -> None:
    """A live System's obligation is the recovery point ADR-0588 retains; never discharge it."""

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn, system_state=SystemState.READY)

        assert await _run(migrated_url) == 0
        assert await _discharges(conn, system_id) == [(None, None)]
        await conn.close()

    asyncio.run(run())


def test_second_pass_is_a_noop(migrated_url: str) -> None:
    """First-write-wins: the `mutation_discharged_at IS NULL` predicate holds the first evidence."""

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)

        assert await _run(migrated_url) == 1
        first = await _discharges(conn, system_id)
        assert await _run(migrated_url) == 0
        assert await _discharges(conn, system_id) == first
        await conn.close()

    asyncio.run(run())


def test_count_is_obligation_rows(migrated_url: str) -> None:
    """The repair kind is named for obligations, so the count is rows and not Systems."""

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn, nonces=(_NONCE, _SECOND_NONCE))

        assert await _run(migrated_url) == 2
        repaired = await _discharges(conn, system_id)
        assert len(repaired) == 2
        assert all(at is not None for at, _ in repaired)
        assert [reason for _, reason in repaired] == ["terminal_escape", "terminal_escape"]
        await conn.close()

    asyncio.run(run())


def test_one_failing_candidate_does_not_starve_the_rest(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unordered, unbounded candidate set lets a poison row block every System behind it."""

    async def run() -> None:
        conn = await connect(migrated_url)
        await _seed_open_obligation(conn)
        await _seed_open_obligation(conn)

        repo = RemoteModuleAttemptObligationRepository
        original = repo.worker_discharge_system_mutation_obligations
        calls = {"n": 0}

        async def flaky(
            self: RemoteModuleAttemptObligationRepository,
            connection: psycopg.AsyncConnection,
            system_id: UUID,
        ) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first candidate fails")
            return await original(self, connection, system_id)

        monkeypatch.setattr(
            RemoteModuleAttemptObligationRepository,
            "worker_discharge_system_mutation_obligations",
            flaky,
        )

        with caplog.at_level("ERROR", logger="kdive.reconciler.repairs.systems"):
            assert await _run(migrated_url) == 1
        assert calls["n"] == 2
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "1 of 2 leaked mutation obligation candidates failed" in errors[0].getMessage()
        await conn.close()

    asyncio.run(run())


def test_repair_runs_under_the_reconciler_role(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """The whole lane over real `kdive_reconciler` grants: a direct UPDATE would be denied.

    `kdive_reconciler` holds SELECT only on the obligations table (0126:230-232) and EXECUTE on
    the ADR-0629 definer function (0152:39-40), so this passes only through the function.
    """

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)

        async with AsyncConnectionPool(
            authority_role_dsns("kdive_reconciler"), min_size=1, open=False
        ) as pool:
            await pool.open()
            discharged = await run_repair(pool, repair_leaked_mutation_obligations)

        assert discharged == 1
        assert (await _discharges(conn, system_id))[0][1] == "terminal_escape"
        await conn.close()

    asyncio.run(run())


def test_total_failure_is_not_reported_as_nothing_to_do(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pass whose candidates all fail returns 0 — the same count as a clean database.

    Per-candidate isolation is what makes that possible, so the lane owes an ERROR when it
    discharged nothing and something failed. Without it a systematic denial (the reconciler login
    losing its role membership, a statement timeout) is the silent failure #2326 was filed about.
    """

    async def run() -> None:
        conn = await connect(migrated_url)
        await _seed_open_obligation(conn)
        await _seed_open_obligation(conn)

        async def always_fails(
            self: RemoteModuleAttemptObligationRepository,
            connection: psycopg.AsyncConnection,
            system_id: UUID,
        ) -> int:
            raise RuntimeError("systematic discharge failure")

        monkeypatch.setattr(
            RemoteModuleAttemptObligationRepository,
            "worker_discharge_system_mutation_obligations",
            always_fails,
        )

        with caplog.at_level("ERROR", logger="kdive.reconciler.repairs.systems"):
            assert await _run(migrated_url) == 0

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "2 of 2 leaked mutation obligation candidates failed" in errors[0].getMessage()
        await conn.close()

    asyncio.run(run())


def test_teardown_job_appearing_under_the_lock_defers_the_candidate(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The locked re-read, not the candidate query, is what closes the selection race.

    The candidate query runs in its own transaction and the lock is taken per System afterwards, so
    a teardown can be enqueued in between. This drives that interleaving directly: the patched lock
    helper inserts the teardown job after acquiring, which is the only way to reach the `continue`
    on the re-read.
    """

    async def run() -> None:
        conn = await connect(migrated_url)
        system_id = await _seed_open_obligation(conn)

        real_lock = repairs_systems.advisory_xact_lock

        @asynccontextmanager
        async def racing_lock(
            connection: psycopg.AsyncConnection, scope: LockScope, key: UUID | str
        ) -> AsyncIterator[None]:
            async with real_lock(connection, scope, key):
                # A separate autocommit connection, so the insert is visible to the locked
                # transaction's re-read under READ COMMITTED.
                racer = await connect(migrated_url)
                await _seed_teardown_job(racer, system_id, state=JobState.RUNNING.value)
                await racer.close()
                yield

        monkeypatch.setattr(repairs_systems, "advisory_xact_lock", racing_lock)

        assert await _run(migrated_url) == 0
        assert await _discharges(conn, system_id) == [(None, None)]
        await conn.close()

    asyncio.run(run())


def test_repair_runs_after_abandoned_jobs() -> None:
    """abandoned_jobs is what makes a stuck teardown repairable *at all*, not what exposes it.

    A teardown whose worker died keeps its job `running` with a lapsed lease, and a `running` job
    defers this repair's candidate forever. `repair_abandoned_jobs` moves it to `failed`, which
    turns "deferred forever" into "deferred one settle window" — that write stamps
    `jobs.updated_at`, so the dead-lettered System is repaired a later pass rather than this one.
    """
    kinds = loop.ALL_REPAIR_KINDS

    assert "leaked_mutation_obligations" in kinds
    assert kinds.index("abandoned_jobs") < kinds.index("leaked_mutation_obligations")
