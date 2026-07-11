"""`inventory.clear_override` re-enable tool tests (ADR-0199, M2.7 B, #639).

The handler is called directly with an injected pool + RequestContext (the repo's unit
contract). It deletes a ledger entry for a config-declared identity so the next no-entry
reconcile pass re-asserts the file. Coverage:

* clears a `removed` resource override → success (and the success payload carries no
  `source_kind`, ADR-0319).
* clearing a non-existent override → `not_found`; a second clear → `not_found` (idempotent).
* an invalid `resource_kind` → `configuration_error`.
* a non-admin token → `authorization_denied` (audited iff it holds ≥1 platform role).
* a cleared override is gone (a reconcile pass would re-assert the file).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.inventory.overrides import (
    InventoryOverrideDisposition,
    InventorySourceKind,
    OverrideIdentity,
    set_override,
)
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.ops.inventory import inventory as inventory_tools
from kdive.security.authz.rbac import PlatformRole, Role


def _admin_ctx(*, principal: str = "ops-admin") -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-admin",
        projects=("team-a",),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
        client_id="kdivectl",
    )


def _operator_ctx() -> RequestContext:
    return RequestContext(
        principal="ops-op",
        agent_session="sess-op",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
        client_id="kdivectl",
    )


def _project_only_ctx() -> RequestContext:
    return RequestContext(
        principal="proj-user",
        agent_session="sess-user",
        projects=("team-a",),
        roles={"team-a": Role.ADMIN},
        platform_roles=frozenset(),
        client_id=None,
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_override(
    pool: AsyncConnectionPool,
    *,
    source_kind: InventorySourceKind,
    resource_kind: str,
    name: str,
    disposition: InventoryOverrideDisposition = InventoryOverrideDisposition.REMOVED,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        await set_override(
            conn,
            OverrideIdentity(source_kind=source_kind, resource_kind=resource_kind, name=name),
            disposition=disposition,
            reason="seed",
            actor="operator",
        )


async def _override_exists(url: str, *, source_kind: str, resource_kind: str, name: str) -> bool:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM inventory_overrides "
            "WHERE source_kind = %s AND resource_kind = %s AND name = %s",
            (source_kind, resource_kind, name),
        )
        return await cur.fetchone() is not None


async def _audit_count(url: str) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_clear_override_removed_resource(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_override(
                pool,
                source_kind=InventorySourceKind.RESOURCE,
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="rl-back",
            )
            resp = await inventory_tools.clear_override(
                pool,
                _admin_ctx(),
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="rl-back",
            )
        assert resp.status == "cleared", resp.model_dump()
        assert "source_kind" not in (resp.data or {})
        assert (
            await _override_exists(
                migrated_url,
                source_kind="resource",
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="rl-back",
            )
            is False
        )

    asyncio.run(_run())


def test_clear_override_absent_is_not_found_and_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first = await inventory_tools.clear_override(
                pool,
                _admin_ctx(),
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="never-set",
            )
            second = await inventory_tools.clear_override(
                pool,
                _admin_ctx(),
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="never-set",
            )
        assert first.error_category == ErrorCategory.NOT_FOUND.value
        assert second.error_category == ErrorCategory.NOT_FOUND.value

    asyncio.run(_run())


def test_clear_override_illegal_resource_kind_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inventory_tools.clear_override(
                pool,
                _admin_ctx(),
                resource_kind="not-a-kind",
                name="x",
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_clear_override_non_admin_denied_and_audited_for_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inventory_tools.clear_override(
                pool,
                _operator_ctx(),
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="x",
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        # a platform-role holder's denial is audited
        assert await _audit_count(migrated_url) == 1

    asyncio.run(_run())


def test_clear_override_project_only_denied_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inventory_tools.clear_override(
                pool,
                _project_only_ctx(),
                resource_kind=ResourceKind.REMOTE_LIBVIRT.value,
                name="x",
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _audit_count(migrated_url) == 0

    asyncio.run(_run())
