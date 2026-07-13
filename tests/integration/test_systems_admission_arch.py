"""Admission arch validation + accelerator persistence against a real DB (#1141, ADR-0339).

Drives the create-provision lane through the admission service, proving that a profile arch the
backing host advertises is accepted and its accelerator recorded on the System; an arch the host
does not advertise is rejected at admission (no System, allocation stays granted); and a host that
advertises no guest arches at all falls open (provisions, records no accel).
"""

from __future__ import annotations

import asyncio
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.capacity.state import AllocationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.mcp.tools.lifecycle.systems.view import system_envelope
from tests.mcp.systems_support import (
    SYSTEM_PROVISION_HANDLERS as _HANDLERS,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provisioning_profile as _profile,
)

_X86_GUEST_ARCHES = {
    "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
    "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
}
_PPC_ONLY_GUEST_ARCHES = {
    "ppc64le": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-ppc64"},
}


async def _set_resource_guest_arches(
    pool: AsyncConnectionPool, alloc_id: str, guest_arches: dict[str, Any]
) -> None:
    """Overwrite the backing Resource's ``guest_arches`` capability for the seeded allocation."""
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        assert alloc is not None and alloc.resource_id is not None
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT capabilities FROM resources WHERE id = %s", (alloc.resource_id,)
            )
            row = await cur.fetchone()
            assert row is not None
            caps = dict(row["capabilities"])
            caps["guest_arches"] = guest_arches
            await cur.execute(
                "UPDATE resources SET capabilities = %s WHERE id = %s",
                (Jsonb(caps), alloc.resource_id),
            )


async def _system_for_allocation(pool: AsyncConnectionPool, alloc_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM systems WHERE allocation_id = %s", (alloc_id,))
        return await cur.fetchone()


def test_provision_records_accel_when_host_advertises_arch(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["accel"] == "kvm"  # x86_64 is native on this advertised host

    asyncio.run(_run())


def test_systems_get_envelope_surfaces_accel(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)
            await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None

        envelope = system_envelope(System.model_validate(row))
        assert envelope.data["accel"] == "kvm"

    asyncio.run(_run())


def test_provision_rejects_arch_host_does_not_advertise(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            # Host advertises only ppc64le; the default profile requests x86_64.
            await _set_resource_guest_arches(pool, alloc_id, _PPC_ONLY_GUEST_ARCHES)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
            message = str(resp.data)
            assert "x86_64" in message and "ppc64le" in message  # supported set named
            # All-or-nothing: no System minted, allocation stays granted.
            assert await _system_for_allocation(pool, alloc_id) is None
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, alloc_id)
            assert alloc is not None and alloc.state is AllocationState.GRANTED

    asyncio.run(_run())


def test_provision_falls_open_when_host_advertises_no_guest_arches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # Default seed: the fake host advertises no <guest> blocks, so guest_arches is empty.
            alloc_id = await _granted_allocation(pool)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["accel"] is None  # no host-derived accelerator recorded

    asyncio.run(_run())


def test_provision_rejection_names_supported_set_in_details(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _PPC_ONLY_GUEST_ARCHES)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
        assert resp.data.get("accepted_values") == ["ppc64le"]
        assert resp.data.get("requested_arch") == "x86_64"

    asyncio.run(_run())
