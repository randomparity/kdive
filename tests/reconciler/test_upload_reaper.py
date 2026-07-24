"""Tests for the reconciler upload reaper (ADR-0048 §6, ADR-0104 §7, ADR-0444, issue #11).

The reaper prefix-reaps uncommitted objects of a past-deadline manifest, then deletes the
manifest row. For ``runs`` it sweeps whether the Run is pre-finalize (a true abandon) or
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
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.uploads import ManifestEntry
from kdive.domain.capacity.state import RunState
from kdive.reconciler.cleanup.uploads import (
    lock_scope_for as _lock_scope_for,
)
from kdive.reconciler.cleanup.uploads import (
    reap_one_owner as _reap_one_owner,
)
from kdive.reconciler.cleanup.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system


class _FakeStore:
    def __init__(self, objects: dict[str, list[str]]) -> None:
        self._objects = objects  # prefix -> [keys]
        self.deleted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, []))

    def delete(self, key: str) -> None:
        self.deleted.append(key)


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


def test_reaps_uncommitted_objects_past_deadline_for_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
            prefix, request = _run_manifest(run_id, timedelta(seconds=-1))
            await upload_manifest.replace_manifest(seed, request)
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
            assert await _reap_one_owner(conn, store, "runs", run_id) is False
            assert store.deleted == []

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
