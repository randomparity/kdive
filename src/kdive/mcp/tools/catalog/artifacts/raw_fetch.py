"""Owner-fetchable raw vmcore + vmlinux egress (`artifacts.fetch_raw`, ADR-0243).

Mints a presigned download URL for a Run's raw debug asset — its ``vmlinux`` debuginfo or the
raw ``vmcore`` of the System it booted — gated by project membership plus the ``contributor``
role on the **asset's own** owning project, not by sensitivity. The raw objects stay
``SENSITIVE``; the closed ``RawAsset`` enum is the egress allow-list. URL-only (these are
multi-GB binaries); the ``REDACTED``-only inline/search gate on ``artifacts.get`` is unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.storage import HeadResult
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS
from kdive.db.artifact_queries import (
    RunFetchContext,
    raw_vmcore_key,
    run_fetch_context,
    system_project,
)
from kdive.domain.errors import CategorizedError
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.store.objectstore import object_store_from_env


class RawAsset(StrEnum):
    """The owner-fetchable raw debug assets — the closed egress allow-list (ADR-0243)."""

    VMCORE = "vmcore"
    VMLINUX = "vmlinux"


class _RawStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def presign_get(self, key: str, *, expires_in: int) -> str: ...


async def _resolve_key(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: RunFetchContext,
    asset: RawAsset,
    run_id: str,
) -> str | ToolResponse:
    """Authorize and resolve the object key for ``asset``, or return a failure envelope.

    Each asset is gated on its **own** owning project: ``vmlinux`` on the Run's project, the raw
    ``vmcore`` on the System's project (the core is the System's asset). A genuinely-absent asset
    in the caller's own project is a ``configuration_error`` with a ``*_unavailable`` reason; a
    cross-project System masks existence as ``not_found``.
    """
    if asset is RawAsset.VMLINUX:
        require_role(ctx, run.project, Role.CONTRIBUTOR)
        if run.debuginfo_ref is None:
            return _config_error(run_id, data={"reason": "vmlinux_unavailable"})
        return run.debuginfo_ref
    if run.system_id is None:
        return _config_error(run_id, data={"reason": "vmcore_unavailable"})
    sysproj = await system_project(conn, run.system_id)
    if sysproj is None or sysproj not in ctx.projects:
        return _not_found(run_id)
    require_role(ctx, sysproj, Role.CONTRIBUTOR)
    key = await raw_vmcore_key(conn, run.system_id)
    if key is None:
        return _config_error(run_id, data={"reason": "vmcore_unavailable"})
    return key


async def fetch_raw(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    asset: RawAsset,
    store_factory: Callable[[], _RawStore] = object_store_from_env,
) -> ToolResponse:
    """Mint a presigned download URL for a Run's raw ``vmcore`` or ``vmlinux`` (ADR-0243).

    Resolves the asset's object key from existing row data, HEADs the object to confirm it exists,
    presigns a short-lived download URL (``KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS``), and audits the
    egress. Returns the URL under ``refs.download_uri`` with ``data.asset``/``data.size_bytes``;
    never returns inline bytes. Requires ``contributor`` on the asset's owning project.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await run_fetch_context(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            resolved = await _resolve_key(conn, ctx, run, asset, run_id)
            if isinstance(resolved, ToolResponse):
                return resolved
            try:
                store = store_factory()
                head = await asyncio.to_thread(store.head, resolved)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(run_id, exc)
            if head is None:
                return _config_error(run_id, data={"reason": f"{asset.value}_unavailable"})
            ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
            try:
                url = await asyncio.to_thread(store.presign_get, resolved, expires_in=ttl)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(run_id, exc)
            async with conn.transaction():
                await audit.record(
                    conn,
                    ctx,
                    audit.AuditEvent(
                        tool="artifacts.fetch_raw",
                        object_kind="runs",
                        object_id=uid,
                        transition="fetch_raw",
                        args={"run_id": run_id, "asset": asset.value},
                        project=run.project,
                    ),
                )
            return ToolResponse.success(
                run_id,
                "available",
                suggested_next_actions=["artifacts.fetch_raw"],
                refs={"download_uri": url},
                data={"asset": asset.value, "size_bytes": str(head.size_bytes), "ttl": str(ttl)},
            )
