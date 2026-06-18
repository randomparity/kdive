"""DB-backed happy-path test for the worker-check dispatcher (ADR-0164).

Exercises the production pool wrappers (`_pool_enqueue`/`_pool_get`) and the real queue once:
the dispatcher enqueues a `diagnostics_worker_check` job, a concurrent stand-in worker claims and
completes it with a serialized result, and the dispatcher returns the real CheckResults. SKIPs
without Docker (the migrated disposable Postgres is unavailable).
"""

from __future__ import annotations

import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckResult,
    CheckStatus,
)
from kdive.diagnostics.result_codec import serialize_results
from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
from kdive.jobs import queue as job_queue

_WORKER_ID = "stand-in-worker"


async def _complete_when_claimable(url: str) -> None:
    """Claim the single queued diagnostics job and complete it with a serialized result."""
    serialized = serialize_results(
        [
            CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
            CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        ]
    )
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        for _ in range(200):
            claimed = await job_queue.dequeue(conn, _WORKER_ID)
            if claimed is not None:
                await job_queue.complete(conn, claimed.id, _WORKER_ID, serialized)
                return
            await asyncio.sleep(0.02)


def test_dispatch_enqueue_poll_complete_roundtrip(migrated_url: str) -> None:
    async def _run() -> list[CheckResult]:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            dispatcher = JobWorkerCheckDispatcher(
                pool,
                provider="remote-libvirt",
                worker_check_ids=(PROVIDER_TLS_ID, GDBSTUB_ACL_ID),
                poll_interval=0.02,
                dedup_suffix="dbtest",
            )
            worker = asyncio.create_task(_complete_when_claimable(migrated_url))
            results = await dispatcher.run_worker_checks()
            await worker
            return results

    results = asyncio.run(_run())
    assert {r.check_id for r in results} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}
    assert {r.status for r in results} == {CheckStatus.PASS}
