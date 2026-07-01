"""Resolver cutover: resolve_rootfs returns one registered image visible to a project.

Public-or-owned, private-shadows-public on the same (provider, name); only `registered` rows
resolve (a `defined`-only baseline is listed but not bootable).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg import sql

from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.images.catalog import resolve_public_rootfs_sync, resolve_rootfs

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_FUTURE = datetime.now(UTC) + timedelta(days=365)


def _entry(**kw: object) -> ImageCatalogEntry:
    base: dict[str, object] = {
        "id": uuid4(),
        "created_at": _DT,
        "updated_at": _DT,
        "pending_since": _DT,
        "provider": "local-libvirt",
        "name": "base",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "object_key": "images/local-libvirt/base/x86_64.qcow2",
        "digest": "sha256:abc",
        "capabilities": ["agent", "drgn"],
        "provenance": {"releasever": "43"},
        "visibility": ImageVisibility.PUBLIC,
        "owner": None,
        "expires_at": None,
        "state": ImageState.REGISTERED,
    }
    base.update(kw)
    return ImageCatalogEntry.model_validate(base)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_resolves_registered_public(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is not None
            assert result.visibility is ImageVisibility.PUBLIC
            assert result.object_key == "images/local-libvirt/base/x86_64.qcow2"

    asyncio.run(_run())


def test_defined_only_resolves_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(
                conn, _entry(state=ImageState.DEFINED, object_key=None, digest=None)
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is None

    asyncio.run(_run())


def test_pending_resolves_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(state=ImageState.PENDING))
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is None

    asyncio.run(_run())


def test_unknown_identity_resolves_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            assert await resolve_rootfs(conn, "local-libvirt", "other", project="proj") is None
            assert await resolve_rootfs(conn, "other", "base", project="proj") is None

    asyncio.run(_run())


def test_private_shadows_public_for_owning_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(object_key="images/public"))
            await IMAGE_CATALOG.insert(
                conn,
                _entry(
                    object_key="images/private",
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj",
                    expires_at=_FUTURE,
                ),
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is not None
            assert result.visibility is ImageVisibility.PRIVATE
            assert result.object_key == "images/private"

    asyncio.run(_run())


def test_other_project_sees_public_not_private(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(object_key="images/public"))
            await IMAGE_CATALOG.insert(
                conn,
                _entry(
                    object_key="images/private",
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj-a",
                    expires_at=_FUTURE,
                ),
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj-b")
            assert result is not None
            assert result.visibility is ImageVisibility.PUBLIC
            assert result.object_key == "images/public"

    asyncio.run(_run())


def test_private_only_invisible_to_other_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(
                conn,
                _entry(
                    object_key="images/private",
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj-a",
                    expires_at=_FUTURE,
                ),
            )
            assert await resolve_rootfs(conn, "local-libvirt", "base", project="proj-b") is None

    asyncio.run(_run())


def _insert_registered_sync(conn: psycopg.Connection, **kw: object) -> None:
    row: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "fed",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "object_key": None,
        "volume": None,
        "path": "/r/x.img",
        "digest": None,
        "visibility": "public",
        "owner": None,
        "expires_at": None,
        "state": "registered",
    }
    row.update(kw)
    cols = list(row.keys())
    query = sql.SQL("INSERT INTO image_catalog ({cols}) VALUES ({vals})").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in cols),
    )
    conn.execute(query, row)


def test_resolve_public_sync_matches_arch(migrated_url: str) -> None:
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        _insert_registered_sync(conn, name="fed", arch="x86_64", path="/r/x.img")
        _insert_registered_sync(conn, name="fed", arch="aarch64", path="/r/a.img")
        row = resolve_public_rootfs_sync(conn, "local-libvirt", "fed", "x86_64")
        assert row is not None and row.path == "/r/x.img"


def test_resolve_public_sync_misses_unknown_arch(migrated_url: str) -> None:
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        _insert_registered_sync(conn, name="fed", arch="x86_64", path="/r/x.img")
        assert resolve_public_rootfs_sync(conn, "local-libvirt", "fed", "riscv64") is None


def test_resolve_public_sync_ignores_private(migrated_url: str) -> None:
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        _insert_registered_sync(
            conn,
            name="fed",
            arch="x86_64",
            path="/r/x.img",
            visibility="private",
            owner="proj",
            expires_at=_FUTURE,
        )
        assert resolve_public_rootfs_sync(conn, "local-libvirt", "fed", "x86_64") is None
