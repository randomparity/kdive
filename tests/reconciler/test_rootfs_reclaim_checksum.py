"""Per-checksum investigation-rootfs reclaim: pinned order, fault contract, .partial sweep (4.1c).

Pinned order (ADR-0441 §6): object -> unlink staged base -> row (last, fail-loud). A real object
or unlink fault defers the whole checksum before the row delete (row kept, retried); a 404/ENOENT
target is success (idempotent reconverge). Stale ``.partial`` files are swept before empty-dir
removal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import psycopg

from kdive.reconciler.cleanup.gc import (
    _reclaim_rootfs_checksum,
    _sweep_investigation_staging_dir,
)
from tests.reconciler.conftest import connect

_TOKEN = "dGVzdC10b2tlbg"  # an arbitrary base64url content-address token


class _RecordingStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class _FlakyStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        raise RuntimeError("object store unavailable")


def _object_key(inv: UUID) -> str:
    return f"local/investigations/{inv}/rootfs-{_TOKEN}"


def _staged_path(uploads_dir: Path, inv: UUID) -> Path:
    return uploads_dir / str(inv) / f"{_TOKEN}.qcow2"


async def _seed_rootfs_artifact(conn: psycopg.AsyncConnection, inv: UUID) -> UUID:
    artifact_id = uuid4()
    await conn.execute(
        "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES (%s, 'investigations', %s, %s, 'etag', 'redacted', 'rootfs')",
        (artifact_id, inv, _object_key(inv)),
    )
    return artifact_id


async def _row_exists(conn: psycopg.AsyncConnection, artifact_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM artifacts WHERE id = %s", (artifact_id,))
    return await cur.fetchone() is not None


def test_reclaim_deletes_object_file_and_row(migrated_url: str, tmp_path: Path) -> None:
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            artifact_id = await _seed_rootfs_artifact(seed, inv)
        finally:
            await seed.close()
        staged = _staged_path(tmp_path, inv)
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"base")
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            drained = await _reclaim_rootfs_checksum(
                conn,
                store,
                artifact_id=artifact_id,
                object_key=_object_key(inv),
                investigation_id=inv,
                uploads_dir=str(tmp_path),
            )
        finally:
            await conn.close()
        assert drained is True
        assert store.deleted == [_object_key(inv)]
        assert not staged.exists()
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, artifact_id)
        finally:
            await check.close()

    asyncio.run(_run())


def test_reclaim_idempotent_when_target_absent(migrated_url: str, tmp_path: Path) -> None:
    # AC-8g: object 404 (store.delete no-ops) + staged file already gone (ENOENT) = success; the
    # row still drains — no permanent wedge on a reconverge after a prior partial run.
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            artifact_id = await _seed_rootfs_artifact(seed, inv)
        finally:
            await seed.close()
        store = _RecordingStore()  # idempotent delete, no staged file created
        conn = await connect(migrated_url)
        try:
            drained = await _reclaim_rootfs_checksum(
                conn,
                store,
                artifact_id=artifact_id,
                object_key=_object_key(inv),
                investigation_id=inv,
                uploads_dir=str(tmp_path),
            )
        finally:
            await conn.close()
        assert drained is True
        check = await connect(migrated_url)
        try:
            assert not await _row_exists(check, artifact_id)
        finally:
            await check.close()

    asyncio.run(_run())


def test_reclaim_defers_on_object_fault_keeps_row(migrated_url: str, tmp_path: Path) -> None:
    # AC-8g: a non-404 object-store fault defers (row kept) BEFORE the row delete, so the SENSITIVE
    # object is never orphaned without its handle.
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            artifact_id = await _seed_rootfs_artifact(seed, inv)
        finally:
            await seed.close()
        staged = _staged_path(tmp_path, inv)
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"base")
        conn = await connect(migrated_url)
        try:
            drained = await _reclaim_rootfs_checksum(
                conn,
                _FlakyStore(),
                artifact_id=artifact_id,
                object_key=_object_key(inv),
                investigation_id=inv,
                uploads_dir=str(tmp_path),
            )
        finally:
            await conn.close()
        assert drained is False
        assert staged.exists()  # not unlinked — deferred before the unlink
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)  # row kept as the retry anchor
        finally:
            await check.close()

    asyncio.run(_run())


def test_reclaim_defers_on_unlink_fault_keeps_row(migrated_url: str, tmp_path: Path) -> None:
    # AC-8e: object deleted, but the staged-base unlink faults (path is a directory -> IsADirectory
    # OSError). The row is kept and the checksum retried — the file is never stranded without a row.
    inv = uuid4()

    async def _run() -> None:
        seed = await connect(migrated_url)
        try:
            artifact_id = await _seed_rootfs_artifact(seed, inv)
        finally:
            await seed.close()
        staged = _staged_path(tmp_path, inv)
        staged.mkdir(parents=True)  # a dir at the base path -> unlink raises IsADirectoryError
        store = _RecordingStore()
        conn = await connect(migrated_url)
        try:
            drained = await _reclaim_rootfs_checksum(
                conn,
                store,
                artifact_id=artifact_id,
                object_key=_object_key(inv),
                investigation_id=inv,
                uploads_dir=str(tmp_path),
            )
        finally:
            await conn.close()
        assert drained is False
        assert store.deleted == [_object_key(inv)]  # object delete happened before the defer
        check = await connect(migrated_url)
        try:
            assert await _row_exists(check, artifact_id)
        finally:
            await check.close()

    asyncio.run(_run())


def test_partial_sweep_unlinks_and_removes_empty_dir(tmp_path: Path) -> None:
    # AC-8h: a crash-orphaned <token>.*.partial is swept before the now-empty dir is removed.
    inv = uuid4()
    inv_dir = tmp_path / str(inv)
    inv_dir.mkdir(parents=True)
    orphan = inv_dir / f"{_TOKEN}.{uuid4().hex}.partial"
    orphan.write_bytes(b"partial")
    _sweep_investigation_staging_dir(str(tmp_path), inv)
    assert not orphan.exists()
    assert not inv_dir.exists()  # empty after the partial swept -> removed


def test_partial_sweep_keeps_dir_holding_a_base(tmp_path: Path) -> None:
    inv = uuid4()
    inv_dir = tmp_path / str(inv)
    inv_dir.mkdir(parents=True)
    (inv_dir / f"{_TOKEN}.{uuid4().hex}.partial").write_bytes(b"partial")
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"base")  # a still-deferred base keeps the dir non-empty
    _sweep_investigation_staging_dir(str(tmp_path), inv)
    assert inv_dir.exists()
    assert base.exists()
    assert not list(inv_dir.glob("*.partial"))  # partial still swept
