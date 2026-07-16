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
# The inverted (POWER-host) matrix: ppc64le is native (kvm), x86_64 is the foreign guest (tcg).
# Mirrors the x86-host `_X86_GUEST_ARCHES` with the arches' accelerators swapped (#1155, ADR-0354).
_PPC_HOST_GUEST_ARCHES = {
    "ppc64le": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-ppc64"},
    "x86_64": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-x86_64"},
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


async def _set_resource_pseries_fadump(
    pool: AsyncConnectionPool, alloc_id: str, supported: bool
) -> None:
    """Set the backing Resource's ``pseries_fadump`` capability for the seeded allocation."""
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
            caps["pseries_fadump"] = supported
            await cur.execute(
                "UPDATE resources SET capabilities = %s WHERE id = %s",
                (Jsonb(caps), alloc.resource_id),
            )


def _fadump_profile() -> dict[str, Any]:
    """A ppc64le fadump profile (opt-in + reservation), for the admission host gate (ADR-0349)."""
    profile = _profile()
    profile["arch"] = "ppc64le"
    section = profile["provider"]["local-libvirt"]
    section["crashkernel"] = "512M"
    section["debug"] = {"fadump": True}
    return profile


async def _set_resource_host_cpu(
    pool: AsyncConnectionPool, alloc_id: str, host_cpu: dict[str, Any]
) -> None:
    """Overwrite the backing Resource's ``host_cpu`` capability for the seeded allocation."""
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
            caps["host_cpu"] = host_cpu
            await cur.execute(
                "UPDATE resources SET capabilities = %s WHERE id = %s",
                (Jsonb(caps), alloc.resource_id),
            )


async def _system_for_allocation(pool: AsyncConnectionPool, alloc_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM systems WHERE allocation_id = %s", (alloc_id,))
        return await cur.fetchone()


_HOST_CPU = {
    "model": "Skylake-Client-IBRS",
    "vendor": "Intel",
    "arch": "x86_64",
    "baseline_level": "x86-64-v3",
}


def test_local_mint_does_not_snapshot_host_cpu(migrated_url: str) -> None:
    # ADR-0369: the mint-time host_cpu snapshot is remote-only. A LOCAL System minted against a
    # host that advertises host_cpu records resolved_cpu = None — the native snapshot would be
    # wrong for a CPU pin / arch-mismatched for a foreign-TCG guest; local resolved_cpu is a
    # post-provision live read, not a mint snapshot.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_host_cpu(pool, alloc_id, _HOST_CPU)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["resolved_cpu"] is None

    asyncio.run(_run())


def test_mint_records_null_resolved_cpu_when_host_advertises_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            # Default seed: the fake host advertises no host_cpu capability.
            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["resolved_cpu"] is None

    asyncio.run(_run())


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


def test_provision_records_tcg_accel_for_x86_guest_on_ppc_host(migrated_url: str) -> None:
    # The inverted host/guest matrix (#1155, ADR-0354): on a ppc64le host advertising
    # {ppc64le: kvm, x86_64: tcg}, the default (x86_64) profile is admitted and records accel=tcg
    # — the symmetric counterpart to the ppc64le-guest-on-x86-host case. `accel=="tcg"` is already
    # asserted for a ppc64le guest (the fadump tests); this pins the x86_64-guest-under-TCG key,
    # so a future x86-specific special-case in the arch-agnostic resolution would fail here.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _PPC_HOST_GUEST_ARCHES)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["accel"] == "tcg"  # x86_64 is the foreign (TCG) guest on this ppc64le host

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


# The define lane (systems.define -> systems.provision_defined) resolves accel + validates arch at
# a second, independent call site (_insert_defined_system); it must reject/record the same way.


def test_define_records_accel_when_host_advertises_arch(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)

            resp = await _HANDLERS.define_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["state"] == "defined"
            assert row["accel"] == "kvm"  # recorded at define, not deferred to provision_defined

    asyncio.run(_run())


def test_define_rejects_arch_host_does_not_advertise(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _PPC_ONLY_GUEST_ARCHES)

            resp = await _HANDLERS.define_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_profile()
            )

            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
            assert resp.data.get("accepted_values") == ["ppc64le"]
            # All-or-nothing: no System minted, allocation stays granted (never flipped active).
            assert await _system_for_allocation(pool, alloc_id) is None
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, alloc_id)
            assert alloc is not None and alloc.state is AllocationState.GRANTED

    asyncio.run(_run())


# The fadump host gate (ADR-0349) sits beside accel resolution at both mint sites: a fadump-opted
# profile is admitted only when the bound host advertises pseries_fadump=True; otherwise it is
# rejected before the granted->active flip (no System, capacity untouched — never a hang).


def test_provision_admits_fadump_when_host_supports_it(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)  # ppc64le -> tcg
            await _set_resource_pseries_fadump(pool, alloc_id, supported=True)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_fadump_profile()
            )

            assert resp.error_category is None, resp.data
            row = await _system_for_allocation(pool, alloc_id)
            assert row is not None
            assert row["accel"] == "tcg"

    asyncio.run(_run())


def test_provision_rejects_fadump_when_host_unsupported(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)
            await _set_resource_pseries_fadump(pool, alloc_id, supported=False)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_fadump_profile()
            )

            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
            assert "10.2" in str(resp.data)  # names the QEMU floor
            # All-or-nothing: no System minted, allocation stays granted.
            assert await _system_for_allocation(pool, alloc_id) is None
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, alloc_id)
            assert alloc is not None and alloc.state is AllocationState.GRANTED

    asyncio.run(_run())


def test_provision_rejects_fadump_when_host_signal_absent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # ppc64le advertised (arch passes) but no pseries_fadump key: fail-closed (a host not
            # re-discovered since ADR-0349 must not silently admit fadump).
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)

            resp = await _HANDLERS.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_fadump_profile()
            )

            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
            assert await _system_for_allocation(pool, alloc_id) is None

    asyncio.run(_run())


def test_define_rejects_fadump_when_host_unsupported(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _set_resource_guest_arches(pool, alloc_id, _X86_GUEST_ARCHES)
            await _set_resource_pseries_fadump(pool, alloc_id, supported=False)

            resp = await _HANDLERS.define_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_fadump_profile()
            )

            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
            assert await _system_for_allocation(pool, alloc_id) is None
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, alloc_id)
            assert alloc is not None and alloc.state is AllocationState.GRANTED

    asyncio.run(_run())
