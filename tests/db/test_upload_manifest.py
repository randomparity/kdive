"""Tests for owner-scoped upload-manifest storage (ADR-0048 §4)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg

from kdive.artifacts.upload_manifest import (
    UploadManifestReplaceRequest,
    delete_manifest,
    get_manifest,
    get_manifest_sync,
    replace_manifest,
)
from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch import (
    read_rootfs_upload_encoding,
)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def _request(
    owner_id: UUID,
    entries: list[ManifestEntry],
    *,
    prefix: str | None = None,
    ttl: timedelta = timedelta(hours=1),
) -> UploadManifestReplaceRequest:
    return UploadManifestReplaceRequest(
        owner_kind="runs",
        owner_id=owner_id,
        prefix=prefix or f"local/runs/{owner_id}/",
        entries=entries,
        ttl=ttl,
    )


def test_round_trip(migrated_url: str) -> None:
    """replace_manifest then get_manifest returns the entries, prefix, and a deadline."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10), ManifestEntry("vmlinux", "YmFy", 20)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(entries)
        assert got.prefix == f"local/runs/{owner_id}/"
        assert got.deadline is not None

    asyncio.run(_run_test())


def test_round_trips_chunks(migrated_url: str) -> None:
    """A chunked entry persists and reloads its ordered chunk list through the JSONB column."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [
            ManifestEntry(
                "vmlinux",
                "whole",
                10,
                chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)),
            ),
            ManifestEntry("kernel", "Zm9v", 3),
        ]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        by_name = {e.name: e for e in got.entries}
        assert by_name["vmlinux"].chunks == (ChunkEntry("c0", 6), ChunkEntry("c1", 4))
        assert by_name["kernel"].chunks is None

    asyncio.run(_run_test())


def test_round_trips_encoding(migrated_url: str) -> None:
    """An encoded entry persists encoding + uncompressed_size; a plain entry stays identity."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [
            ManifestEntry("rootfs", "whole", 4096, encoding="gzip", uncompressed_size=6 * 1024**3),
            ManifestEntry("kernel", "Zm9v", 3),
        ]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        by_name = {e.name: e for e in got.entries}
        assert by_name["rootfs"].encoding == "gzip"
        assert by_name["rootfs"].uncompressed_size == 6 * 1024**3
        # A plain (no-encoding) entry deserializes as identity.
        assert by_name["kernel"].encoding is None
        assert by_name["kernel"].uncompressed_size is None

    asyncio.run(_run_test())


def test_preexisting_payload_without_encoding_defaults_to_identity() -> None:
    """A manifest payload written before ADR-0437 (no encoding keys) deserializes as identity."""
    from kdive.artifacts.upload_manifest import _entry_from_payload

    entry = _entry_from_payload({"name": "rootfs", "sha256": "a", "size_bytes": 10})
    assert entry.encoding is None
    assert entry.uncompressed_size is None


def test_get_manifest_sync_round_trips(migrated_url: str) -> None:
    """The sync manifest read returns the same entries an async replace persisted (ADR-0438)."""

    async def _seed(owner_id: UUID) -> None:
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn,
                UploadManifestReplaceRequest(
                    owner_kind="systems",
                    owner_id=owner_id,
                    prefix=f"local/systems/{owner_id}/",
                    entries=[
                        ManifestEntry(
                            "rootfs", "whole", 4096, encoding="gzip", uncompressed_size=6 * 1024**3
                        )
                    ],
                    ttl=timedelta(hours=1),
                ),
            )

    owner_id = uuid4()
    asyncio.run(_seed(owner_id))
    with psycopg.connect(migrated_url) as conn:
        got = get_manifest_sync(conn, "systems", owner_id)
        absent = get_manifest_sync(conn, "systems", uuid4())
    assert got is not None
    assert got.entries[0].encoding == "gzip"
    assert got.entries[0].uncompressed_size == 6 * 1024**3
    assert absent is None


def test_read_rootfs_upload_encoding(migrated_url: str) -> None:
    """read_rootfs_upload_encoding resolves the rootfs entry; absent manifest ⇒ identity."""

    async def _seed(owner_id: UUID, entry: ManifestEntry) -> None:
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn,
                UploadManifestReplaceRequest(
                    owner_kind="systems",
                    owner_id=owner_id,
                    prefix=f"local/systems/{owner_id}/",
                    entries=[entry],
                    ttl=timedelta(hours=1),
                ),
            )

    gzip_owner = uuid4()
    identity_owner = uuid4()
    asyncio.run(
        _seed(gzip_owner, ManifestEntry("rootfs", "w", 4096, encoding="gzip", uncompressed_size=99))
    )
    asyncio.run(_seed(identity_owner, ManifestEntry("rootfs", "w", 4096)))
    with psycopg.connect(migrated_url) as conn:
        assert read_rootfs_upload_encoding(conn, gzip_owner) == ("gzip", 99)
        assert read_rootfs_upload_encoding(conn, identity_owner) == (None, None)
        # No manifest at all ⇒ identity fallback (today's behavior).
        assert read_rootfs_upload_encoding(conn, uuid4()) == (None, None)


def test_full_set_replacement(migrated_url: str) -> None:
    """A second replace_manifest with fewer entries replaces, not merges, the prior set."""

    async def _run_test() -> None:
        owner_id = uuid4()
        first_entries = [
            ManifestEntry("kernel", "Zm9v", 10),
            ManifestEntry("vmlinux", "YmFy", 20),
        ]
        second_entries = [ManifestEntry("kernel", "bmV3", 30)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, first_entries))
            await replace_manifest(
                conn, _request(owner_id, second_entries, prefix=f"local/runs/{owner_id}/v2/")
            )
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(second_entries)
        assert got.prefix == f"local/runs/{owner_id}/v2/"

    asyncio.run(_run_test())


def test_remint_updates_deadline(migrated_url: str) -> None:
    """A re-mint with a longer ttl moves the deadline forward (proves EXCLUDED.deadline)."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)]))
            got1 = await get_manifest(conn, "runs", owner_id)
            assert got1 is not None
            first_deadline = got1.deadline
            await replace_manifest(
                conn,
                _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=timedelta(hours=5)),
            )
            got2 = await get_manifest(conn, "runs", owner_id)
        assert got2 is not None
        assert got2.deadline > first_deadline

    asyncio.run(_run_test())


def test_replace_returns_stamp_matching_persisted_deadline(migrated_url: str) -> None:
    """replace_manifest returns a (server_time, deadline) stamp read from the same
    transaction, so deadline - server_time == ttl exactly and deadline equals the value
    a later get_manifest reads (the reaper's contract). Both are timezone-aware (#1336)."""

    async def _run_test() -> None:
        owner_id = uuid4()
        ttl = timedelta(hours=1)
        async with await _connect(migrated_url) as conn:
            stamp = await replace_manifest(
                conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=ttl)
            )
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert stamp.server_time.tzinfo is not None
        assert stamp.deadline.tzinfo is not None
        assert stamp.deadline - stamp.server_time == ttl
        assert stamp.deadline == got.deadline

    asyncio.run(_run_test())


def test_absent_returns_none(migrated_url: str) -> None:
    """get_manifest returns None when no manifest exists for the owner."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())


def test_delete_removes_row(migrated_url: str) -> None:
    """delete_manifest removes the row; subsequent get_manifest returns None."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            await delete_manifest(conn, "runs", owner_id)
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())


def test_delete_is_idempotent(migrated_url: str) -> None:
    """delete_manifest on an absent owner does not raise; get_manifest stays None."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await delete_manifest(conn, "runs", owner_id)
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())
