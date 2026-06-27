"""Full inventory export and writeback MCP tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.config.core_settings import INVENTORY_WRITEBACK, MAX_BUILD_CONFIG_BYTES
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.serialize import read_inventory_snapshot, serialize_inventory
from kdive.inventory.writeback import (
    WritebackTarget,
    assert_persistable,
    resolve_writeback_target,
)
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.serialization import JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable

    from kdive.security.authz.context import RequestContext

_EXPORT_SYSTEMS_OBJECT_ID = "systems_toml_export"
_EXPORT_SYSTEMS_TOOL = "ops.export_systems_toml"
_EXPORT_SYSTEMS_SCOPE = "all-inventory"
_WRITEBACK_SYSTEMS_SCOPE = "all-inventory-writeback"


async def export_systems_toml(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    persist: bool = False,
    document: str | None = None,
    resolve_target: Callable[[], WritebackTarget | None] = resolve_writeback_target,
) -> ToolResponse:
    """Serialize live inventory to ``systems.toml``, optionally persisting it.

    ``persist=True`` requires configured writeback and refuses unresolved placeholders before any
    write.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool, ctx, tool=_EXPORT_SYSTEMS_TOOL, scope=_EXPORT_SYSTEMS_SCOPE
            )
            return _denied(_EXPORT_SYSTEMS_OBJECT_ID, _EXPORT_SYSTEMS_TOOL)
        if document is not None and not persist:
            return _export_config_error(
                "a document was supplied without persist=true; nothing would store it"
            )
        async with pool.connection() as conn:
            snapshot = await read_inventory_snapshot(conn)
            toml = serialize_inventory(snapshot)
            if not persist:
                await _audit_inventory_read(conn, ctx)
                return _export_ok(toml)
            return await _persist_export(conn, ctx, toml, document, resolve_target)


async def _persist_export(
    conn: AsyncConnection,
    ctx: RequestContext,
    toml: str,
    document: str | None,
    resolve_target: Callable[[], WritebackTarget | None],
) -> ToolResponse:
    """Validate, write, and audit a ``persist=True`` export; return the outcome envelope."""
    to_write = document if document is not None else toml
    target = resolve_target()
    if target is None:
        return _export_config_error(
            f"writeback is disabled; set {INVENTORY_WRITEBACK.name} to one of "
            f"configmap/file to persist (off by default)"
        )
    try:
        _bound_document(to_write)
        assert_persistable(to_write)
        await target.write(to_write)
    except CategorizedError as exc:
        await _audit_inventory_write(conn, ctx, target.target_kind, outcome="failed")
        return ToolResponse.failure_from_error(
            _EXPORT_SYSTEMS_OBJECT_ID, exc, suggested_next_actions=[_EXPORT_SYSTEMS_TOOL]
        )
    await _audit_inventory_write(conn, ctx, target.target_kind, outcome="applied")
    return _export_ok(toml, persisted=True, target=target.target_kind)


def _bound_document(text: str) -> None:
    """Reject a document past the inventory file size cap."""
    cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
    size = len(text.encode("utf-8"))
    if size > cap:
        raise CategorizedError(
            f"document is {size} bytes, over the {MAX_BUILD_CONFIG_BYTES.name} cap ({cap} bytes)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"variable": MAX_BUILD_CONFIG_BYTES.name, "size": size, "cap": cap},
        )


def _export_ok(toml: str, *, persisted: bool = False, target: str | None = None) -> ToolResponse:
    data: dict[str, JsonValue] = {"toml": toml}
    if persisted:
        data["persisted"] = True
        data["target"] = target
    return ToolResponse.success(
        _EXPORT_SYSTEMS_OBJECT_ID,
        "ok",
        suggested_next_actions=[_EXPORT_SYSTEMS_TOOL],
        data=data,
    )


def _export_config_error(message: str) -> ToolResponse:
    return ToolResponse.failure(
        _EXPORT_SYSTEMS_OBJECT_ID,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=message,
        suggested_next_actions=[_EXPORT_SYSTEMS_TOOL],
    )


async def _audit_inventory_read(conn: AsyncConnection, ctx: RequestContext) -> None:
    """Audit the full-inventory export read to ``platform_audit_log``."""
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_EXPORT_SYSTEMS_TOOL,
                scope=_EXPORT_SYSTEMS_SCOPE,
                args={"tool": _EXPORT_SYSTEMS_TOOL},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


async def _audit_inventory_write(
    conn: AsyncConnection, ctx: RequestContext, target: str, *, outcome: str
) -> None:
    """Audit a writeback attempt (applied or failed) to ``platform_audit_log``."""
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_EXPORT_SYSTEMS_TOOL,
                scope=_WRITEBACK_SYSTEMS_SCOPE,
                args={
                    "tool": _EXPORT_SYSTEMS_TOOL,
                    "persist": True,
                    "target": target,
                    "outcome": outcome,
                },
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the full-inventory export tool on ``app``, bound to ``pool``."""

    @app.tool(
        name=_EXPORT_SYSTEMS_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_export_systems_toml(
        persist: Annotated[
            bool,
            Field(
                description=(
                    "When true, also persist the inventory to the configured writeback target "
                    "(KDIVE_INVENTORY_WRITEBACK; off by default). A skeleton export is refused."
                )
            ),
        ] = False,
        document: Annotated[
            str | None,
            Field(
                description=(
                    "A completed systems.toml to persist verbatim instead of the live "
                    "serialization; required for a fleet with remote_libvirt hosts whose export "
                    "is a skeleton. Requires persist=true."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Export the live inventory; optionally persist it to the writeback target. Operator."""
        return await export_systems_toml(
            pool, current_context(), persist=persist, document=document
        )
