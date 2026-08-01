"""The write-lease fence and the lock that makes it a closure (ADR-0502, issue #1687).

ADR-0455 §3 disclosed a residual and ADR-0497 §3 confirmed it survives: an object PUT between the
orphan sweep's per-key re-classify and its ``delete_object`` is destroyed. ADR-0497 fenced the *row*
side, so a Run no longer records rows against destroyed bytes; the bytes still went.

Two mechanisms close it here, and neither is sufficient alone:

* the **fence** — a third ``NOT EXISTS`` in ``_RECLAIMABLE_SQL`` over ``object_write_leases``,
  effective only while the holding job is live;
* the **lock** — the per-key re-classify and delete run inside one transaction holding
  ``pg_try_advisory_xact_lock`` on the owner, which the mint also takes. Without it a lease
  committed
  *after* the re-classify and before the delete would still lose its bytes, which is the whole
  reason
  a fence by itself is a fourth mitigation rather than a closure.

The lock arm is exercised with a **real** advisory lock held by a real second connection, not a
monkeypatched stand-in, because what is being asserted is that two independent sessions serialize.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import (
    HeadResult,
    ObjectListing,
    ObjectVersion,
    VersionBatch,
    VersionPage,
)
from kdive.artifacts.upload_manifest import (
    INVESTIGATION_UPLOAD_OWNER,
    RUN_UPLOAD_OWNER,
    UPLOAD_TENANT,
    lock_scope_for,
)
from kdive.artifacts.write_lease import (
    hold_write_lease,
    reap_stale_write_leases,
    release_write_lease,
)
from kdive.db.locks import advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.capacity.state import InvestigationState, RunState
from kdive.domain.lifecycle.records import Investigation
from kdive.reconciler.cleanup.upload_orphans import (
    UploadOrphanCandidate,
    reclaimable_upload_keys,
    repair_leaked_upload_objects,
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_running_job, seed_system

_GRACE = timedelta(hours=1)
_NO_TTL = timedelta(0)
#: A lease that outlives the test; the fence reads ``lease_expires_at > now()``.
_LIVE = 3600
#: A lease already past, i.e. the job the queue would reclaim.
_LAPSED = -3600


class _LeaseFakeStore:
    """The sweep's store port over ``key -> age``; ``last_modified`` is ``now - age``.

    Deliberately minimal — the aging, paging and fault behaviour has its own coverage in
    ``test_upload_orphan_sweep.py``. What these tests vary is rows and locks, not the store.
    """

    def __init__(self, objects: dict[str, timedelta]) -> None:
        self._objects = dict(objects)
        self.deleted: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._objects)

    def put(self, key: str, age: timedelta = timedelta(0)) -> None:
        self._objects[key] = age

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage:
        del version_id_marker
        keys = [key for key in self.list_prefix(prefix) if key_marker is None or key > key_marker]
        keys = keys[:max_keys]
        entries = tuple(
            ObjectVersion(
                key=key,
                version_id=f"version-of-{key}",
                last_modified=self._mtime(self._objects[key]),
                etag=f"etag-of-{key}",
                is_latest=True,
                is_delete_marker=False,
            )
            for key in keys
        )
        return VersionPage(entries, False, None, None)

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        del limit
        page = self.list_version_page(key)
        exact = tuple(version for version in page.entries if version.key == key)
        return VersionBatch(key, exact, True)

    def delete_batch(self, batch: VersionBatch) -> bool:
        self.delete(batch.key)
        return True

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[ObjectListing]]:
        listing = [
            ObjectListing(key=key, last_modified=self._mtime(self._objects[key]))
            for key in self.list_prefix(prefix)
        ]
        if listing:
            yield listing

    def head(self, key: str) -> HeadResult | None:
        age = self._objects.get(key)
        if age is None:
            return None
        return HeadResult(
            etag=f"etag-of-{key}",
            size_bytes=1,
            last_modified=self._mtime(age),
            checksum_sha256=None,
            version_id="test-version",
        )

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self._objects.pop(key, None)

    @staticmethod
    def _mtime(age: timedelta) -> datetime:
        return datetime.now(UTC) - age


def _sweep(store: _LeaseFakeStore):
    """The repair under test, with the TTL term zeroed so the threshold reads off ``_GRACE``."""
    return lambda conn: repair_leaked_upload_objects(conn, store, _GRACE, _NO_TTL)


async def _seed_leased_run(url: str, *, lease_seconds: int) -> tuple[UUID, UUID, str]:
    """A Run with a rowless, manifest-less prefix and a write lease held by a ``running`` job.

    Returns ``(run_id, job_id, prefix)``. The job's lease is ``lease_seconds`` from now, which is
    the
    only thing distinguishing a lease that fences from one that does not.
    """
    async with await connect(url) as seed:
        system_id = await seed_system(seed)
        run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
        job_id = await seed_running_job(
            seed,
            f"{run_id}:capture_vmcore:host_dump",
            kind="capture_vmcore",
            lease_seconds=lease_seconds,
            attempt=1,
            max_attempts=3,
        )
        await hold_write_lease(seed, RUN_UPLOAD_OWNER, run_id, job_id)
    return run_id, job_id, f"{UPLOAD_TENANT}/{RUN_UPLOAD_OWNER}/{run_id}/"


async def _seed_unleased_run(url: str) -> tuple[UUID, str]:
    """The same rowless, manifest-less prefix with no lease at all — the reclaimable baseline."""
    async with await connect(url) as seed:
        system_id = await seed_system(seed)
        run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
    return run_id, f"{UPLOAD_TENANT}/{RUN_UPLOAD_OWNER}/{run_id}/"


async def _writer_connection(url: str) -> psycopg.AsyncConnection:
    """A **non**-autocommit connection, so ``transaction()`` really opens one and holds its locks.

    ``connect`` in the shared conftest is autocommit, where an advisory *xact* lock would release
    the
    instant its statement returned — which would make the contention test pass for the wrong reason.
    """
    return await psycopg.AsyncConnection.connect(url)


async def _lease_count(url: str) -> int:
    async with await connect(url) as conn:
        cur = await conn.execute("SELECT count(*) FROM object_write_leases")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_a_live_write_lease_protects_every_key_under_its_owner_prefix(migrated_url: str) -> None:
    """The fence: an aged, rowless, manifest-less key is spared while its owner holds a live lease.

    Every other reason to spare it is removed on purpose — the object is twice the grace old, no
    ``artifacts`` row references it, no ``upload_manifests`` row exists for the owner — so the lease
    is the only thing that can account for the skip. The unleased sibling Run in the same pass is
    what proves the sweep was working and reached this root at all: a fence that silently scoped the
    sweep out of the bucket would produce the same zero.
    """

    async def _run() -> None:
        _leased_run, _job, leased = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        _unleased_run, unleased = await _seed_unleased_run(migrated_url)
        store = _LeaseFakeStore(
            {f"{leased}vmcore-host_dump": _GRACE * 2, f"{unleased}vmcore-host_dump": _GRACE * 2}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{unleased}vmcore-host_dump"]
        assert f"{leased}vmcore-host_dump" in store.present

    asyncio.run(_run())


def test_a_lease_whose_holding_job_is_no_longer_live_fences_nothing(migrated_url: str) -> None:
    """The fence is the *job's* liveness, so a lease cannot pin a prefix after its writer died.

    This is what stands in for ADR-0444's own deadline column: ``reap_stale_write_leases`` running
    late costs no correctness, because a lease whose holder stopped being a live claim is already
    inert. The row is still present when the sweep runs here — only the job's lease has lapsed —
    which is precisely the state a killed worker leaves behind.
    """

    async def _run() -> None:
        _run_id, _job, prefix = await _seed_leased_run(migrated_url, lease_seconds=_LAPSED)
        key = f"{prefix}vmcore-host_dump"
        store = _LeaseFakeStore({key: _GRACE * 2})
        assert await _lease_count(migrated_url) == 1, "the lease row must still exist"
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [key]

    asyncio.run(_run())


def test_a_released_lease_stops_fencing_immediately(migrated_url: str) -> None:
    """``release_write_lease`` is what finalize commits; after it the key is reclaimable again.

    The same live job holds the lease throughout, so the only thing that changed is the row. Without
    this the fence would be indistinguishable from one keyed on the job alone.
    """

    async def _run() -> None:
        run_id, job_id, prefix = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        key = f"{prefix}vmcore-host_dump"
        store = _LeaseFakeStore({key: _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
            async with await connect(migrated_url) as seed:
                await release_write_lease(seed, RUN_UPLOAD_OWNER, run_id, job_id)
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [key]

    asyncio.run(_run())


def test_releasing_one_holder_s_lease_leaves_a_concurrent_holder_s_standing(
    migrated_url: str,
) -> None:
    """The primary key carries the holder, which is why a capture cannot un-fence its twin.

    Two concurrent ``capture_vmcore`` attempts on one Run are reachable — ``finalize_capture``'s
    replay arm exists for exactly that — and on a row keyed ``(owner_kind, owner_id)`` alone the
    first to finalize would delete the second's fence mid-write. That is one of the three reasons
    ADR-0502 rejects the issue's ``upload_manifests`` proposal, and this pins that the replacement
    does not inherit it.
    """

    async def _run() -> None:
        run_id, first_job, prefix = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        key = f"{prefix}vmcore-host_dump"
        async with await connect(migrated_url) as seed:
            second_job = await seed_running_job(
                seed,
                f"{run_id}:capture_vmcore:host_dump:retry",
                kind="capture_vmcore",
                lease_seconds=_LIVE,
                attempt=1,
                max_attempts=3,
            )
            await hold_write_lease(seed, RUN_UPLOAD_OWNER, run_id, second_job)
            await release_write_lease(seed, RUN_UPLOAD_OWNER, run_id, first_job)
        store = _LeaseFakeStore({key: _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert key in store.present

    asyncio.run(_run())


def test_a_lease_fences_only_its_own_owner_s_prefix(migrated_url: str) -> None:
    """Owner-scoped, not global: one Run's lease must not stall the drain for every other Run.

    A fence written as an unqualified ``NOT EXISTS`` over the table — the shape a hurried
    implementation produces — passes every test above and fails this one.
    """

    async def _run() -> None:
        _leased_run, _job, leased = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        _other_run, other = await _seed_unleased_run(migrated_url)
        store = _LeaseFakeStore({f"{leased}core": _GRACE * 2, f"{other}core": _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{other}core"]

    asyncio.run(_run())


def test_an_investigation_owner_can_hold_a_lease_too(migrated_url: str) -> None:
    """Both swept roots are leasable, and the investigation half locks under its own scope.

    ``lock_scope_for`` maps the two owner kinds to two different advisory scopes, so a mint that
    silently fell back to ``LockScope.RUN`` for an investigation would take a lock nothing else
    takes
    and serialize against nothing. The prefix here is ``local/investigations/``, the rootfs upload
    lane's root.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            investigation = await INVESTIGATIONS.insert(
                seed,
                Investigation(
                    id=uuid4(),
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    principal="alice",
                    project="proj",
                    title="t",
                    state=InvestigationState.OPEN,
                ),
            )
            job_id = await seed_running_job(
                seed,
                f"{investigation.id}:stage_rootfs",
                kind="build",
                lease_seconds=_LIVE,
                attempt=1,
                max_attempts=3,
            )
            await hold_write_lease(seed, INVESTIGATION_UPLOAD_OWNER, investigation.id, job_id)
        prefix = f"{UPLOAD_TENANT}/{INVESTIGATION_UPLOAD_OWNER}/{investigation.id}/"
        store = _LeaseFakeStore({f"{prefix}rootfs.tar": _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert f"{prefix}rootfs.tar" in store.present
        assert lock_scope_for(INVESTIGATION_UPLOAD_OWNER) is not lock_scope_for(RUN_UPLOAD_OWNER)

    asyncio.run(_run())


def test_reclaimable_upload_keys_declines_a_leased_candidate(migrated_url: str) -> None:
    """The shared predicate carries the lease term, not only the sweep that calls it.

    ``reclaimable_upload_keys`` is documented as reusable for the reaper's #1557 residual, so a
    fence
    added to the sweep's own loop rather than to this statement would be one the reuse never
    inherits.
    """

    async def _run() -> None:
        run_id, job_id, prefix = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        candidate = UploadOrphanCandidate(
            key=f"{prefix}core",
            last_modified=datetime.now(UTC) - _GRACE * 2,
            owner_kind=RUN_UPLOAD_OWNER,
            owner_id=run_id,
        )
        async with await connect(migrated_url) as conn:
            assert await reclaimable_upload_keys(conn, [candidate], _GRACE) == []
            await release_write_lease(conn, RUN_UPLOAD_OWNER, run_id, job_id)
            assert await reclaimable_upload_keys(conn, [candidate], _GRACE) == [candidate.key]

    asyncio.run(_run())


def test_a_lease_minted_in_the_classify_delete_gap_cannot_be_missed(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The closure. A real second session holds the owner lock; the sweep must not delete under it.

    This is the arm a fence alone does not cover. The window is between the per-key re-classify and
    the ``delete_object``, and a writer that commits its lease inside it is invisible to every
    committed-row fence the sweep evaluates — which is why ADR-0455 §3's residual survived ADR-0497.

    The window is opened with the genuine mechanism rather than a stand-in: a second connection
    holds
    ``pg_advisory_xact_lock`` on the same ``(RUN, run_id)`` key ``hold_write_lease`` takes, in an
    open
    transaction, with no lease row committed yet. That is the state a mint is in at the instant it
    matters. The sweep must therefore decline the key — and decline it as a *skip*, returning zero
    with nothing raised, because nothing failed.

    Note what makes this falsifiable: the object is a plain aged rowless orphan that four other
    tests
    in this module delete. Only the held lock differs.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_unleased_run(migrated_url)
        key = f"{prefix}vmcore-host_dump"
        store = _LeaseFakeStore({key: _GRACE * 2})
        writer = await _writer_connection(migrated_url)
        try:
            async with (
                writer.transaction(),
                advisory_xact_lock(writer, lock_scope_for(RUN_UPLOAD_OWNER), run_id),
                AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
            ):
                with caplog.at_level(
                    logging.INFO, logger="kdive.reconciler.cleanup.upload_orphans"
                ):
                    assert await run_repair(pool, _sweep(store)) == 0
                assert store.deleted == []
                assert key in store.present
                assert any("is locked" in r.getMessage() for r in caplog.records)
        finally:
            await writer.close()

    asyncio.run(_run())


def test_the_sweep_reclaims_the_key_once_the_owner_lock_is_released(migrated_url: str) -> None:
    """The lock skip defers; it does not exempt. Otherwise the drain would leak on contention.

    Paired with the test above so the two together pin *deferral* rather than merely "nothing was
    deleted": the same store, the same key, the same pass shape, and the only difference is whether
    the writer's transaction is still open.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_unleased_run(migrated_url)
        key = f"{prefix}vmcore-host_dump"
        store = _LeaseFakeStore({key: _GRACE * 2})
        writer = await _writer_connection(migrated_url)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            try:
                async with (
                    writer.transaction(),
                    advisory_xact_lock(writer, lock_scope_for(RUN_UPLOAD_OWNER), run_id),
                ):
                    assert await run_repair(pool, _sweep(store)) == 0
            finally:
                await writer.close()
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [key]

    asyncio.run(_run())


def test_a_manifest_window_still_fences_alongside_the_lease(migrated_url: str) -> None:
    """The lease is an added term, not a replacement: the ADR-0455 manifest fence is untouched.

    A rewrite of ``_RECLAIMABLE_SQL`` that dropped or reordered the existing ``upload_manifests``
    anti-join would pass every lease test above.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_unleased_run(migrated_url)
        key = f"{prefix}kernel"
        async with await connect(migrated_url) as seed:
            await upload_manifest.replace_manifest(
                seed,
                upload_manifest.UploadManifestReplaceRequest(
                    owner_kind=RUN_UPLOAD_OWNER,
                    owner_id=run_id,
                    prefix=prefix,
                    entries=[],
                    ttl=timedelta(hours=1),
                ),
            )
        store = _LeaseFakeStore({key: _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert key in store.present

    asyncio.run(_run())


def test_the_reap_collects_a_dead_holder_s_lease_and_keeps_a_live_one(migrated_url: str) -> None:
    """``reap_stale_write_leases`` is scoped to exactly the leases the fence already ignores.

    A reap looser than the fence would delete a row that is actively protecting bytes, which is why
    both read the same ``LIVE_HOLDER_SQL``. Both arms run in one pass so the count is a
    discrimination and not just a non-zero.
    """

    async def _run() -> None:
        _dead_run, _dead_job, _dead_prefix = await _seed_leased_run(
            migrated_url, lease_seconds=_LAPSED
        )
        live_run, live_job, _live_prefix = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        assert await _lease_count(migrated_url) == 2
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, reap_stale_write_leases) == 1
        assert await _lease_count(migrated_url) == 1
        async with await connect(migrated_url) as conn:
            cur = await conn.execute("SELECT owner_id, job_id FROM object_write_leases")
            rows = await cur.fetchall()
        assert rows == [(live_run, live_job)]

    asyncio.run(_run())


def test_the_reap_returns_zero_when_every_lease_is_live(migrated_url: str) -> None:
    """A clean pass reports zero rather than raising or over-collecting."""

    async def _run() -> None:
        await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, reap_stale_write_leases) == 0
        assert await _lease_count(migrated_url) == 1

    asyncio.run(_run())


def test_deleting_the_holding_job_cascades_its_lease_away(migrated_url: str) -> None:
    """A lease can never outlive the ``jobs`` row its liveness is read from.

    Without the ``ON DELETE CASCADE`` the fence's ``EXISTS`` would simply find no job and the lease
    would be inert — safe, but it would also make the row unreachable from any owner and permanent.
    """

    async def _run() -> None:
        _run_id, job_id, _prefix = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        async with await connect(migrated_url) as conn:
            await conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        assert await _lease_count(migrated_url) == 0

    asyncio.run(_run())


def test_holding_a_lease_twice_for_one_job_is_idempotent(migrated_url: str) -> None:
    """A retried mint must not raise: the handler mints before every capture attempt."""

    async def _run() -> None:
        run_id, job_id, _prefix = await _seed_leased_run(migrated_url, lease_seconds=_LIVE)
        async with await connect(migrated_url) as conn:
            await hold_write_lease(conn, RUN_UPLOAD_OWNER, run_id, job_id)
        assert await _lease_count(migrated_url) == 1

    asyncio.run(_run())
