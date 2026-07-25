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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import ObjectListing
from kdive.artifacts.uploads import ManifestEntry
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.upload_orphans import (
    DEFAULT_UPLOAD_ORPHAN_GRACE,
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


class _FakeUploadStore:
    """A store stand-in over ``key -> age``; the absolute mtime is ``now - age``.

    Satisfies both the reaper's port (``list_prefix``/``delete``) and the sweep's
    (``list_prefix_with_mtime``/``delete``), so one instance can carry a failed reap's aftermath
    straight into the sweep.
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
        return sorted(key for key in self._objects if key.startswith(prefix))

    def list_prefix_with_mtime(self, prefix: str) -> list[ObjectListing]:
        now = datetime.now(UTC)
        return [
            ObjectListing(key=key, last_modified=now - age)
            for key, age in sorted(self._objects.items())
            if key.startswith(prefix)
        ]

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self.deleted.append(key)


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


def _sweep(store: _FakeUploadStore, grace: timedelta = _GRACE):
    return lambda conn: _repair_leaked_upload_objects(conn, store, grace)


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
    """AC-6, and the deliberate asymmetry with the reaper (ADR-0455 §4).

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
    assert DEFAULT_UPLOAD_ORPHAN_GRACE.total_seconds() == 24 * 60 * 60
