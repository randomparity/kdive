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
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Protocol, cast
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.catalog.read_model import (
    RunFetchContext,
    fetch_context_for_run,
    raw_pcap_key,
)
from kdive.artifacts.storage import HeadResult
from kdive.artifacts.vmcore_retention import (
    await_vmcore_io,
    protect_vmcore_url,
    published_vmcore,
    require_vmcore_available,
    vmcore_retention_view,
)
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
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
    PCAP = "pcap"


class _RawStore(Protocol):
    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None: ...
    def presign_get(self, key: str, *, expires_in: int, version_id: str | None = None) -> str: ...


_DEFAULT_STORE_FACTORY = cast("Callable[[], _RawStore]", object_store_from_env)


@dataclass(frozen=True, slots=True)
class _ResolvedRaw:
    key: str
    version_id: str | None = None


async def _resolve_key(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: RunFetchContext,
    asset: RawAsset,
    run_uid: UUID,
    artifact_id: str | None,
) -> _ResolvedRaw | ToolResponse:
    """Authorize and resolve the object key for ``asset``, or return a failure envelope.

    All assets are gated on the **Run's** project: ``vmlinux`` is the Run's ``debuginfo_ref``, the
    raw ``vmcore`` is Run-owned (``owner_kind='runs'``, ADR-0244), and a ``pcap`` is Run-owned
    (ADR-0385) — selected by ``artifact_id`` (validated to belong to the Run) or the newest when
    omitted. A bound Run always shares its System's project (enforced at
    ``services/runs/admission.py`` and ``services/runs/bind.py``), so gating on ``run.project``
    preserves the cross-project isolation ADR-0243's System-project re-check provided, with no
    System indirection. A genuinely-absent asset is a ``configuration_error`` with a
    ``*_unavailable`` reason.
    """
    run_id = str(run_uid)
    require_role(ctx, run.project, Role.CONTRIBUTOR)
    if asset is RawAsset.VMLINUX:
        if run.debuginfo_ref is None:
            return _config_error(run_id, data={"reason": "vmlinux_unavailable"})
        if run.reusable_build and run.debuginfo_version_id is None:
            return ToolResponse.failure_from_error(
                run_id,
                CategorizedError(
                    "reusable vmlinux is missing its immutable object version",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                ),
            )
        return _ResolvedRaw(run.debuginfo_ref, run.debuginfo_version_id)
    if asset is RawAsset.PCAP:
        aid = _as_uuid(artifact_id) if artifact_id is not None else None
        if artifact_id is not None and aid is None:
            return _config_error(run_id, data={"reason": "invalid_artifact_id"})
        pcap_key = await raw_pcap_key(conn, run_uid, aid)
        if pcap_key is None:
            return _config_error(run_id, data={"reason": "pcap_unavailable"})
        return _ResolvedRaw(pcap_key)
    raise ValueError("vmcore reads require the protected Run transaction")


async def fetch_raw(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    asset: RawAsset,
    artifact_id: str | None = None,
    store_factory: Callable[[], _RawStore] = _DEFAULT_STORE_FACTORY,
) -> ToolResponse:
    """Mint a presigned download URL for a Run's raw ``vmcore``, ``vmlinux``, or ``pcap``.

    Resolves the asset's object key from existing row data, HEADs the object to confirm it exists,
    presigns a short-lived download URL (``KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS``), and audits the
    egress. Returns the URL under ``refs.download_uri`` with ``data.asset``/``data.size_bytes``;
    never returns inline bytes. ``artifact_id`` selects a specific ``pcap`` (a Run may own several;
    ADR-0385) and is ignored for ``vmcore``/``vmlinux``. Requires ``contributor`` on the asset's
    owning project.
    """
    if asset is RawAsset.VMCORE:
        with bind_context(principal=ctx.principal):
            return await _fetch_vmcore_raw(pool, ctx, run_id=run_id, store_factory=store_factory)
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await fetch_context_for_run(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            resolved = await _resolve_key(conn, ctx, run, asset, uid, artifact_id)
            if isinstance(resolved, ToolResponse):
                return resolved
            try:
                store = store_factory()
                if resolved.version_id is None:
                    head = await asyncio.to_thread(store.head, resolved.key)
                else:
                    head = await asyncio.to_thread(
                        store.head, resolved.key, version_id=resolved.version_id
                    )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(run_id, exc)
            if head is None:
                return _config_error(run_id, data={"reason": f"{asset.value}_unavailable"})
            ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
            try:
                if resolved.version_id is None:
                    url = await asyncio.to_thread(store.presign_get, resolved.key, expires_in=ttl)
                else:
                    url = await asyncio.to_thread(
                        store.presign_get,
                        resolved.key,
                        expires_in=ttl,
                        version_id=resolved.version_id,
                    )
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
                        args={"run_id": run_id, "asset": asset.value}
                        | ({"artifact_id": artifact_id} if artifact_id is not None else {}),
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


async def _fetch_vmcore_raw(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    store_factory: Callable[[], _RawStore],
) -> ToolResponse:
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    try:
        async with pool.connection() as conn, conn.transaction():
            run = await fetch_context_for_run(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _not_found(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            async with advisory_xact_lock(conn, LockScope.RUN, uid):
                await require_vmcore_available(conn, uid)
                execution = await published_vmcore(conn, uid)
                if execution is None:
                    return _config_error(run_id, data={"reason": "vmcore_unavailable"})
                store = store_factory()
                head = await await_vmcore_io(
                    conn,
                    partial(
                        store.head,
                        execution.raw_key,
                        version_id=execution.raw_version_id,
                    ),
                )
                if head is None:
                    raise CategorizedError(
                        "published vmcore version is missing",
                        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    )
                ttl = config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
                url = await await_vmcore_io(
                    conn,
                    partial(
                        store.presign_get,
                        execution.raw_key,
                        expires_in=ttl,
                        version_id=execution.raw_version_id,
                    ),
                )
                server_time, expires_at = await protect_vmcore_url(conn, uid, url)
                retention = await vmcore_retention_view(conn, uid)
                await audit.record(
                    conn,
                    ctx,
                    audit.AuditEvent(
                        tool="artifacts.fetch_raw",
                        object_kind="runs",
                        object_id=uid,
                        transition="fetch_raw",
                        args={"run_id": run_id, "asset": "vmcore"},
                        project=run.project,
                    ),
                )
                response = ToolResponse.success(
                    run_id,
                    "available",
                    suggested_next_actions=["artifacts.fetch_raw"],
                    refs={"download_uri": url},
                    data={
                        "asset": "vmcore",
                        "size_bytes": head.size_bytes,
                        "ttl": ttl,
                        "server_time": server_time.isoformat(),
                        "download_expires_at": expires_at.isoformat(),
                        "vmcore_retention": retention,
                    },
                )
        return response
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(
            run_id,
            exc,
            suggested_next_actions=["runs.get"]
            if exc.details.get("reason") == "vmcore_retired"
            else None,
        )
    except psycopg.Error:
        return ToolResponse.failure_from_error(
            run_id,
            CategorizedError(
                "vmcore read lost database authority; retry the read",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ),
        )
