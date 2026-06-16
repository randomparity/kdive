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
import re
from typing import Annotated

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.build_configs.catalog import get_build_config, upsert_operator_build_config
from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.store.objectstore import ObjectStore

_TOOL = "buildconfig.get"
_SET_TOOL = "buildconfig.set"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_DESCRIPTION_BYTES = 1024

_MERGE_RECIPE = (
    "make defconfig && scripts/kconfig/merge_config.sh -m .config kdump.config "
    "&& make olddefconfig  # then verify every CONFIG_* in kdump.config is present in .config"
)


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
    store: ObjectStore,
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
    when it holds some platform role, the denial is audited.

    Args:
        pool: The async connection pool.
        store: The object store the fragment bytes are published to.
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
        if not _NAME_RE.match(name):
            return ToolResponse.failure(
                name,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=[_SET_TOOL],
                data={"field": "name"},
            )
        data = content.encode("utf-8")
        cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
        if not data or len(data) > cap:
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
        sha256 = hashlib.sha256(data).hexdigest()
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
                    data=data,
                    sensitivity=Sensitivity.REDACTED,
                    retention_class="build-config",
                ),
            )
            await upsert_operator_build_config(conn, name, written.key, sha256, description)
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_SET_TOOL,
                    scope=name,
                    args={"name": name, "sha256": sha256, "bytes": len(data)},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
        return ToolResponse.success(
            name,
            "published",
            suggested_next_actions=[_TOOL],
            data={"name": name, "sha256": sha256, "bytes": len(data), "source": "operator"},
        )


def _resolve_store() -> ObjectStore:
    """Resolve the object store from env; deferred so registration never fails without S3."""
    from kdive.store.objectstore import object_store_from_env

    return object_store_from_env()


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``buildconfig.get`` / ``buildconfig.set`` tools, bound to ``pool``."""
    _store: ObjectStore | None = None

    def _resolved_store() -> ObjectStore:
        nonlocal _store
        if _store is None:
            _store = _resolve_store()
        return _store

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
                return await read_build_config(conn, _resolved_store(), name=name)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc, suggested_next_actions=[_TOOL])

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
                _resolved_store(),
                current_context(),
                name=name,
                content=content,
                description=description,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc, suggested_next_actions=[_SET_TOOL])
