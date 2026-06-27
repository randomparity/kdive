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
    """Fetch the seeded build-config fragment by name, returning bytes, sha256, and merge recipe.

    Args:
        conn: An open async psycopg connection.
        store: The object store that holds the fragment bytes.
        name: The fragment name to retrieve (e.g. ``"kdump"``).

    Returns:
        A :class:`ToolResponse` carrying ``content`` (the raw fragment text),
        ``sha256`` (the catalog digest), and ``merge_recipe`` (the ``merge_config.sh``
        invocation to apply the fragment onto a defconfig).

    Raises:
        CategorizedError: CONFIGURATION_ERROR when ``name`` is unknown.
    """
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
        name,
        "available",
        data={
            "content": data.decode(),
            "sha256": entry.sha256,
            "source": entry.source,
            "merge_recipe": _MERGE_RECIPE,
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
    """Publish/replace a build-config fragment (``platform_admin``; ADR-0119).

    Serialized per fragment ``name`` on :attr:`LockScope.BUILD_CONFIG` (the same lock the seed
    takes); the object PUT, the catalog upsert (``source='operator'``), and the
    ``platform_audit_log`` row commit together. A non-``platform_admin`` caller is denied and,
    when it holds some platform role, the denial is audited. The object store is resolved
    (``store_factory``) **after** the authorization gate, so a denied caller never triggers — or
    learns about — object-store configuration state and is always audited.

    Args:
        pool: The async connection pool.
        store_factory: Resolves the object store; called only after the authz gate passes, so a
            store-resolution failure is reachable only by an authorized caller.
        ctx: The caller's request context.
        name: The fragment name (lowercase ``a-z0-9_-``; folds into the object key).
        content: The full kernel-config fragment text (UTF-8).
        description: An optional human label; empty preserves the prior description.

    Returns:
        A :class:`ToolResponse`: ``published`` with ``{name, sha256, bytes, source}`` on success,
        or a failure envelope (``AUTHORIZATION_DENIED`` / ``CONFIGURATION_ERROR``).
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
        },
    )


async def list_build_config_entries(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """List every build-config fragment as a sorted catalog index (authenticated; no RBAC).

    Returns identity + provenance (``name``, ``sha256``, ``source``, ``description``) per row,
    never the fragment bytes — ``buildconfig.get`` serves those by name. An empty catalog is an
    empty ``ok`` collection. The catalog is shared, non-sensitive infra, so any authenticated
    caller may read it (the ``buildconfig.get`` / ``images.list`` / ``shapes.list`` precedent).

    Args:
        pool: The async connection pool.
        ctx: The caller's request context (authenticated; no project scope needed).

    Returns:
        A collection :class:`ToolResponse` of per-row sub-envelopes, sorted by ``name``.
    """
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

    Mirrors :func:`set_build_config`'s gate exactly: a non-``platform_admin`` caller is denied
    and, when it holds some platform role, the denial is audited. Serialized per ``name`` on
    :attr:`LockScope.BUILD_CONFIG` (the same lock ``set``/seed take), so the source-scoped
    delete and its provenance-for-reason read cannot interleave with a concurrent ``set``.
    Removes only a ``source='operator'`` row; a ``seed``/``config`` row is refused with
    ``CONFIGURATION_ERROR`` + ``data.reason='not_operator_source'`` (a ``config`` row is
    re-asserted by the reconcile pass; a ``seed`` is the packaged baseline), and a missing name
    is ``CONFIGURATION_ERROR`` + ``data.reason='not_found'``. Only a successful removal writes a
    success audit row. The fragment's object-store bytes are left in place (ADR-0231).

    Args:
        pool: The async connection pool.
        ctx: The caller's request context.
        name: The fragment name to remove.

    Returns:
        A ``deleted`` :class:`ToolResponse` on success, or a failure envelope
        (``AUTHORIZATION_DENIED`` / ``CONFIGURATION_ERROR``).
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
