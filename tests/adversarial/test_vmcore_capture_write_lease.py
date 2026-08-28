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
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import (
    HeadResult,
    ObjectListing,
    ObjectVersion,
    StoredArtifact,
    VersionBatch,
    VersionPage,
)
from kdive.artifacts.uploads.upload_manifest import RUN_UPLOAD_OWNER, lock_scope_for
from kdive.artifacts.uploads.write_lease import hold_write_lease, reap_stale_write_leases
from kdive.db.locks import require_top_level_transaction, try_advisory_xact_lock
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.artifacts import vmcore as vmcore_plane
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.providers.ports.retrieve import CaptureOutput
from kdive.reconciler.cleanup.uploads.upload_orphans import repair_leaked_upload_objects
from tests.capture_store import WrittenObjects
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.systems_support import provider_resolver
from tests.support.worker_fence import dequeue_as_current_worker

_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")
_METHOD = CaptureMethod.HOST_DUMP
_GRACE = timedelta(hours=1)
_NO_TTL = timedelta(0)
_ABANDONED_ETAG = "etag-of-the-abandoned-first-attempt"


def _core(run_id: str) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{_METHOD.value}",
        "e1",
        Sensitivity.SENSITIVE,
        "vmcore",
        version_id="test-version",
    )
    redacted = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{_METHOD.value}-redacted",
        "e2",
        Sensitivity.REDACTED,
        "vmcore",
        version_id="test-version",
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
        #: When set, the capture's objects are recorded here instead of in ``_objects`` — the joined
        #: test needs the provider and the swept bucket to be the *same* store.
        self.write_through: _SweepableCaptureStore | None = None

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.entered.set()
        assert self.resume.wait(timeout=30), "the probe never released the capture"
        if self.raised is not None:
            raise self.raised
        if self.write_through is not None:
            return self.write_through.record(_core(self._run_id))
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


class _SweepableCaptureStore:
    """One double serving the real sweep's port *and* the finalize verify, over ``key -> (etag,
    age)``.

    The joined test needs both: the ADR-0455 sweep lists, heads and deletes through it while the
    capture is suspended, and ``finalize_capture`` then heads through it. ``age`` fixes
    ``last_modified`` at ``now - age``, which is what lets a key sit past the sweep's grace — the
    state a first attempt that died before finalize leaves behind.
    """

    def __init__(self, objects: dict[str, tuple[str, timedelta]]) -> None:
        self._objects = dict(objects)
        self._next_version = 1
        self._versions: dict[str, list[ObjectVersion]] = {}
        for key, (etag, age) in objects.items():
            self._append_version(key, etag, age)
        self.deleted: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._objects)

    def record(self, output: CaptureOutput) -> CaptureOutput:
        for stored in (output.raw, output.redacted):
            self._objects[stored.key] = (stored.etag, timedelta(0))
            self._append_version(stored.key, stored.etag, timedelta(0))
        return output

    def _append_version(self, key: str, etag: str, age: timedelta) -> None:
        prior = [replace(version, is_latest=False) for version in self._versions.get(key, ())]
        version = ObjectVersion(
            key=key,
            version_id=f"v{self._next_version}",
            last_modified=datetime.now(UTC) - age,
            etag=etag,
            is_latest=True,
            is_delete_marker=False,
        )
        self._next_version += 1
        self._versions[key] = [*prior, version]

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[ObjectListing]]:
        page = [
            ObjectListing(key=key, last_modified=self._mtime(key))
            for key in sorted(self._objects)
            if key.startswith(prefix)
        ]
        if page:
            yield page

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage:
        entries = tuple(
            version
            for key in sorted(self._versions)
            if key.startswith(prefix) and (key_marker is None or key > key_marker)
            for version in self._versions[key]
        )
        return VersionPage(entries[:max_keys], False, None, None)

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        versions = list(self._versions.get(key, ()))
        complete = len(versions) <= limit
        if not complete:
            latest = [version for version in versions if version.is_latest]
            versions = [*latest, *(version for version in versions if not version.is_latest)]
        return VersionBatch(key, tuple(versions[:limit]), complete)

    def delete_batch(self, batch: VersionBatch) -> bool:
        targets = [target for target in batch.targets if not target.is_latest]
        if batch.history_complete:
            targets.extend(target for target in batch.targets if target.is_latest)
        for target in targets:
            versions = self._versions.get(target.key, [])
            if not any(version.version_id == target.version_id for version in versions):
                continue
            self._versions[target.key] = [
                version for version in versions if version.version_id != target.version_id
            ]
            self.deleted.append(target.key)
            if not self._versions[target.key]:
                self._versions.pop(target.key)
                self._objects.pop(target.key, None)
        return batch.history_complete

    def head(self, key: str) -> HeadResult | None:
        entry = self._objects.get(key)
        if entry is None:
            return None
        return HeadResult(
            etag=entry[0],
            size_bytes=1,
            last_modified=self._mtime(key),
            checksum_sha256=None,
            version_id="test-version",
        )

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))

    def _mtime(self, key: str) -> datetime:
        return datetime.now(UTC) - self._objects[key][1]


async def _claimed_capture_job(pool: AsyncConnectionPool) -> tuple[str, Job]:
    """A capture job in the state the worker actually dispatches from: claimed and leased.

    ``queue.dequeue`` is what makes ``jobs.state = 'running'`` with a future ``lease_expires_at``,
    and that pair *is* the write lease's liveness predicate. An ``enqueue``-only job is ``queued``
    with a NULL lease, so a lease it holds fences nothing — which is why the tests above, which only
    need the row's visibility, cannot stand in for this one.
    """
    run_id, _job = await _seeded_capture_job(pool)
    async with pool.connection() as conn:
        claimed = await dequeue_as_current_worker(conn, "w-capture-1", lease=timedelta(minutes=5))
    assert claimed is not None, "the capture job must be claimable"
    return run_id, claimed


async def _sweep_now(url: str, store: _SweepableCaptureStore) -> int:
    """Run the real ADR-0455 orphan sweep on a connection of its own, as the reconciler does."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        return await repair_leaked_upload_objects(conn, store, _GRACE, _NO_TTL)


def test_the_real_sweep_cannot_reclaim_under_a_claimed_capture(migrated_url: str) -> None:
    """The acceptance criterion, with both halves real: handler *and* sweep, live job lease.

    Everything else about the key says "delete me": it is the abandoned first attempt's object,
    twice the grace old, with no ``artifacts`` row and no ``upload_manifests`` row. The only thing
    standing between it and ``delete_object`` is the lease the claimed capture holds — and the
    sweep runs here for real, on its own connection, while the provider is mid-write.

    The two halves are tested apart elsewhere (the fence in
    ``tests/reconciler/test_upload_orphan_sweep_write_lease.py``, the mint's visibility above) and
    neither is sufficient: the fence tests hand-seed a ``running`` job and never run the handler,
    and the visibility tests run the handler on an ``enqueue``-only job whose lease is not live, so
    the fence they exercise is inert. This is the only test in the tree where the mechanism that
    ships is the mechanism under test.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _claimed_capture_job(pool)
            raw = f"local/runs/{run_id}/vmcore-{_METHOD.value}"
            store = _SweepableCaptureStore({raw: (_ABANDONED_ETAG, _GRACE * 2)})
            retriever = _SuspendedCapture(run_id)
            retriever.write_through = store

            async def _handle() -> object:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=store,
                    )

            handler = asyncio.create_task(_handle())
            try:
                await asyncio.to_thread(retriever.entered.wait, 30)
                assert retriever.entered.is_set()
                assert await _sweep_now(migrated_url, store) == 0
            finally:
                retriever.resume.set()
            result = await handler
        assert store.deleted == [], "the in-flight capture's prefix must not be swept"
        assert raw in store.present
        assert isinstance(result, str), f"the capture should have finalized, got {result!r}"

    asyncio.run(_run())


def test_the_same_pass_reclaims_once_the_lease_is_gone(migrated_url: str) -> None:
    """The falsifier for the test above: identical pass, identical key, no lease.

    Without this arm the assertion "nothing was deleted" is satisfied by any sweep that failed to
    reach the root at all, and by the store-mtime fence had the ages been chosen differently. Here
    the lease is dropped from an outside session at the same instant, and the same pass destroys the
    same key — so the lease is what accounts for the difference and nothing else can.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _claimed_capture_job(pool)
            raw = f"local/runs/{run_id}/vmcore-{_METHOD.value}"
            store = _SweepableCaptureStore({raw: (_ABANDONED_ETAG, _GRACE * 2)})
            retriever = _SuspendedCapture(run_id)
            retriever.write_through = store

            async def _handle() -> object:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=store,
                    )

            handler = asyncio.create_task(_handle())
            try:
                await asyncio.to_thread(retriever.entered.wait, 30)
                async with await psycopg.AsyncConnection.connect(
                    migrated_url, autocommit=True
                ) as outside:
                    await outside.execute("DELETE FROM object_write_leases")
                assert await _sweep_now(migrated_url, store) == 1
            finally:
                retriever.resume.set()
            await handler
        assert store.deleted == [raw]

    asyncio.run(_run())
