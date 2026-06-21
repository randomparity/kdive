"""Runtime-tuning ops tools (#139) — coeff upsert + host-capacity, gating, and audit.

The handlers are called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #139 acceptance bullets:

* set_cost_class_coeff: the next ``resolve_coeff`` returns the new value (upsert of an
  existing class and insert of a new class); committed ledger rows are unchanged; fail-closed
  on a non-positive / non-numeric coeff; platform_operator gating (denied + audit-iff-role).
* set_host_capacity: admission honors the new cap; lowering below the live count blocks new
  placement WITHOUT evicting the live allocations; unknown id rejected; negative cap rejected;
  platform_operator gating.
* success rows land in ``platform_audit_log`` with the tuned target as scope.
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.accounting.cost import Selector, resolve_coeff
from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import ManagedBy, Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.inventory import writeback
from kdive.inventory.model import InventoryDoc
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.ops import tuning
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.allocation.admission.core import AllocationRequest, admit

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    *,
    platform_roles: frozenset[PlatformRole] = frozenset(),
    roles: dict[str, Role] | None = None,
    projects: tuple[str, ...] = (),
    principal: str = "op-1",
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-1",
        projects=projects,
        roles=roles or {},
        platform_roles=platform_roles,
    )


_OPERATOR = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _resource(
    conn: psycopg.AsyncConnection,
    *,
    cap: int = 5,
    cost_class: str = "local",
    kind: ResourceKind = ResourceKind.LOCAL_LIBVIRT,
    managed_by: ManagedBy = ManagedBy.RUNTIME,
    name: str | None = None,
) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=kind,
            name=name,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: cap,
                "vcpus": 16,
                "memory_mb": 65536,
            },
            pool="local-libvirt",
            cost_class=cost_class,
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
            managed_by=managed_by,
        ),
    )
    return res.id


async def _alloc(
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    project: str,
    state: AllocationState = AllocationState.ACTIVE,
) -> UUID:
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            agent_session="sess",
            project=project,
            resource_id=resource_id,
            state=state,
            lease_expiry=None,
        ),
    )
    return alloc.id


async def _budget_quota(conn: psycopg.AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 100000, 0)",
            (project,),
        )
        await cur.execute(
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, 100, 100)",
            (project,),
        )


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    return len(await _platform_audit_rows(url))


async def _live_count(conn: psycopg.AsyncConnection, resource_id: UUID) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE resource_id = %s AND state <> 'released'",
            (resource_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _override_disposition(url: str, *, kind: str, name: str) -> str | None:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT disposition FROM inventory_overrides "
            "WHERE source_kind = 'resource' AND resource_kind = %s AND name = %s",
            (kind, name),
        )
        row = await cur.fetchone()
    return None if row is None else str(row[0])


# ---- set_cost_class_coeff -------------------------------------------------------------


def test_set_coeff_changes_next_charge_resolution(migrated_url: str) -> None:
    # After set_cost_class_coeff, the next resolve_coeff (the pricing read) returns the new
    # value — committed ledger rows are not touched (none exist; the upsert is pricing-only).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("1.0")
            resp = await tuning.set_cost_class_coeff(
                pool, _OPERATOR, cost_class="local", coeff="2.5"
            )
            assert resp.status == "ok"
            assert resp.data["coeff"] == "2.5"
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("2.5")
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0] == ("op-1", "platform_operator", "ops.set_cost_class_coeff", "local")

    asyncio.run(_run())


def test_set_coeff_inserts_new_class(migrated_url: str) -> None:
    # A class with no row is fail-closed today (resolve_coeff raises); the upsert seeds it.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="gpu", coeff="7")
            assert resp.status == "ok"
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "gpu") == Decimal("7")

    asyncio.run(_run())


def test_set_coeff_does_not_reprice_committed_ledger(migrated_url: str) -> None:
    # Committed ledger rows are priced at write time; a later coeff change leaves them as-is.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget_quota(conn, "proj-a")
            request = AllocationRequest(
                ctx=_ctx(roles={"proj-a": Role.OPERATOR}, projects=("proj-a",), principal="alice"),
                resource=await _get_resource(pool, res),
                project="proj-a",
                selector=Selector(vcpus=2, memory_gb=4, cost_class="local"),
                window=1,
            )
            async with pool.connection() as conn:
                outcome = await admit(conn, request)
            assert outcome.granted
            before = await _ledger_rows(migrated_url)
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="local", coeff="9")
            after = await _ledger_rows(migrated_url)
            assert before == after  # the reserved row's kcu is unchanged by the reprice

    asyncio.run(_run())


def test_set_coeff_rejects_non_positive(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for bad in ("0", "-1", "nan", "infinity", "abc"):
                resp = await tuning.set_cost_class_coeff(
                    pool, _OPERATOR, cost_class="local", coeff=bad
                )
                assert resp.status == "error", bad
                assert resp.error_category == "configuration_error", bad
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("1.0")  # untouched

    asyncio.run(_run())


def test_set_coeff_rejects_blank_cost_class(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for bad in ("", "   "):
                resp = await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class=bad, coeff="2")
                assert resp.status == "error", repr(bad)
                assert resp.error_category == "configuration_error", repr(bad)
        assert await _count_platform_audit(migrated_url) == 0  # nothing applied, nothing audited

    asyncio.run(_run())


def test_set_cost_class_coeff_rejects_toml_significant_name(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_cost_class_coeff(
                pool, _OPERATOR, cost_class='evil"\ncoeff = "9', coeff="1.0"
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# ---- set_host_capacity ----------------------------------------------------------------


def test_set_capacity_updates_capabilities_jsonb(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=2
            )
            assert resp.status == "ok"
            assert resp.data[CONCURRENT_ALLOCATION_CAP_KEY] == "2"
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None
            assert row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 2
            # the other capabilities are preserved by the jsonb merge
            assert row.capabilities["vcpus"] == 16
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "ops.set_host_capacity", str(res))]

    asyncio.run(_run())


def test_set_capacity_config_host_writes_detached_override(migrated_url: str) -> None:
    """A config-owned host's cap change writes a `detached` ledger entry (ADR-0199, M2.7 B)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(
                    conn,
                    cap=5,
                    kind=ResourceKind.REMOTE_LIBVIRT,
                    managed_by=ManagedBy.CONFIG,
                    name="rl-detach",
                )
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=3
            )
            assert resp.status == "ok", resp.model_dump()
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None and row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 3
        disposition = await _override_disposition(
            migrated_url, kind=ResourceKind.REMOTE_LIBVIRT.value, name="rl-detach"
        )
        assert disposition == "detached"

    asyncio.run(_run())


def test_set_capacity_runtime_host_writes_no_override(migrated_url: str) -> None:
    """A runtime host's cap change writes no ledger entry (reconcile never clobbers it)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(
                    conn,
                    cap=5,
                    kind=ResourceKind.REMOTE_LIBVIRT,
                    managed_by=ManagedBy.RUNTIME,
                    name="rl-runtime-cap",
                )
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=2
            )
            assert resp.status == "ok", resp.model_dump()
        disposition = await _override_disposition(
            migrated_url, kind=ResourceKind.REMOTE_LIBVIRT.value, name="rl-runtime-cap"
        )
        assert disposition is None

    asyncio.run(_run())


def test_set_capacity_blocks_new_placement_without_evicting(migrated_url: str) -> None:
    # Two live allocations occupy a host with cap 5. Lower the cap to 2 (below the live
    # count): the two live allocations stay (no eviction), but a new admission is denied.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
                await _budget_quota(conn, "proj-a")
                a1 = await _alloc(conn, res, "proj-a")
                a2 = await _alloc(conn, res, "proj-a")
            await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=2
            )
            # no eviction: both live allocations still occupy the host
            async with pool.connection() as conn:
                assert await _live_count(conn, res) == 2
                row_a1 = await ALLOCATIONS.get(conn, a1)
                row_a2 = await ALLOCATIONS.get(conn, a2)
            assert row_a1 is not None and row_a1.state is AllocationState.ACTIVE
            assert row_a2 is not None and row_a2.state is AllocationState.ACTIVE
            # admission honors the new cap: live(2) >= cap(2) → denied, no durable write
            request = AllocationRequest(
                ctx=_ctx(roles={"proj-a": Role.OPERATOR}, projects=("proj-a",), principal="bob"),
                resource=await _get_resource(pool, res),
                project="proj-a",
                selector=Selector(vcpus=1, memory_gb=1, cost_class="local"),
                window=1,
            )
            async with pool.connection() as conn:
                outcome = await admit(conn, request)
            assert not outcome.granted
            assert outcome.reason == "at_capacity"
            assert outcome.cap == 2
            assert outcome.in_use == 2

    asyncio.run(_run())


def test_set_capacity_unknown_resource_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(uuid4()), concurrent_allocation_cap=3
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_capacity_rejects_negative_cap(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=-1
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None
            assert row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5  # untouched

    asyncio.run(_run())


def test_set_capacity_malformed_id_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id="not-a-uuid", concurrent_allocation_cap=3
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# ---- platform_operator gating ---------------------------------------------------------


def test_coeff_denied_for_project_only_token_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await tuning.set_cost_class_coeff(pool, ctx, cost_class="local", coeff="3")
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("1.0")  # not applied
        assert await _count_platform_audit(migrated_url) == 0  # no write amplification

    asyncio.run(_run())


def test_capacity_denied_for_auditor_but_audited(migrated_url: str) -> None:
    # platform_auditor does NOT satisfy the operator gate, but holds a platform role, so the
    # over-reach denial IS audited (the accountability target).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await tuning.set_host_capacity(
                pool, ctx, resource_id=str(res), concurrent_allocation_cap=1
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None
            assert row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5  # not applied
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_auditor"
        assert rows[0][2] == "ops.set_host_capacity"

    asyncio.run(_run())


def test_admin_does_not_imply_operator_gate(migrated_url: str) -> None:
    # platform_admin implies only platform_auditor (not operator), so admin is DENIED here —
    # the operator/admin separation of duties holds.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await tuning.set_cost_class_coeff(pool, ctx, cost_class="local", coeff="3")
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


# ---- helpers needing the pool ---------------------------------------------------------


async def _get_resource(pool: AsyncConnectionPool, resource_id: UUID) -> Resource:
    async with pool.connection() as conn:
        row = await RESOURCES.get(conn, resource_id)
    assert row is not None
    return row


async def _ledger_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT allocation_id, cost_class, event_type, kcu_delta FROM ledger ORDER BY id"
        )
        return list(await cur.fetchall())


# ---- export_cost_classes --------------------------------------------------------------


def test_export_cost_classes_requires_platform_operator(migrated_url: str) -> None:
    # No platform role → denied (the gate), no table read amplification.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.export_cost_classes(pool, _ctx())
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_export_cost_classes_returns_deterministic_sorted_toml(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="zeta", coeff="3.0")
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="alpha", coeff="0.5")
            resp = await tuning.export_cost_classes(pool, _OPERATOR)
            assert resp.status == "ok"
            toml_text = str(resp.data["toml"])
        # 'local' is seeded; the export is name-sorted, so alpha < local < zeta.
        assert toml_text.index("alpha") < toml_text.index("local") < toml_text.index("zeta")
        # The successful read is audited — exactly one export row, alongside the two set rows.
        rows = await _platform_audit_rows(migrated_url)
        export_rows = [r for r in rows if r[2] == "ops.export_cost_classes"]
        assert len(export_rows) == 1

    asyncio.run(_run())


def test_export_round_trips_through_the_model(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="premium", coeff="2.5")
            resp = await tuning.export_cost_classes(pool, _OPERATOR)
            toml_text = str(resp.data["toml"])
        parsed = tomllib.loads("schema_version = 2\n" + toml_text)
        doc = InventoryDoc.parse(parsed)
        by_name = {c.name: c.coeff for c in doc.cost_class}
        assert by_name["premium"] == Decimal("2.5")

    asyncio.run(_run())


# ---- export_systems_toml --------------------------------------------------------------


async def _seed_inventory(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, visibility, capabilities, volume, state, "
        " managed_by) "
        "VALUES ('remote-libvirt', 'base', 'x86_64', 'qcow2', '/dev/vda', 'public', '{}', "
        " 'vol-x', 'registered', 'config')"
    )
    caps = {"vcpus": 8, "memory_mb": 16384, "concurrent_allocation_cap": 2}
    await conn.execute(
        "INSERT INTO resources (kind, name, capabilities, pool, cost_class, status, host_uri, "
        " managed_by) "
        "VALUES ('remote-libvirt', 'host-a', %s, 'remote', 'remote', 'available', "
        " 'qemu+tls://host/system', 'config')",
        (Jsonb(caps),),
    )
    await conn.execute(
        "INSERT INTO build_hosts "
        "(name, kind, workspace_root, max_concurrent, managed_by) "
        "VALUES ('bh-local', 'local', '/var/lib/kdive/build', 4, 'config')"
    )


def test_export_systems_toml_requires_platform_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.export_systems_toml(pool, _ctx())
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_export_systems_toml_auditor_denied_but_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await tuning.export_systems_toml(pool, ctx)
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][2] == "ops.export_systems_toml"

    asyncio.run(_run())


def test_export_systems_toml_emits_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
            resp = await tuning.export_systems_toml(pool, _OPERATOR)
            assert resp.status == "ok"
            toml_text = str(resp.data["toml"])
            assert "schema_version = 2" in toml_text
            assert 'name = "host-a"' in toml_text
            assert 'name = "bh-local"' in toml_text
        rows = await _platform_audit_rows(migrated_url)
        export_rows = [r for r in rows if r[2] == "ops.export_systems_toml"]
        assert len(export_rows) == 1
        assert export_rows[0][3] == "all-inventory"

    asyncio.run(_run())


def test_export_systems_toml_is_byte_deterministic(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
            first = await tuning.export_systems_toml(pool, _OPERATOR)
            second = await tuning.export_systems_toml(pool, _OPERATOR)
            assert first.data["toml"] == second.data["toml"]

    asyncio.run(_run())


def test_export_systems_toml_omits_removed_identity(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
                await conn.execute(
                    "INSERT INTO inventory_overrides "
                    "(source_kind, resource_kind, name, disposition, reason, actor) "
                    "VALUES ('resource', 'remote-libvirt', 'host-a', 'removed', 'gone', 'op')"
                )
            resp = await tuning.export_systems_toml(pool, _OPERATOR)
            toml_text = str(resp.data["toml"])
        assert "host-a" not in toml_text  # removed identity omitted

    asyncio.run(_run())


def test_export_systems_toml_round_trips_through_reconcile(migrated_url: str) -> None:
    # Export, fill the remote skeleton, re-parse, and reconcile against a fresh DB; the resulting
    # config rows match the original state for images/build_hosts + resource identity/sizing.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
            resp = await tuning.export_systems_toml(pool, _OPERATOR)
            toml_text = str(resp.data["toml"])
        completed = _complete_remote(toml_text, base_image="base")
        doc = InventoryDoc.parse(tomllib.loads(completed))
        assert doc.remote_libvirt[0].name == "host-a"
        assert doc.remote_libvirt[0].vcpus == 8
        assert doc.remote_libvirt[0].memory_mb == 16384
        assert doc.remote_libvirt[0].concurrent_allocation_cap == 2
        assert doc.build_host[0].name == "bh-local"
        assert doc.image[0].name == "base"

    asyncio.run(_run())


# ---- export_systems_toml(persist=...) -------------------------------------------------


async def _seed_non_remote_inventory(conn: psycopg.AsyncConnection) -> None:
    # An inventory with NO remote_libvirt host and NO defined image — its export carries no
    # REPLACE_ME_* placeholder, so a live-serialization persist is allowed.
    await conn.execute(
        "INSERT INTO build_hosts (name, kind, workspace_root, max_concurrent, managed_by) "
        "VALUES ('bh-local', 'local', '/var/lib/kdive/build', 4, 'config')"
    )
    await conn.execute(
        "INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES ('local', '1.0') "
        "ON CONFLICT (cost_class) DO NOTHING"
    )


def test_persist_without_writeback_target_is_configuration_error(migrated_url: str) -> None:
    fake = writeback.FakeWriteback()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_non_remote_inventory(conn)
            resp = await tuning.export_systems_toml(
                pool, _OPERATOR, persist=True, resolve_target=lambda: None
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert "KDIVE_INVENTORY_WRITEBACK" in str(resp.detail)
        # writeback off is a config rejection before any target exists — not a writeback attempt,
        # so it is not audited under the writeback scope.
        rows = await _platform_audit_rows(migrated_url)
        assert not [r for r in rows if r[3] == "all-inventory-writeback"]

    asyncio.run(_run())
    assert fake.written is None


def test_persist_clean_export_writes_and_reports(migrated_url: str) -> None:
    fake = writeback.FakeWriteback()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_non_remote_inventory(conn)
            resp = await tuning.export_systems_toml(
                pool, _OPERATOR, persist=True, resolve_target=lambda: fake
            )
            assert resp.status == "ok"
            assert resp.data["persisted"] is True
            assert resp.data["target"] == "fake"
            assert resp.data["toml"] == fake.written
        rows = await _platform_audit_rows(migrated_url)
        write_rows = [r for r in rows if r[3] == "all-inventory-writeback"]
        assert len(write_rows) == 1

    asyncio.run(_run())
    assert fake.written is not None
    assert "bh-local" in fake.written


def test_persist_skeleton_is_refused_and_writes_nothing(migrated_url: str) -> None:
    # A fleet WITH a remote_libvirt host: its live export is a skeleton (REPLACE_ME_*), so a
    # bare persist is refused before any write.
    fake = writeback.FakeWriteback()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
            resp = await tuning.export_systems_toml(
                pool, _OPERATOR, persist=True, resolve_target=lambda: fake
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
        # a refused writeback attempt against a configured target is still audited.
        rows = await _platform_audit_rows(migrated_url)
        assert any(r[3] == "all-inventory-writeback" for r in rows)

    asyncio.run(_run())
    assert fake.written is None


def test_persist_with_completed_document_writes_verbatim(migrated_url: str) -> None:
    fake = writeback.FakeWriteback()

    async def _run() -> str:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
            exported = await tuning.export_systems_toml(pool, _OPERATOR)
            completed = _complete_remote(str(exported.data["toml"]), base_image="base")
            resp = await tuning.export_systems_toml(
                pool, _OPERATOR, persist=True, document=completed, resolve_target=lambda: fake
            )
            assert resp.status == "ok"
            assert resp.data["persisted"] is True
            return completed

    completed = asyncio.run(_run())
    # the operator's completed document is written verbatim, not a re-serialization.
    assert fake.written == completed
    # no placeholder VALUE remains (the header prose still mentions REPLACE_ME_* — that is fine).
    assert writeback.WRITEBACK_PLACEHOLDER_MARKER not in str(fake.written)


def test_persist_with_uncompleted_document_is_refused(migrated_url: str) -> None:
    fake = writeback.FakeWriteback()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_inventory(conn)
            exported = await tuning.export_systems_toml(pool, _OPERATOR)
            resp = await tuning.export_systems_toml(
                pool,
                _OPERATOR,
                persist=True,
                document=str(exported.data["toml"]),  # still has REPLACE_ME_*
                resolve_target=lambda: fake,
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())
    assert fake.written is None


def test_document_without_persist_is_refused(migrated_url: str) -> None:
    fake = writeback.FakeWriteback()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_non_remote_inventory(conn)
            resp = await tuning.export_systems_toml(
                pool, _OPERATOR, document="schema_version = 2\n", resolve_target=lambda: fake
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())
    assert fake.written is None


def test_persist_oversized_document_is_refused(migrated_url: str, monkeypatch: object) -> None:
    import kdive.config as config
    from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES

    fake = writeback.FakeWriteback()
    config.load({"KDIVE_MAX_BUILD_CONFIG_BYTES": "32"})

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_non_remote_inventory(conn)
            resp = await tuning.export_systems_toml(
                pool,
                _OPERATOR,
                persist=True,
                document="x = 1\n" * 50,
                resolve_target=lambda: fake,
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert MAX_BUILD_CONFIG_BYTES.name in str(resp.detail)

    asyncio.run(_run())
    config.reset()
    assert fake.written is None


def test_persist_write_failure_is_surfaced(migrated_url: str) -> None:
    boom = CategorizedError("configmap unreachable", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    fake = writeback.FakeWriteback(fail=boom)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await _seed_non_remote_inventory(conn)
            resp = await tuning.export_systems_toml(
                pool, _OPERATOR, persist=True, resolve_target=lambda: fake
            )
            assert resp.status == "error"
            assert resp.error_category == "infrastructure_failure"
            assert resp.data.get("persisted") is not True
        # the failed writeback attempt is recorded under the writeback scope (the outcome rides
        # in the hashed args_digest; the scope is the observable forensic signal here).
        rows = await _platform_audit_rows(migrated_url)
        assert [r for r in rows if r[3] == "all-inventory-writeback"]

    asyncio.run(_run())


def test_persist_denied_for_non_operator_writes_nothing(migrated_url: str) -> None:
    fake = writeback.FakeWriteback()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.export_systems_toml(
                pool, _ctx(), persist=True, resolve_target=lambda: fake
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"

    asyncio.run(_run())
    assert fake.written is None


def _complete_remote(text: str, *, base_image: str) -> str:
    completions = {
        "base_image": base_image,
        "gdb_addr": "10.0.0.1:1234",
        "gdbstub_range": "1234-1240",
        "client_cert_ref": "ref://cc",
        "client_key_ref": "ref://ck",  # pragma: allowlist secret
        "ca_cert_ref": "ref://ca",
    }
    for field, value in completions.items():
        text = text.replace(f'{field} = "REPLACE_ME_{field}"', f'{field} = "{value}"')
    return text
