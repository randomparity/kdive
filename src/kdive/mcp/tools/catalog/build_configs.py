"""``buildconfig.get`` read tool: the canonical kdump fragment served inline (ADR-0096).

An agent building a kernel from source can call this tool to retrieve the seeded kdump
fragment, its sha256, and a merge recipe.  Because the fragment is non-sensitive (it
contains only kernel-config options, no secrets), the raw bytes are returned inline.

The tool is read-only and requires only a verified token — no project-scope RBAC is
needed for a shared, operator-seeded catalog resource (the ``images.list`` / ``shapes.list``
precedent: shared-infra reads need only an authenticated caller).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.build_configs.catalog import (
    BuildConfigDeleteOutcome,
    BuildConfigEntry,
    delete_operator_build_config,
    get_build_config,
    list_build_configs,
    upsert_operator_build_config,
)
from kdive.build_configs.defaults import catalog_config_ref
from kdive.build_configs.rules import exceeds_build_config_cap, validate_build_config_name
from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.store.objectstore import ObjectStore, object_store_from_env

_TOOL = "buildconfig.get"
_SET_TOOL = "buildconfig.set"
_LIST_TOOL = "buildconfig.list"
_DELETE_TOOL = "buildconfig.delete"

_MAX_DESCRIPTION_BYTES = 1024

_MERGE_RECIPE = (
    "make defconfig && scripts/kconfig/merge_config.sh -m .config kdump.config "
    "&& make olddefconfig  # then verify every CONFIG_* in kdump.config is present in .config"
)


@dataclass(frozen=True, slots=True)
class _ValidatedBuildConfigInput:
    data: bytes
    sha256: str
    cap: int


def _validate_set_build_config_input(
    name: str, content: str, description: str
) -> _ValidatedBuildConfigInput | ToolResponse:
    try:
        validate_build_config_name(name)
    except ValueError:
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFIGURATION_ERROR,
            suggested_next_actions=[_SET_TOOL],
            data={"field": "name"},
        )
    data = content.encode("utf-8")
    cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
    if not data or exceeds_build_config_cap(data, cap):
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFIGURATION_ERROR,
            suggested_next_actions=[_SET_TOOL],
            data={"field": "content", "limit": cap, "actual": len(data)},
        )
    if len(description.encode("utf-8")) > _MAX_DESCRIPTION_BYTES:
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFIGURATION_ERROR,
            suggested_next_actions=[_SET_TOOL],
            data={"field": "description"},
        )
    return _ValidatedBuildConfigInput(data=data, sha256=hashlib.sha256(data).hexdigest(), cap=cap)


async def read_build_config(
    conn: AsyncConnection,
    store: ObjectStore,
    *,
    name: str,
) -> ToolResponse:
    """Return one fragment's bytes plus digest and merge recipe."""
    entry = await get_build_config(conn, name)
    if entry is None:
        raise CategorizedError(
            f"build-config fragment {name!r} not found in the catalog",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": name},
        )
    fetched = await asyncio.to_thread(store.get_artifact, entry.object_key, None)
    data = fetched.data
    entry.verify_bytes(data)
    return ToolResponse.success(
        entry.name,
        "available",
        data={
            "content": data.decode(),
            "sha256": entry.sha256,
            "source": entry.source,
            "merge_recipe": _MERGE_RECIPE,
            "config_ref": catalog_config_ref(entry.name).model_dump(),
        },
    )


async def set_build_config(
    pool: AsyncConnectionPool,
    store_factory: Callable[[], ObjectStore],
    ctx: RequestContext,
    *,
    name: str,
    content: str,
    description: str,
) -> ToolResponse:
    """Publish or replace an operator build-config fragment.

    The object store is resolved only after platform-admin authorization and input validation.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=_SET_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return ToolResponse.failure(
            name, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_SET_TOOL]
        )
    with bind_context(principal=ctx.principal):
        validated = _validate_set_build_config_input(name, content, description)
        if isinstance(validated, ToolResponse):
            return validated
        store = store_factory()  # resolved only after the authz gate + validation
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.BUILD_CONFIG, name),
        ):
            written = await asyncio.to_thread(
                store.put_artifact,
                ArtifactWriteRequest(
                    tenant="system",
                    owner_kind="build-configs",
                    owner_id=name,
                    name=f"{name}.config",
                    data=validated.data,
                    sensitivity=Sensitivity.REDACTED,
                    retention_class="build-config",
                ),
            )
            await upsert_operator_build_config(
                conn, name, written.key, validated.sha256, description
            )
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_SET_TOOL,
                    scope=name,
                    args={"name": name, "sha256": validated.sha256, "bytes": len(validated.data)},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
        return ToolResponse.success(
            name,
            "published",
            suggested_next_actions=[_TOOL],
            data={
                "name": name,
                "sha256": validated.sha256,
                "bytes": len(validated.data),
                "source": "operator",
                "config_ref": catalog_config_ref(name).model_dump(),
            },
        )


def _entry_envelope(entry: BuildConfigEntry) -> ToolResponse:
    """One catalog row as a sub-envelope: identity + provenance, no fragment bytes."""
    return ToolResponse.success(
        entry.name,
        "ok",
        data={
            "name": entry.name,
            "sha256": entry.sha256,
            "source": entry.source,
            "description": entry.description,
            "config_ref": catalog_config_ref(entry.name).model_dump(),
        },
    )


async def list_build_config_entries(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """List shared build-config metadata for authenticated callers; no fragment bytes."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            entries = await list_build_configs(conn)
        items = [_entry_envelope(entry) for entry in entries]
        return ToolResponse.collection(
            "build-configs",
            "ok",
            items,
            suggested_next_actions=[_TOOL, _SET_TOOL],
        )


async def delete_build_config(
    pool: AsyncConnectionPool, ctx: RequestContext, *, name: str
) -> ToolResponse:
    """Delete an operator-published fragment (``platform_admin``; audited).

    Only ``source='operator'`` rows are removable; seeded/configured rows are refused, and
    object-store bytes are intentionally retained (ADR-0231).
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=_DELETE_TOOL, scope=f"denied:{name}", args={"name": name}
        )
        return ToolResponse.failure(
            name, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_DELETE_TOOL]
        )
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.BUILD_CONFIG, name),
        ):
            outcome, source = await delete_operator_build_config(conn, name)
            if outcome is BuildConfigDeleteOutcome.NOT_FOUND:
                return ToolResponse.failure(
                    name,
                    ErrorCategory.CONFIGURATION_ERROR,
                    suggested_next_actions=[_LIST_TOOL],
                    data={"reason": BuildConfigDeleteOutcome.NOT_FOUND.value, "name": name},
                )
            if outcome is BuildConfigDeleteOutcome.NOT_OPERATOR:
                return ToolResponse.failure(
                    name,
                    ErrorCategory.CONFIGURATION_ERROR,
                    suggested_next_actions=[_LIST_TOOL],
                    data={
                        "reason": BuildConfigDeleteOutcome.NOT_OPERATOR.value,
                        "source": source or "",
                        "name": name,
                    },
                )
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_DELETE_TOOL,
                    scope=name,
                    args={"name": name},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
        return ToolResponse.success(name, "deleted", suggested_next_actions=[_LIST_TOOL, _SET_TOOL])


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    store_factory: Callable[[], ObjectStore] | None = None,
) -> None:
    """Register the ``buildconfig.{get,set,list,delete}`` tools, bound to ``pool``."""
    store_factory = store_factory or object_store_from_env
    _store: ObjectStore | None = None

    def _resolved_store() -> ObjectStore:
        nonlocal _store
        if _store is None:
            _store = store_factory()
        return _store

    _register_buildconfig_get(app, pool, _resolved_store)
    _register_buildconfig_set(app, pool, _resolved_store)
    _register_buildconfig_list(app, pool)
    _register_buildconfig_delete(app, pool)


def _register_buildconfig_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def buildconfig_list_tool() -> ToolResponse:
        """List build-config fragments with name, sha256, source, and description. Auth only."""
        return await list_build_config_entries(pool, current_context())


def _register_buildconfig_delete(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name=_DELETE_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def buildconfig_delete_tool(
        name: Annotated[
            str,
            Field(description="Operator-published fragment name to remove (e.g. kdump)."),
        ],
    ) -> ToolResponse:
        """Delete an operator-published fragment. Requires platform_admin; audited."""
        try:
            return await delete_build_config(pool, current_context(), name=name)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc, suggested_next_actions=[_DELETE_TOOL])


def _register_buildconfig_get(
    app: FastMCP, pool: AsyncConnectionPool, resolved_store: Callable[[], ObjectStore]
) -> None:
    @app.tool(
        name=_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def buildconfig_get_tool(
        name: Annotated[
            str,
            Field(description="The build-config fragment name to retrieve (e.g. kdump)."),
        ],
    ) -> ToolResponse:
        """Fetch a seeded kernel-config fragment inline with sha256 and merge recipe. Auth only."""
        ctx = current_context()
        _ = ctx  # authenticated caller established; no project RBAC for shared catalog
        try:
            async with pool.connection() as conn:
                return await read_build_config(conn, resolved_store(), name=name)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc, suggested_next_actions=[_TOOL])


def _register_buildconfig_set(
    app: FastMCP, pool: AsyncConnectionPool, resolved_store: Callable[[], ObjectStore]
) -> None:
    @app.tool(
        name=_SET_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def buildconfig_set_tool(
        name: Annotated[
            str,
            Field(description="Fragment name (lowercase a-z0-9_-, e.g. kdump)."),
        ],
        content: Annotated[
            str,
            Field(description="The full kernel-config fragment text (UTF-8)."),
        ],
        description: Annotated[
            str,
            Field(description="Optional human label; empty keeps the prior description."),
        ] = "",
    ) -> ToolResponse:
        """Publish/replace a build-config fragment. Requires platform_admin; audited."""
        try:
            return await set_build_config(
                pool,
                resolved_store,
                current_context(),
                name=name,
                content=content,
                description=description,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc, suggested_next_actions=[_SET_TOOL])
