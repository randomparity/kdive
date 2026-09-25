"""Resolver cutover: resolve_rootfs returns one registered image visible to a project.

Public-or-owned, private-shadows-public on the same (provider, name); only `registered` rows
resolve (a `defined`-only baseline is listed but not bootable).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.lifecycle.records import System
from kdive.images.cataloging.catalog import (
    image_os_id,
    resolve_public_rootfs_sync,
    resolve_rootfs,
    resolve_system_catalog_rootfs,
)
from kdive.images.cataloging.projection import IMAGE_CATALOG_ENTRY_PROJECTION

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
    if base["state"] is ImageState.PENDING:
        base.setdefault("publication_attempt_id", uuid4())
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


def test_projection_ignores_future_additive_column(migrated_url: str) -> None:
    projected_fields = {
        field.strip()
        for line in IMAGE_CATALOG_ENTRY_PROJECTION.splitlines()
        for field in line.split(",")
        if field.strip()
    }
    assert projected_fields == set(ImageCatalogEntry.model_fields)

    async def _run() -> None:
        async with (
            await _connect(migrated_url) as conn,
            conn.transaction(force_rollback=True),
        ):
            await IMAGE_CATALOG.insert(conn, _entry())
            await conn.execute(
                "ALTER TABLE image_catalog ADD COLUMN future_publication_attempt text"
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is not None
            inserted = await IMAGE_CATALOG.insert(conn, _entry(name="second"))
            assert await IMAGE_CATALOG.get(conn, inserted.id) == inserted
            listed = await IMAGE_CATALOG.list_all(conn)
            assert {entry.id: entry for entry in listed} == {
                result.id: result,
                inserted.id: inserted,
            }
            assert "future_publication_attempt" not in IMAGE_CATALOG_ENTRY_PROJECTION

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


def test_catalog_build_capabilities_are_per_distro() -> None:
    from kdive.domain.catalog.images import Capability
    from kdive.images.rootfs.specs import catalog_rootfs_build

    fedora = catalog_rootfs_build("local-libvirt", "fedora-kdive-ready-44").spec.capabilities
    debian = catalog_rootfs_build("local-libvirt", "debian-kdive-ready-13").spec.capabilities
    tumbleweed = catalog_rootfs_build(
        "local-libvirt", "opensuse-tumbleweed-kdive-ready"
    ).spec.capabilities
    leap = catalog_rootfs_build("local-libvirt", "opensuse-leap-kdive-ready-15.6").spec.capabilities

    assert Capability.SELINUX in fedora and Capability.APPARMOR not in fedora
    assert Capability.APPARMOR in debian and Capability.SELINUX not in debian
    assert Capability.APPARMOR in tumbleweed and Capability.APPARMOR in leap
    assert Capability.DRGN in tumbleweed and Capability.DRGN not in leap
    for capabilities in (fedora, debian, tumbleweed, leap):
        assert Capability.AGENT not in capabilities
        assert Capability.SSH in capabilities


class _StubSystem:
    def __init__(self, provisioning_profile: object) -> None:
        self.provisioning_profile = provisioning_profile


def _local_profile(rootfs: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git#v7.0",
        "provider": {"local-libvirt": {"rootfs": rootfs}},
    }


def _catalog_system(name: str) -> System:
    rootfs = {"kind": "catalog", "provider": "local-libvirt", "name": name}
    return cast(System, _StubSystem(_local_profile(rootfs)))


def test_system_catalog_rootfs_resolves_the_public_arch_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            entry = await resolve_system_catalog_rootfs(conn, _catalog_system("base"))
            assert entry is not None and entry.name == "base"
            assert await resolve_system_catalog_rootfs(conn, _catalog_system("other")) is None

    asyncio.run(_run())


def test_system_catalog_rootfs_is_none_for_an_unparsable_profile() -> None:
    conn = cast(psycopg.AsyncConnection, object())
    system = cast(System, _StubSystem("::not-a-profile::"))
    assert asyncio.run(resolve_system_catalog_rootfs(conn, system)) is None


def test_system_catalog_rootfs_is_none_for_a_non_catalog_rootfs() -> None:
    conn = cast(psycopg.AsyncConnection, object())
    rootfs = {"kind": "local", "path": "/var/lib/kdive/rootfs/x.qcow2"}
    system = cast(System, _StubSystem(_local_profile(rootfs)))
    assert asyncio.run(resolve_system_catalog_rootfs(conn, system)) is None


@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        ({"os_release": {"id": "fedora", "version_id": "44"}}, "fedora"),
        ({}, None),
        ({"os_release": "fedora"}, None),
        ({"os_release": {"id": ""}}, None),
        ({"os_release": {"id": 7}}, None),
    ],
)
def test_image_os_id_reads_the_recorded_os_release_id(
    provenance: dict[str, object], expected: str | None
) -> None:
    assert image_os_id(_entry(provenance=provenance)) == expected
