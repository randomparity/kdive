"""A stored row that fails its model rebuild in the repository layer is a server fault (#3009)."""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg
import pytest
from pydantic import ValidationError

from kdive.db.repositories import RESOURCES
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
