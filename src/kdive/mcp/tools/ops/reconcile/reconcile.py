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

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.providers.infra.reaping import (
    CaptureReaper,
    DumpVolumeReaper,
    InfraReaper,
    NullDumpVolumeReaper,
)
from kdive.reconciler.cleanup.system_object_versions import (
    MAX_TARGETS_PER_KEY,
    MAX_TARGETS_PER_LANE,
)
from kdive.reconciler.loop import (
    ALL_REPAIR_KINDS,
    ReconcileConfig,
    ReconcileReport,
    ReconcileUploadStore,
    reconcile_once,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.serialization import JsonValue
from kdive.services.images.retention import ImageSweepStore

# A module-level singleton so it can be a stateless default arg (ruff B008).
_NULL_DUMP_VOLUME_REAPER: DumpVolumeReaper = NullDumpVolumeReaper()

# No capture reaper by default: an empty registry keeps every kind out of selection (ADR-0556).
_NO_CAPTURE_REAPERS: Mapping[str, CaptureReaper] = MappingProxyType({})

_RECONCILE_TOOL = "ops.reconcile_now"
_RECONCILE_OBJECT_ID = "reconcile"
# A control action over every project, not one project/object (ADR-0062 §reconcile).
_RECONCILE_SCOPE = "all-projects"


def _with_system_object_limits(
    func: Callable[[], Awaitable[ToolResponse]],
) -> Callable[[], Awaitable[ToolResponse]]:
    """Interpolate enforced sweep limits before FastMCP reads the wrapper docstring."""
    if func.__doc__ is None:
        raise AssertionError("ops.reconcile_now wrapper must have a docstring")
    func.__doc__ = func.__doc__.format(
        max_targets_per_lane=MAX_TARGETS_PER_LANE,
        max_targets_per_key=MAX_TARGETS_PER_KEY,
    )
    return func


@dataclass(frozen=True, slots=True)
class ReconcileRepairPorts:
    """Repair dependencies used by one on-demand reconcile pass."""

    reaper: InfraReaper
    upload_store: ReconcileUploadStore
    image_store: ImageSweepStore
    dump_volume_reaper: DumpVolumeReaper = _NULL_DUMP_VOLUME_REAPER
    #: ``Resource kind -> CaptureReaper`` for the ADR-0556 capture sweep. Threaded through so an
    #: on-demand pass runs the same lanes as the periodic loop; a kind wired ``NullCaptureReaper``
    #: is excluded from selection either way.
    capture_reapers: Mapping[str, CaptureReaper] = _NO_CAPTURE_REAPERS


async def reconcile_now(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    ports: ReconcileRepairPorts,
) -> ToolResponse:
    """Run one on-demand reconcile pass and return per-repair counts.

    The on-demand config includes local System-object version cleanup but deliberately has no
    console-hosting gate, so the remote System-object lane is absent.
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
            return ToolResponse.denied(
                _RECONCILE_OBJECT_ID, missing_roles=[PlatformRole.PLATFORM_OPERATOR]
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
                capture_reapers=ports.capture_reapers,
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
    repair_counts: dict[str, JsonValue] = {
        repair_kind: report.repair_counts[repair_kind] for repair_kind in ALL_REPAIR_KINDS
    }
    data: dict[str, JsonValue] = {
        "repair_counts": repair_counts,
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
        "reaped_captures": report.reaped_captures,
        "failures": ",".join(report.failures),
    }
    return ToolResponse.success(
        _RECONCILE_OBJECT_ID,
        "ok",
        suggested_next_actions=["ops.reconcile_now"],
        data=data,
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
    @_with_system_object_limits
    async def ops_reconcile_now() -> ToolResponse:
        """Run reconciler cleanup once (platform_operator).

        Repairs runtime drift such as expired leases and orphaned allocations. Among its repairs,
        the local System-object sweep can permanently delete at most {max_targets_per_lane} rowless
        local System object version identities per call and at most {max_targets_per_key} per exact
        key. It first takes the System lock and confirms a present gone System and that no artifact
        row names the exact key, then releases the database transaction before the object-store
        deletion. If eligible versions remain, call `ops.reconcile_now` again. Confirmed deletions
        appear in
        `data.repair_counts.local_system_object_versions_deleted`.

        The remote System-object version lane is skipped because this on-demand configuration has
        no console-hosting gate; that lane runs only in the periodic reconciler. Returns
        `data.repair_counts`, keyed by every cataloged repair kind, plus the human-readable scalar
        summary fields and comma-joined `data.failures`.

        This does not reconcile `systems.toml` into the catalog. For that pass, which can prune
        rows and free their object-store bytes, use `ops.reconcile_systems`.
        """
        return await reconcile_now(
            pool,
            current_context(),
            ports=ports,
        )
