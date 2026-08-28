"""Tests for the reconciler upload reaper (ADR-0048 §6, ADR-0104 §7, ADR-0444, ADR-0453, #11).

The reaper commits the manifest-row delete of a past-deadline window under the owner's advisory
lock, then sweeps the window's uncommitted objects, re-deciding each key under that same lock
(ADR-0509) — row first, so a failing sweep can never restore a window over deleted bytes (ADR-0453,
issue #1552). Neither phase waits on the owner lock: an owner whose lock is held is deferred to the
next pass rather than blocking the ones behind it (ADR-0510, issue #1554). For ``runs`` it sweeps
whether the Run is pre-finalize (a true abandon) or
finalized with leftover chunks (ADR-0104 §7); for ``investigations`` it sweeps on the deadline
alone, in every investigation state (ADR-0444, superseding ADR-0441 §6's terminal-state gate —
``complete_rootfs_upload`` now rejects a past-deadline finalize, so the reap races nothing
legitimate). It exempts any object with a committed ``artifacts`` row, and the per-owner locked
re-read declines a manifest whose deadline was renewed since the candidate select.

The ``_repair_abandoned_uploads`` tests run the repair through a real non-autocommit
``AsyncConnectionPool`` via ``run_repair`` (mirroring ``test_loop.py``), so the
candidate-select transaction-nesting hazard is exercised; seeding and assertions use
separate autocommit ``connect`` connections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import ObjectVersion, VersionBatch, VersionPage
from kdive.artifacts.uploads import upload_manifest
from kdive.artifacts.uploads.upload_manifest import lock_scope_for as _lock_scope_for
from kdive.artifacts.uploads.uploads import ManifestEntry
from kdive.db.locks import advisory_xact_lock
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.uploads.uploads import ReapOutcome
from kdive.reconciler.cleanup.uploads.uploads import (
    reap_one_owner as _reap_one_owner,
)
from kdive.reconciler.cleanup.uploads.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system


class _FakeStore:
    def __init__(self, objects: dict[str, list[str]]) -> None:
        self._objects = objects  # prefix -> [keys]
        self.deleted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, []))

    def iter_prefix_version_pages(self, prefix: str) -> Iterator[VersionPage]:
        entries = tuple(self._version(key) for key in self._objects.get(prefix, []))
        yield VersionPage(entries, False, None, None)

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch:
        assert limit > 0
        return VersionBatch(key, (self._version(key),), True)

    def delete_batch(self, batch: VersionBatch) -> bool:
        self.deleted.append(batch.key)
        return batch.history_complete

    @staticmethod
    def _version(key: str) -> ObjectVersion:
        return ObjectVersion(
            key=key,
            version_id=f"version-of-{key}",
            last_modified=datetime(2026, 7, 1, tzinfo=UTC),
            etag=f"etag-of-{key}",
            is_latest=True,
            is_delete_marker=False,
        )


class _FailingStore(_FakeStore):
    """A store whose version-batch delete fails for named keys, as a real outage would.

    ``ObjectStore.delete_batch`` surfaces exact-delete faults as a
    ``CategorizedError``, so that is the exception a mid-sweep failure actually presents.
    """

    def __init__(
        self,
        objects: dict[str, list[str]],
        *,
        fail_keys: set[str] | None = None,
        fail_list_prefixes: set[str] | None = None,
    ) -> None:
        super().__init__(objects)
        self._fail_keys = fail_keys or set()
        self._fail_list_prefixes = fail_list_prefixes or set()
        self.attempted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        if prefix in self._fail_list_prefixes:
            raise CategorizedError(
                f"list_objects_v2 failed for {prefix}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return super().list_prefix(prefix)

    def iter_prefix_version_pages(self, prefix: str) -> Iterator[VersionPage]:
        if prefix in self._fail_list_prefixes:
            raise CategorizedError(
                f"list_object_versions failed for {prefix}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        yield from super().iter_prefix_version_pages(prefix)

    def delete_batch(self, batch: VersionBatch) -> bool:
        self.attempted.append(batch.key)
        if batch.key in self._fail_keys:
            raise CategorizedError(
                f"delete_object failed for {batch.key}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return super().delete_batch(batch)


class _HookedStore(_FakeStore):
    """A store that runs ``before_delete`` from inside its version-batch delete.

    The hook lands in the gap phase 2 leaves open: after the manifest-row delete has committed and
    the owner lock has been released, before the object is actually gone. It runs on the
    ``asyncio.to_thread`` worker, so a sync ``psycopg`` connection opened there observes only
    committed state — which is what lets these tests assert *ordering* rather than end state.

    ``once`` fires it before the first delete only; otherwise before every one.
    """

    def __init__(
        self,
        objects: dict[str, list[str]],
        *,
        before_delete: Callable[[], None],
        once: bool = False,
    ) -> None:
        super().__init__(objects)
        self._before_delete = before_delete
        self._once = once
        self._fired = False

    def delete_batch(self, batch: VersionBatch) -> bool:
        if not (self._once and self._fired):
            self._fired = True
            self._before_delete()
        return super().delete_batch(batch)


class _SecondPageForbiddenStore(_FakeStore):
    """Expose one useful page and fail if the reaper tries to drain the whole prefix."""

    def __init__(self, objects: dict[str, list[str]]) -> None:
        super().__init__(objects)
        self.pages_requested: dict[str, int] = {}

    def iter_prefix_version_pages(self, prefix: str) -> Iterator[VersionPage]:
        self.pages_requested[prefix] = self.pages_requested.get(prefix, 0) + 1
        entries = tuple(self._version(key) for key in self._objects.get(prefix, []))
        yield VersionPage(entries, True, "next-key", "next-version")
        raise AssertionError("expired-upload reaping must leave later pages to orphan repair")


def _advisory_locks_held_by(url: str, backend_pid: int) -> int:
    """Count granted advisory locks held by a backend from a second connection."""
    with psycopg.connect(url, autocommit=True) as observer:
        row = observer.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s AND granted",
            (backend_pid,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _manifest_is_gone(
    url: str, owner_kind: upload_manifest.UploadOwnerKind, owner_id: UUID
) -> Callable[[], bool]:
    """Return a probe reporting whether the owner's manifest row has *committed* away yet."""

    def _probe() -> bool:
        with psycopg.connect(url, autocommit=True) as conn:
            return upload_manifest.get_manifest_sync(conn, owner_kind, owner_id) is None

    return _probe


async def _insert_artifact_row(
    conn: psycopg.AsyncConnection, *, owner_kind: str, owner_id: UUID, object_key: str
) -> None:
    """Insert a minimal committed ``artifacts`` row (id/timestamps defaulted)."""
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "    retention_class) VALUES (%s, %s, %s, %s, %s, %s)",
        (owner_kind, owner_id, object_key, "etag-1", "sensitive", "default"),
    )


async def _seed_investigation(conn: psycopg.AsyncConnection, *, state: str) -> UUID:
    inv_id = uuid4()
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'user-1', 'proj', 't', %s)",
        (inv_id, state),
    )
    return inv_id


def _reap(store: _FakeStore):
    return lambda conn: _repair_abandoned_uploads(conn, store)


def _run_manifest(
    run_id: UUID, ttl: timedelta
) -> tuple[str, upload_manifest.UploadManifestReplaceRequest]:
    prefix = f"local/runs/{run_id}/"
    request = upload_manifest.UploadManifestReplaceRequest(
        owner_kind="runs",
        owner_id=run_id,
        prefix=prefix,
        entries=[ManifestEntry("kernel", "a", 1)],
        ttl=ttl,
    )
    return prefix, request


def _investigation_manifest(
    inv_id: UUID, ttl: timedelta
) -> tuple[str, upload_manifest.UploadManifestReplaceRequest]:
    prefix = f"local/investigations/{inv_id}/"
    request = upload_manifest.UploadManifestReplaceRequest(
        owner_kind="investigations",
        owner_id=inv_id,
        prefix=prefix,
        entries=[ManifestEntry("rootfs", "a", 1)],
        ttl=ttl,
    )
    return prefix, request


async def _seed_expired_run(url: str) -> tuple[UUID, str]:
    """Seed a CREATED Run with a past-deadline upload manifest; return its id and window prefix."""
    async with await connect(url) as seed:
        system_id = await seed_system(seed)
        run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
        prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
        await upload_manifest.replace_manifest(seed, request)
    return run_id, prefix


def test_reaps_uncommitted_objects_past_deadline_for_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}kernel", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_multiple_abandoned_owners_counted(migrated_url: str) -> None:
    # Two independent past-deadline Run manifests must each increment the reaped tally: the
    # return is the number of owners reaped, not a fixed 1.
    async def _run() -> None:
        prefixes: list[str] = []
        run_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(2):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
                run_ids.append(run_id)
        store = _FakeStore({p: [f"{p}kernel"] for p in prefixes})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 2  # both owners reaped, not a fixed 1
        assert sorted(store.deleted) == sorted(f"{p}kernel" for p in prefixes)
        async with await connect(migrated_url) as check:
            for run_id in run_ids:
                assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_uncommitted_objects_past_deadline_for_closed_investigation(
    migrated_url: str,
) -> None:
    # AC-10: a stale investigation upload window on a CLOSED investigation past deadline has its
    # uncommitted object + manifest reaped (the finalize will never land).
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="closed")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_exempts_committed_object(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="closed")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []  # committed object exempt
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_skips_owner_not_past_deadline(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(hours=1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is not None

    asyncio.run(_run())


def test_succeeded_run_with_lingering_manifest_reaps_chunks_not_final(migrated_url: str) -> None:
    """A SUCCEEDED Run whose post-commit chunk cleanup failed: its leftover chunks (no row) are
    reaped but the reassembled final object (committed row) is exempt, then the manifest goes
    (ADR-0104 §7, runs-branch generalization)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.SUCCEEDED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="runs", owner_id=run_id, object_key=f"{prefix}kernel"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}kernel", f"{prefix}kernel.part0001"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == [f"{prefix}kernel.part0001"]  # final object exempt (has a row)
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaps_past_deadline_uncommitted_manifest_on_open_investigation(migrated_url: str) -> None:
    """AC-3 (ADR-0444 decision 2, superseding ADR-0441 §6): an OPEN investigation is no longer
    excluded. Its past-deadline uncommitted object + manifest are reaped on the deadline alone —
    a finalize arriving this late is rejected by `complete_rootfs_upload`, so nothing legitimate
    is raced."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="open")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_open_investigation_committed_object_is_exempt(migrated_url: str) -> None:
    """AC-4: with the state gate gone, the per-key committed-object skip carries the whole safety
    burden for an OPEN investigation — a finalized rootfs (with an `artifacts` row) is never
    deleted, though its spent manifest still goes."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="open")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == [f"{prefix}stray"]  # committed object exempt, stray bytes go
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_reaps_uncommitted_objects_past_deadline_for_abandoned_investigation(
    migrated_url: str,
) -> None:
    """An ``abandoned`` investigation past its manifest deadline has its uncommitted staged object +
    manifest reaped (ADR-0441 §6) — the terminal state means finalize will never land."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="abandoned")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs", f"{prefix}stray"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert sorted(store.deleted) == [f"{prefix}rootfs", f"{prefix}stray"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_closed_investigation_committed_object_is_exempt(migrated_url: str) -> None:
    """Even for a terminal investigation, a committed object (with an ``artifacts`` row) is never
    deleted — the per-key skip protects it, so only the truly uncommitted staged bytes go."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="closed")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await _insert_artifact_row(
                seed, owner_kind="investigations", owner_id=inv_id, object_key=f"{prefix}rootfs"
            )
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []  # committed object exempt even for a closed investigation
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_reaps_past_deadline_uncommitted_manifest_on_active_investigation(
    migrated_url: str,
) -> None:
    """AC-3, the ``active`` half: the reap keys on the deadline, not on investigation state, so an
    in-flight investigation's lapsed upload window is collected too (ADR-0444 decision 2)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="active")
            prefix, request = _investigation_manifest(inv_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == [f"{prefix}rootfs"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is None

    asyncio.run(_run())


def test_open_investigation_within_deadline_is_not_reaped(migrated_url: str) -> None:
    """AC-5, the investigations half: the deadline is the only gate, so a live window on an OPEN
    investigation is still untouched — the reap did not become unconditional."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            inv_id = await _seed_investigation(seed, state="open")
            prefix, request = _investigation_manifest(inv_id, timedelta(hours=1))
            await upload_manifest.replace_manifest(seed, request)
        store = _FakeStore({prefix: [f"{prefix}rootfs"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "investigations", inv_id) is not None

    asyncio.run(_run())


def test_reap_one_owner_declines_renewed_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as conn:
            system_id = await seed_system(conn)
            run_id = await seed_run(conn, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(hours=1))
            await upload_manifest.replace_manifest(conn, request)
            store = _FakeStore({prefix: [f"{prefix}kernel"]})
            outcome = await _reap_one_owner(conn, store, "runs", run_id)
            # ``deferred`` false: a renewed manifest is a *decline*, and the two must not be
            # conflated — a decline is final for this owner, a deferral is retried (ADR-0510).
            assert outcome == ReapOutcome(
                reaped=False, deferred=False, attempted=0, declined=0, undeleted=0
            )
            assert store.deleted == []

    asyncio.run(_run())


def test_mid_sweep_delete_failure_still_removes_the_manifest_row(migrated_url: str) -> None:
    """#1552 / ADR-0453 §1: the row delete commits *before* the objects are swept, so a delete
    that fails partway through can no longer restore a window over deleted bytes.

    Under the old object-first order this exact scenario rolled the transaction back, leaving an
    ``upload_manifests`` row byte-identical to the one finalize validated — same prefix, same
    deadline — over an object that was already gone. ``_require_unreaped_window`` compares deadline
    *identity* and a rollback re-stamps nothing, so the runs single-PUT finalize would sail past it
    and register ``artifacts`` rows against deleted keys.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        keys = [f"{prefix}a", f"{prefix}b", f"{prefix}c"]
        store = _FailingStore({prefix: keys}, fail_keys={f"{prefix}b"})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):  # reported once the pass is complete
                await run_repair(pool, _reap(store))
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None
        # AC-4: one bad key strands neither the keys before it nor the keys after it.
        assert store.attempted == keys
        assert store.deleted == [f"{prefix}a", f"{prefix}c"]

    asyncio.run(_run())


def test_a_totally_failed_sweep_is_reported_as_a_failed_pass(migrated_url: str) -> None:
    """ADR-0453 §3: a store rejecting every delete must not read as healthy repair work.

    ``_run_repair_plan`` increments the ADR-0190 group-E error counter only for a repair that
    *raises*; a repair that returns normally has its count added to `kdive.reconciler.repairs` and
    emits no error. Swallowing the sweep failures outright would therefore make the one condition
    that leaks bytes permanently the best-looking pass on the dashboard. The pass forfeits its
    reaped count to say so — the count is a gauge, the error is the alert, and the rows are
    durably deleted either way.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FailingStore({prefix: [f"{prefix}a"]}, fail_keys={f"{prefix}a"})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _reap(store))
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert "1 key batch(es) across 1 reaped owner(s)" in str(caught.value)

    asyncio.run(_run())


def test_a_clean_sweep_returns_the_reaped_count_and_does_not_raise(migrated_url: str) -> None:
    """The counterweight: the end-of-pass raise is conditional on a real failure.

    Without this, the previous test would be satisfied by a reaper that raised unconditionally.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FailingStore({prefix: [f"{prefix}a"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [f"{prefix}a"]

    asyncio.run(_run())


def test_undeleted_objects_are_reported_at_error(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0453 §3 and §Consequences: the leaked bytes must be *reported*, not just tolerated.

    Nothing in this tree sweeps the upload prefix — `gc_expired_build_artifacts` is row-driven
    over `artifacts` and the `images/` orphan scan is a different prefix — so once the manifest
    row is gone this log is the only trace the objects were ever there.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FailingStore(
            {prefix: [f"{prefix}a", f"{prefix}b"]}, fail_keys={f"{prefix}a", f"{prefix}b"}
        )
        with caplog.at_level(
            logging.WARNING,
            logger="kdive.reconciler.cleanup.uploads.uploads",
        ):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                with pytest.raises(CategorizedError):
                    await run_repair(pool, _reap(store))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(warnings) == 2  # one per failed key, naming the key
        logged = [r.getMessage() for r in warnings]
        assert [k for k in (f"{prefix}a", f"{prefix}b") if any(k in m for m in logged)] == [
            f"{prefix}a",
            f"{prefix}b",
        ]
        assert len(errors) == 1  # one summary, naming the counts and the owner
        assert f"left 2 of 2 key batch(es) for owner runs/{run_id}" in errors[0].getMessage()

    asyncio.run(_run())


def test_the_prefix_is_logged_before_the_sweep_starts(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0453 §3: the prefix and the key count must be on the record *before* the first delete.

    An abort that never reaches the sweep's own reporting — cancellation at shutdown, a process
    kill — leaves this as the only record of when the claim happened and how much it doomed.
    Asserted by snapshotting the log from inside the first delete, so moving the call after the
    sweep fails this test rather than passing it.
    """
    seen_at_first_delete: list[str] = []

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        keys = [f"{prefix}a", f"{prefix}b"]

        def _snapshot_the_log() -> None:
            seen_at_first_delete.extend(r.getMessage() for r in caplog.records)

        store = _HookedStore({prefix: keys}, before_delete=_snapshot_the_log, once=True)
        with caplog.at_level(logging.INFO, logger="kdive.reconciler.cleanup.uploads.uploads"):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == keys
        claimed = [m for m in seen_at_first_delete if "claimed; sweeping" in m]
        assert len(claimed) == 1  # already logged when the first delete ran
        assert prefix in claimed[0]
        assert "2 key(s)" in claimed[0]

    asyncio.run(_run())


def test_a_failing_version_list_leaves_the_claimed_row_gone_for_orphan_repair(
    migrated_url: str,
) -> None:
    """Inventory happens after the DB-only claim; a fault leaves an orphan-discoverable prefix."""

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FailingStore({prefix: [f"{prefix}kernel"]}, fail_list_prefixes={prefix})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError):  # not silently absorbed
                await run_repair(pool, _reap(store))
        assert store.attempted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_successful_sweep_logs_no_failure_summary(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The ERROR summary is conditional on a failure, not emitted on every reap."""

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({prefix: [f"{prefix}a"]})
        with caplog.at_level(
            logging.WARNING,
            logger="kdive.reconciler.cleanup.uploads.uploads",
        ):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _reap(store)) == 1
        assert caplog.records == []

    asyncio.run(_run())


def test_mid_sweep_delete_failure_does_not_abandon_later_owners(migrated_url: str) -> None:
    """AC-4, the cross-owner half: one bad key must not cost a later owner its reap.

    ``repair_abandoned_uploads`` has no per-candidate ``try``, so a raise out of the object sweep
    would drop every later candidate on the floor for one unrelated object-store hiccup. Each
    owner here loses one key of two, which is a bad key rather than a refusing store — so the pass
    walks every candidate and only then reports.
    """

    async def _run() -> None:
        prefixes: list[str] = []
        run_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(2):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
                run_ids.append(run_id)
        objects = {p: [f"{p}kernel", f"{p}stray"] for p in prefixes}
        store = _FailingStore(objects, fail_keys={f"{p}kernel" for p in prefixes})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _reap(store))
        # Both owners reaped despite a failed key each; the report names both, none left unclaimed.
        assert "2 key batch(es) across 2 reaped owner(s)" in str(caught.value)
        assert "Left 0 candidate(s) unclaimed" in str(caught.value)
        assert sorted(store.deleted) == sorted(f"{p}stray" for p in prefixes)
        async with await connect(migrated_url) as check:
            for run_id in run_ids:
                assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reaper_reads_one_version_page_per_owner_and_progresses_later_owners(
    migrated_url: str,
) -> None:
    """The recurring orphan sweep, not the serial reaper, owns later prefix pages."""

    async def _run() -> None:
        prefixes: list[str] = []
        async with await connect(migrated_url) as seed:
            for _ in range(2):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
        store = _SecondPageForbiddenStore({p: [f"{p}first-page"] for p in prefixes})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            assert await run_repair(pool, _reap(store)) == 2

        assert store.pages_requested == {prefix: 1 for prefix in prefixes}
        assert sorted(store.deleted) == sorted(f"{prefix}first-page" for prefix in prefixes)

    asyncio.run(_run())


def test_a_wholly_refused_sweep_stops_the_pass_claiming_more_owners(migrated_url: str) -> None:
    """ADR-0453 §3, the brake: a store that lists but refuses every delete must not orphan the
    whole backlog in one pass.

    One bad key is a bad key; a whole owner's sweep failing is the signature of a condition that
    will fail the next owner too — a bucket policy without ``s3:DeleteObject``, an endpoint
    rejecting DELETE. The candidate select is unbounded, and every candidate claimed under that
    fault costs an irreversible row delete over bytes nothing reclaims (#1556), so the pass stops.
    A failing ``list_prefix`` already ended the pass for the same reason; this makes the
    delete-side treatment consistent rather than the opposite.

    Order-independent: every owner's delete fails, so whichever the scan yields first is the one
    that gets reaped, and exactly one must survive.
    """

    async def _run() -> None:
        prefixes: list[str] = []
        run_ids: list[UUID] = []
        async with await connect(migrated_url) as seed:
            for _ in range(3):
                system_id = await seed_system(seed)
                run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
                prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
                await upload_manifest.replace_manifest(seed, request)
                prefixes.append(prefix)
                run_ids.append(run_id)
        objects = {p: [f"{p}kernel"] for p in prefixes}
        store = _FailingStore(objects, fail_keys={f"{p}kernel" for p in prefixes})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(CategorizedError) as caught:
                await run_repair(pool, _reap(store))
        assert "1 key batch(es) across 1 reaped owner(s)" in str(caught.value)
        assert "Left 2 candidate(s) unclaimed" in str(caught.value)
        assert len(store.attempted) == 1  # the brake tripped after the first owner
        async with await connect(migrated_url) as check:
            surviving = [
                r
                for r in run_ids
                if await upload_manifest.get_manifest(check, "runs", r) is not None
            ]
        assert len(surviving) == 2  # two windows kept for the next pass, bytes and rows intact

    asyncio.run(_run())


def test_manifest_row_is_already_committed_gone_when_the_first_object_is_deleted(
    migrated_url: str,
) -> None:
    """AC-1: the ordering itself, observed rather than inferred from the end state.

    Both orders leave no row once the reap returns, so the end state cannot tell them apart. This
    probes from inside the delete callback on a separate connection, which sees only committed
    state — under the old order it saw the row still present.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        keys = [f"{prefix}a", f"{prefix}b"]
        probe = _manifest_is_gone(migrated_url, "runs", run_id)
        gone_at_each_delete: list[bool] = []
        store = _HookedStore(
            {prefix: keys}, before_delete=lambda: gone_at_each_delete.append(probe())
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == keys
        assert gone_at_each_delete == [True, True]

    asyncio.run(_run())


def test_reap_exact_delete_runs_after_owner_unlock(migrated_url: str) -> None:
    """Every exact-version deletion callback observes the manifest commit and no owner lock."""

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        observations: list[tuple[int, bool]] = []
        sweeper = await psycopg.AsyncConnection.connect(migrated_url)

        def _observe_delete() -> None:
            probe = _manifest_is_gone(migrated_url, "runs", run_id)
            observations.append(
                (
                    _advisory_locks_held_by(migrated_url, sweeper.info.backend_pid),
                    probe(),
                )
            )

        store = _HookedStore({prefix: [f"{prefix}a", f"{prefix}b"]}, before_delete=_observe_delete)
        try:
            outcome = await _reap_one_owner(sweeper, store, "runs", run_id)
        finally:
            await sweeper.close()

        assert outcome.reaped is True
        assert observations == [(0, True), (0, True)]

    asyncio.run(_run())


def test_all_committed_objects_reaps_row_without_any_delete(migrated_url: str) -> None:
    """The empty doomed set: every listed key holds an ``artifacts`` row, so the set-valued
    exemption query leaves nothing to sweep and the sweep phase issues no delete at all."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.SUCCEEDED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            for name in ("kernel", "initrd"):
                await _insert_artifact_row(
                    seed, owner_kind="runs", owner_id=run_id, object_key=f"{prefix}{name}"
                )
            await upload_manifest.replace_manifest(seed, request)
        store = _FailingStore(
            {prefix: [f"{prefix}kernel", f"{prefix}initrd"]},
            fail_keys={f"{prefix}kernel", f"{prefix}initrd"},
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.attempted == []  # exempt keys are never even attempted
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reap_with_no_objects_under_the_prefix_still_removes_the_row(migrated_url: str) -> None:
    """An empty prefix listing: nothing to sweep, but the abandoned window is still collected."""

    async def _run() -> None:
        run_id, _prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(store))
        assert count == 1
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_lock_scope_rejects_unknown_owner_kind() -> None:
    # AC-6: the state gate is gone, but its fail-loud arm survives on the lock-scope lookup — an
    # unrecognized owner kind must never be locked under a guessed scope (ADR-0444 decision 2).
    try:
        _lock_scope_for(cast(upload_manifest.UploadOwnerKind, "allocations"))
    except ValueError as exc:
        assert str(exc) == "unsupported upload owner kind: allocations"
    else:
        raise AssertionError("unknown owner kind should fail loud, not resolve to a scope")


# --- #1554: a held owner lock defers that owner, not the pass (ADR-0510) -------------------------


@asynccontextmanager
async def _owner_lock_held(url: str, owner_id: UUID) -> AsyncIterator[None]:
    """Hold the ``runs`` advisory lock for ``owner_id`` on a separate connection over the block.

    Models the holder that motivates #1554: chunked ``complete_build`` takes ``LockScope.RUN``
    before its reassembly and holds it to request end (ADR-0244), so the lock can span a whole
    multi-GiB write — and therefore a whole reconciler pass.
    """
    holder = await psycopg.AsyncConnection.connect(url)
    try:
        async with (
            holder.transaction(),
            advisory_xact_lock(holder, _lock_scope_for("runs"), owner_id),
        ):
            yield
    finally:
        await holder.close()


def test_a_locked_owner_does_not_stall_an_unrelated_owner_in_the_same_pass(
    migrated_url: str,
) -> None:
    """#1554, the head-of-line stall: a held lock must cost that owner a pass, not the pass.

    Phase 1 used to *block* on the owner lock, so a candidate whose lock a slow finalize held parked
    ``repair_abandoned_uploads`` mid-loop — every remaining candidate waited behind it, and so did
    every repair after it, because ``_run_repair_plan`` keeps one pooled connection checked out for
    the whole call and runs its repairs serially.

    The timeout is the assertion. Against the blocking acquisition this pass never returns at all:
    the holder is released only *after* the pass is awaited, so the wait is unbounded rather than
    slow. Candidate order is deliberately not pinned — the candidate select has no ``ORDER BY`` —
    and does not need to be, because blocking hangs the pass whether the locked owner is reached
    first or second, while deferring reaps the free owner either way.

    The mutation control for ``count == 1`` is ``test_reaps_multiple_abandoned_owners_counted``:
    with no holder, the same two-owner shape returns 2. Without it, a phase 1 that deferred
    unconditionally would satisfy this test while reaping nothing.
    """

    async def _run() -> None:
        locked_id, locked_prefix = await _seed_expired_run(migrated_url)
        free_id, free_prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore(
            {locked_prefix: [f"{locked_prefix}kernel"], free_prefix: [f"{free_prefix}kernel"]}
        )
        async with (
            _owner_lock_held(migrated_url, locked_id),
            AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
        ):
            count = await asyncio.wait_for(run_repair(pool, _reap(store)), timeout=20)
        assert count == 1  # the free owner, reaped without waiting for the locked one
        assert store.deleted == [f"{free_prefix}kernel"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", free_id) is None
            # The locked owner's row is untouched — nothing was claimed, so nothing was lost.
            assert await upload_manifest.get_manifest(check, "runs", locked_id) is not None

    asyncio.run(_run())


def test_a_deferred_owner_is_reaped_by_the_next_pass_once_its_lock_is_free(
    migrated_url: str,
) -> None:
    """A deferral costs a pass, not the reap — the claim ADR-0510 makes against ADR-0509.

    ADR-0509 §Consequences kept phase 1 blocking on the ground that "a reap that gave up on a
    contended owner would never claim it — the manifest row is the pass's only record that the
    window is past its deadline". The row is not consumed by a deferral: it is left exactly as
    found, still past its deadline, which is the predicate the candidate select uses. So the next
    pass re-derives the owner. This test drives both passes.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            async with _owner_lock_held(migrated_url, run_id):
                deferred_pass = await asyncio.wait_for(run_repair(pool, _reap(store)), timeout=20)
            assert deferred_pass == 0
            assert store.deleted == []
            async with await connect(migrated_url) as check:
                assert await upload_manifest.get_manifest(check, "runs", run_id) is not None
            assert await run_repair(pool, _reap(store)) == 1
        assert store.deleted == [f"{prefix}kernel"]
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())


def test_reap_one_owner_reports_a_held_lock_as_deferred_not_declined(migrated_url: str) -> None:
    """The two no-claim outcomes are distinct, and only one of them is retried.

    A decline (``test_reap_one_owner_declines_renewed_manifest``) is final for this owner: the
    window it would have reaped no longer exists. A deferral is not — the window is still there and
    still past its deadline. Collapsing them into a bare ``reaped=False`` would make the pass unable
    to report the one that can starve.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        async with (
            _owner_lock_held(migrated_url, run_id),
            await connect(migrated_url) as conn,
        ):
            outcome = await _reap_one_owner(conn, store, "runs", run_id)
        assert outcome == ReapOutcome(
            reaped=False, deferred=True, attempted=0, declined=0, undeleted=0
        )
        assert store.deleted == []

    asyncio.run(_run())


def test_a_deferred_owner_is_reported_with_its_count_and_age(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Deferring silently would make perpetual starvation invisible, so the pass reports it.

    Each pass looks locally fine when an owner is deferred — nothing failed, nothing was lost — so
    an owner whose lock is *never* free is never reaped and never complained about. The summary
    carries the count and the oldest candidate's age past its deadline, computed by Postgres at the
    candidate select (never from a Python clock, which would not share the database's session
    timezone), so a starved owner shows as an age that grows pass over pass.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        with caplog.at_level(
            logging.WARNING,
            logger="kdive.reconciler.cleanup.uploads.uploads",
        ):
            async with (
                _owner_lock_held(migrated_url, run_id),
                AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool,
            ):
                assert await asyncio.wait_for(run_repair(pool, _reap(store)), timeout=20) == 0
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "deferred 1 of 1 past-deadline owner(s)" in message
        assert "past its deadline" in message

    asyncio.run(_run())


def test_a_clean_pass_reports_no_deferral(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The mutation control for the summary above: an uncontended pass must stay quiet.

    A summary emitted unconditionally would warn on every pass of a healthy deployment and train
    operators to ignore the one line that says an owner is starving.
    """

    async def _run() -> None:
        run_id, prefix = await _seed_expired_run(migrated_url)
        store = _FakeStore({prefix: [f"{prefix}kernel"]})
        with caplog.at_level(
            logging.WARNING,
            logger="kdive.reconciler.cleanup.uploads.uploads",
        ):
            async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
                assert await run_repair(pool, _reap(store)) == 1
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
        async with await connect(migrated_url) as check:
            assert await upload_manifest.get_manifest(check, "runs", run_id) is None

    asyncio.run(_run())
