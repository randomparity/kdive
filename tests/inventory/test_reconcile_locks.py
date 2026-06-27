"""Inventory reconcile advisory-lock behavior."""

from __future__ import annotations

import asyncio

import psycopg

from kdive.domain.catalog.resources import ResourceKind
from kdive.inventory.reconcile.locks import resource_identity_lock
from tests.db_waits import wait_until_backend_waiting


def test_resource_identity_lock_serializes_same_resource_name(postgres_url: str) -> None:
    async def _run() -> None:
        async with (
            await psycopg.AsyncConnection.connect(postgres_url) as a,
            await psycopg.AsyncConnection.connect(postgres_url) as b,
        ):
            acquired_b = asyncio.Event()

            async def acquire_same_identity() -> str:
                async with (
                    b.transaction(),
                    resource_identity_lock(b, ResourceKind.REMOTE_LIBVIRT, "host-a"),
                ):
                    acquired_b.set()
                    return "acquired"

            async with (  # noqa: SIM117
                a.transaction(),
                resource_identity_lock(a, ResourceKind.REMOTE_LIBVIRT, "host-a"),
            ):
                async with (
                    b.transaction(),
                    resource_identity_lock(b, ResourceKind.REMOTE_LIBVIRT, "host-b"),
                ):
                    pass

                task = asyncio.create_task(acquire_same_identity())
                await wait_until_backend_waiting(a, b.info.backend_pid, locktype="advisory")
                assert not task.done()
                assert not acquired_b.is_set()

            assert await asyncio.wait_for(task, timeout=5) == "acquired"

    asyncio.run(_run())
