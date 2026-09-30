"""Reconciler recovery for a stranded `reprovisioning` System (#2980, ADR-0435 amendment).

`reprovisioning` leaves only through the reprovision handler, and since #2928 `systems.teardown`
refuses it, so a reprovision job that dead-lettered or was canceled strands the System. The repair
drives it to `failed`. Two things these arms hold in place:

- **Job state alone does not bound a handler.** `jobs.cancel` and a lapsed lease both leave a
  non-capture handler running, and a reclaimed attempt can fail terminally while the lapsed one
  still runs. So a canceled row, a `lease_expired` row and a multi-attempt failed row defer the
  repair for the settle window; only a single-attempt worker-finalized failure settles at once.
- **The recheck under the System lock is what closes the selection race.**
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.errors import ErrorCategory
from kdive.reconciler import loop
from kdive.reconciler.repairs import systems as repairs_systems
from kdive.reconciler.repairs.systems import repair_stalled_reprovisioning_systems
from tests.db.external_boot_authority_support import _RoleDsns
from tests.reconciler.conftest import connect, run_repair, seed_system

_SETTLED_AGE = 16 * 60
_LEASE_EXPIRED = ErrorCategory.LEASE_EXPIRED.value
_INFRA = ErrorCategory.INFRASTRUCTURE_FAILURE.value


async def _seed_job(
    conn: psycopg.AsyncConnection,
    system_id: UUID,
    *,
    state: str,
    error_category: str | None = None,
    attempt: int = 1,
    age_seconds: int = 0,
) -> None:
    """A reprovision job for ``system_id``, last written ``age_seconds`` ago.

    ``updated_at`` is backdated in the INSERT because `jobs_set_updated_at` is a BEFORE UPDATE
    trigger that overwrites the column on any later UPDATE.
    """
    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key, "
        "    error_category, updated_at) "
        "VALUES ('reprovision', %s, %s, %s, 3, %s, %s, %s, now() - make_interval(secs => %s))",
        (
            Jsonb({"system_id": str(system_id), "profile_digest": "d"}),
            state,
            attempt,
            Jsonb({"principal": "alice", "agent_session": None, "project": "proj"}),
            f"{system_id}:reprovision:{uuid4()}",
            error_category,
            age_seconds,
        ),
    )


async def _state_and_category(
    conn: psycopg.AsyncConnection, system_id: UUID
) -> tuple[str, str | None]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT state, failure_category FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def _audit_count(conn: psycopg.AsyncConnection, system_id: UUID) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log WHERE object_kind = 'systems' AND object_id = %s "
            "AND transition = 'reprovisioning->failed' AND tool = 'systems.reprovision'",
            (system_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _repair(url: str) -> int:
    async with AsyncConnectionPool(url, min_size=1, open=False) as pool:
        await pool.open()
        return await run_repair(pool, repair_stalled_reprovisioning_systems)


_FAILED = SystemState.FAILED.value
_REPROVISIONING = SystemState.REPROVISIONING.value
_INCOMPLETE = ErrorCategory.REPROVISION_INCOMPLETE.value


def test_system_with_no_reprovision_job_settles_with_category_and_audit(
    migrated_url: str,
) -> None:
    async def run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        assert await _repair(migrated_url) == 1
        assert await _state_and_category(conn, sid) == (_FAILED, _INCOMPLETE)
        assert await _audit_count(conn, sid) == 1
        assert await _repair(migrated_url) == 0  # a second pass is a no-op
        await conn.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error_category", "expected"),
    [(None, _INCOMPLETE), (_INFRA, None), (ErrorCategory.CONFIGURATION_ERROR.value, None)],
)
def test_single_attempt_failure_settles_without_the_window(
    migrated_url: str, error_category: str | None, expected: str | None
) -> None:
    """The worker finalized the only attempt after its handler returned: nothing is running.

    A category the job already recorded outranks the limbo verdict (ADR-0513 §1a), so the column
    stays NULL and the job fallback answers.
    """

    async def run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        await _seed_job(conn, sid, state=JobState.FAILED.value, error_category=error_category)
        assert await _repair(migrated_url) == 1
        assert await _state_and_category(conn, sid) == (_FAILED, expected)
        await conn.close()

    asyncio.run(run())


_WINDOWED = [
    pytest.param(JobState.CANCELED.value, None, 1, _INCOMPLETE, id="canceled"),
    pytest.param(JobState.FAILED.value, _LEASE_EXPIRED, 3, _INCOMPLETE, id="lease-expired"),
    pytest.param(JobState.FAILED.value, _INFRA, 3, None, id="reclaimed-attempt"),
]


@pytest.mark.parametrize(("state", "error_category", "attempt", "expected"), _WINDOWED)
def test_windowed_row_defers_until_it_settles(
    migrated_url: str,
    state: str,
    error_category: str | None,
    attempt: int,
    expected: str | None,
) -> None:
    """A handler may still be rebuilding the disk behind these rows, so they wait 15 minutes."""

    async def run() -> None:
        conn = await connect(migrated_url)
        fresh = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        await _seed_job(conn, fresh, state=state, error_category=error_category, attempt=attempt)
        settled = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        await _seed_job(
            conn,
            settled,
            state=state,
            error_category=error_category,
            attempt=attempt,
            age_seconds=_SETTLED_AGE,
        )
        assert await _repair(migrated_url) == 1
        assert await _state_and_category(conn, fresh) == (_REPROVISIONING, None)
        assert await _audit_count(conn, fresh) == 0
        assert await _state_and_category(conn, settled) == (_FAILED, expected)
        await conn.close()

    asyncio.run(run())


@pytest.mark.parametrize("state", [JobState.QUEUED.value, JobState.RUNNING.value])
def test_active_job_defers_at_any_age(migrated_url: str, state: str) -> None:
    async def run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        await _seed_job(conn, sid, state=JobState.CANCELED.value, age_seconds=_SETTLED_AGE)
        await _seed_job(conn, sid, state=state, age_seconds=_SETTLED_AGE)
        assert await _repair(migrated_url) == 0
        assert await _state_and_category(conn, sid) == (_REPROVISIONING, None)
        await conn.close()

    asyncio.run(run())


def test_system_not_reprovisioning_is_untouched(migrated_url: str) -> None:
    async def run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.READY)
        await _seed_job(conn, sid, state=JobState.FAILED.value)
        assert await _repair(migrated_url) == 0
        assert await _state_and_category(conn, sid) == (SystemState.READY.value, None)
        await conn.close()

    asyncio.run(run())


def test_job_appearing_under_the_lock_defers_the_candidate(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate query and the lock are separate; the locked re-read closes the gap."""

    async def run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        real_lock = repairs_systems.advisory_xact_lock

        @asynccontextmanager
        async def racing_lock(
            connection: psycopg.AsyncConnection, scope: LockScope, key: UUID | str
        ) -> AsyncIterator[None]:
            async with real_lock(connection, scope, key):
                # Autocommit, so the insert is visible to the locked re-read under READ COMMITTED.
                racer = await connect(migrated_url)
                await _seed_job(racer, sid, state=JobState.RUNNING.value)
                await racer.close()
                yield

        monkeypatch.setattr(repairs_systems, "advisory_xact_lock", racing_lock)
        assert await _repair(migrated_url) == 0
        assert await _state_and_category(conn, sid) == (_REPROVISIONING, None)
        await conn.close()

    asyncio.run(run())


def test_repair_runs_under_the_reconciler_role(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        conn = await connect(migrated_url)
        sid = await seed_system(conn, system_state=SystemState.REPROVISIONING)
        assert await _repair(authority_role_dsns("kdive_reconciler")) == 1
        assert await _state_and_category(conn, sid) == (_FAILED, _INCOMPLETE)
        assert await _audit_count(conn, sid) == 1
        await conn.close()

    asyncio.run(run())


def test_repair_runs_after_abandoned_jobs() -> None:
    """abandoned_jobs dead-letters a zombie reprovision job; this lane then waits one window."""
    kinds = loop.ALL_REPAIR_KINDS
    assert kinds.index("abandoned_jobs") < kinds.index("stalled_reprovisioning_systems")
