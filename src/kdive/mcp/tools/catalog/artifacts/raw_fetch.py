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
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.storage import HeadResult
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS
from kdive.db.artifact_queries import (
    RunFetchContext,
    raw_vmcore_key,
    run_fetch_context,
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
    run_uid: UUID,
) -> str | ToolResponse:
    """Authorize and resolve the object key for ``asset``, or return a failure envelope.

    Both assets are gated on the **Run's** project: ``vmlinux`` is the Run's ``debuginfo_ref`` and
    the raw ``vmcore`` is Run-owned (``owner_kind='runs'``, ADR-0244). A bound Run always shares its
    System's project (enforced at ``services/runs/admission.py`` and ``services/runs/bind.py``), so
    gating on ``run.project`` preserves the cross-project isolation ADR-0243's System-project
    re-check provided, with no System indirection. A genuinely-absent asset is a
    ``configuration_error`` with a ``*_unavailable`` reason.
    """
    run_id = str(run_uid)
    require_role(ctx, run.project, Role.CONTRIBUTOR)
    if asset is RawAsset.VMLINUX:
        if run.debuginfo_ref is None:
            return _config_error(run_id, data={"reason": "vmlinux_unavailable"})
        return run.debuginfo_ref
    key = await raw_vmcore_key(conn, run_uid)
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
            resolved = await _resolve_key(conn, ctx, run, asset, uid)
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
                data={"asset": asset.value, "size_bytes": head.size_bytes, "ttl": ttl},
            )
