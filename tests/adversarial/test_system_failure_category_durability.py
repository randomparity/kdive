"""Adversarial: a System's failure category survives a worker that dies before `queue.fail`.

`_run_handler` commits the handler's `systems.state = 'failed'` on an autocommit dispatch
connection and only *then* acquires a second pool connection for `queue.fail` — the write that
used to be the category's only durable home (`jobs.error_category`). Nothing spans the two, and
the gap needs no process death to open: `queue.fail` raising from inside the `except` block
escapes into `_claim_loop`, which logs and continues.

Two distinct losses follow from that one window, and this module pins both (ADR-0492, #1562):

1. **Attempts exhausted.** The job stays `running` until its lease lapses; `repair_abandoned_jobs`
   dead-letters it with `lease_expired` and an empty `failure_context`.
2. **Attempts remaining.** `queue.dequeue` reclaims the lapsed-lease job, the handler re-enters a
   System that is already `failed` (a `TERMINAL_SYSTEM_STATES` early return), and the job ends
   **`succeeded`** — so ADR-0454's "the newest terminal job did not fail" branch attributes
   nothing at all.

Both are simulated by running the real handler against a real database and then simply *not*
calling `queue.fail`, which is exactly what the worker does when it dies in the window.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import AllocationState, JobState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.payloads import SystemPayload
from kdive.mcp.tools.lifecycle.systems.view import get_system
from kdive.reconciler.repairs.jobs import repair_abandoned_jobs
from tests.adversarial.conftest import seed_allocation, seed_resource
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_WORKER = "worker-1"
_REASON = "base image volume not staged"

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
            "crashkernel": "256M",
        }
    },
}


class _FailingProvisioner:
    """A provisioner whose `provision` always raises a non-retryable configuration error."""

    def __init__(self) -> None:
        self.torn_down: list[str] = []

    def provision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: Any = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        del system_id, profile, overlay_customizers, bootstrap_pubkey
        raise CategorizedError(_REASON, category=ErrorCategory.CONFIGURATION_ERROR)

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)

    def read_resolved_cpu(self, system_id: object) -> None:
        del system_id
        return None


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(pool: AsyncConnectionPool) -> tuple[str, str]:
    async with pool.connection() as conn:
        resource = await seed_resource(conn, cap=4)
        allocation = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=SystemState.PROVISIONING,
                provisioning_profile=_PROFILE,
            ),
        )
    return str(system.id), str(allocation.id)


async def _enqueue_provision(
    pool: AsyncConnectionPool, system_id: str, *, max_attempts: int
) -> None:
    async with pool.connection() as conn:
        await queue.enqueue(
            conn,
            JobKind.PROVISION,
            SystemPayload(system_id=system_id),
            {"principal": "alice", "agent_session": "s", "project": "proj"},
            f"{system_id}:provision",
            max_attempts=max_attempts,
        )


async def _claim(pool: AsyncConnectionPool) -> Job:
    """Claim the queued provision job exactly as the worker's `dequeue` does."""
    async with pool.connection() as conn:
        job = await queue.dequeue(conn, _WORKER)
    assert job is not None, "expected a claimable provision job"
    return job


async def _lapse_lease(pool: AsyncConnectionPool, job_id: UUID) -> None:
    """Age the claimed job's lease past `now()` — the worker died without finalizing it."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = %s",
            (job_id,),
        )


async def _run_handler_until_it_raises(
    pool: AsyncConnectionPool, job: Job, provisioner: _FailingProvisioner
) -> None:
    """Run `provision_handler` on an autocommit connection, as `_run_handler` does.

    The worker would now acquire a *second* connection for `queue.fail`. This helper stops
    short of that on purpose: the whole point is what a worker that never got there leaves
    behind.
    """
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        with pytest.raises(CategorizedError) as excinfo:
            await systems_handlers.provision_handler(
                conn, job, resolver=provider_resolver(provisioner=provisioner)
            )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.terminal is True


async def _job_row(pool: AsyncConnectionPool, job_id: UUID) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state, error_category FROM jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    return row


async def _system_state(pool: AsyncConnectionPool, system_id: str) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row["state"])


def test_abandoned_job_does_not_erase_the_systems_failure_category(migrated_url: str) -> None:
    """Loss shape 1: attempts exhausted, so `repair_abandoned_jobs` stamps `lease_expired`.

    The job row truthfully records that *its* lease expired — that is not the defect and is
    asserted here so the fix is seen not to paper over it. The defect is that `systems.get`
    used to read the System's verdict off that row, reporting a retryable
    `infrastructure_failure`/`lease_expired` for a System failed by a non-retryable
    configuration error.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, _ = await _seed_system(pool)
            await _enqueue_provision(pool, system_id, max_attempts=1)
            job = await _claim(pool)
            await _run_handler_until_it_raises(pool, job, _FailingProvisioner())

            # The worker dies here: `queue.fail` never runs. The lease lapses and, with the
            # attempt already charged and exhausted, the reconciler dead-letters the zombie.
            await _lapse_lease(pool, job.id)
            async with pool.connection() as conn:
                assert await repair_abandoned_jobs(conn) == 1

            row = await _job_row(pool, job.id)
            assert row["state"] == JobState.FAILED.value
            assert row["error_category"] == ErrorCategory.LEASE_EXPIRED.value
            assert await _system_state(pool, system_id) == SystemState.FAILED.value

            resp = await get_system(
                pool, _ctx(), system_id, resolver=provider_resolver(provisioner=None)
            )
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
            assert resp.retryable is False

    asyncio.run(_run())


def test_reclaimed_job_that_re_enters_a_failed_system_keeps_the_real_category(
    migrated_url: str,
) -> None:
    """Loss shape 2: attempts remain, so `dequeue` reclaims and the retry ends `succeeded`.

    This is the commoner shape and the one #1562 does not describe. The reconciler never sees
    the job (its sweep requires `attempt >= max_attempts`); `dequeue` reclaims it, the handler
    re-enters a System already in `TERMINAL_SYSTEM_STATES` and early-returns, and the job lands
    `succeeded` with `error_category` NULL. ADR-0454's attribution then finds a newest terminal
    job that did **not** fail and reports nothing.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, _ = await _seed_system(pool)
            await _enqueue_provision(pool, system_id, max_attempts=3)
            first = await _claim(pool)
            await _run_handler_until_it_raises(pool, first, _FailingProvisioner())

            # The worker dies here: `queue.fail` never runs, so the job is neither dead-lettered
            # nor requeued. Attempts remain, so the next `dequeue` — not the reconciler — takes it.
            await _lapse_lease(pool, first.id)
            second = await _claim(pool)
            assert second.id == first.id
            assert second.attempt == 2

            reentry = _FailingProvisioner()
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                result = await systems_handlers.provision_handler(
                    conn, second, resolver=provider_resolver(provisioner=reentry)
                )
            assert result == system_id, "the terminal-state re-entry returns success (ADR-0128)"
            async with pool.connection() as conn:
                completed = await queue.complete(conn, second.id, _WORKER, result)
            assert completed is not None

            row = await _job_row(pool, first.id)
            assert row["state"] == JobState.SUCCEEDED.value
            assert row["error_category"] is None
            assert await _system_state(pool, system_id) == SystemState.FAILED.value

            resp = await get_system(
                pool, _ctx(), system_id, resolver=provider_resolver(provisioner=None)
            )
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
            assert resp.retryable is False

    asyncio.run(_run())


def test_recorded_failure_category_is_committed_with_the_failed_transition(
    migrated_url: str,
) -> None:
    """The category is on the System row the instant the handler's transaction commits.

    Read directly, on a fresh connection, before anything touches the job — the window's whole
    length. Without this the fix could satisfy the two tests above by any later repair.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, _ = await _seed_system(pool)
            await _enqueue_provision(pool, system_id, max_attempts=3)
            job = await _claim(pool)
            await _run_handler_until_it_raises(pool, job, _FailingProvisioner())

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, failure_category FROM systems WHERE id = %s", (system_id,)
                )
                row = await cur.fetchone()
            assert row is not None
            assert row["state"] == SystemState.FAILED.value
            assert row["failure_category"] == ErrorCategory.CONFIGURATION_ERROR.value

            job_row = await _job_row(pool, job.id)
            assert job_row["state"] == JobState.RUNNING.value, "queue.fail deliberately never ran"
            assert job_row["error_category"] is None

    asyncio.run(_run())
