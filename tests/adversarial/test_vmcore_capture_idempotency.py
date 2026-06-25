"""Adversarial: per-Run vmcore capture stays idempotent under concurrency (ADR-0244).

The capture handler's first-method-wins guard is enforced under a per-Run advisory lock
(``LockScope.RUN``) in both ``precheck_run`` (before the slow ``capture()`` seam) and
``finalize_capture`` (the race backstop). The falsifying case: two workers dispatch the same
Run+method capture concurrently, both pass ``precheck_run`` before either ``finalize_capture``
commits (a ``threading.Barrier`` in the capture seam forces both past precheck), so the *finalize*
re-check is the sole thing that prevents a second core row. These tests pin that exactly one
Run-owned raw core persists, and that two distinct Runs retain two distinct cores.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import vmcore as vmcore_plane
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.providers.ports import CaptureOutput
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.systems_support import provider_resolver

_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")

_RAW_CORE_SQL = (
    "SELECT count(*) AS n FROM artifacts WHERE owner_kind = 'runs' AND owner_id = %s "
    "AND object_key LIKE '%%/vmcore-%%' AND object_key NOT LIKE '%%-redacted'"
)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=6, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _core(run_id: str, method: CaptureMethod = CaptureMethod.HOST_DUMP) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{method.value}", "e1", Sensitivity.SENSITIVE, "vmcore"
    )
    red = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{method.value}-redacted", "e2", Sensitivity.REDACTED, "vmcore"
    )
    return CaptureOutput(raw=raw, redacted=red, vmcore_build_id="deadbeef", raw_size_bytes=512)


class _BarrierRetriever:
    """A retriever whose capture seam rendezvouses on a barrier, forcing the precheck race."""

    def __init__(self, run_id: str, barrier: threading.Barrier) -> None:
        self._run_id = run_id
        self._barrier = barrier
        self._lock = threading.Lock()
        self.calls = 0

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        with self._lock:
            self.calls += 1
        self._barrier.wait()  # both handlers reach here only after both passed precheck
        return _core(self._run_id, method)


async def _raw_core_count(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_CORE_SQL, (run_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


def test_concurrent_same_run_capture_writes_one_core(migrated_url: str) -> None:
    # The barrier forces both handlers past precheck_run before either finalize commits, so the
    # finalize re-check (under LockScope.RUN) is the only thing preventing a second core. Exactly
    # one Run-owned raw core must persist, even though the capture seam ran twice.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.CAPTURE_VMCORE,
                    CaptureVmcorePayload(run_id=run_id, method=CaptureMethod.HOST_DUMP),
                    _AUTH,
                    f"{run_id}:capture_vmcore:host_dump",
                )
            barrier = threading.Barrier(2, timeout=15)
            retriever = _BarrierRetriever(run_id, barrier)
            resolver = provider_resolver(retriever=retriever)

            async def _handle() -> str | None:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(conn, job, resolver=resolver)

            results = await asyncio.gather(_handle(), _handle())
            count = await _raw_core_count(pool, run_id)

        assert retriever.calls == 2, "both handlers must pass precheck and capture (the race)"
        assert count == 1, "the per-Run lock + finalize re-check must keep it to one core"
        assert results[0] == results[1], "both return the same Run-owned core key"

    asyncio.run(_run())


def test_two_runs_on_one_system_retain_distinct_cores(migrated_url: str) -> None:
    # Two distinct Runs each capturing a core retain two distinct Run-owned cores — the artifact
    # multiplicity ADR-0244 delivers (asserted at the ownership level; one system_id crashes at
    # most once today, so this seeds two Runs on the same crashed System directly).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_a = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            run_b = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            for run_id in (run_a, run_b):
                async with pool.connection() as conn:
                    job = await queue.enqueue(
                        conn,
                        JobKind.CAPTURE_VMCORE,
                        CaptureVmcorePayload(run_id=run_id, method=CaptureMethod.HOST_DUMP),
                        _AUTH,
                        f"{run_id}:capture_vmcore:host_dump",
                    )
                resolver = provider_resolver(retriever=_PlainRetriever(run_id))
                async with pool.connection() as conn:
                    await vmcore_plane.capture_handler(conn, job, resolver=resolver)
            count_a = await _raw_core_count(pool, run_a)
            count_b = await _raw_core_count(pool, run_b)

        assert count_a == 1 and count_b == 1, "each Run keeps its own core, neither shadows"
        assert run_a != run_b

    asyncio.run(_run())


class _PlainRetriever:
    """A retriever that returns a deterministic Run-owned core with no rendezvous."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        return _core(self._run_id, method)
