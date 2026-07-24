"""The two investigation-rootfs reclaim sweeps (ADR-0441 §6, Task 4.1d).

``gc_investigation_uploaded_rootfs`` (close-driven, keyed on ``rootfs_cleanup_pending_at``) and
``gc_expired_investigation_rootfs`` (TTL backstop) reclaim a committed rootfs object + staged base +
row on the overlay-absence liveness gate, fail closed per pass, and are independent of the build
sweep's marker.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg

from kdive.artifacts.content_address import rootfs_object_token
from kdive.providers.local_libvirt.lifecycle.storage import overlay_name
from kdive.reconciler.cleanup.gc import (
    gc_expired_investigation_rootfs,
    gc_investigation_uploaded_rootfs,
)
from tests.reconciler.conftest import connect

_CHECKSUM = base64.b64encode(hashlib.sha256(b"rootfs").digest()).decode("ascii")
_TOKEN = rootfs_object_token(_CHECKSUM)


class _RecordingStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


def _object_key(inv: UUID) -> str:
    return f"local/investigations/{inv}/rootfs-{_TOKEN}"


def _upload_profile() -> str:
    return json.dumps(
        {
            "provider": {
                "local-libvirt": {"rootfs": {"kind": "upload", "checksum_sha256": _CHECKSUM}}
            }
        }
    )


async def _seed_investigation(
    conn: psycopg.AsyncConnection, *, state: str, rootfs_marker_age: timedelta | None
) -> UUID:
    inv_id = uuid4()
    if rootfs_marker_age is None:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'p', 'proj', 't', %s)",
            (inv_id, state),
        )
    else:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state, "
            "rootfs_cleanup_pending_at) VALUES (%s, 'p', 'proj', 't', %s, now() - %s)",
            (inv_id, state, rootfs_marker_age),
        )
    return inv_id


async def _seed_allocation(conn: psycopg.AsyncConnection) -> UUID:
    resource_id, alloc_id = uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (alloc_id, resource_id),
    )
    return alloc_id


async def _seed_system(conn: psycopg.AsyncConnection, inv: UUID, state: str) -> UUID:
    alloc = await _seed_allocation(conn)
    system_id = uuid4()
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, investigation_id, state, provisioning_profile, "
        "principal, project) VALUES (%s, %s, %s, %s, %s::jsonb, 'p', 'proj')",
        (system_id, alloc, inv, state, _upload_profile()),
    )
    return system_id


async def _seed_rootfs_object(
    conn: psycopg.AsyncConnection, inv: UUID, *, created_age: timedelta | None = None
) -> UUID:
    artifact_id = uuid4()
    if created_age is None:
        await conn.execute(
            "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES (%s, 'investigations', %s, %s, 'e', 'redacted', 'rootfs')",
            (artifact_id, inv, _object_key(inv)),
        )
    else:
        await conn.execute(
            "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class, created_at) VALUES (%s, 'investigations', %s, %s, 'e', 'redacted', "
            "'rootfs', now() - %s)",
            (artifact_id, inv, _object_key(inv), created_age),
        )
    return artifact_id


def _stage(uploads_dir: Path, inv: UUID) -> Path:
    staged = uploads_dir / str(inv) / f"{_TOKEN}.qcow2"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"base")
    return staged


async def _row_exists(conn: psycopg.AsyncConnection, artifact_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    return await cur.fetchone() is not None


async def _marker(conn: psycopg.AsyncConnection, inv: UUID) -> object:
    cur = await conn.execute(
        "SELECT rootfs_cleanup_pending_at FROM investigations WHERE id = %s", (inv,)
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


def test_close_driven_reclaims_object_row_and_file_past_grace(
    migrated_url: str, tmp_path: Path
) -> None:
    # AC-7 / AC-4d reclaim branch: a finalize-committed-then-closed investigation past grace, whose
    # bound System is torn_down (overlay gone), reclaims with no orphan.
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            inv2 = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            artifact_id = await _seed_rootfs_object(seed, inv2)
            await _seed_system(seed, inv2, "torn_down")
            nonlocal inv
            inv = inv2
        finally:
            await seed.close()
        rootfs_dir = tmp_path / "rootfs"
        rootfs_dir.mkdir()
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        staged = _stage(uploads, inv)
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_investigation_uploaded_rootfs(
                conn,
                store,
                timedelta(days=1),
                rootfs_dir=str(rootfs_dir),
                uploads_dir=str(uploads),
            )
        finally:
            await conn.close()
        assert deleted == 1
        assert store.deleted == [_object_key(inv)]
        assert not staged.exists()
        assert not (uploads / str(inv)).exists()  # empty staging dir removed
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, artifact_id)
            assert await _marker(check, inv) is None  # own marker cleared after drain
        finally:
            await check.close()

    asyncio.run(_run())


def test_deferred_while_overlay_present_then_drains(migrated_url: str, tmp_path: Path) -> None:
    # AC-8: a LIVE bound System with an actual overlay file on disk pins the base — the sweep must
    # NOT reclaim (a naive "delete if past grace" regresses here). Drains once the overlay is gone.
    inv = uuid4()
    sys_id = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            nonlocal inv, sys_id
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            await _seed_rootfs_object(seed, inv)
            sys_id = await _seed_system(seed, inv, "ready")
        finally:
            await seed.close()
        rootfs_dir = tmp_path / "rootfs"
        rootfs_dir.mkdir()
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        _stage(uploads, inv)
        overlay = rootfs_dir / overlay_name(str(sys_id))
        overlay.write_bytes(b"overlay")  # LIVE backing file
        store = _RecordingStore()

        async def _sweep() -> int:
            conn = await connect(migrated_url)
            try:
                return await gc_investigation_uploaded_rootfs(
                    conn,
                    store,
                    timedelta(days=1),
                    rootfs_dir=str(rootfs_dir),
                    uploads_dir=str(uploads),
                )
            finally:
                await conn.close()

        assert await _sweep() == 0  # overlay present -> deferred
        assert store.deleted == []
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is not None  # marker retained
        finally:
            await check.close()

        overlay.unlink()  # teardown reclaimed the overlay
        assert await _sweep() == 1  # now drains

    asyncio.run(_run())


def test_ttl_backstop_reclaims_never_closed(migrated_url: str, tmp_path: Path) -> None:
    # AC-8b: a never-closed (open) investigation's committed object past retention is reclaimed by
    # the TTL sweep on the same gate.
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            nonlocal inv
            inv = await _seed_investigation(seed, state="open", rootfs_marker_age=None)
            await _seed_rootfs_object(seed, inv, created_age=timedelta(days=40))
        finally:
            await seed.close()
        rootfs_dir = tmp_path / "rootfs"
        rootfs_dir.mkdir()
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        _stage(uploads, inv)
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_expired_investigation_rootfs(
                conn,
                store,
                timedelta(days=30),
                rootfs_dir=str(rootfs_dir),
                uploads_dir=str(uploads),
            )
        finally:
            await conn.close()
        assert deleted == 1
        assert store.deleted == [_object_key(inv)]

    asyncio.run(_run())


def test_marker_independence_from_build_sweep(migrated_url: str, tmp_path: Path) -> None:
    # AC-8d: cleanup_pending_at NULL (build sweep drained it) but rootfs_cleanup_pending_at set —
    # the rootfs sweep still fires, keyed on its OWN marker.
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            nonlocal inv
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            # cleanup_pending_at stays NULL (never set / already cleared by the build sweep).
            await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        rootfs_dir = tmp_path / "rootfs"
        rootfs_dir.mkdir()
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        _stage(uploads, inv)
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            deleted = await gc_investigation_uploaded_rootfs(
                conn,
                store,
                timedelta(days=1),
                rootfs_dir=str(rootfs_dir),
                uploads_dir=str(uploads),
            )
        finally:
            await conn.close()
        assert deleted == 1

    asyncio.run(_run())


def test_fail_closed_when_rootfs_dir_inaccessible_then_recovers(
    migrated_url: str, tmp_path: Path
) -> None:
    # AC-8i: an inaccessible rootfs_dir reclaims nothing (defers the whole pass), never reading a
    # missing root as "all overlays gone"; a later pass with an accessible dir reclaims (no reboot).
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            nonlocal inv
            inv = await _seed_investigation(
                seed, state="closed", rootfs_marker_age=timedelta(days=2)
            )
            await _seed_rootfs_object(seed, inv)
        finally:
            await seed.close()
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        _stage(uploads, inv)
        missing = tmp_path / "gone-rootfs"  # never created
        store = _RecordingStore()

        async def _sweep(rootfs_dir: Path) -> int:
            conn = await connect(migrated_url)
            try:
                return await gc_investigation_uploaded_rootfs(
                    conn,
                    store,
                    timedelta(days=1),
                    rootfs_dir=str(rootfs_dir),
                    uploads_dir=str(uploads),
                )
            finally:
                await conn.close()

        assert await _sweep(missing) == 0  # fail-closed: reclaims nothing
        assert store.deleted == []
        check = await connect(migrated_url)
        try:
            assert await _marker(check, inv) is not None
        finally:
            await check.close()

        real = tmp_path / "rootfs"
        real.mkdir()
        assert await _sweep(real) == 1  # recovers once the dir is accessible

    asyncio.run(_run())
