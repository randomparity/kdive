"""``build_hosts.list``, ``build_hosts.disable``, and ``build_hosts.remove`` handlers.

``list`` is ``platform_auditor``-gated: returns id, name, kind, address,
ssh_credential_ref (the reference string only — never key bytes), workspace_root,
max_concurrent, enabled, state, and ``resolves`` (whether the host backs a declared
``[[remote_libvirt]]`` instance, ADR-0195) for every row in ``build_hosts``.

``disable`` and ``remove`` are ``platform_admin``-gated mutating ops. Both reject the
protected ``worker-local`` seed (CONFLICT). ``remove`` deletes a ``managed_by='runtime'`` host
(refused if it holds active leases — FK ON DELETE RESTRICT). A ``managed_by='config'`` host is
durably removed under the override ledger (ADR-0199): it requires a non-empty ``reason`` and is
cordoned (``enabled=false``) if leased, else deleted, with a ``removed`` ledger entry written in
the same transaction so reconcile stops re-creating the still-declared host.
"""

from __future__ import annotations

import logging
from uuid import UUID

import psycopg.errors
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import WORKER_LOCAL_ID, BuildHostKind, get_by_name
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import ErrorCategory
from kdive.inventory.overrides import (
    BUILD_HOST_RESOURCE_KIND,
    InventoryOverrideDisposition,
    InventorySourceKind,
    OverrideIdentity,
    set_override,
)
from kdive.inventory.reconcile.records import CONFIG_MANAGED_BY
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.mcp.tools.ops import _reads
from kdive.providers.assembly.build_hosts import declared_remote_instance_names
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.services.runs.build_host_selection import (
    accepted_source_kinds,
    build_host_resolves,
)

_log = logging.getLogger(__name__)

LIST_TOOL = "build_hosts.list"
DISABLE_TOOL = "build_hosts.disable"
REMOVE_TOOL = "build_hosts.remove"

_PROTECTED_NAME = "worker-local"


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def _conflict(object_id: str, reason: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFLICT, data={"reason": reason})


def config_error(object_id: str, reason: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.CONFIGURATION_ERROR, data={"reason": reason}
    )


def _not_found(name: str) -> ToolResponse:
    return ToolResponse.failure(name, ErrorCategory.NOT_FOUND)


async def list_build_hosts(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
) -> ToolResponse:
    """Return all build host rows. Requires ``platform_auditor``.

    The response includes only the ``ssh_credential_ref`` reference string — never
    key bytes.
    """
    args: dict[str, object] = {"scope": ALL_PROJECTS_SCOPE}
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)
    except AuthorizationError:
        await _reads.audit_denial(pool, ctx, tool=LIST_TOOL, args=args)
        return _denied("build_hosts", LIST_TOOL)

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, address, ssh_credential_ref, workspace_root, "
                "       max_concurrent, enabled, state "
                "FROM build_hosts ORDER BY name"
            )
            rows = await cur.fetchall()
        await _reads.record_read(conn, ctx, tool=LIST_TOOL, args=args)

    # Resolved once per call (not per row): an ephemeral_libvirt host resolves only when a
    # [[remote_libvirt]] instance of the same name is declared (ADR-0195, #626); a missing or
    # malformed systems.toml degrades to an empty set, so such a host reports resolves=false.
    declared = declared_remote_instance_names()
    items = [
        ToolResponse.success(
            str(row["id"]),
            "ok",
            data={
                "id": str(row["id"]),
                "name": row["name"],
                "kind": row["kind"],
                "address": row["address"] or "",
                "ssh_credential_ref": row["ssh_credential_ref"] or "",
                "workspace_root": row["workspace_root"],
                "max_concurrent": int(row["max_concurrent"]),
                "enabled": bool(row["enabled"]),
                "state": row["state"],
                "resolves": build_host_resolves(BuildHostKind(row["kind"]), row["name"], declared),
                "supported_source_kinds": [
                    kind.value for kind in accepted_source_kinds(BuildHostKind(row["kind"]))
                ],
            },
        )
        for row in rows
    ]
    return ToolResponse.collection("build_hosts", "ok", items)


async def disable_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
) -> ToolResponse:
    """Set ``enabled=false`` on the named host. Requires ``platform_admin``.

    Rejects the ``worker-local`` seed (CONFLICT) and an absent name (NOT_FOUND).
    Writes a ``platform_audit_log`` row on success.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=DISABLE_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return _denied(name, DISABLE_TOOL)

    if name == _PROTECTED_NAME:
        return _conflict(name, f"{name!r} is a protected fallback and cannot be disabled")

    async with pool.connection() as conn:
        host = await get_by_name(conn, name)
        if host is None:
            return _not_found(name)
        if host.id == WORKER_LOCAL_ID:
            return _conflict(name, f"{name!r} is a protected fallback and cannot be disabled")
        async with conn.transaction():
            await conn.execute("UPDATE build_hosts SET enabled = false WHERE id = %s", (host.id,))
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=DISABLE_TOOL,
                    scope=f"build_host:{host.id}",
                    args={"name": name, "host_id": str(host.id)},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )

    _log.info("build host %r (%s) disabled by %s", name, host.id, ctx.principal)
    return ToolResponse.success(
        str(host.id),
        "disabled",
        suggested_next_actions=[LIST_TOOL],
        data={"name": name},
    )


async def remove_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    reason: str = "",
) -> ToolResponse:
    """Remove the named host. Requires ``platform_admin``.

    A ``managed_by='runtime'`` host is deleted as before (a host with outstanding leases is
    refused — FK ON DELETE RESTRICT). A ``managed_by='config'`` host is durably removed under the
    override ledger (ADR-0199): it requires a non-empty ``reason`` and applies the FK-safe
    disposition (cordon via ``enabled=false`` if it holds a lease, else delete), writing a
    ``removed`` ledger entry in the same transaction so reconcile stops re-creating the still-
    declared host. Rejects the ``worker-local`` seed (CONFLICT) and an absent name (NOT_FOUND).

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        name: The build-host name to remove.
        reason: Required (non-empty) audit reason for a config-owned removal; ignored for a
            runtime host.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=REMOVE_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return _denied(name, REMOVE_TOOL)

    if name == _PROTECTED_NAME:
        return _conflict(name, f"{name!r} is a protected fallback and cannot be removed")

    async with pool.connection() as conn:
        host = await get_by_name(conn, name)
        if host is None:
            return _not_found(name)
        if host.id == WORKER_LOCAL_ID:
            return _conflict(name, f"{name!r} is a protected fallback and cannot be removed")
        managed_by = await _host_managed_by(conn, host.id)

    if managed_by == CONFIG_MANAGED_BY:
        return await _remove_config_build_host(pool, ctx, name=name, host_id=host.id, reason=reason)
    return await _remove_runtime_build_host(pool, ctx, name=name, host_id=host.id)


async def _remove_runtime_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    host_id: UUID,
) -> ToolResponse:
    """Delete a runtime build host (no ledger write); refuse if leased (FK ON DELETE RESTRICT)."""
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                await conn.execute("DELETE FROM build_hosts WHERE id = %s", (host_id,))
                await _audit_remove(conn, ctx, name=name, host_id=host_id)
        except psycopg.errors.ForeignKeyViolation:
            return _conflict(name, f"build host {name!r} has active leases and cannot be removed")

    _log.info("build host %r (%s) removed by %s", name, host_id, ctx.principal)
    return ToolResponse.success(
        str(host_id), "removed", suggested_next_actions=[LIST_TOOL], data={"name": name}
    )


async def _remove_config_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    host_id: UUID,
    reason: str,
) -> ToolResponse:
    """Durably remove a config-owned build host under the override ledger (ADR-0199).

    Runs in one transaction holding the per-identity build-host lock. SELECTs the parent
    ``build_hosts`` row ``FOR UPDATE`` **before** the lease check (the same lock
    ``prune_or_cordon_build_host`` relies on: it conflicts with the ``FOR KEY SHARE`` a concurrent
    lease INSERT takes, so a lease cannot land between the check and the delete to hit
    ``ON DELETE RESTRICT``). A leased host is cordoned (``enabled=false``); an idle host's row is
    deleted. The ``removed`` ledger entry commits in the same transaction.
    """
    if not reason.strip():
        return config_error(name, "a config-owned build-host remove requires a non-empty reason")
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.BUILD_HOST, name),
    ):
        if not await _lock_config_host(conn, host_id):
            return _not_found(name)
        disposition = await _apply_removed_build_host(conn, host_id)
        await set_override(
            conn,
            OverrideIdentity(
                source_kind=InventorySourceKind.BUILD_HOST,
                resource_kind=BUILD_HOST_RESOURCE_KIND,
                name=name,
            ),
            disposition=InventoryOverrideDisposition.REMOVED,
            reason=reason,
            actor=actor_for(ctx),
        )
        await _audit_remove(conn, ctx, name=name, host_id=host_id, disposition=disposition)

    _log.info(
        "config build host %r (%s) removed (%s) by %s", name, host_id, disposition, ctx.principal
    )
    return ToolResponse.success(
        str(host_id),
        "removed",
        suggested_next_actions=[LIST_TOOL],
        data={"name": name, "disposition": disposition},
    )


async def _host_managed_by(conn: AsyncConnection, host_id: UUID) -> str:
    """Read a build host's ``managed_by`` (the ``BuildHost`` dataclass does not carry it)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT managed_by FROM build_hosts WHERE id = %s", (host_id,))
        row = await cur.fetchone()
    return str(row[0]) if row is not None else ""


async def _lock_config_host(conn: AsyncConnection, host_id: UUID) -> bool:
    """SELECT … FOR UPDATE the build host only if it is ``managed_by='config'``; True if present."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM build_hosts WHERE id = %s AND managed_by = %s FOR UPDATE",
            (host_id, CONFIG_MANAGED_BY),
        )
        return await cur.fetchone() is not None


async def _apply_removed_build_host(conn: AsyncConnection, host_id: UUID) -> str:
    """Cordon (``enabled=false``) a leased host, else delete it. Returns the resulting disposition.

    The lease is checked first under the parent-row ``FOR UPDATE`` already held, so a leased host
    is never blind-deleted into ``ON DELETE RESTRICT``.
    """
    if await _has_live_lease(conn, host_id):
        await conn.execute(
            "UPDATE build_hosts SET enabled = false WHERE id = %s AND enabled", (host_id,)
        )
        return "cordoned"
    await conn.execute("DELETE FROM build_hosts WHERE id = %s", (host_id,))
    return "removed"


async def _has_live_lease(conn: AsyncConnection, host_id: UUID) -> bool:
    """True when the build host holds an in-flight capacity lease (the refuse-if-live guard)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM build_host_leases WHERE build_host_id = %s LIMIT 1", (host_id,)
        )
        return await cur.fetchone() is not None


async def _audit_remove(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    name: str,
    host_id: UUID,
    disposition: str | None = None,
) -> None:
    """Write the build-host remove audit row."""
    args: dict[str, object] = {"name": name, "host_id": str(host_id)}
    if disposition is not None:
        args["disposition"] = disposition
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=REMOVE_TOOL,
            scope=f"build_host:{host_id}",
            args=args,
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
    )
