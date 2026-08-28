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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import (
    HeadResult,
    ObjectListing,
    ObjectVersion,
    VersionBatch,
    VersionPage,
)
from kdive.artifacts.uploads import upload_manifest
from kdive.artifacts.uploads.uploads import ManifestEntry
from kdive.config.core_settings import UPLOAD_ORPHAN_GRACE, UPLOAD_TTL_SECONDS
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.uploads import upload_orphans
from kdive.reconciler.cleanup.uploads.upload_orphans import (
    MAX_RECLAIMS_PER_ROOT,
    MAX_VERSIONS_PER_KEY,
    UPLOAD_ORPHAN_ROOTS,
    UploadOrphanCandidate,
    reclaimable_upload_keys,
)
from kdive.reconciler.cleanup.uploads.upload_orphans import (
    repair_leaked_upload_objects as _repair_leaked_upload_objects,
)
from kdive.reconciler.cleanup.uploads.uploads import (
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
        self._versions: dict[str, list[ObjectVersion]] = {
            key: [self._make_version(key, age, "v1", is_latest=True)]
            for key, age in objects.items()
        }
        self._next_version = 2
        self._page_size = page_size
        self.deleted: list[str] = []
        self.deleted_versions: list[tuple[str, str]] = []
        self.deleted_etags: list[str] = []
        self.listed_prefixes: list[str] = []
        self.pages_yielded: list[list[str]] = []
        self.headed_keys: list[str] = []
        self.capture_limits: list[tuple[str, int]] = []
        self.version_page_calls: list[tuple[str, str | None, str | None, int]] = []
        self.events: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._objects)

    def put(self, key: str, age: timedelta = timedelta(0), etag: str | None = None) -> None:
        prior = self._versions.get(key, [])
        self._versions[key] = [replace(version, is_latest=False) for version in prior]
        version_id = f"v{self._next_version}"
        self._next_version += 1
        self._versions[key].append(self._make_version(key, age, version_id, is_latest=True))
        self._objects[key] = age
        if etag is not None:
            self._etags[key] = etag

    def _etag(self, key: str) -> str:
        return self._etags.get(key, f"etag-of-{key}")

    def forget(self, key: str) -> None:
        """Remove an object without recording a delete — another actor got there first."""
        self._objects.pop(key, None)
        self._versions.pop(key, None)

    def seed_versions(self, key: str, versions: list[ObjectVersion]) -> None:
        """Replace one key's immutable history for version-specific tests."""
        self._versions[key] = list(versions)
        latest = next((version for version in versions if version.is_latest), None)
        if latest is None or latest.is_delete_marker:
            self._objects.pop(key, None)
            return
        self._objects[key] = datetime.now(UTC) - latest.last_modified

    def version_ids(self, key: str) -> set[str]:
        """Return the surviving immutable identities for one key."""
        return {version.version_id for version in self._versions.get(key, ())}

    def _mtime(self, age: timedelta) -> datetime:
        return datetime.now(UTC) - age

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(key for key in self._objects if key.startswith(prefix))

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage:
        self.listed_prefixes.append(prefix)
        self.version_page_calls.append((prefix, key_marker, version_id_marker, max_keys))
        entries = self._version_listing(prefix)
        start = 0
        if key_marker is not None:
            if version_id_marker is None:
                while start < len(entries) and entries[start].key <= key_marker:
                    start += 1
            else:
                marker = (key_marker, version_id_marker)
                while start < len(entries):
                    entry = entries[start]
                    if (entry.key, entry.version_id) > marker:
                        break
                    start += 1
        page_size = min(self._page_size, max_keys)
        selected = entries[start : start + page_size]
        self.pages_yielded.append([entry.key for entry in selected])
        self.events.append(f"page:{len(selected)}")
        truncated = start + page_size < len(entries)
        last = selected[-1] if selected else None
        return VersionPage(
            entries=tuple(selected),
            is_truncated=truncated,
            next_key_marker=last.key if truncated and last is not None else None,
            next_version_id_marker=last.version_id if truncated and last is not None else None,
        )

    def iter_prefix_version_pages(self, prefix: str) -> Iterator[VersionPage]:
        key_marker = version_marker = None
        while True:
            page = self.list_version_page(
                prefix, key_marker=key_marker, version_id_marker=version_marker
            )
            yield page
            if not page.is_truncated:
                return
            key_marker = page.next_key_marker
            version_marker = page.next_version_id_marker

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        self.capture_limits.append((key, limit))
        versions = list(self._versions.get(key, ()))
        complete = len(versions) <= limit
        if not complete:
            latest = [version for version in versions if version.is_latest]
            nonlatest = [version for version in versions if not version.is_latest]
            versions = [*latest, *nonlatest]
        return VersionBatch(key, tuple(versions[:limit]), complete)

    def delete_batch(self, batch: VersionBatch) -> bool:
        for version in batch.targets:
            if not version.is_latest:
                self.delete_version(version.key, version.version_id)
        if not batch.history_complete:
            return False
        for version in batch.targets:
            if version.is_latest:
                self.delete_version(version.key, version.version_id)
        return True

    def delete_version(self, key: str, version_id: str) -> None:
        versions = self._versions.get(key, [])
        target = next((version for version in versions if version.version_id == version_id), None)
        if target is None:
            return
        if target.etag is not None:
            self.deleted_etags.append(target.etag)
        survivors = [version for version in versions if version.version_id != version_id]
        self.deleted.append(key)
        self.deleted_versions.append((key, version_id))
        self.events.append(f"delete:{key}")
        if not survivors:
            self._versions.pop(key, None)
            self._objects.pop(key, None)
            return
        if not any(version.is_latest for version in survivors):
            newest = max(survivors, key=lambda version: version.last_modified)
            survivors = [
                replace(version, is_latest=version.version_id == newest.version_id)
                for version in survivors
            ]
        self._versions[key] = survivors
        current = next(version for version in survivors if version.is_latest)
        if current.is_delete_marker:
            self._objects.pop(key, None)
        else:
            self._objects[key] = datetime.now(UTC) - current.last_modified

    def _version_listing(self, prefix: str) -> list[ObjectVersion]:
        return sorted(
            (
                version
                for key, versions in self._versions.items()
                if key.startswith(prefix)
                for version in versions
            ),
            key=lambda version: (version.key, version.last_modified, version.version_id),
        )

    def _make_version(
        self,
        key: str,
        age: timedelta,
        version_id: str,
        *,
        is_latest: bool,
        is_delete_marker: bool = False,
    ) -> ObjectVersion:
        return ObjectVersion(
            key=key,
            version_id=version_id,
            last_modified=self._mtime(age),
            etag=None if is_delete_marker else self._etag(key),
            is_latest=is_latest,
            is_delete_marker=is_delete_marker,
        )

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
            version_id="test-version",
        )


class _FailingDeleteStore(_FakeUploadStore):
    """Raises ``CategorizedError`` from exact deletion for named keys."""

    def __init__(self, objects: dict[str, timedelta], *, fail_keys: set[str]) -> None:
        super().__init__(objects)
        self._fail_keys = fail_keys
        self.attempted: list[str] = []

    def delete_version(self, key: str, version_id: str) -> None:
        self.attempted.append(key)
        if key in self._fail_keys:
            raise CategorizedError(
                f"delete_object failed for {key} version {version_id}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        super().delete_version(key, version_id)


class _FailingVersionStore(_FakeUploadStore):
    """Fail selected immutable identities while allowing sibling versions and keys."""

    def __init__(
        self,
        objects: dict[str, timedelta],
        *,
        fail_versions: set[tuple[str, str]],
        page_size: int = _PAGE_SIZE,
    ) -> None:
        super().__init__(objects, page_size=page_size)
        self._fail_versions = fail_versions
        self.attempted_versions: list[tuple[str, str]] = []

    def delete_version(self, key: str, version_id: str) -> None:
        identity = (key, version_id)
        self.attempted_versions.append(identity)
        if identity in self._fail_versions:
            raise CategorizedError(
                f"delete_object failed for {key} version {version_id}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        super().delete_version(key, version_id)


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
        self._version_pages_seen: dict[str, int] = {}

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

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage:
        seen = self._version_pages_seen.get(prefix, 0)
        if prefix in self._fail_list_prefixes and seen >= self._fail_after_pages:
            self.listed_prefixes.append(prefix)
            raise CategorizedError(
                f"list_object_versions failed for {prefix}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        self._version_pages_seen[prefix] = seen + 1
        return super().list_version_page(
            prefix,
            key_marker=key_marker,
            version_id_marker=version_id_marker,
            max_keys=max_keys,
        )


class _FailingCaptureStore(_FakeUploadStore):
    """Raise ``CategorizedError`` while capturing immutable history for named keys."""

    def __init__(self, objects: dict[str, timedelta], *, fail_keys: set[str]) -> None:
        super().__init__(objects)
        self._fail_keys = fail_keys

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        self.capture_limits.append((key, limit))
        if key in self._fail_keys:
            raise CategorizedError(
                f"list_object_versions failed for {key}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        self.capture_limits.pop()
        return super().capture_exact_versions(key, limit)


class _EmptyCaptureStore(_FakeUploadStore):
    """Model keys disappearing between broad inventory and exact capture."""

    def __init__(self, objects: dict[str, timedelta], *, empty_keys: set[str]) -> None:
        super().__init__(objects)
        self._empty_keys = empty_keys

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        if key in self._empty_keys:
            self.capture_limits.append((key, limit))
            return VersionBatch(key, (), True)
        return super().capture_exact_versions(key, limit)


class _HookedStore(_FakeUploadStore):
    """Runs ``before_delete`` from exact deletion, once, on the ``to_thread`` worker.

    That lands the hook in the gap between the per-key re-check and the delete, which is the only
    place a concurrent committer can still lose its object.
    """

    def __init__(self, objects: dict[str, timedelta], *, before_delete: Callable[[], None]) -> None:
        super().__init__(objects)
        self._before_delete = before_delete
        self._fired = False

    def delete_version(self, key: str, version_id: str) -> None:
        if not self._fired:
            self._fired = True
            self._before_delete()
        super().delete_version(key, version_id)


def _advisory_locks_held_by(url: str, backend_pid: int) -> int:
    """Count granted advisory locks held by ``backend_pid`` from a second connection."""
    with psycopg.connect(url, autocommit=True) as observer:
        row = observer.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s AND granted",
            (backend_pid,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _sweep(
    store: _FakeUploadStore,
    grace: timedelta = _GRACE,
    upload_ttl: timedelta = _NO_TTL,
):
    """The repair under test. ``upload_ttl`` defaults to zero so a test that is not *about* the
    TTL stacking reads its threshold straight off ``grace``; the stacking has its own test."""
    return lambda conn: _repair_leaked_upload_objects(conn, store, grace, upload_ttl)


def _data_history(store: _FakeUploadStore, key: str, count: int) -> list[ObjectVersion]:
    """Build an old version history whose final member is latest."""
    return [
        store._make_version(  # noqa: SLF001 - explicit test-fixture history
            key,
            _GRACE * 2 + timedelta(seconds=count - number),
            f"v{number:04d}",
            is_latest=number == count,
        )
        for number in range(1, count + 1)
    ]


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


def test_a_live_upload_window_protects_every_version_and_marker(migrated_url: str) -> None:
    """The owner fence protects the key, not merely the version visible as latest."""

    async def _run() -> None:
        _run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(hours=1))
        key = f"{prefix}kernel"
        store = _FakeUploadStore({key: _GRACE * 10})
        history = _data_history(store, key, 3)
        history[-1] = replace(history[-1], etag=None, is_delete_marker=True)
        store.seed_versions(key, history)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 0
        assert store.deleted_versions == []
        assert store.version_ids(key) == {"v0001", "v0002", "v0003"}

    asyncio.run(_run())


def test_literal_null_version_and_delete_marker_are_deleted_exactly(migrated_url: str) -> None:
    """An unversioned ``null`` identity and a delete marker are both first-class targets."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        key = f"{prefix}kernel"
        store = _FakeUploadStore({key: _GRACE * 2})
        old = store._make_version(  # noqa: SLF001 - explicit immutable test identity
            key, _GRACE * 3, "null", is_latest=False
        )
        marker = store._make_version(  # noqa: SLF001 - explicit immutable test identity
            key, _GRACE * 2, "marker-1", is_latest=True, is_delete_marker=True
        )
        store.seed_versions(key, [old, marker])
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 2
        assert store.deleted_versions == [(key, "null"), (key, "marker-1")]
        assert store.version_ids(key) == set()

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


def test_orphan_exact_delete_runs_after_owner_unlock(migrated_url: str) -> None:
    """The final database fence commits before any exact VersionId deletion starts."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        key = f"{prefix}kernel"
        observations: list[int] = []
        sweeper = await psycopg.AsyncConnection.connect(migrated_url)
        store = _HookedStore(
            {key: _GRACE * 2},
            before_delete=lambda: observations.append(
                _advisory_locks_held_by(migrated_url, sweeper.info.backend_pid)
            ),
        )
        try:
            assert await _repair_leaked_upload_objects(sweeper, store, _GRACE, _NO_TTL) == 1
        finally:
            await sweeper.close()
        assert observations == [0]

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
        with caplog.at_level(
            logging.WARNING,
            logger="kdive.reconciler.cleanup.uploads.upload_orphans",
        ):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _sweep(store)) == 0
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # only the root that listed something warns
        assert "attributed none of 1 version entry(s) under local/runs/" in warnings[0]

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
        with caplog.at_level(
            logging.WARNING,
            logger="kdive.reconciler.cleanup.uploads.upload_orphans",
        ):
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


def test_a_put_after_the_same_key_s_final_fence_survives_exact_delete(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A peer PUT after the owner fence is not among the captured immutable identities."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        vmcore = f"{prefix}vmcore-kdump"
        fresh = "etag-of-the-retried-capture"
        store: _FakeUploadStore

        def _the_retried_capture_s_put_completes() -> None:
            store.put(vmcore, timedelta(seconds=0), etag=fresh)

        # One key, so the hook fires in *this* key's gap rather than ahead of a sibling's.
        store = _HookedStore(
            {vmcore: _GRACE * 2}, before_delete=_the_retried_capture_s_put_completes
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with caplog.at_level(
                logging.INFO,
                logger="kdive.reconciler.cleanup.uploads.upload_orphans",
            ):
                assert await run_repair(pool, _sweep(store)) == 1
        assert store.deleted_versions == [(vmcore, "v1")]
        assert store.version_ids(vmcore) == {"v2"}
        assert store.deleted_etags == [f"etag-of-{vmcore}"]
        reclaims = [r.getMessage() for r in caplog.records if "deleted" in r.getMessage()]
        assert len(reclaims) == 1
        assert "version v1" in reclaims[0]
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
        assert "encountered 1 failed operation(s); 2 were confirmed reclaimed" in str(caught.value)
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


def test_one_hot_key_is_capped_and_a_key_only_marker_reaches_its_sibling(
    migrated_url: str,
) -> None:
    """Twenty captured identities cap one key without stranding the next key in that root."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        hot, sibling = f"{prefix}a-hot", f"{prefix}b-sibling"
        store = _FakeUploadStore({hot: _GRACE * 2, sibling: _GRACE * 2})
        store.seed_versions(hot, _data_history(store, hot, MAX_VERSIONS_PER_KEY + 5))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == MAX_VERSIONS_PER_KEY
        assert store.capture_limits[:2] == [
            (hot, MAX_VERSIONS_PER_KEY),
            (sibling, MAX_VERSIONS_PER_KEY),
        ]
        assert store.version_ids(hot) == {
            f"v{number:04d}" for number in range(MAX_VERSIONS_PER_KEY, 26)
        }
        assert sibling not in store.present
        assert any(
            key_marker == hot and version_marker is None
            for _root, key_marker, version_marker, _limit in store.version_page_calls
        )

    asyncio.run(_run())


def test_a_denied_hot_history_over_the_root_budget_does_not_hide_the_next_key(
    migrated_url: str,
) -> None:
    """One denied 205-version key costs 20 targets, then key-only resume reaches a sibling."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        hot, sibling = f"{prefix}a-denied", f"{prefix}b-sibling"
        store = _FailingDeleteStore({hot: _GRACE * 2, sibling: _GRACE * 2}, fail_keys={hot})
        store.seed_versions(hot, _data_history(store, hot, MAX_RECLAIMS_PER_ROOT + 5))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):
                await run_repair(pool, _sweep(store))
        assert store.attempted == [hot, sibling]
        assert store.capture_limits == [
            (hot, MAX_VERSIONS_PER_KEY),
            (sibling, MAX_VERSIONS_PER_KEY),
        ]
        assert store.version_ids(hot) == {
            f"v{number:04d}" for number in range(1, MAX_RECLAIMS_PER_ROOT + 6)
        }
        assert store.deleted_versions == [(sibling, "v1")]

    asyncio.run(_run())


def test_a_capture_denied_hot_history_uses_a_key_only_marker_before_its_sibling(
    migrated_url: str,
) -> None:
    """An exact-inventory deny cannot force broad enumeration of one key's 205 versions."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        hot, sibling = f"{prefix}a-denied", f"{prefix}b-sibling"
        store = _FailingCaptureStore({hot: _GRACE * 2, sibling: _GRACE * 2}, fail_keys={hot})
        store.seed_versions(hot, _data_history(store, hot, MAX_RECLAIMS_PER_ROOT + 5))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):
                await run_repair(pool, _sweep(store))
        assert store.capture_limits == [
            (hot, MAX_VERSIONS_PER_KEY),
            (sibling, MAX_VERSIONS_PER_KEY),
        ]
        assert store.deleted_versions == [(sibling, "v1")]
        assert any(
            key_marker == hot and version_marker is None
            for _root, key_marker, version_marker, _limit in store.version_page_calls
        )

    asyncio.run(_run())


def test_many_capture_denials_spend_the_root_budget_without_starving_the_other_root(
    migrated_url: str,
) -> None:
    """Ten denied 20-target requests consume the runs allowance; investigations still run."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        inv_id, inv_prefix = await _seed_investigation_with_window(
            migrated_url, timedelta(seconds=-1)
        )
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        denied = [f"{prefix}denied-{number:02d}" for number in range(15)]
        sibling_root = f"{inv_prefix}rootfs"
        store = _FailingCaptureStore(
            dict.fromkeys([*denied, sibling_root], _GRACE * 2), fail_keys=set(denied)
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _sweep(store))
        run_captures = [call for call in store.capture_limits if call[0].startswith(prefix)]
        assert run_captures == [(key, MAX_VERSIONS_PER_KEY) for key in denied[:10]]
        assert store.capture_limits[-1] == (sibling_root, MAX_VERSIONS_PER_KEY)
        assert store.deleted_versions == [(sibling_root, "v1")]
        assert "encountered 10 failed operation(s)" in str(caught.value)

    asyncio.run(_run())


def test_many_empty_capture_races_charge_one_listed_identity_each(
    migrated_url: str,
) -> None:
    """Disappearing exact histories cannot evade the 200-unit root work brake."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        inv_id, inv_prefix = await _seed_investigation_with_window(
            migrated_url, timedelta(seconds=-1)
        )
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
            await upload_manifest.delete_manifest(seed, "investigations", inv_id)
        raced = [f"{prefix}raced-{number:04d}" for number in range(MAX_RECLAIMS_PER_ROOT + 5)]
        sibling_root = f"{inv_prefix}rootfs"
        store = _EmptyCaptureStore(
            dict.fromkeys([*raced, sibling_root], _GRACE * 2), empty_keys=set(raced)
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 1
        run_captures = [call for call in store.capture_limits if call[0].startswith(prefix)]
        assert run_captures == [
            (key, min(MAX_VERSIONS_PER_KEY, MAX_RECLAIMS_PER_ROOT - index))
            for index, key in enumerate(raced[:MAX_RECLAIMS_PER_ROOT])
        ]
        assert store.deleted_versions == [(sibling_root, "v1")]

    asyncio.run(_run())


def test_a_latest_version_delete_failure_leaves_it_discoverable_and_sweeps_sibling(
    migrated_url: str,
) -> None:
    """Non-latest progress survives a latest-delete fault; the remaining latest is listed later."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        key, sibling = f"{prefix}a-history", f"{prefix}b-sibling"
        store = _FailingVersionStore(
            {key: _GRACE * 2, sibling: _GRACE * 2}, fail_versions={(key, "latest")}
        )
        old = store._make_version(  # noqa: SLF001 - explicit immutable test identity
            key, _GRACE * 3, "old", is_latest=False
        )
        latest = store._make_version(  # noqa: SLF001 - explicit immutable test identity
            key, _GRACE * 2, "latest", is_latest=True
        )
        store.seed_versions(key, [old, latest])
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):
                await run_repair(pool, _sweep(store))
        assert store.attempted_versions[:2] == [(key, "old"), (key, "latest")]
        assert store.version_ids(key) == {"latest"}
        assert store.deleted_versions[-1] == (sibling, "v1")

    asyncio.run(_run())


def test_complete_latest_failure_resumes_after_key_when_page_ends_inside_its_history(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Deleted page-marker identities cannot poison continuation after a complete-batch fault."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        key, sibling = f"{prefix}a-history", f"{prefix}b-sibling"
        store = _FailingVersionStore(
            {key: _GRACE * 2, sibling: _GRACE * 2},
            fail_versions={(key, "v0004")},
            page_size=3,
        )
        store.seed_versions(key, _data_history(store, key, 4))
        with caplog.at_level(
            logging.INFO,
            logger="kdive.reconciler.cleanup.uploads.upload_orphans",
        ):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                with pytest.raises(CategorizedError) as caught:
                    await run_repair(pool, _sweep(store))
        assert store.version_page_calls[:2] == [
            ("local/runs/", None, None, 1000),
            ("local/runs/", key, None, 1000),
        ]
        assert store.version_ids(key) == {"v0004"}
        assert store.deleted_versions == [
            (key, "v0001"),
            (key, "v0002"),
            (key, "v0003"),
            (sibling, "v1"),
        ]
        confirmed = [
            record.getMessage()
            for record in caplog.records
            if "leaked upload object" in record.getMessage()
        ]
        assert len(confirmed) == 1
        assert sibling in confirmed[0]
        assert key not in confirmed[0]
        assert "encountered 1 failed operation(s); 1 were confirmed reclaimed" in str(caught.value)

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
        assert (
            f"encountered {MAX_RECLAIMS_PER_ROOT} failed operation(s); 1 were confirmed reclaimed"
        ) in str(caught.value)

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


def test_each_candidate_gets_one_bounded_exact_version_capture(migrated_url: str) -> None:
    """Every unique key is captured exactly once and legacy HEAD is not used."""

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
        assert set(store.listed_prefixes) == set(UPLOAD_ORPHAN_ROOTS)
        assert sorted(store.capture_limits) == sorted((key, MAX_VERSIONS_PER_KEY) for key in keys)
        assert store.headed_keys == []

    asyncio.run(_run())


def test_an_exact_capture_failure_is_skipped_and_counted_like_every_per_key_fault(
    migrated_url: str,
) -> None:
    """A per-key inventory fault preserves that key and does not starve its sibling."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        denied, reachable = f"{prefix}vmcore", f"{prefix}kernel"
        store = _FailingCaptureStore(
            {denied: _GRACE * 2, reachable: _GRACE * 2}, fail_keys={denied}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as excinfo:
                await run_repair(pool, _sweep(store))
        assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert store.deleted == [reachable]
        assert store.present == {denied}

    asyncio.run(_run())


def test_the_reclaim_log_names_the_exact_version_identity_and_kind(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The reclaim record identifies the immutable data version that was deleted."""

    async def _run() -> None:
        run_id, prefix = await _seed_run_with_window(migrated_url, timedelta(seconds=-1))
        async with await connect(migrated_url) as seed:
            await upload_manifest.delete_manifest(seed, "runs", run_id)
        key = f"{prefix}vmcore"
        store = _FakeUploadStore({key: _GRACE * 2})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with caplog.at_level(
                logging.INFO,
                logger="kdive.reconciler.cleanup.uploads.upload_orphans",
            ):
                assert await run_repair(pool, _sweep(store)) == 1
        reclaims = [r.getMessage() for r in caplog.records if "deleted" in r.getMessage()]
        assert len(reclaims) == 1
        assert key in reclaims[0]
        assert "version v1 (data version)" in reclaims[0]

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
        assert "encountered 1 failed operation(s); 1 were confirmed reclaimed" in str(caught.value)

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
        assert "encountered 1 failed operation(s); 1 were confirmed reclaimed" in str(caught.value)

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
    migrated_url: str,
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
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == len(keys)
        # Store order is lexicographic by key, and that is exactly the delete sequence.
        assert store.deleted == sorted(keys)
        # And it really did span pages — otherwise this pins order within one listing, not across.
        assert store.events.count("page:3") > 1

    asyncio.run(_run())


def test_one_key_crossing_version_pages_is_captured_and_deleted_once(
    migrated_url: str,
) -> None:
    """Broad-page duplication does not repeat an exact capture or exact version deletion."""

    async def _run() -> None:
        _prefix, keys = await _seed_reaped_run_orphans(migrated_url, ["a-history", "b-next"])
        history_key, sibling = keys
        store = _FakeUploadStore(
            {history_key: _GRACE * 2, sibling: _GRACE * 2}, page_size=_PAGE_SIZE
        )
        store.seed_versions(history_key, _data_history(store, history_key, 7))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _sweep(store)) == 8
        assert store.capture_limits.count((history_key, MAX_VERSIONS_PER_KEY)) == 1
        assert [version_id for key, version_id in store.deleted_versions if key == history_key] == [
            f"v{number:04d}" for number in range(1, 8)
        ]
        assert store.deleted_versions[-1] == (sibling, "v1")
        assert any(len(page) == _PAGE_SIZE for page in store.pages_yielded)

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
    done work. It is safe for the reason a budget-truncated root is: every survivor stays in version
    inventory, so the next pass re-derives it from where this one stopped.
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
        assert "encountered 1 failed operation(s); 3 were confirmed reclaimed" in str(caught.value)
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
        with caplog.at_level(
            logging.ERROR,
            logger="kdive.reconciler.cleanup.uploads.upload_orphans",
        ):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                with pytest.raises(CategorizedError):
                    await run_repair(pool, _sweep(store))
        assert store.deleted == [f"{prefix}orphan"]  # the first root's delete really happened
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "confirmed 1 version target(s) reclaimed by completed batches" in errors[0]

    asyncio.run(_run())
