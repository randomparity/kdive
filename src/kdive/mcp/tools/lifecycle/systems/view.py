"""Read-only `systems.*` MCP handlers (ADR-0025, ADR-0070)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.sql import Composable
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory, suppressed_detail
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job
from kdive.domain.pcie import parse_match_spec
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT, ConfigErrorReason, InvalidCursor
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import clamp_list_limit as _clamp_list_limit
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import decode_ts_uuid_cursor as _decode_ts_uuid_cursor
from kdive.mcp.tools._common import encode_ts_uuid_cursor as _encode_ts_uuid_cursor
from kdive.mcp.tools._common import invalid_cursor_error as _invalid_cursor_error
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools._common import paginate as _paginate
from kdive.mcp.tools.lifecycle._recovery import iso, provisioning_profile_summary
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.debug.sessions import active_session_ids_for_system

CUSTOM_SHAPE_SENTINEL = "__custom__"
"""The ``shape`` filter value selecting full-custom Systems (``shape IS NULL``)."""

NO_JOB_SYSTEM_FAILURE_DETAIL = "System failed with no job recording a reason"
"""``detail`` for a ``failed`` System whose failing job recorded no reason (ADR-0454 §3).

Used when no job could be attributed at all, and — the commoner case — when the attributed job
carries an empty ``failure_context``. Names the condition and no project, host, or id, so it
carries no resource-existence signal.
"""

ABANDONED_JOB_SYSTEM_FAILURE_DETAIL = (
    "System failed while its job was abandoned: the job's lease expired before it recorded a "
    "reason, so the original failure reason was not retained"
)
"""``detail`` for a ``failed`` System attributed to a lease-expired job (ADR-0454 §3).

``repair_abandoned_jobs`` dead-letters a zombie job with ``lease_expired`` and an untouched
(empty) ``failure_context``, so this is what a System failed by a worker that died — or whose
``queue.fail`` never landed — actually looks like. Saying so beats a bare ``lease_expired``,
which reads as a verdict on the System rather than on the bookkeeping. Resource-free.
"""

_SYSTEM_FAILURE_DETAIL: dict[ErrorCategory, str] = {
    ErrorCategory.LEASE_EXPIRED: ABANDONED_JOB_SYSTEM_FAILURE_DETAIL,
}
"""Category-derived reasons for a ``failed`` System whose job recorded no message.

Only ``lease_expired`` earns its own string: it is the one category reachable *without* a
handler having written a reason (the reconciler stamps it), so it is the one whose bare form
would actively mislead. Every other category arrives from a handler that normally supplies a
message, and falls back to :data:`NO_JOB_SYSTEM_FAILURE_DETAIL`.
"""

FAILED_SYSTEM_NEXT_ACTIONS = ["allocations.release", "allocations.request"]
"""Recovery actions for a ``failed`` System (ADR-0454 §5, matching ADR-0149's guidance).

``SystemState.FAILED`` is terminal — its outbound transition set is empty — so there is no
System-scoped tool to retry with, whatever ``retryable`` says. The way forward is the one
``systems.provision`` already names on this System: release the allocation and request a fresh
one. Static and unfiltered, like the success path's list in the same function.
"""


@dataclass(frozen=True, slots=True)
class SystemsListRequest:
    """Filter payload for ``systems.list``."""

    allocation_id: str | None = None
    state: str | None = None
    shape: str | None = None
    pcie: str | None = None
    limit: int = DEFAULT_LIST_LIMIT
    cursor: str | None = None


def system_envelope(
    system: System,
    *,
    resource_kind: str | None = None,
    resource_id: str | None = None,
    active_debug_session_ids: list[str] | None = None,
    active_run: dict[str, JsonValue] | None = None,
    supports_snapshots: bool | None = None,
    supports_traffic_capture: bool | None = None,
    failing_job: Job | None = None,
) -> ToolResponse:
    """Render a System with recovery context; ``failed`` becomes a failure envelope.

    ``resource_kind``/``resource_id`` are the backing Resource and the granted resource id
    (ADR-0169/0180). The provisioning summary, ``allocation_id``, ``shape``, and timestamps
    come from the System row (no extra query, both paths). ``active_run`` and
    ``active_debug_session_ids`` are get-only (an N+1 on the list path), omitted otherwise.

    ``failing_job`` is the job that put the System in ``failed`` (ADR-0454); the failure
    envelope reports its category and reason instead of assuming one for every ``failed``
    System. Like the other get-only arguments it is a per-row query, so ``systems.list`` passes
    none and keeps the ``infrastructure_failure`` default (ADR-0454 §4).
    """
    data: dict[str, JsonValue] = {
        "project": system.project,
        "allocation_id": str(system.allocation_id),
        "shape": system.shape,
        "label": system.label,
        # Host-derived accelerator resolved at admission (ADR-0339); null when not host-derived.
        "accel": system.accel,
        "created_at": iso(system.created_at),
        "updated_at": iso(system.updated_at),
        **provisioning_profile_summary(system.provisioning_profile),
    }
    # The CPU baseline the System was minted against (ADR-0368); omitted when the host advertised
    # none (local/fault/un-refreshed remote). A cheap row read — systems.get makes no libvirt call.
    if system.resolved_cpu is not None:
        data["resolved_cpu"] = system.resolved_cpu
    if resource_kind is not None:
        data["resource_kind"] = resource_kind
    if resource_id is not None:
        data["resource_id"] = resource_id
    if active_debug_session_ids is not None:
        data["active_debug_session_ids"] = list(active_debug_session_ids)
    if active_run is not None:
        data["active_run"] = active_run
    if supports_snapshots is not None:
        data["supports_snapshots"] = supports_snapshots
    if supports_traffic_capture is not None:
        data["supports_traffic_capture"] = supports_traffic_capture
    if system.state is SystemState.FAILED:
        return _failed_system_envelope(system, failing_job, data)
    return ToolResponse.success(
        str(system.id),
        system.state.value,
        suggested_next_actions=["systems.get", "systems.teardown"],
        data=data,
    )


def _failed_system_envelope(
    system: System, failing_job: Job | None, data: dict[str, JsonValue]
) -> ToolResponse:
    """Build the ``failed`` System envelope from its failing job's verdict (ADR-0454).

    The category is the job's, defaulting to ``infrastructure_failure`` only when there is no
    job to attribute or the job never recorded one.

    ``detail`` is derived from the **resolved reason**, not from the presence of a job: a job
    with an empty ``failure_context`` is as reasonless as no job at all, and is in fact the
    likelier shape — ``repair_abandoned_jobs`` dead-letters a zombie job with ``lease_expired``
    and an untouched context, and the System-failing reconciler repairs run after it. Gating the
    fallback on ``failing_job is None`` would leave that path a bare category, which is the
    surface #1550 set out to remove.

    The job-derived surface (``detail``, ``failing_job_id``) is suppressed entirely for a
    no-leak category (ADR-0123): ``ToolResponse.failure`` already suppresses ``detail``, but the
    ``data`` extra bypasses that seam, so it is gated here on the same rule.
    ``suppressed_detail(category, None) is not None`` is true exactly for a suppressed category
    (it returns the fixed constant even when ``raw`` is ``None``). The structured
    ``failure_detail_*`` keys are deliberately left on ``jobs.get`` rather than duplicated here;
    ``failing_job_id`` is the pointer to them.
    """
    category = (failing_job.error_category if failing_job else None) or (
        ErrorCategory.INFRASTRUCTURE_FAILURE
    )
    failure_data: dict[str, JsonValue] = {"current_status": system.state.value, **data}
    detail = failing_job.failure_context.get("failure_message") if failing_job else None
    if not detail:
        detail = _SYSTEM_FAILURE_DETAIL.get(category, NO_JOB_SYSTEM_FAILURE_DETAIL)
    if failing_job is not None and suppressed_detail(category, None) is None:
        failure_data["failing_job_id"] = str(failing_job.id)
    return ToolResponse.failure(
        str(system.id),
        category,
        detail=detail,
        suggested_next_actions=list(FAILED_SYSTEM_NEXT_ACTIONS),
        data=failure_data,
    )


def defined_system_envelope(system: System) -> ToolResponse:
    """Render a newly defined System with its provision next action."""
    return ToolResponse.success(
        str(system.id),
        SystemState.DEFINED.value,
        suggested_next_actions=["systems.provision_defined"],
        data={"project": system.project},
    )


async def _placement_for_system(
    conn: AsyncConnection, allocation_id: UUID
) -> tuple[str | None, str | None]:
    """Return ``(resource_id, resource_kind)`` for a System's allocation (one query)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT a.resource_id, r.kind FROM allocations a "
            "LEFT JOIN resources r ON r.id = a.resource_id WHERE a.id = %s",
            (allocation_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    resource_id, kind = row
    return (str(resource_id) if resource_id is not None else None), kind


async def _active_run_for_system(
    conn: AsyncConnection, system_id: UUID
) -> dict[str, JsonValue] | None:
    """The most-recent non-terminal run holding the System, or ``None`` (#568)."""
    # SUCCEEDED is intentionally excluded from terminal: per ADR-0179 it means the build
    # succeeded and the run continues through install/boot while still holding the System.
    terminal = [RunState.FAILED.value, RunState.CANCELED.value]
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, state FROM runs WHERE system_id = %s AND state <> ALL(%s) "
            "ORDER BY created_at DESC, id LIMIT 1",
            (system_id, terminal),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "state": row[1]}


async def get_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Return a System the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            resource_id, resource_kind = await _placement_for_system(conn, system.allocation_id)
            active_sessions = await active_session_ids_for_system(conn, system.id)
            active_run = await _active_run_for_system(conn, system.id)
            # In-memory capability read (no libvirt round-trip): whether this System's provider can
            # snapshot/restore, surfaced so an agent discovers support before calling the tools.
            # A System whose provider kind is no longer registered (disabled/uncomposed) cannot
            # resolve a runtime; keep systems.get resilient (its metadata still lets an agent tear
            # the System down) by omitting the field rather than failing the whole read.
            supports_snapshots: bool | None = None
            supports_traffic_capture: bool | None = None
            try:
                runtime = await resolver.runtime_for_system(conn, system.id)
                supports_snapshots = runtime.support.supports_snapshots
                supports_traffic_capture = runtime.support.supports_traffic_capture
            except CategorizedError:
                supports_snapshots = None
                supports_traffic_capture = None
            # The `systems` table carries no failure category, so a `failed` System's real
            # reason is recovered from the job that failed it (ADR-0454). One indexed lookup,
            # on the `failed` branch only — every other state pays nothing.
            failing_job = (
                await queue.latest_failed_job_for_system(conn, system.id)
                if system.state is SystemState.FAILED
                else None
            )
        return system_envelope(
            system,
            resource_kind=resource_kind,
            resource_id=resource_id,
            active_debug_session_ids=active_sessions,
            active_run=active_run,
            supports_snapshots=supports_snapshots,
            supports_traffic_capture=supports_traffic_capture,
            failing_job=failing_job,
        )


def _viewer_projects(ctx: RequestContext) -> list[str]:
    """Projects the caller may view: a member project with any granted role."""
    return [p for p in ctx.projects if ctx.roles.get(p) is not None]


@dataclass(frozen=True, slots=True)
class _SystemFilters:
    """The validated, SQL-ready clauses and params for a :func:`list_systems` query."""

    clauses: list[Composable]
    params: list[object]


def _build_filters(
    viewer_projects: list[str],
    *,
    allocation_id: str | None,
    state: str | None,
    shape: str | None,
    pcie: str | None,
) -> _SystemFilters | ToolResponse:
    """Translate filter args into SQL clauses, or a ``configuration_error`` envelope."""
    clauses: list[Composable] = [sql.SQL("s.project = ANY(%s)")]
    params: list[object] = [viewer_projects]
    if allocation_id is not None:
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _invalid_uuid_error("allocation_id", allocation_id)
        clauses.append(sql.SQL("s.allocation_id = %s"))
        params.append(uid)
    if state is not None:
        try:
            resolved = SystemState(state)
        except ValueError:
            return _config_error_reason(
                state,
                ConfigErrorReason.INVALID_STATE,
                accepted_values=[s.value for s in SystemState],
                detail=f"state {state!r} is not a valid System state",
            )
        clauses.append(sql.SQL("s.state = %s"))
        params.append(resolved.value)
    if shape is not None:
        if shape == CUSTOM_SHAPE_SENTINEL:
            clauses.append(sql.SQL("s.shape IS NULL"))
        else:
            clauses.append(sql.SQL("s.shape = %s"))
            params.append(shape)
    if pcie is not None:
        pcie_clause = _pcie_clause(pcie, params)
        if isinstance(pcie_clause, ToolResponse):
            return pcie_clause
        clauses.append(pcie_clause)
    return _SystemFilters(clauses, params)


def _pcie_clause(pcie: str, params: list[object]) -> Composable | ToolResponse:
    """Build the ``pcie`` SQL predicate, or a ``configuration_error`` envelope."""
    try:
        spec = parse_match_spec(pcie.strip())
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(pcie, exc)
    if spec.vendor_id is None or spec.device_id is None:
        return _config_error_reason(
            pcie,
            ConfigErrorReason.INVALID_PCIE_MATCH,
            detail="pcie match must specify both a vendor id and a device id",
        )
    params.extend([spec.vendor_id, spec.device_id])
    return sql.SQL(
        "EXISTS (SELECT 1 FROM jsonb_array_elements(a.pcie_claim) e "
        "WHERE e->>'vendor_id' = %s AND e->>'device_id' = %s)"
    )


async def list_systems(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: SystemsListRequest | None = None,
) -> ToolResponse:
    """List the caller's Systems, filterable by allocation, state, shape, and PCIe match."""
    request = request or SystemsListRequest()
    viewer_projects = _viewer_projects(ctx)
    filters = _build_filters(
        viewer_projects,
        allocation_id=request.allocation_id,
        state=request.state,
        shape=request.shape,
        pcie=request.pcie,
    )
    if isinstance(filters, ToolResponse):
        return filters
    capped = _clamp_list_limit(request.limit)
    after = None
    if request.cursor:
        try:
            after = _decode_ts_uuid_cursor(_SYSTEMS_LIST_TAG, request.cursor)
        except InvalidCursor:
            return _invalid_cursor_error("systems")
    with bind_context(principal=ctx.principal):
        if not viewer_projects:
            return _systems_collection([], truncated=False, next_cursor=None)
        clauses = list(filters.clauses)
        params: list[object] = list(filters.params)
        if after is not None:
            clauses.append(sql.SQL("(s.created_at, s.id) < (%s, %s)"))
            params.extend(after)
        query = sql.SQL(
            "SELECT s.*, r.kind AS resource_kind, a.resource_id AS resource_id FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "WHERE {where} ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
        ).format(where=sql.SQL(" AND ").join(clauses))
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, (*params, capped + 1))
            rows = await cur.fetchall()
        kept, truncated = _paginate(rows, capped)
        next_cursor = (
            _encode_ts_uuid_cursor(_SYSTEMS_LIST_TAG, kept[-1]["created_at"], kept[-1]["id"])
            if truncated and kept
            else None
        )
        return _systems_collection(
            [_split_placement(row) for row in kept],
            truncated=truncated,
            next_cursor=next_cursor,
        )


def _split_placement(row: dict[str, object]) -> tuple[System, str, str | None]:
    """Separate the joined resource kind + id from the System columns before validation."""
    resource_kind = str(row.pop("resource_kind"))
    resource_id = row.pop("resource_id")
    resource_id_str = str(resource_id) if resource_id is not None else None
    return System.model_validate(row), resource_kind, resource_id_str


_SYSTEMS_LIST_TAG = "systems.list"


def _systems_collection(
    systems: list[tuple[System, str, str | None]],
    *,
    truncated: bool,
    next_cursor: str | None,
) -> ToolResponse:
    """Render Systems (each with its backing Resource kind + id) into one envelope."""
    return ToolResponse.collection(
        "systems",
        "ok",
        [
            system_envelope(system, resource_kind=resource_kind, resource_id=resource_id)
            for system, resource_kind, resource_id in systems
        ],
        suggested_next_actions=["systems.get", "runs.create"],
        data={"truncated": truncated, "next_cursor": next_cursor},
    )
