"""A stored row that fails its rebuild below the MCP layer is a server fault (#3009, #3044)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from kdive.db.repositories import IMAGE_CATALOG, RESOURCES
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.lifecycle.records import System
from kdive.images.cataloging.catalog import (
    resolve_public_rootfs_sync,
    resolve_rootfs,
    resolve_system_catalog_rootfs,
)
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.serialization import ServerFaultError
from tests.providers.local_libvirt.fakes import FakeLibvirtConn


async def _corrupt_resource(conn: psycopg.AsyncConnection) -> UUID:
    discovery = LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=FakeLibvirtConn, concurrent_allocation_cap=2
    )
    res = await register_discovered_resource(
        conn, discovery.list_resources()[0], pool="local-libvirt", cost_class="local"
    )
    await conn.execute("UPDATE resources SET capabilities = '[]'::jsonb WHERE id = %s", (res.id,))
    return res.id


def test_repository_rebuild_of_a_corrupt_row_is_a_server_fault(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            res_id = await _corrupt_resource(conn)
            with pytest.raises(ServerFaultError, match="stored Resource failed") as got:
                await RESOURCES.get(conn, res_id)
            assert isinstance(got.value.__cause__, ValidationError)
            with pytest.raises(ServerFaultError, match="stored Resource failed"):
                await RESOURCES.list_all(conn)

    asyncio.run(_run())


class _StubSystem:
    def __init__(self, provisioning_profile: object) -> None:
        self.provisioning_profile = provisioning_profile


def _catalog_system() -> System:
    profile = {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git#v7.0",
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "catalog", "provider": "local-libvirt", "name": "base"}
            }
        },
    }
    return cast(System, _StubSystem(profile))


def _catalog_entry() -> ImageCatalogEntry:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return ImageCatalogEntry.model_validate(
        {
            "id": uuid4(),
            "created_at": at,
            "updated_at": at,
            "pending_since": at,
            "provider": "local-libvirt",
            "name": "base",
            "arch": "x86_64",
            "format": "qcow2",
            "root_device": "/dev/vda",
            "object_key": "images/local-libvirt/base/x86_64.qcow2",
            "digest": "sha256:abc",
            "visibility": ImageVisibility.PUBLIC,
            "state": ImageState.REGISTERED,
        }
    )


def _assert_catalog_fault(got: pytest.ExceptionInfo[ServerFaultError]) -> None:
    assert isinstance(got.value.__cause__, ValidationError)


def test_catalog_rebuild_of_a_corrupt_row_is_a_server_fault(migrated_url: str) -> None:
    async def _corrupt_and_resolve() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await IMAGE_CATALOG.insert(conn, _catalog_entry())
            await conn.execute("UPDATE image_catalog SET capabilities = '{bogus}'")
            with pytest.raises(ServerFaultError, match="stored ImageCatalogEntry failed") as got:
                await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            _assert_catalog_fault(got)
            with pytest.raises(ServerFaultError, match="stored ImageCatalogEntry failed") as got:
                await resolve_system_catalog_rootfs(conn, _catalog_system())
            _assert_catalog_fault(got)

    asyncio.run(_corrupt_and_resolve())
    with (
        psycopg.connect(migrated_url, autocommit=True) as sync_conn,
        pytest.raises(ServerFaultError, match="stored ImageCatalogEntry failed") as got,
    ):
        resolve_public_rootfs_sync(sync_conn, "local-libvirt", "base", "x86_64")
    _assert_catalog_fault(got)
