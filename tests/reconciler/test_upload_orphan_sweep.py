"""Tests for the upload-prefix orphan sweep (ADR-0455, issue #1556).

The sweep drains the leak ADR-0453 §Consequences disclosed and declined to fix: a row-first reap
whose object phase fails partway leaves objects under ``local/<kind>/<id>/`` with no
``upload_manifests`` row and no ``artifacts`` row, and no other mechanism in the tree reclaims them
(``gc_expired_build_artifacts`` is row-driven over ``artifacts``; the only prefix-driven orphan scan
covers ``images/``).

Three fences decide reclaimability, all in one Postgres statement: no ``artifacts`` row for the
key, **no** ``upload_manifests`` row for the owner at all (so a live *or re-minted* window owns its
owner-addressed key names), and a store-mtime grace measured against Postgres ``now()`` so a
presigned PUT that began before the deadline and completed after it is not destroyed. The same
statement is re-run per key immediately before each delete.

Seeding uses autocommit ``connect`` connections; repairs run through a real non-autocommit pool via
``run_repair``, mirroring ``test_upload_reaper.py``. Object mtimes are wall-clock relative
(``now - age``), which is the DB clock too in these tests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import HeadResult, ObjectListing
from kdive.artifacts.uploads import ManifestEntry
from kdive.config.core_settings import UPLOAD_ORPHAN_GRACE, UPLOAD_TTL_SECONDS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup import upload_orphans
from kdive.reconciler.cleanup.upload_orphans import (
    MAX_RECLAIMS_PER_ROOT,
    UPLOAD_ORPHAN_ROOTS,
    UploadOrphanCandidate,
    reclaimable_upload_keys,
)
from kdive.reconciler.cleanup.upload_orphans import (
    repair_leaked_upload_objects as _repair_leaked_upload_objects,
)
from kdive.reconciler.cleanup.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system

_GRACE = timedelta(hours=1)
_NO_TTL = timedelta(0)


#: The fakes' listing page size. Small so a handful of objects spans several pages, which is what
#: lets a test distinguish a paged sweep from one that materializes a whole root (#1569).
_PAGE_SIZE = 3


class _FakeUploadStore:
    """A store stand-in over ``key -> age``; the absolute mtime is ``now - age``.

    Satisfies both the reaper's port (``list_prefix``/``delete``) and the sweep's
    (``iter_prefix_pages_with_mtime``/``head``/``delete``), so one instance can carry a failed
    reap's aftermath straight into the sweep.

    The listing is **paged**, at ``page_size`` keys per page in key order, mirroring
    ``ObjectStore.iter_prefix_pages_with_mtime`` over ``list_objects_v2`` (ADR-0498). The page
    contents are snapshotted when the iterator starts, which is also what S3 does: a continuation
    token is a key marker, so keys the caller deletes out of an earlier page do not shift a later
    one.

    ``listed_prefixes``, ``pages_yielded`` and ``headed_keys`` record every store round trip in
    order, so a test can assert what the sweep *spent* and not only what it deleted (#1575, #1569).
    ``events`` is the same round trips in one interleaved sequence — ``page:<n>`` for each page
    handed over and ``delete:<key>`` for each delete — which is what distinguishes acting *per page*
    from draining the iterator and then slicing the result into page-shaped calls. Both produce
    identical ``pages_yielded``; only the interleaving differs.

    An object's etag is derived from its key by default, because most tests here are about ages
    and rows rather than identities. ``put`` can override it, and ``deleted_etags`` records the
    etag each delete actually destroyed — which is how a test distinguishes deleting the bytes it
    re-read from deleting a *newer* version that replaced them in the gap (#1574).
    """

    def __init__(self, objects: dict[str, timedelta], *, page_size: int = _PAGE_SIZE) -> None:
        self._objects = dict(objects)
        self._etags: dict[str, str] = {}
        self._page_size = page_size
        self.deleted: list[str] = []
        self.deleted_etags: list[str] = []
        self.listed_prefixes: list[str] = []
        self.pages_yielded: list[list[str]] = []
        self.headed_keys: list[str] = []
        self.events: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._objects)

    def put(self, key: str, age: timedelta = timedelta(0), etag: str | None = None) -> None:
        self._objects[key] = age
        if etag is not None:
            self._etags[key] = etag

    def _etag(self, key: str) -> str:
        return self._etags.get(key, f"etag-of-{key}")

    def forget(self, key: str) -> None:
        """Remove an object without recording a delete — another actor got there first."""
        self._objects.pop(key, None)

    def _mtime(self, age: timedelta) -> datetime:
        return datetime.now(UTC) - age

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(key for key in self._objects if key.startswith(prefix))

    def _listing(self, prefix: str) -> list[ObjectListing]:
        return [
            ObjectListing(key=key, last_modified=self._mtime(age))
            for key, age in sorted(self._objects.items())
            if key.startswith(prefix)
        ]

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[ObjectListing]]:
        self.listed_prefixes.append(prefix)
        listing = self._listing(prefix)
        # An empty prefix still yields one empty page, as list_objects_v2 does.
        for start in range(0, max(len(listing), 1), self._page_size):
            page = listing[start : start + self._page_size]
            self.pages_yielded.append([listed.key for listed in page])
            self.events.append(f"page:{len(page)}")
            yield page

    def head(self, key: str) -> HeadResult | None:
        self.headed_keys.append(key)
        age = self._objects.get(key)
        if age is None:
            return None
        return HeadResult(
            size_bytes=1,
            checksum_sha256=None,
            etag=self._etag(key),
            last_modified=self._mtime(age),
        )

    def delete(self, key: str) -> None:
        if key in self._objects:
            self.deleted_etags.append(self._etag(key))
        self._objects.pop(key, None)
        self.deleted.append(key)
        self.events.append(f"delete:{key}")


class _FailingDeleteStore(_FakeUploadStore):
    """Raises ``CategorizedError`` from ``delete`` for the named keys, as a store outage does."""

    def __init__(self, objects: dict[str, timedelta], *, fail_keys: set[str]) -> None:
        super().__init__(objects)
        self._fail_keys = fail_keys
        self.attempted: list[str] = []

    def delete(self, key: str) -> None:
        self.attempted.append(key)
        if key in self._fail_keys:
            raise CategorizedError(
                f"delete_object failed for {key}", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        super().delete(key)


class _FailingListStore(_FakeUploadStore):
    """Raises ``CategorizedError`` from the listing iterator for the named root prefixes.

    ``fail_after_pages`` chooses *when*: ``0`` fails before the first page, as a scoped
    ``s3:ListBucket`` deny does, and a higher value delivers that many pages first and then fails —
    the mid-root fault a paged listing makes reachable, where the pages already delivered have
    already deleted (ADR-0498 §3).
    """

    def __init__(
        self,
        objects: dict[str, timedelta],
        *,
        fail_list_prefixes: set[str],
        fail_after_pages: int = 0,
        page_size: int = _PAGE_SIZE,
    ) -> None:
        super().__init__(objects, page_size=page_size)
        self._fail_list_prefixes = fail_list_prefixes
        self._fail_after_pages = fail_after_pages

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[ObjectListing]]:
        if prefix not in self._fail_list_prefixes:
            yield from super().iter_prefix_pages_with_mtime(prefix)
            return
        pages = super().iter_prefix_pages_with_mtime(prefix)
        if not self._fail_after_pages:
            # Fail before the first page, as a scoped s3:ListBucket deny does. `pages` is never
            # advanced, so the base fake records nothing; the attempted request is recorded here
            # instead, because it is the reply that failed and not the request.
            self.listed_prefixes.append(prefix)
        for _ in range(self._fail_after_pages):
            page = next(pages, None)
            if page is None:
                break
            yield page
        raise CategorizedError(
            f"list_objects_v2 failed for {prefix}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )


class _FailingHeadStore(_FakeUploadStore):
    """Raises ``CategorizedError`` from ``head`` for the named keys — a per-key HEAD deny."""

    def __init__(self, objects: dict[str, timedelta], *, fail_keys: set[str]) -> None:
        super().__init__(objects)
        self._fail_keys = fail_keys

    def head(self, key: str) -> HeadResult | None:
        if key in self._fail_keys:
            raise CategorizedError(
                f"head_object failed for {key}", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        return super().head(key)


class _HookedStore(_FakeUploadStore):
    """Runs ``before_delete`` from inside ``delete``, once, on the ``to_thread`` worker.

    That lands the hook in the gap between the per-key re-check and the delete, which is the only
    place a concurrent committer can still lose its object.
    """

    def __init__(self, objects: dict[str, timedelta], *, before_delete: Callable[[], None]) -> None:
        super().__init__(objects)
        self._before_delete = before_delete
        self._fired = False

    def delete(self, key: str) -> None:
        if not self._fired:
            self._fired = True
            self._before_delete()
        super().delete(key)


def _sweep(
    store: _FakeUploadStore,
    grace: timedelta = _GRACE,
    upload_ttl: timedelta = _NO_TTL,
):
    """The repair under test. ``upload_ttl`` defaults to zero so a test that is not *about* the
    TTL stacking reads its threshold straight off ``grace``; the stacking has its own test."""
    return lambda conn: _repair_leaked_upload_objects(conn, store, grace, upload_ttl)


async def _seed_run_with_window(url: str, ttl: timedelta) -> tuple[UUID, str]:
    """Seed a CREATED Run with an upload manifest at ``ttl``; return its id and window prefix."""
    async with await connect(url) as seed:
        system_id = await seed_system(seed)
        run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
        prefix = f"local/runs/{run_id}/"
        await upload_manifest.replace_manifest(
            seed,
            upload_manifest.UploadManifestReplaceRequest(
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=ttl,
            ),
        )
    return run_id, prefix


async def _seed_investigation_with_window(url: str, ttl: timedelta) -> tuple[UUID, str]:
    async with await connect(url) as seed:
        inv_id = uuid4()
        await seed.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'user-1', 'proj', 't', 'open')",
            (inv_id,),
        )
        prefix = f"local/investigations/{inv_id}/"
        await upload_manifest.replace_manifest(
            seed,
            upload_manifest.UploadManifestReplaceRequest(
                owner_kind="investigations",
                owner_id=inv_id,
                prefix=prefix,
                entries=[ManifestEntry("rootfs", "a", 1)],
                ttl=ttl,
            ),
        )
    return inv_id, prefix


async def _insert_artifact_row(
    conn: psycopg.AsyncConnection, *, owner_kind: str, owner_id: UUID, object_key: str
) -> None:
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "    retention_class) VALUES (%s, %s, %s, %s, %s, %s)",
        (owner_kind, owner_id, object_key, "etag-1", "sensitive", "default"),
    )


def test_a_reap_whose_object_phase_failed_leaves_orphans_the_sweep_reclaims(
    migrated_url: str,
) -> None:
    """The defect, end to end (AC-1).

    The reaper's phase 1 commits the manifest-row delete and phase 2 then fails on every key, so the
    objects survive with no manifest row and no ``artifacts`` row — exactly ADR-0453
    §Consequences' first residual. Nothing else in the tree can see them: they have no ``artifacts``
    row for ``gc_expired_build_artifacts`` to enumerate and they are not under ``images/``.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        keys = [f"{prefix}kernel", f"{prefix}kernel.part0001"]
        store = _FailingDeleteStore(dict.fromkeys(keys, _GRACE * 2), fail_keys=set(keys))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):  # the reap reports its failed sweep
                await run_repair(pool, lambda conn: _repair_abandoned_uploads(conn, store))
            # The leak: row gone, bytes present, unreachable from any row.
            async with await connect(migrated_url) as check:
                assert await upload_manifest.get_manifest(check, "runs", run_id) is None
            assert store.present == set(keys)

            survivor = _FakeUploadStore(dict.fromkeys(store.present, _GRACE * 2))
            reclaimed = await run_repair(pool, _sweep(survivor))
        assert sorted(survivor.deleted) == sorted(keys)
        assert reclaimed == 2

    asyncio.run(_run())


def test_the_sweep_deletes_exactly_the_orphans_and_nothing_else(migrated_url: str) -> None:
    """AC-1 + AC-4: two orphans go, the registered sibling under the same prefix stays."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        registered = f"{prefix}vmcore-kdump"
        async with await connect(migrated_url) as seed:
            await _insert_artifact_row(
                seed, owner_kind="runs", owner_id=run_id, object_key=registered
            )
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        store = _FakeUploadStore(
            {
                f"{prefix}kernel": _GRACE * 2,
                f"{prefix}kernel.part0001": _GRACE * 2,
                registered: _GRACE * 2,
            }
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 2
        assert sorted(store.deleted) == [f"{prefix}kernel", f"{prefix}kernel.part0001"]
        assert store.present == {registered}

    asyncio.run(_run())


def test_a_live_upload_window_is_never_reclaimed(migrated_url: str) -> None:
    """AC-2: an owner holding a manifest row owns its owner-addressed keys, however old the bytes.

    Age alone must not condemn an object: a chunked upload can sit staged for hours under an open
    window that is repeatedly deadline-refreshed.
    """

    async def _run() -> None:
        _run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(hours=1))
        store = _FakeUploadStore({f"{prefix}kernel": _GRACE * 10})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_a_re_minted_window_protects_the_same_key_names(migrated_url: str) -> None:
    """AC-2, the case that matters: re-mint is the *documented* recovery from a reap (ADR-0448).

    Upload keys are owner-addressed, so a re-minted window reuses the reaped window's key names.
    Fencing on "no manifest row at all" — rather than "no *live* manifest row" — is what makes the
    recovery path safe, so this pins that a **past-deadline** re-mint also protects its bytes: that
    window is the reaper's to collect, not this sweep's.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        # The reap happened; the operator re-minted, and that window has since lapsed too.
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
            await upload_manifest.replace_manifest(
                seed,
                upload_manifest.UploadManifestReplaceRequest(
                    owner_kind="runs",
                    owner_id=run_id,
                    prefix=prefix,
                    entries=[ManifestEntry("kernel", "a", 1)],
                    ttl=timedelta(seconds=-1),
                ),
            )
        store = _FakeUploadStore({f"{prefix}kernel": _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_a_freshly_written_object_is_protected_by_the_grace(migrated_url: str) -> None:
    """AC-3: a presigned PUT may begin before the deadline and complete after it.

    The object then exists with the window already reaped, so prefix membership alone would destroy
    a live upload's bytes. The grace is the fence, and it is compared against Postgres ``now()``.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        fresh, old = f"{prefix}just-landed", f"{prefix}long-abandoned"
        store = _FakeUploadStore({fresh: timedelta(seconds=5), old: _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [old]
        assert fresh in store.present

    asyncio.run(_run())


def test_an_object_just_past_the_grace_is_reclaimed(migrated_url: str) -> None:
    """The counterweight to the grace test: the fence is a boundary, not a blanket exemption."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        store = _FakeUploadStore({f"{prefix}kernel": timedelta(seconds=90)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store, timedelta(seconds=60))) == 1
        assert store.deleted == [f"{prefix}kernel"]

    asyncio.run(_run())


def test_an_investigation_orphan_is_reclaimed(migrated_url: str) -> None:
    """AC-1 for the second root: ADR-0453's residual covers the investigations lane too."""

    async def _run() -> None:
        inv_id, prefix = await _seed_investigation_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        store = _FakeUploadStore({f"{prefix}rootfs-abc": _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{prefix}rootfs-abc"]

    asyncio.run(_run())


def test_a_committed_artifacts_row_protects_a_finalized_rootfs(migrated_url: str) -> None:
    """AC-4: a finalized investigation rootfs has a row and no window — age must not condemn it."""

    async def _run() -> None:
        inv_id, prefix = await _seed_investigation_with_window(migrated_url, timedelta(seconds=-1))
        key = f"{prefix}rootfs-abc"
        async with await connect(migrated_url) as seed:
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=key
            )
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        store = _FakeUploadStore({key: _GRACE * 100})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_a_key_that_cannot_be_attributed_to_an_owner_is_never_deleted(migrated_url: str) -> None:
    """AC-5: only ``local/<kind>/<uuid>/<name>`` is swept; anything else is dropped, not deleted.

    An unattributable key cannot be fenced on its owner's manifest row, so deleting it would be
    deleting on prefix membership alone — which is the one thing this sweep must never do.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        odd = {
            "local/runs/not-a-uuid/kernel": _GRACE * 2,  # unparseable owner id
            f"local/runs/{run_id}/nested/kernel": _GRACE * 2,  # deeper than an upload key
            f"local/runs/{run_id}/": _GRACE * 2,  # the prefix marker itself, empty name
        }
        store = _FakeUploadStore({**odd, f"{prefix}kernel": _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{prefix}kernel"]
        assert store.present == set(odd)

    asyncio.run(_run())


def test_a_row_committed_between_the_classify_and_the_delete_protects_the_object(
    migrated_url: str,
) -> None:
    """ADR-0455 §3: the bulk classify is re-checked per key immediately before the delete.

    Without the re-check, a ``capture_traffic`` retry or a vmcore finalize that commits its row
    after the listing would have its bytes deleted under it — the very failure #1557 pins for the
    *reaper*. This sweep does not inherit it.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        pcap = f"{prefix}pcap-late"

        def _commit_the_row_mid_sweep() -> None:
            with psycopg.connect(migrated_url, autocommit=True) as writer:
                writer.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, "
                    "    sensitivity, retention_class) VALUES (%s, %s, %s, %s, %s, %s)",
                    ("runs", run_id, pcap, "etag-1", "sensitive", "pcap"),
                )

        # Two keys: the hook fires before the *first* delete, so the pcap's re-check runs after the
        # row is committed and must decline it while the other orphan still goes.
        store = _HookedStore(
            {f"{prefix}kernel": _GRACE * 2, pcap: _GRACE * 2},
            before_delete=_commit_the_row_mid_sweep,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{prefix}kernel"]
        assert pcap in store.present

    asyncio.run(_run())


def test_a_delete_failure_propagates_and_costs_only_the_pass(migrated_url: str) -> None:
    """AC-6, and the deliberate asymmetry with the reaper (ADR-0455 §5).

    The reaper tolerates a failed key because its row delete has already committed and there is
    nothing to retry. Here nothing is committed at all, so a fault propagates: ``_run_repair_plan``
    records it in ``failures`` — the sole input to the ADR-0190 group-E error counter — and the next
    pass re-derives the identical candidate from the store and the database.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        bad = f"{prefix}kernel"
        store = _FailingDeleteStore({bad: _GRACE * 2}, fail_keys={bad})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):
                await run_repair(pool, _sweep(store))
            assert store.attempted == [bad]
            # Nothing was committed, so the retry sees the same candidate and succeeds.
            retry = _FakeUploadStore({bad: _GRACE * 2})
            assert await run_repair(pool, _sweep(retry)) == 1
        assert retry.deleted == [bad]

    asyncio.run(_run())


def test_an_empty_bucket_sweeps_nothing_and_returns_zero(migrated_url: str) -> None:
    """The empty listing: the bulk classify must tolerate zero candidates, not error on it."""

    async def _run() -> None:
        store = _FakeUploadStore({})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_reclaimable_upload_keys_is_the_reusable_per_key_predicate(migrated_url: str) -> None:
    """What #1557 is handed (ADR-0455 §Consequences).

    ADR-0453 §Consequences costed the fix for its second residual as "a per-key re-check
    (``repair_leaked_images``' precedent)". That predicate is this function: it takes a connection
    and a candidate list and returns the reclaimable subset, so wiring it into
    ``_sweep_uncommitted_objects`` is a call rather than a rewrite. Pinned here so a change that
    makes it un-reusable fails loudly.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        fenced, free = f"{prefix}fenced", f"{prefix}free"
        now = datetime.now(UTC)
        candidates = [
            UploadOrphanCandidate(
                key=key,
                last_modified=now - _GRACE * 2,
                owner_kind="runs",
                owner_id=run_id,
            )
            for key in (fenced, free)
        ]
        async with await connect(migrated_url) as check:
            # The window is still open, so the manifest fence declines both.
            assert await reclaimable_upload_keys(check, candidates, _GRACE) == []
            await upload_manifest.delete_manifest(check, "runs", run_id)
            await _insert_artifact_row(check, owner_kind="runs", owner_id=run_id, object_key=fenced)
            assert await reclaimable_upload_keys(check, candidates, _GRACE) == [free]
            assert await reclaimable_upload_keys(check, [], _GRACE) == []

    asyncio.run(_run())


def test_the_swept_roots_cover_both_upload_owner_kinds() -> None:
    """The scope is derived from the reaper's owner kinds, not hand-listed alongside them."""
    assert UPLOAD_ORPHAN_ROOTS == ("local/runs/", "local/investigations/")


def test_both_threshold_terms_are_declared_for_both_processes_that_sweep() -> None:
    """ADR-0455 §2/§8: the threshold only tracks the operator's values where it is told to read.

    ``processes`` does not gate ``Registry.get``, but it does gate ``config validate`` and the
    generated operator reference — which is what an operator provisions each process's environment
    from. Both terms must name **both** processes: the reconciler runs the sweep on its loop, and
    the server runs a full ``reconcile_once`` on demand via ``ops.reconcile_now``, so a brake
    declared for only one of them lets the other keep deleting at the default.
    """
    both = frozenset({"server", "reconciler"})
    assert both <= UPLOAD_TTL_SECONDS.processes
    assert both <= UPLOAD_ORPHAN_GRACE.processes
    # Both declare a default, which is why the resolvers use `require` and carry no unset branch.
    assert UPLOAD_TTL_SECONDS.default == "86400"
    assert UPLOAD_ORPHAN_GRACE.default == "86400"


def test_neither_threshold_term_accepts_a_value_that_inverts_the_cutoff() -> None:
    """A negative term inverts the brake: it moves the cutoff into the *future*.

    ``now() - grace`` with a negative sum makes every rowless object under both roots older than
    the threshold, including one whose PUT landed seconds ago, and the per-key re-read cannot catch
    it because it re-evaluates the same inverted predicate. This is the only brake on a repair that
    deletes irreversibly, so a sign error has to fail at ``config validate``, not at the delete.

    **Both** terms are pinned because the threshold is their sum: guarding the grace alone leaves a
    negative TTL cancelling it and reaching the identical inversion through the unguarded half.
    """
    for setting in (UPLOAD_ORPHAN_GRACE, UPLOAD_TTL_SECONDS):
        assert setting.parse("0") == 0
        with pytest.raises(ValueError, match="must be >= 0"):
            setting.parse("-86400")


def test_the_reclaim_threshold_stacks_the_orphan_grace_on_the_upload_ttl(
    migrated_url: str,
) -> None:
    """ADR-0455 §2: the manifest fence lapses at the reap, so the grace must clear the TTL too.

    An object PUT moments after its window is minted is rowless for the whole window, and the
    manifest fence protects it only until the reaper deletes the row a TTL later. A threshold
    measured on the object's mtime alone and merely *equal* to the TTL therefore expires within
    seconds of the reap, and one above it expires before it — letting the sweep reclaim the bytes
    in the very pass that reaped them, and destroying ADR-0448's re-mint recovery. Stacking the
    two makes the margin a full orphan grace past the earliest possible reap, whatever the
    operator sets ``KDIVE_UPLOAD_TTL_SECONDS`` to.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        ttl = timedelta(hours=24)
        # Minted a TTL ago and reaped just now: past the bare grace, inside grace + ttl.
        just_reaped = f"{prefix}just-reaped"
        store = _FakeUploadStore({just_reaped: ttl + timedelta(minutes=1)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store, _GRACE, ttl)) == 0
            assert store.deleted == []
            # A full orphan grace later it is reclaimable, so the fence is a threshold, not a veto.
            older = _FakeUploadStore({just_reaped: ttl + _GRACE + timedelta(minutes=1)})
            assert await run_repair(pool, _sweep(older, _GRACE, ttl)) == 1

    asyncio.run(_run())


def test_a_root_whose_keys_are_all_unattributable_is_reported(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0455 §4: a sweep scoped out of its own bucket must not look like a clean one.

    Zero deleted is also the healthy steady state, so key-layout drift — the one failure that
    scopes this sweep out without raising — would otherwise be invisible. It is distinguishable
    because objects were listed and none were attributed.
    """

    async def _run() -> None:
        store = _FakeUploadStore({"local/runs/not-a-uuid/kernel": _GRACE * 2})
        with caplog.at_level(logging.WARNING, logger="kdive.reconciler.cleanup.upload_orphans"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _sweep(store)) == 0
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # only the root that listed something warns
        assert "attributed none of 1 object(s) under local/runs/" in warnings[0]

    asyncio.run(_run())


def test_a_clean_sweep_logs_no_drift_warning(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The counterweight: the drift warning is conditional, not emitted on every empty pass."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        store = _FakeUploadStore({f"{prefix}kernel": _GRACE * 2})
        with caplog.at_level(logging.WARNING, logger="kdive.reconciler.cleanup.upload_orphans"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _sweep(store)) == 1
        assert caplog.records == []

    asyncio.run(_run())


def test_an_object_rewritten_between_the_listing_and_the_delete_is_not_reclaimed(
    migrated_url: str,
) -> None:
    """The object-before-row writers under this prefix, and why the mtime is re-read from the store.

    A vmcore's multi-GiB ``put_stream`` and a ``capture_traffic`` retry's re-PUT both land minutes
    before their ``artifacts`` row commits, and both reuse a deterministic key name. So a rowless
    key that outlived the threshold and is then re-written has, for the length of that PUT, no row
    to protect it — only its mtime. Re-checking the *listed* mtime would find it stale and delete
    bytes that were just written, and ``finalize_capture`` would then commit rows against an object
    that no longer exists. ``repair_leaked_images`` is not exposed to this because image publishes
    are row-before-object; under ``local/runs/`` the ordering is reversed.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        vmcore = f"{prefix}vmcore-kdump"
        store: _FakeUploadStore

        def _rewrite_the_vmcore() -> None:
            # The retry's PUT completes; its artifacts row is still minutes away.
            store.put(vmcore, timedelta(seconds=0))

        # 'kernel' sorts before 'vmcore-kdump', so the hook fires while the vmcore is still pending.
        store = _HookedStore(
            {f"{prefix}kernel": _GRACE * 2, vmcore: _GRACE * 2},
            before_delete=_rewrite_the_vmcore,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{prefix}kernel"]
        assert vmcore in store.present

    asyncio.run(_run())


def test_a_put_inside_the_same_key_s_re_read_delete_gap_is_destroyed(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0455 §3's residual, for the key actually being deleted (#1574, ADR-0497).

    Every other concurrency test in this module fires its hook before a **different** key's delete,
    so what they pin is the re-check declining a *later* candidate — the gap this one is about was
    never exercised. Here the store holds one key, so the hook lands between that key's own
    re-check and its own ``delete_object``.

    The assertion is the version identity, not the deletion. A key going away proves nothing: an
    aged rowless orphan is deleted by four earlier tests in this module. What is specific to this
    race is the *pair* — the sweep's reclaim line names the etag it re-read and decided on, and the
    etag it actually destroyed is a different, newer one. The delete is issued on bytes the sweep
    never examined.

    This asserts the loss on purpose, and since ADR-0502 it pins the **unleased** path
    specifically. A fence can only protect a writer that declares itself, and the writer here
    declares nothing: no ``object_write_leases`` row exists for this owner, so the sweep is
    behaving correctly in deleting an aged rowless orphan. The capture lane no longer takes this
    path — it holds a write lease across its ``put_stream``, proven end to end against this same
    sweep in ``tests/adversarial/test_vmcore_capture_write_lease.py``. What remains true here is
    the raw store-level residual, and it is kept as the regression test for the sweep's behaviour
    toward an undeclared writer.

    ADR-0497 rejected the other fix this test invites — S3 ``If-Match`` on ``DeleteObject``, using
    the etag the re-read already observed — because both MinIO releases this repo pins accept the
    header, return success, and delete the object regardless, so a guard built on it would leave
    this pair true while reading as if the race were closed. The loss is also made *loud* one layer
    down, where ``finalize_capture`` refuses to commit a row against the object this delete removed
    (``tests/adversarial/test_vmcore_finalize_object_verify.py``). If the two etags below ever stop
    diverging, the store has gained the precondition and ADR-0497 §1 should be revisited.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        vmcore = f"{prefix}vmcore-kdump"
        stale, fresh = "etag-of-the-abandoned-attempt", "etag-of-the-retried-capture"
        store: _FakeUploadStore

        def _the_retried_capture_s_put_completes() -> None:
            store.put(vmcore, timedelta(seconds=0), etag=fresh)

        # One key, so the hook fires in *this* key's gap rather than ahead of a sibling's.
        store = _HookedStore(
            {vmcore: _GRACE * 2}, before_delete=_the_retried_capture_s_put_completes
        )
        store.put(vmcore, _GRACE * 2, etag=stale)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with caplog.at_level(logging.INFO, logger="kdive.reconciler.cleanup.upload_orphans"):
                assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [vmcore]
        # The bytes destroyed are the retry's, not the ones the re-read decided on.
        assert store.deleted_etags == [fresh]
        reclaims = [r.getMessage() for r in caplog.records if "deleted" in r.getMessage()]
        assert len(reclaims) == 1
        assert stale in reclaims[0], "the decision was made on the version that no longer existed"
        assert fresh not in reclaims[0]

    asyncio.run(_run())


def test_an_object_deleted_by_someone_else_before_the_delete_is_skipped(
    migrated_url: str,
) -> None:
    """The re-read's other arm: an object already gone is not a delete and not a failure."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        vanishing = f"{prefix}vanishing"
        store: _FakeUploadStore

        def _drop_the_other_object() -> None:
            store.forget(vanishing)

        store = _HookedStore(
            {f"{prefix}kernel": _GRACE * 2, vanishing: _GRACE * 2},
            before_delete=_drop_the_other_object,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted == [f"{prefix}kernel"]

    asyncio.run(_run())


def test_one_undeletable_key_does_not_starve_the_keys_behind_it(migrated_url: str) -> None:
    """ADR-0455 §5: aborting at the first failed key would make one stuck object a permanent leak.

    A transient store fault costs a pass either way. A *persistent* per-object one — an S3 Object
    Lock retention, a per-key deny — would abort at the same key on every pass forever under a
    first-failure abort, so every candidate behind it and the whole second root would never be
    reclaimed. The failure is still reported, once, after the loop.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        inv_id, inv_prefix = await _seed_investigation_with_window(
            migrated_url, timedelta(seconds=-1)
        )
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        stuck, behind = f"{prefix}a-locked", f"{prefix}b-behind"
        other_root = f"{inv_prefix}rootfs-abc"
        store = _FailingDeleteStore(
            dict.fromkeys([stuck, behind, other_root], _GRACE * 2), fail_keys={stuck}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:  # still reported, once
                await run_repair(pool, _sweep(store))
        assert "could not reclaim 1 object(s); 2 were reclaimed" in str(caught.value)
        assert sorted(store.deleted) == sorted([behind, other_root])

    asyncio.run(_run())


def test_one_pass_reclaims_at_most_the_per_root_budget(migrated_url: str) -> None:
    """ADR-0455 §6: an unbounded drain would stall every other repair behind it.

    The reconciler runs its catalog sequentially on one connection with no per-pass deadline, and
    each reclaim costs a HEAD, a query, and a delete — so the first pass against a backlog that has
    accumulated since ADR-0453 would hold allocation expiry, orphaned-System repair, and domain
    reaping for as long as it took. The remainder is reclaimed by the following passes.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        over = MAX_RECLAIMS_PER_ROOT + 5
        store = _FakeUploadStore({f"{prefix}orphan-{i:04d}": _GRACE * 2 for i in range(over)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == MAX_RECLAIMS_PER_ROOT
            assert len(store.present) == 5
            # The next pass picks the remainder up; nothing is stranded by the cap.
            assert await run_repair(pool, _sweep(store)) == 5
        assert store.present == set()

    asyncio.run(_run())


def test_the_per_root_budget_counts_every_examined_candidate(migrated_url: str) -> None:
    """The budget bounds *work*, not successes.

    A failed key — and equally a key whose re-check declines — still costs the re-read HEAD and the
    re-check query, so a budget that only counted deletes would leave a pass against a wholly
    undeletable backlog, or a pass overlapping another that already deleted everything, unbounded
    again. Those are the degraded modes the budget exists for, not exotic ones.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        keys = [f"{prefix}orphan-{i:04d}" for i in range(MAX_RECLAIMS_PER_ROOT + 5)]
        store = _FailingDeleteStore(dict.fromkeys(keys, _GRACE * 2), fail_keys=set(keys))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):
                await run_repair(pool, _sweep(store))
        assert len(store.attempted) == MAX_RECLAIMS_PER_ROOT

    asyncio.run(_run())


def test_a_wholly_stuck_first_root_does_not_starve_the_second(migrated_url: str) -> None:
    """ADR-0455 §6: the work budget is per root, which is what keeps §5's guarantee true at scale.

    A per-*pass* budget that counted failures would be consumed entirely by a scoped persistent
    fault — an object lock over a prefix, a per-prefix ``s3:DeleteObject`` deny — covering a whole
    budget's worth of keys under ``local/runs/``. ``local/investigations/`` would then never even be
    listed, on any pass, and its leak would resume unbounded behind the stuck batch: exactly the
    starvation the skip-and-count design was chosen to avoid.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        inv_id, inv_prefix = await _seed_investigation_with_window(
            migrated_url, timedelta(seconds=-1)
        )
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        locked = [f"{prefix}locked-{i:04d}" for i in range(MAX_RECLAIMS_PER_ROOT + 5)]
        rootfs = f"{inv_prefix}rootfs-abc"
        store = _FailingDeleteStore(
            dict.fromkeys([*locked, rootfs], _GRACE * 2), fail_keys=set(locked)
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _sweep(store))
        # The stuck root spent its own budget; the investigations root still got swept.
        assert store.deleted == [rootfs]
        assert f"{MAX_RECLAIMS_PER_ROOT} object(s); 1 were reclaimed" in str(caught.value)

    asyncio.run(_run())


def test_each_key_under_one_prefix_is_aged_on_its_own_mtime(migrated_url: str) -> None:
    """Sibling keys sharing a prefix are aged independently; membership alone condemns nothing.

    A chunked window's parts are ``<base>.partNNNN``, and a row-first reap that failed partway
    leaves the base and its parts rowless together — so this shape is the sweep's own subject
    matter, not a curiosity. What it pins is the **classify**: ``_RECLAIMABLE_SQL`` compares each
    candidate's own ``last_modified``, so an old base drains while a part written seconds ago
    keeps its grace. A predicate that took any single mtime for the group would either protect
    bytes that should drain or delete a PUT that just landed.

    It does *not* pin the per-key re-read, and it never did much: since #1575 the re-read is a
    ``head`` on the exact key, so resolving to a sibling is not a mistake the code can make. The
    re-read's own behaviour is pinned by the rewrite, already-deleted, and cost tests below.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        base = f"{prefix}vmcore"
        part = f"{base}.part0001"
        store = _FakeUploadStore({base: _GRACE * 2, part: timedelta(seconds=0)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        # The old base drained on its own mtime; the freshly written part kept its grace.
        assert store.deleted == [base]
        assert store.present == {part}

    asyncio.run(_run())


def test_the_mtime_re_read_costs_one_head_and_never_a_listing(migrated_url: str) -> None:
    """The re-read is a single ``head``, so a base key does not enumerate its parts (#1575).

    The whole point is that the cost is independent of how many keys the candidate prefixes: a
    LIST-backed re-read of ``<base>`` returns ``<base>`` plus every ``<base>.partNNNN``, paginated
    to exhaustion. Both halves of that are asserted — one ``head`` per examined candidate, and
    **no** listing beyond the one per root the sweep opens with — because a re-read that merely
    got cheaper (a bounded LIST) would still pass a call-count-only assertion.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        base = f"{prefix}vmcore"
        parts = [f"{base}.part{n:04d}" for n in range(1, 4)]
        keys = [base, *parts]
        store = _FakeUploadStore(dict.fromkeys(keys, _GRACE * 2))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == len(keys)
        assert sorted(store.deleted) == sorted(keys)
        # One listing per root, and not one more: the re-read never lists.
        assert store.listed_prefixes == list(UPLOAD_ORPHAN_ROOTS)
        # Exactly one head per examined candidate, the base's no dearer than a part's.
        assert sorted(store.headed_keys) == sorted(keys)

    asyncio.run(_run())


def test_a_re_read_failure_is_skipped_and_counted_like_every_other_per_key_fault(
    migrated_url: str,
) -> None:
    """The re-read is a fourth per-key failure site, and it fails the same way the others do.

    Since #1575 it is also the only store call in the sweep that needs ``s3:GetObject`` rather
    than ``s3:ListBucket``, so a credential that can list and delete but not HEAD faults *here*
    and nowhere else. It must not delete on a re-read it could not make, and it must not abort
    the pass: the key is skipped, counted, and the pass raises once at the end (ADR-0455 §5).
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        denied, reachable = f"{prefix}vmcore", f"{prefix}kernel"
        store = _FailingHeadStore({denied: _GRACE * 2, reachable: _GRACE * 2}, fail_keys={denied})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as excinfo:
                await run_repair(pool, _sweep(store))
        assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        # The un-HEADable key survives — a re-read that failed is not a licence to delete — and
        # the key behind it is still reclaimed.
        assert store.deleted == [reachable]
        assert store.present == {denied}

    asyncio.run(_run())


def test_the_reclaim_log_names_the_etag_the_delete_decision_was_made_on(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The re-read observes an identity, not just an age, and the record says which one.

    ``head`` returns the object's etag in the same round trip as its mtime, so the two cannot
    disagree about which bytes were examined. Naming it on the reclaim line is what lets an
    operator investigating a wrongly-drained key tell whether the deleted version is the one they
    expect — the alternative is a log that says an object was deleted and nothing about which.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        key = f"{prefix}vmcore"
        store = _FakeUploadStore({key: _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with caplog.at_level(logging.INFO, logger="kdive.reconciler.cleanup.upload_orphans"):
                assert await run_repair(pool, _sweep(store)) == 1
        reclaims = [r.getMessage() for r in caplog.records if "deleted" in r.getMessage()]
        assert len(reclaims) == 1
        assert key in reclaims[0]
        assert f"etag-of-{key}" in reclaims[0]

    asyncio.run(_run())


def test_a_classify_failure_on_the_first_root_does_not_starve_the_second(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0455 §5: the classify is a failure site, and it ends its root without ending the pass.

    A ``statement_timeout`` or a dropped pool connection out of the anti-join is not made impossible
    by the narrower statement #1569 brought — the parameter is a listing page wide now rather than a
    whole root's, so the fault is no longer *preferentially* `local/runs/`'s, but it is still
    reachable on either root. Aborting the pass on it would leave ``local/investigations/`` unswept
    on every pass while the condition held, which is the starvation skip-and-count prevents.
    """
    real_classify = upload_orphans.reclaimable_upload_keys

    async def _classify(conn, candidates, grace):  # type: ignore[no-untyped-def]
        if any(c.key.startswith("local/runs/") for c in candidates):
            raise psycopg.OperationalError("statement timeout on the runs anti-join")
        return await real_classify(conn, candidates, grace)

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        inv_id, inv_prefix = await _seed_investigation_with_window(
            migrated_url, timedelta(seconds=-1)
        )
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        rootfs = f"{inv_prefix}rootfs-abc"
        store = _FakeUploadStore({f"{prefix}orphan": _GRACE * 2, rootfs: _GRACE * 2})
        monkeypatch.setattr(upload_orphans, "reclaimable_upload_keys", _classify)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _sweep(store))
        # The root whose classify raised was skipped; the sibling still drained.
        assert store.deleted == [rootfs]
        assert "could not reclaim 1 object(s); 1 were reclaimed" in str(caught.value)

    asyncio.run(_run())


def test_a_listing_failure_on_the_first_root_does_not_starve_the_second(
    migrated_url: str,
) -> None:
    """ADR-0455 §5: a scoped list deny is the twin of the delete deny the budget is per root for.

    An IAM ``s3:prefix`` condition is the standard way to scope list authority, so a deny covering
    ``local/runs/`` alone is the same class of misconfiguration as the per-prefix
    ``s3:DeleteObject`` deny §6 makes the budget per root to survive. Aborting the pass on it would
    leave ``local/investigations/`` — the rootfs upload lane's root — unlisted on *every* pass for
    as long as the fault persisted, resuming exactly the leak this repair drains. The failing root
    is skipped and counted instead, and the pass still raises at the end so the fault is not silent.
    """

    async def _run() -> None:
        inv_id, inv_prefix = await _seed_investigation_with_window(
            migrated_url, timedelta(seconds=-1)
        )
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        rootfs = f"{inv_prefix}rootfs-abc"
        store = _FailingListStore({rootfs: _GRACE * 2}, fail_list_prefixes={"local/runs/"})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _sweep(store))
        # The unlistable root did not stop the sibling from draining...
        assert store.deleted == [rootfs]
        # ...and the fault is still reported, counted as the one failure of the pass.
        assert "could not reclaim 1 object(s); 1 were reclaimed" in str(caught.value)

    asyncio.run(_run())


def _recording_classify(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch the classify to record the candidate keys of every call, changing nothing else.

    The recorded calls are the sweep's two uses of one predicate: a per-page classify, and the
    single-key re-check before each delete. A page's call is therefore the one whose width can
    exceed one, which is what #1569 is about.

    It deliberately does not perturb the returned order. Reordering the result would prove nothing:
    ``_reclaim_page`` puts it straight into a ``set``, so any order a stub returned is discarded
    before use. What pins the delete order is that the sequence matches the *pages*, asserted
    against enough keys that a ``set``'s hash order is not lexicographic — verified by mutation,
    not assumed.
    """
    real = upload_orphans.reclaimable_upload_keys
    calls: list[list[str]] = []

    async def _classify(
        conn: psycopg.AsyncConnection, candidates: list[UploadOrphanCandidate], grace: timedelta
    ) -> list[str]:
        calls.append([c.key for c in candidates])
        return await real(conn, candidates, grace)

    monkeypatch.setattr(upload_orphans, "reclaimable_upload_keys", _classify)
    return calls


async def _seed_reaped_run_orphans(url: str, names: list[str]) -> tuple[str, list[str]]:
    """Seed a run whose window was reaped, with ``names`` aged orphans left under its prefix."""
    run_id, prefix = await _seed_run_with_window(url, timedelta(seconds=-1))
    async with await connect(url) as seed:
        await upload_manifest.delete_manifest(seed, "runs", run_id)
    return prefix, [f"{prefix}{name}" for name in names]


def test_the_classify_never_sees_more_than_one_listing_page_of_candidates(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1569: a root is classified page by page, so no statement is as wide as the bucket.

    The whole-root version built one ``candidates`` list per root and passed it to
    ``reclaimable_upload_keys`` as four parallel arrays sized to the entire listing — a six-figure
    Python list and a multi-megabyte psycopg parameter payload every 30 seconds under a prefix that
    only ever grows. Two independent halves are asserted, because neither implies the other: no
    statement is wider than a page, **and** the sweep acted on each page before fetching the next.
    The second needs the interleaving and not the page count — draining the iterator and then
    slicing the result into page-shaped calls produces identical page counts and identical
    statement widths, and is exactly the shape that would leave the whole root in memory.
    """

    async def _run() -> None:
        _prefix, keys = await _seed_reaped_run_orphans(
            migrated_url, [f"orphan-{i:02d}" for i in range(10)]
        )
        store = _FakeUploadStore(dict.fromkeys(keys, _GRACE * 2))
        calls = _recording_classify(monkeypatch)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == len(keys)
        assert max(len(call) for call in calls) <= _PAGE_SIZE
        # 10 keys at 3 per page: four pages for this root, plus the empty sibling root's one.
        assert [len(page) for page in store.pages_yielded] == [3, 3, 3, 1, 0]
        assert sorted(store.deleted) == sorted(keys)
        # Every page's deletes land before the next page is fetched. A drain-then-slice
        # implementation yields all five pages first and this sequence would not alternate.
        assert store.events == [
            "page:3",
            *(f"delete:{k}" for k in keys[0:3]),
            "page:3",
            *(f"delete:{k}" for k in keys[3:6]),
            "page:3",
            *(f"delete:{k}" for k in keys[6:9]),
            "page:1",
            *(f"delete:{k}" for k in keys[9:10]),
            "page:0",
        ]

    asyncio.run(_run())


def test_the_delete_order_is_store_order_across_page_boundaries(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0455 §3 and ADR-0498 §2: paging must not become the classify's row order.

    Store order is an acceptance criterion, not a nicety: a pass truncated by the per-root budget or
    by a mid-root listing fault is only reproducible — the same prefix of the same sequence every
    pass — if the order comes from the store. Postgres guarantees no ordering for an anti-join's
    output, and ``_reclaim_page`` puts the approved keys into a ``set``, so an implementation that
    iterated *that* would emit hash order. Eight keys is enough that hash order is not
    lexicographic, which is what gives this assertion its bite; the mutation that iterates the set
    instead of the page is confirmed to redden it.
    """

    async def _run() -> None:
        _prefix, keys = await _seed_reaped_run_orphans(
            migrated_url, [f"orphan-{i:02d}" for i in range(8)]
        )
        store = _FakeUploadStore(dict.fromkeys(keys, _GRACE * 2))
        calls = _recording_classify(monkeypatch)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == len(keys)
        # Store order is lexicographic by key, and that is exactly the delete sequence.
        assert store.deleted == sorted(keys)
        # And it really did span pages — otherwise this pins order within one listing, not across.
        assert len([call for call in calls if len(call) > 1]) > 1
        assert store.events.count("page:3") > 1

    asyncio.run(_run())


def test_the_budget_stops_paging_the_root_whose_allowance_it_has_spent(
    migrated_url: str,
) -> None:
    """ADR-0498 §4: the per-root budget now bounds the **listing**, not just the deletes.

    ``MAX_RECLAIMS_PER_ROOT`` always bounded how many candidates one pass examines, but the
    whole-root listing was materialized first regardless — a drain enumerated a six-figure backlog
    to act on 200 of it. Paged, the root stops being fetched once the allowance is examined. What
    budget *counts* is unchanged, which is why the reclaimed total is still exactly the allowance
    and the remainder is still left for the following pass.
    """

    async def _run() -> None:
        _prefix, keys = await _seed_reaped_run_orphans(
            migrated_url, [f"orphan-{i:04d}" for i in range(MAX_RECLAIMS_PER_ROOT + 60)]
        )
        store = _FakeUploadStore(dict.fromkeys(keys, _GRACE * 2), page_size=MAX_RECLAIMS_PER_ROOT)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == MAX_RECLAIMS_PER_ROOT
        # One page of the runs root — the 60-key second page was never fetched — then the empty
        # sibling root's single page.
        assert [len(page) for page in store.pages_yielded] == [MAX_RECLAIMS_PER_ROOT, 0]
        assert len(store.present) == 60

    asyncio.run(_run())


def test_a_listing_fault_partway_through_a_root_keeps_the_pages_it_already_swept(
    migrated_url: str,
) -> None:
    """ADR-0498 §3: a paged listing can fault mid-root, and the earlier pages' deletes stand.

    With the whole-root listing there was no such state — the listing either produced a candidate
    set or produced nothing, so a fault cost the entire root. Paged, a page-2 fault arrives after
    page 1 has already deleted irreversibly, and unwinding is not an option: the objects are gone.
    So the root is abandoned from the failed page on, the fault is counted, and the pass raises once
    at the end — the same skip-and-count ADR-0455 §5 chose, now reached by a path that has already
    done work. It is safe for the reason a budget-truncated root is: nothing is committed, so the
    next pass re-derives the same candidates from where this one stopped.
    """

    async def _run() -> None:
        _prefix, keys = await _seed_reaped_run_orphans(
            migrated_url, [f"orphan-{i:02d}" for i in range(6)]
        )
        store = _FailingListStore(
            dict.fromkeys(keys, _GRACE * 2),
            fail_list_prefixes={"local/runs/"},
            fail_after_pages=1,
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _sweep(store))
        # Page 1's three keys are gone and stay gone; the fault is the pass's one failure.
        assert store.deleted == sorted(keys)[:_PAGE_SIZE]
        assert "could not reclaim 1 object(s); 3 were reclaimed" in str(caught.value)
        assert store.present == set(sorted(keys)[_PAGE_SIZE:])

    asyncio.run(_run())


def test_a_listing_failure_on_the_second_root_still_records_the_first_root_s_deletes(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0455 §5: a raising pass reports no count, so the count has to reach the log.

    A listing fault ends that root deliberately — without a listing there is no candidate set to be
    partial about — and the pass still raises at the end, but by then the first root may already
    have deleted irreversibly, and those deletes would otherwise leave no trace on the repairs gauge
    or anywhere else.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        store = _FailingListStore(
            {f"{prefix}orphan": _GRACE * 2}, fail_list_prefixes={"local/investigations/"}
        )
        with caplog.at_level(logging.ERROR, logger="kdive.reconciler.cleanup.upload_orphans"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                with pytest.raises(CategorizedError):
                    await run_repair(pool, _sweep(store))
        assert store.deleted == [f"{prefix}orphan"]  # the first root's delete really happened
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "reclaimed 1 object(s)" in errors[0]

    asyncio.run(_run())
