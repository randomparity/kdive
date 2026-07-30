"""Adversarial: the capture holds a real, visible write lease across its write (ADR-0502, #1687).

The fence in ``_RECLAIMABLE_SQL`` is only worth as much as the mint's *ordering* and *visibility*,
and both are properties of the moment the provider starts streaming — not of anything the handler
returns. So these tests suspend the capture seam mid-write and interrogate the database from an
**independent connection**, which is the only vantage point that can tell a committed lease from an
uncommitted one and a released lock from a held one:

* the lease row must be **visible** to another session while the write is in flight. A mint that
  landed in a savepoint instead of a transaction returns without error, inserts the row, and fences
  nothing at all, because the sweep reads committed rows;
* the owner's advisory lock must be **free** while the write is in flight. ``precheck_run`` releases
  ``LockScope.RUN`` before the capture by design (ADR-0244) precisely so a multi-GiB stream is not
  held under it, and a mint whose transaction degraded to a savepoint holds that lock until the
  handler returns — reversing that decision silently.

Both were true of a first implementation of this ADR that passed every row-level test, so neither is
hypothetical.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult, StoredArtifact
from kdive.artifacts.upload_manifest import RUN_UPLOAD_OWNER, lock_scope_for
from kdive.artifacts.write_lease import hold_write_lease, reap_stale_write_leases
from kdive.db.locks import require_top_level_transaction, try_advisory_xact_lock
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.artifacts import vmcore as vmcore_plane
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.providers.ports.retrieve import CaptureOutput
from tests.capture_store import WrittenObjects
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.systems_support import provider_resolver

_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")
_METHOD = CaptureMethod.HOST_DUMP


def _core(run_id: str) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{_METHOD.value}", "e1", Sensitivity.SENSITIVE, "vmcore"
    )
    redacted = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{_METHOD.value}-redacted", "e2", Sensitivity.REDACTED, "vmcore"
    )
    return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id="deadbeef", raw_size_bytes=512)


class _SuspendedCapture:
    """A retriever (and the store the finalize verifies through) that pauses inside ``capture``.

    ``capture`` runs on the ``to_thread`` worker, so a probe scheduled on the event loop can observe
    the database *while the write is in flight* — the only window in which the lease's visibility
    and the lock's freedom are the questions ADR-0502 is about. ``entered`` releases the probe;
    ``resume`` lets the capture finish.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._objects = WrittenObjects()
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.raised: BaseException | None = None

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.entered.set()
        assert self.resume.wait(timeout=30), "the probe never released the capture"
        if self.raised is not None:
            raise self.raised
        return self._objects.record(_core(self._run_id))

    def head(self, key: str) -> HeadResult | None:
        return self._objects.head(key)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=6, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seeded_capture_job(pool: AsyncConnectionPool) -> tuple[str, Job]:
    sys_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn,
            JobKind.CAPTURE_VMCORE,
            CaptureVmcorePayload(run_id=run_id, method=_METHOD),
            _AUTH,
            f"{run_id}:capture_vmcore:{_METHOD.value}",
        )
    return run_id, job


async def _leases_for(url: str, run_id: str) -> list[UUID]:
    """The committed lease holders for this Run, read on a connection of its own.

    A separate connection rather than the handler's, because that is the whole assertion: an
    uncommitted mint is visible to its own session and to nobody else, and the sweep is nobody else.
    """
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as probe:
        cur = await probe.execute(
            "SELECT job_id FROM object_write_leases WHERE owner_kind = %s AND owner_id = %s",
            (RUN_UPLOAD_OWNER, run_id),
        )
        return [row[0] for row in await cur.fetchall()]


async def _owner_lock_is_free(url: str, run_id: str) -> bool:
    """Whether ``(RUN, run_id)`` can be taken right now — i.e. the capture is not holding it."""
    async with await psycopg.AsyncConnection.connect(url) as probe, probe.transaction():
        return await try_advisory_xact_lock(probe, lock_scope_for(RUN_UPLOAD_OWNER), UUID(run_id))


async def _drive_capture_and_probe(
    url: str, *, fail_with: BaseException | None = None
) -> tuple[str, Job, list[UUID], bool, object]:
    """Run one ``capture_handler`` and sample the database from outside while it writes.

    Returns ``(run_id, job, in_flight_leases, lock_free_in_flight, outcome)`` where ``outcome``
    is the handler's return value or the exception it raised.
    """
    async with _pool(url) as pool:
        run_id, job = await _seeded_capture_job(pool)
        retriever = _SuspendedCapture(run_id)
        retriever.raised = fail_with

        async def _handle() -> object:
            async with pool.connection() as conn:
                try:
                    return await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=retriever,
                    )
                except BaseException as exc:  # noqa: BLE001 - handed back for the caller to assert
                    return exc

        handler = asyncio.create_task(_handle())
        try:
            await asyncio.to_thread(retriever.entered.wait, 30)
            assert retriever.entered.is_set(), "the capture seam was never reached"
            in_flight = await _leases_for(url, run_id)
            lock_free = await _owner_lock_is_free(url, run_id)
        finally:
            retriever.resume.set()
        outcome = await handler
        return run_id, job, in_flight, lock_free, outcome


def test_the_lease_is_committed_and_visible_while_the_capture_writes(migrated_url: str) -> None:
    """The mint's visibility, sampled from another session mid-write.

    This is the assertion a savepoint mint fails. It inserts the row, raises nothing, and returns —
    but the row is invisible outside its own session until the handler's connection commits, which
    is
    *after* the write it was supposed to fence. The sweep's classify reads committed rows, so such a
    lease fences exactly nothing.
    """

    async def _run() -> None:
        _run_id, job, in_flight, _lock_free, outcome = await _drive_capture_and_probe(migrated_url)
        assert in_flight == [job.id], "the lease must be committed before the write begins"
        assert isinstance(outcome, str), f"the capture should have succeeded, got {outcome!r}"

    asyncio.run(_run())


def test_the_run_lock_is_not_held_across_the_capture(migrated_url: str) -> None:
    """ADR-0244's release of ``LockScope.RUN`` before the capture survives the mint.

    The lease exists so a writer can declare itself *without* holding a lock across a multi-GiB
    stream, so a mint that leaves the Run lock held for the duration has reintroduced exactly the
    cost it was designed to avoid — and would deadlock the sweep's own owner-locked delete into
    skipping every key of an active Run for as long as the capture ran.
    """

    async def _run() -> None:
        _run_id, _job, _in_flight, lock_free, outcome = await _drive_capture_and_probe(migrated_url)
        assert lock_free, "the capture must not hold LockScope.RUN while it writes (ADR-0244)"
        assert isinstance(outcome, str), f"the capture should have succeeded, got {outcome!r}"

    asyncio.run(_run())


def test_a_successful_capture_releases_its_lease(migrated_url: str) -> None:
    """``finalize_capture`` drops the lease, so a finished capture stops fencing its prefix.

    Paired with the visibility test above: together they pin held-then-released rather than merely
    "a row exists at some point", which a mint with no release would also satisfy.
    """

    async def _run() -> None:
        run_id, job, in_flight, _lock_free, outcome = await _drive_capture_and_probe(migrated_url)
        assert in_flight == [job.id]
        assert isinstance(outcome, str)
        assert await _leases_for(migrated_url, run_id) == []

    asyncio.run(_run())


def test_a_failed_capture_leaves_its_lease_for_the_reap(migrated_url: str) -> None:
    """The ``except Exception:`` deliberately does not release — and the reap is what collects it.

    Releasing on the failure path would be a fence that holds only for the failures Python observed:
    a SIGKILLed worker releases nothing, so the reap has to exist regardless, and a second release
    path would only make the row's absence a weaker signal. What this pins is that the row survives
    the failure *and* that the reap then collects it, since the job the queue never renewed is not a
    live holder.
    """

    async def _run() -> None:
        boom = CategorizedError("the capture failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        run_id, job, in_flight, _lock_free, outcome = await _drive_capture_and_probe(
            migrated_url, fail_with=boom
        )
        assert in_flight == [job.id]
        assert outcome is boom
        assert await _leases_for(migrated_url, run_id) == [job.id], "the failure path must not drop"
        async with await psycopg.AsyncConnection.connect(migrated_url) as reaper:
            assert await reap_stale_write_leases(reaper) == 1
        assert await _leases_for(migrated_url, run_id) == []

    asyncio.run(_run())


def test_minting_inside_an_open_transaction_is_refused(migrated_url: str) -> None:
    """The savepoint degradation is refused rather than silently accepted.

    The guard is what stops the ordering in ``capture_handler`` from rotting: one added read before
    the mint puts a non-autocommit connection in a transaction, after which the mint would commit
    nothing until the handler returned and would hold the Run lock the whole time. Both failures are
    invisible at the call site, and this is the only thing that makes them loud.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")  # exactly what the resolver's read would do
                with pytest.raises(RuntimeError, match="savepoint"):
                    await hold_write_lease(conn, RUN_UPLOAD_OWNER, UUID(run_id), job.id)

    asyncio.run(_run())


def test_the_guard_admits_a_transaction_free_connection(migrated_url: str) -> None:
    """The counterpart, so the guard is not merely "always raises" (it is checked either way).

    Without this the ``pytest.raises`` above would pass against a guard that rejected every
    connection, including the one ``capture_handler`` actually uses.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            async with pool.connection() as conn:
                require_top_level_transaction(conn, "the test's own precondition")
                await hold_write_lease(conn, RUN_UPLOAD_OWNER, UUID(run_id), job.id)
            assert await _leases_for(migrated_url, run_id) == [job.id]

    asyncio.run(_run())
