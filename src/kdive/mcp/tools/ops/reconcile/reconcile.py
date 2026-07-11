"""On-demand reconcile MCP tool (``ops.reconcile_now``) — ADR-0062 §reconcile.

``ops.reconcile_now`` runs one :func:`kdive.reconciler.loop.reconcile_once` pass on
demand and returns its per-class repair summary. It calls the **same** ``reconcile_once``
the periodic loop runs (:mod:`kdive.reconciler.loop`), so it inherits that pass's
per-Project / per-Allocation / per-System ``advisory_xact_lock`` discipline unchanged:
there is no second, lock-free repair path. An on-demand pass and a concurrent periodic
pass therefore serialize on the same advisory locks and cannot double-act on one object.
It does **not** stop or restart the periodic loop — it triggers one extra pass.

Gated ``platform_operator`` (a cross-project control action) and audited to
``platform_audit_log``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.providers.infra.reaping import (
    DumpVolumeReaper,
    InfraReaper,
    NullDumpVolumeReaper,
)
from kdive.reconciler.loop import ReconcileConfig, ReconcileReport, UploadStore, reconcile_once
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.services.images.retention import ImageSweepStore

# A module-level singleton so it can be a stateless default arg (ruff B008).
_NULL_DUMP_VOLUME_REAPER: DumpVolumeReaper = NullDumpVolumeReaper()

_RECONCILE_TOOL = "ops.reconcile_now"
_RECONCILE_OBJECT_ID = "reconcile"
# A control action over every project, not one project/object (ADR-0062 §reconcile).
_RECONCILE_SCOPE = "all-projects"


@dataclass(frozen=True, slots=True)
class ReconcileRepairPorts:
    """Repair dependencies used by one on-demand reconcile pass."""

    reaper: InfraReaper
    upload_store: UploadStore | None
    image_store: ImageSweepStore | None = None
    dump_volume_reaper: DumpVolumeReaper = _NULL_DUMP_VOLUME_REAPER


async def reconcile_now(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    ports: ReconcileRepairPorts,
) -> ToolResponse:
    """Run one on-demand reconcile pass and return per-repair counts.

    Denials are audited before repair dependencies touch the database.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_RECONCILE_TOOL,
                scope=_RECONCILE_SCOPE,
                args={"tool": _RECONCILE_TOOL},
            )
            return ToolResponse.failure(
                _RECONCILE_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[_RECONCILE_TOOL],
            )
        # reconcile_once isolates every per-repair failure into report.failures and does
        # not re-raise it, so there is no CategorizedError to catch here; a rare whole-pass
        # error (e.g. pool acquisition) propagates, matching the periodic loop's contract.
        report = await reconcile_once(
            pool,
            ports.reaper,
            config=ReconcileConfig(
                upload_store=ports.upload_store,
                image_store=ports.image_store,
                dump_volume_reaper=ports.dump_volume_reaper,
            ),
        )
        async with pool.connection() as conn, conn.transaction():
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_RECONCILE_TOOL,
                    scope=_RECONCILE_SCOPE,
                    args={"tool": _RECONCILE_TOOL},
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
        return _reconcile_response(report)


def _reconcile_response(report: ReconcileReport) -> ToolResponse:
    """Render a :class:`ReconcileReport` as a per-class summary ``ToolResponse``."""
    return ToolResponse.success(
        _RECONCILE_OBJECT_ID,
        "ok",
        suggested_next_actions=["ops.reconcile_now"],
        data={
            "expired_allocations": report.expired_allocations,
            "reaped_active_allocations": report.reaped_active_allocations,
            "promoted_allocations": report.promoted_allocations,
            "queue_timeouts": report.queue_timeouts,
            "orphaned_systems": report.orphaned_systems,
            "abandoned_jobs": report.abandoned_jobs,
            "dead_sessions": report.dead_sessions,
            "leaked_domains": report.leaked_domains,
            "idempotency_keys_gc_count": report.idempotency_keys_gc_count,
            "abandoned_uploads": report.abandoned_uploads,
            "reconciled_inventory": report.reconciled_inventory,
            "leaked_images": report.leaked_images,
            "dangling_images": report.dangling_images,
            "expired_private_images": report.expired_private_images,
            "reaped_dump_volumes": report.reaped_dump_volumes,
            "failures": ",".join(report.failures),
        },
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    ports: ReconcileRepairPorts,
) -> None:
    """Register ``ops.reconcile_now`` with one assembled repair-port bundle."""

    @app.tool(
        name=_RECONCILE_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_reconcile_now() -> ToolResponse:
        """Run reconciler cleanup once."""
        return await reconcile_now(
            pool,
            current_context(),
            ports=ports,
        )
