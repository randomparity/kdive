"""Adversarial: the dump-volume sweep cannot delete a volume a claimed capture is creating.

ADR-0562, #1955. ADR-0557 left the host-dump live-holder guard as a *state sample*: the sweep asks
whether the System has a ``running`` capture job and then deletes, holding nothing. A worker that
claims the job in that gap makes the answer stale, and the capture's own
``_delete_stale_volume``/``coreDumpWithFormat`` pair then puts a **new** volume at the same
deterministic name — so the sweep's name-addressed delete lands on the live capture's volume rather
than on the orphan it classified.

These tests drive the real ``capture_handler`` and the real ``reap_orphaned_dump_volumes`` against
each other with the interleaving fixed by events rather than by timing, so the ordering claim is
falsifiable rather than probabilistic:

* the sweep is suspended *after* its classification, inside the port's ``delete_dump_volume``;
* the queued-to-running transition then happens for real — ``queue.dequeue``, then the handler;
* what the test waits for is a state that is **stable once true** on either tree: the handler's mint
  blocked on ``(SYSTEM, system_id)`` (fixed), or the provider call reached with nothing declared
  (unfixed). Neither can flip back, so no wait here is a timing bet.

The double implements the ``DumpVolumeReaper`` contract, including the identity-addressed delete;
the shipped libvirt implementation of that contract is covered in
``tests/providers/remote_libvirt/retrieve/test_dump_volume_reaper.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult, StoredArtifact
from kdive.db.locks import (
    LockScope,
    _advisory_lock_oids,
    _lock_key,
    try_advisory_xact_lock,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.artifacts import vmcore as vmcore_plane
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.providers.infra.reaping import DumpVolume, DumpVolumeReaper
from kdive.providers.ports.retrieve import CaptureOutput
from kdive.providers.remote_libvirt.reaping.dump_volume import system_id_from_dump_volume_name
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import host_dump_volume_name
from kdive.providers.shared.host_dump_volume_leases import (
    reap_stale_host_dump_volume_leases,
)
from kdive.reconciler.cleanup.provider_reaping import reap_orphaned_dump_volumes
from tests.capture_store import WrittenObjects
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.systems_support import provider_resolver
from tests.support.worker_fence import dequeue_as_current_worker

_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")
_GRACE = timedelta(minutes=30)
_ORPHAN_AGE_S = 3600.0
#: The mtime the capture's freshly created volume carries. Distinct from the orphan's by an hour,
#: which is what makes an identity comparison on this field meaningful rather than incidental.
_FRESH_MTIME = 2_000_000_000.0


def test_the_double_conforms_to_the_dump_volume_reaper_port() -> None:
    """The doubles below stand in for the port, so they must satisfy it structurally.

    Without this a double could reintroduce the defaulted ``expected_mtime_epoch_s`` the port bans,
    and every test in this module would keep passing while exercising a contract the shipped reaper
    does not offer.
    """
    assert isinstance(_PausingDumpVolumeReaper(_StoragePool()), DumpVolumeReaper)


def _core(run_id: str) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{CaptureMethod.HOST_DUMP.value}",
        "e1",
        Sensitivity.SENSITIVE,
        "vmcore",
        version_id="test-version",
    )
    redacted = StoredArtifact(
        f"local/runs/{run_id}/vmcore-{CaptureMethod.HOST_DUMP.value}-redacted",
        "e2",
        Sensitivity.REDACTED,
        "vmcore",
        version_id="test-version",
    )
    return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id="deadbeef", raw_size_bytes=512)


class _StoragePool:
    """The provider's dump volumes as ``name -> mtime``, which is the whole identity the port has.

    The reconciler samples ``(name, mtime)`` from a list and later deletes; every question this
    module asks is about whether those two observations refer to the same volume.
    """

    def __init__(self, volumes: dict[str, float] | None = None) -> None:
        self._volumes = dict(volumes or {})

    def present(self) -> dict[str, float]:
        return dict(self._volumes)

    def mtime(self, name: str) -> float | None:
        return self._volumes.get(name)

    def recreate(self, name: str, mtime: float) -> None:
        """What the capture does: drop any prior volume of this name, then create its own."""
        self._volumes.pop(name, None)
        self._volumes[name] = mtime

    def remove(self, name: str) -> None:
        self._volumes.pop(name, None)


class _PausingDumpVolumeReaper:
    """A ``DumpVolumeReaper`` that suspends inside ``delete_dump_volume``.

    The pause sits after the sweep's classification and — once the sweep holds its boundary — inside
    the transaction that holds it, which is the only vantage point from which the mint's contention
    is observable.
    """

    def __init__(self, pool: _StoragePool, *, pause: bool = True) -> None:
        self._pool = pool
        self._pause = pause
        self.about_to_delete = asyncio.Event()
        self.proceed = asyncio.Event()
        #: ``(name, mtime)`` actually unlinked, so a test can name *which* volume was destroyed.
        self.deleted: list[tuple[str, float]] = []
        #: ``(name, expected, observed)`` refused because the volume is no longer the sampled one.
        self.refused: list[tuple[str, float, float]] = []
        self.expected_mtimes: list[float] = []

    async def list_dump_volumes(self) -> list[DumpVolume]:
        return [
            DumpVolume(
                name=name, system_id=system_id_from_dump_volume_name(name), mtime_epoch_s=mtime
            )
            for name, mtime in sorted(self._pool.present().items())
        ]

    async def delete_dump_volume(self, name: str, *, expected_mtime_epoch_s: float) -> bool:
        """The port's contract, including the identity refusal, over the in-memory pool.

        ``expected_mtime_epoch_s`` is required exactly as the port requires it. A default here would
        let this double accept the call the port forbids — and the ADR's whole reason for making it
        required is that a defaulted identity is a guard that silently does nothing.
        """
        self.about_to_delete.set()
        if self._pause:
            await self.proceed.wait()
        self.expected_mtimes.append(expected_mtime_epoch_s)
        observed = self._pool.mtime(name)
        if observed is None:
            return False  # already gone: not an error, and not a reap either
        if observed != expected_mtime_epoch_s:
            self.refused.append((name, expected_mtime_epoch_s, observed))
            return False
        self._pool.remove(name)
        self.deleted.append((name, observed))
        return True


class _SuspendedCapture:
    """A retriever that creates the System's volume, then pauses mid-operation.

    ``capture`` runs on the ``to_thread`` worker, so the pool mutation and the pause both happen
    while the event loop is free to run the sweep — the interleaving the race needs.
    """

    def __init__(self, run_id: str, volume_name: str, pool: _StoragePool) -> None:
        self._run_id = run_id
        self._volume_name = volume_name
        self._pool = pool
        self._objects = WrittenObjects()
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.raised: BaseException | None = None

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        del system_id, run_id, method
        # host_dump_capture's own order: delete the stale volume of this name, then dump into a
        # fresh one. Set `entered` only after that, so the flag means "a live volume exists".
        self._pool.recreate(self._volume_name, _FRESH_MTIME)
        self.entered.set()
        assert self.resume.wait(timeout=30), "the probe never released the capture"
        if self.raised is not None:
            raise self.raised
        return self._objects.record(_core(self._run_id))

    def head(self, key: str) -> HeadResult | None:
        return self._objects.head(key)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=8, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seeded_capture_job(
    pool: AsyncConnectionPool, *, method: CaptureMethod = CaptureMethod.HOST_DUMP
) -> tuple[str, str, Job]:
    system_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id=None)
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn,
            JobKind.CAPTURE_VMCORE,
            CaptureVmcorePayload(run_id=run_id, method=method),
            _AUTH,
            f"{run_id}:capture_vmcore:{method.value}",
        )
    return system_id, run_id, job


async def _now_epoch(url: str) -> float:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        cur = await conn.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    assert row is not None
    return float(row[0])


async def _lease_holders(url: str, system_id: str) -> list[UUID]:
    """The committed lease holders for this System, read on a connection of its own.

    A separate connection because that is half the assertion: a mint that landed in a savepoint is
    visible to its own session and to nobody else, and the sweep is nobody else.
    """
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as probe:
        cur = await probe.execute(
            "SELECT job_id FROM host_dump_volume_leases WHERE system_id = %s", (system_id,)
        )
        return [row[0] for row in await cur.fetchall()]


async def _system_lock_is_free(url: str, system_id: str) -> bool:
    """Whether ``(SYSTEM, system_id)`` can be taken right now — i.e. nothing is holding it."""
    async with await psycopg.AsyncConnection.connect(url) as probe, probe.transaction():
        return await try_advisory_xact_lock(probe, LockScope.SYSTEM, UUID(system_id))


async def _has_lock_waiter(probe: psycopg.AsyncConnection, system_id: str) -> bool:
    """Whether some backend is *blocked* acquiring ``(SYSTEM, system_id)``.

    An ungranted advisory-lock row is the mint's own evidence that the sweep's boundary is real and
    that it is holding it across the delete. Scoped to this database, because ``pg_locks`` is
    cluster-wide and advisory keys are per-database.
    """
    classid, objid = _advisory_lock_oids(_lock_key(LockScope.SYSTEM, UUID(system_id)))
    cur = await probe.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s"
        "  AND objsubid = 1 AND NOT granted"
        "  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
        (classid, objid),
    )
    row = await cur.fetchone()
    return bool(row is not None and row[0])


async def _await_capture_reaching_the_volume_or_the_boundary(
    url: str, system_id: str, entered: threading.Event, *, timeout: float = 30.0
) -> str:
    """Wait for whichever of the two stable outcomes the tree under test produces.

    ``"blocked-on-boundary"`` — the handler's declaration is waiting on the lock the sweep holds
    across its delete, so the capture provably has not touched the volume yet.
    ``"reached-the-volume"`` — the handler declared nothing, ran straight to the provider call, and
    has already created the volume the sweep is about to resolve by name.

    Both are terminal for the purposes of this wait: neither reverts while the sweep stays
    suspended. Returning rather than asserting keeps the *behavioural* assertion — which volume
    survives — as the thing that fails, instead of a wait.
    """
    deadline = time.monotonic() + timeout
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as probe:
        while time.monotonic() < deadline:
            if entered.is_set():
                return "reached-the-volume"
            if await _has_lock_waiter(probe, system_id):
                return "blocked-on-boundary"
            await asyncio.sleep(0.02)
    raise AssertionError(
        "the capture handler neither reached the provider call nor contended the sweep's boundary"
    )


def test_the_sweep_cannot_delete_the_volume_of_a_capture_claimed_mid_pass(
    migrated_url: str,
) -> None:
    """The acceptance criterion of #1955, with both halves real and the claim inside the window.

    Everything about the volume says "reap me": it is an hour old against a 30-minute grace, and at
    the instant the sweep classifies it the only capture job for its System is ``queued`` — which
    ADR-0557 decided, deliberately, is not a live holder. The claim then lands *after* that
    classification, which is the whole of the bug.

    Against unfixed code the handler declares nothing, so it runs on to create its volume while the
    sweep is suspended, and the sweep's name-addressed delete destroys that volume rather than the
    orphan it sampled. Against the fix the handler's mint blocks on the boundary the sweep holds
    across its delete, the sweep removes the identity it classified, and the capture then creates
    and keeps its own.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, run_id, job = await _seeded_capture_job(pool)
            volume = host_dump_volume_name(UUID(system_id))
            orphan_mtime = await _now_epoch(migrated_url) - _ORPHAN_AGE_S
            storage = _StoragePool({volume: orphan_mtime})
            reaper = _PausingDumpVolumeReaper(storage)
            retriever = _SuspendedCapture(run_id, volume, storage)

            async def _sweep() -> int:
                async with pool.connection() as conn:
                    return (await reap_orphaned_dump_volumes(conn, reaper, _GRACE)).reaped

            sweep = asyncio.create_task(_sweep())
            handler: asyncio.Task[object] | None = None
            try:
                await asyncio.wait_for(reaper.about_to_delete.wait(), timeout=30)
                # The queued -> running transition, for real: the queue's own claim, then the
                # handler the worker would run.
                async with pool.connection() as conn:
                    claimed = await dequeue_as_current_worker(
                        conn, "w-capture-1955", lease=timedelta(minutes=5)
                    )
                assert claimed is not None and claimed.id == job.id

                async def _handle() -> object:
                    async with pool.connection() as conn:
                        return await vmcore_plane.capture_handler(
                            conn,
                            claimed,
                            resolver=provider_resolver(retriever=retriever),
                            artifact_store=retriever,
                        )

                handler = asyncio.create_task(_handle())
                reached = await _await_capture_reaching_the_volume_or_the_boundary(
                    migrated_url, system_id, retriever.entered
                )
                # Asserted, not merely returned: this is the direct statement of the ordering claim,
                # where the volume-survival assertions below are its observable consequence.
                assert reached == "blocked-on-boundary", (
                    "the capture's declaration must contend the boundary the sweep holds over its "
                    f"delete; it instead {reached}"
                )
            finally:
                reaper.proceed.set()
                retriever.resume.set()
            reaped = await sweep
            outcome = await handler if handler is not None else None

        assert storage.mtime(volume) == _FRESH_MTIME, (
            "the sweep destroyed the volume the claimed capture created"
        )
        assert reaper.deleted == [(volume, orphan_mtime)], (
            "the sweep must delete the identity it classified and nothing else"
        )
        assert reaper.refused == [], (
            "the boundary, not the identity backstop, must be what accounts for this: a refusal "
            "here would mean the capture had already recreated the volume under the sweep"
        )
        assert reaped == 1
        assert isinstance(outcome, str), f"the capture should have finalized, got {outcome!r}"

    asyncio.run(_run())


def test_a_live_lease_holds_the_sweep_off_an_hour_old_volume(migrated_url: str) -> None:
    """The fence on its own, with a claimed capture in flight and the lock uncontended.

    Separate from the interleaving test because it isolates the *fence* from the boundary: here the
    sweep runs start to finish while the capture is suspended, takes the System lock unopposed, and
    still declines. Two holders account for that, not one — the lease row and the ``running`` job
    ADR-0557's predicate matches — and this test does not distinguish them. What only the lease can
    account for is isolated in
    ``tests/reconciler/test_loop.py::test_a_live_lease_fences_a_volume_whose_job_state_does_not_match``.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, run_id, job = await _seeded_capture_job(pool)
            volume = host_dump_volume_name(UUID(system_id))
            storage = _StoragePool()
            reaper = _PausingDumpVolumeReaper(storage, pause=False)
            retriever = _SuspendedCapture(run_id, volume, storage)
            async with pool.connection() as conn:
                claimed = await dequeue_as_current_worker(
                    conn, "w-capture-1955", lease=timedelta(minutes=5)
                )
            assert claimed is not None and claimed.id == job.id

            async def _handle() -> object:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(
                        conn,
                        claimed,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=retriever,
                    )

            handler = asyncio.create_task(_handle())
            try:
                await asyncio.to_thread(retriever.entered.wait, 30)
                assert retriever.entered.is_set(), "the capture never reached the volume"
                # Age the live capture's volume past the grace: exactly the ADR-0557 hazard, where
                # the mtime guard was evaluated against a timestamp that is not this volume's.
                storage.recreate(volume, await _now_epoch(migrated_url) - _ORPHAN_AGE_S)
                assert await _lease_holders(migrated_url, system_id) == [claimed.id]
                assert await _system_lock_is_free(migrated_url, system_id), (
                    "the mint must not hold LockScope.SYSTEM across the provider operation"
                )
                async with pool.connection() as conn:
                    assert (await reap_orphaned_dump_volumes(conn, reaper, _GRACE)).reaped == 0
            finally:
                retriever.resume.set()
            outcome = await handler

        assert reaper.deleted == []
        assert volume in storage.present()
        assert isinstance(outcome, str), f"the capture should have finalized, got {outcome!r}"
        assert await _lease_holders(migrated_url, system_id) == [], (
            "finalize_capture must release the lease"
        )

    asyncio.run(_run())


def test_the_same_pass_reclaims_the_volume_once_no_holder_remains(migrated_url: str) -> None:
    """The falsifier for the test above: identical pass, identical volume, no live holder.

    Without this arm "nothing was deleted" is also satisfied by a sweep that never classified the
    volume at all, or by the grace window had the ages differed. Here the holders are removed by an
    outside session while the provider operation is still suspended, and the same pass destroys the
    same volume — so the refusal above is a refusal and not a no-op.

    It removes **both** holders, because they are independent and either alone defeats the arm: the
    lease row (ADR-0562) and the ``running`` job ADR-0557's predicate matches. What the lease
    contributes that the job-state predicate cannot is isolated in
    ``tests/reconciler/test_loop.py::test_a_live_lease_fences_a_volume_whose_job_state_does_not_match``,
    and the ordering it contributes is the interleaving test at the top of this module.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, run_id, job = await _seeded_capture_job(pool)
            volume = host_dump_volume_name(UUID(system_id))
            storage = _StoragePool()
            reaper = _PausingDumpVolumeReaper(storage, pause=False)
            retriever = _SuspendedCapture(run_id, volume, storage)
            async with pool.connection() as conn:
                claimed = await dequeue_as_current_worker(
                    conn, "w-capture-1955", lease=timedelta(minutes=5)
                )
            assert claimed is not None and claimed.id == job.id

            async def _handle() -> object:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(
                        conn,
                        claimed,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=retriever,
                    )

            handler = asyncio.create_task(_handle())
            aged = 0.0
            try:
                await asyncio.to_thread(retriever.entered.wait, 30)
                aged = await _now_epoch(migrated_url) - _ORPHAN_AGE_S
                storage.recreate(volume, aged)
                async with await psycopg.AsyncConnection.connect(
                    migrated_url, autocommit=True
                ) as outside:
                    await outside.execute("DELETE FROM host_dump_volume_leases")
                    await outside.execute(
                        "UPDATE jobs SET state = 'succeeded' WHERE id = %s", (claimed.id,)
                    )
                async with pool.connection() as conn:
                    assert (await reap_orphaned_dump_volumes(conn, reaper, _GRACE)).reaped == 1
            finally:
                retriever.resume.set()
            await handler

        assert reaper.deleted == [(volume, aged)]
        assert reaper.expected_mtimes == [aged], (
            "the sweep must address its delete to the identity it sampled"
        )

    asyncio.run(_run())


def test_a_failed_capture_leaves_its_lease_for_the_reap(migrated_url: str) -> None:
    """The ``except`` deliberately does not release, and the reap is what collects the row.

    Releasing on the failure path would be a fence that lifts only for the failures Python
    observed: a worker killed mid-capture releases nothing, so the reap has to exist regardless.
    What this pins is that the row survives the failure *and* that the reap then collects it.

    The job is left ``queued`` — never claimed — on purpose, which is what makes the reap's own
    predicate the thing under test: a queued row has a NULL job lease, so it is not a live holder
    its lease is collectable the moment it exists. A claimed job with a five-minute lease is a live
    holder and its row is correctly *kept*, which would prove nothing here.
    """

    async def _run() -> None:
        boom = CategorizedError("the capture failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        async with _pool(migrated_url) as pool:
            system_id, run_id, job = await _seeded_capture_job(pool)
            volume = host_dump_volume_name(UUID(system_id))
            storage = _StoragePool()
            retriever = _SuspendedCapture(run_id, volume, storage)
            retriever.raised = boom
            retriever.resume.set()
            with pytest.raises(CategorizedError):
                async with pool.connection() as conn:
                    await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=retriever,
                    )

        assert await _lease_holders(migrated_url, system_id) == [job.id]
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            assert await reap_stale_host_dump_volume_leases(conn) == 1
        assert await _lease_holders(migrated_url, system_id) == []

    asyncio.run(_run())


def test_a_kdump_capture_mints_no_dump_volume_lease(migrated_url: str) -> None:
    """Only ``host_dump`` creates the volume, so only ``host_dump`` declares itself over it.

    The counterpart to the mint tests: without the gate a fence would pin a System's dump volume for
    every capture method, including ones that never touch it — an orphan retained for the length of
    an unrelated kdump harvest.

    The sample is taken **while the provider operation is in flight**, because that is the only
    window in which the gate is observable: ``finalize_capture`` releases unconditionally, so an
    end-state check passes whether the mint happened or not.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id, run_id, _job = await _seeded_capture_job(pool, method=CaptureMethod.KDUMP)
            volume = host_dump_volume_name(UUID(system_id))
            storage = _StoragePool()
            retriever = _SuspendedCapture(run_id, volume, storage)
            async with pool.connection() as conn:
                claimed = await dequeue_as_current_worker(
                    conn, "w-capture-1955", lease=timedelta(minutes=5)
                )
            assert claimed is not None

            async def _handle() -> object:
                async with pool.connection() as conn:
                    return await vmcore_plane.capture_handler(
                        conn,
                        claimed,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=retriever,
                    )

            handler = asyncio.create_task(_handle())
            try:
                await asyncio.to_thread(retriever.entered.wait, 30)
                assert retriever.entered.is_set(), "the capture never reached the provider call"
                assert await _lease_holders(migrated_url, system_id) == []
            finally:
                retriever.resume.set()
            outcome = await handler
        assert isinstance(outcome, str)
        assert await _lease_holders(migrated_url, system_id) == []

    asyncio.run(_run())
