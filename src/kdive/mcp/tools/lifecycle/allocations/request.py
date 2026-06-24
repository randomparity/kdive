"""Allocation request/admission MCP handler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import AllocationState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload, ResourceById, ResourceByPool
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.allocations.common import allocation_next_actions
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.services.allocation.admission.core import (
    AFFINITY_DENIAL_REASON,
    BUDGET_DENIAL_REASON,
    AdmissionOutcome,
)
from kdive.services.allocation.admission.metrics import AdmissionMetrics
from kdive.services.allocation.admission.request import (
    AdmissionRequestSpec,
    RequestAdmissionResult,
    denial_details,
    request_admission,
)

_log = logging.getLogger(__name__)
_DISCOVERY_NEXT_ACTIONS = ["resources.list", "shapes.list"]


def _outcome_for_metrics(result: RequestAdmissionResult) -> AdmissionOutcome | None:
    """Translate a request result into the admission outcome to count (ADR-0190 D).

    A grant/enqueue is a success outcome carrying the allocation (classify reads its state);
    a typed denial is recorded as-is; a pre-admission error or a no-schedulable-resource
    rejection is recorded under its category. An infrastructure failure (no signal) → ``None``.
    """
    if result.error is not None:
        return AdmissionOutcome(granted=False, allocation=None, category=result.error.category)
    if result.allocation is not None:
        return AdmissionOutcome(granted=True, allocation=result.allocation)
    if result.denial is not None:
        return result.denial
    if result.resource is None:
        category = result.category or ErrorCategory.CONFIGURATION_ERROR
        return AdmissionOutcome(granted=False, allocation=None, category=category)
    return None


def _spec_from_payload(payload: AllocationRequestPayload) -> AdmissionRequestSpec | ToolResponse:
    resolved_id: UUID | None = None
    kind: ResourceKind | None = None
    pool: str | None = None
    resource = payload.resource
    if isinstance(resource, ResourceById):
        resolved_id = _as_uuid(resource.resource_id)
        if resolved_id is None:
            return _config_error(resource.resource_id)
    elif isinstance(resource, ResourceByPool):
        pool = resource.pool
    else:
        kind = resource.kind
    return AdmissionRequestSpec(
        resource_id=resolved_id,
        kind=kind,
        pool=pool,
        shape=payload.shape,
        vcpus=payload.vcpus,
        memory_gb=payload.memory_gb,
        disk_gb=payload.disk_gb,
        window=payload.window,
        pcie_devices=tuple(payload.pcie_devices),
        on_capacity=payload.on_capacity,
    )


async def request_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    request: AllocationRequestPayload,
    idempotency_key: str | None = None,
    admission_metrics: AdmissionMetrics | None = None,
) -> ToolResponse:
    """Admit an allocation against budget, quota, and selected host capacity.

    ``admission_metrics`` is injected by the registrar (constructed at app build time off the
    proxy meter, ADR-0190 D); when absent (un-instrumented callers and tests) decisions are
    recorded into a no-op emitter rather than reaching for a process-global.
    """
    require_project(ctx, project)
    require_role(ctx, project, Role.CONTRIBUTOR)
    with bind_context(principal=ctx.principal):
        spec = _spec_from_payload(request)
        if isinstance(spec, ToolResponse):
            return spec
        async with pool.connection() as conn:
            result = await request_admission(
                conn,
                ctx,
                project=project,
                spec=spec,
                idempotency_key=idempotency_key,
            )
        outcome = _outcome_for_metrics(result)
        if outcome is not None:
            (admission_metrics or AdmissionMetrics.disabled()).record_decision(outcome)
        return _request_response(result)


def _request_response(result: RequestAdmissionResult) -> ToolResponse:
    if result.error is not None:
        return ToolResponse.failure_from_error(result.object_id, result.error)
    if result.resource is None:
        return _no_resource_response(result)
    if result.allocation is not None:
        return _grant_or_enqueue_response(result.resource, result.project, result.allocation)
    if result.denial is not None:
        return _denial_response(result.resource.id, result.project, result.denial)
    return ToolResponse.failure(result.object_id, ErrorCategory.INFRASTRUCTURE_FAILURE)


def _no_resource_response(result: RequestAdmissionResult) -> ToolResponse:
    if result.available_kinds is not None:
        if result.available_kinds:
            available = f"available kinds: {', '.join(result.available_kinds)}"
        else:
            available = "no resource kinds are registered"
        detail = f"no schedulable {result.object_id!r} resource is registered; {available}"
    elif result.selector == "pool":
        # Generic detail: a by-pool denial never enumerates pools (ADR-0186 — pool names are
        # operator labels on affinity-scoped resources; a fleet list would leak across tenants).
        detail = f"no schedulable resource in pool {result.object_id!r} is registered"
    else:
        detail = f"no schedulable resource {result.object_id!r} is registered"
    return ToolResponse.failure(
        result.object_id,
        result.category or ErrorCategory.CONFIGURATION_ERROR,
        detail=detail,
        suggested_next_actions=list(_DISCOVERY_NEXT_ACTIONS),
    )


def _grant_or_enqueue_response(
    resource: Resource, project: str, allocation: Allocation
) -> ToolResponse:
    data = {"project": project}
    if allocation.state is not AllocationState.REQUESTED:
        data["resource_id"] = str(resource.id)
    return ToolResponse.success(
        str(allocation.id),
        allocation.state.value,
        suggested_next_actions=allocation_next_actions(allocation.state),
        data=data,
    )


def _denial_response(resource_id: UUID, project: str, outcome: AdmissionOutcome) -> ToolResponse:
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data = denial_details(outcome)
    _log.info("allocation denied for project %s on resource %s: %s", project, resource_id, category)
    return ToolResponse.failure(
        str(resource_id),
        category,
        detail=_denial_detail(outcome),
        suggested_next_actions=["allocations.list"],
        data=data,
    )


def _denial_detail(outcome: AdmissionOutcome) -> str:
    if outcome.reason == BUDGET_DENIAL_REASON:
        return "project budget exhausted for the requested window"
    if outcome.reason == AFFINITY_DENIAL_REASON:
        return "the project is not permitted to place on the selected resource"
    if outcome.category is ErrorCategory.QUOTA_EXCEEDED:
        return "project concurrency quota exhausted"
    if outcome.reason == "at_capacity":
        cap = "?" if outcome.cap is None else str(outcome.cap)
        in_use = "?" if outcome.in_use is None else str(outcome.in_use)
        return f"host capacity exhausted (cap {cap}, in use {in_use})"
    return "allocation denied"
