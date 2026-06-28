"""Allocation request/admission MCP handler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import AllocationState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.log import bind_context
from kdive.mcp.exposure import visible_next_actions
from kdive.mcp.provider_schema import assert_kind_composed
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import (
    AllocationRequestPayload,
    ResourceById,
    ResourceByKind,
    ResourceByPool,
)
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.allocations.common import allocation_next_actions
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, projects_with_role, require_role
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
_DENIAL_NEXT_ACTIONS = ["allocations.list"]
# Admin tools that resolve a funding denial (ADR-0245). Both are registered in mcp/exposure.py.
_QUOTA_REMEDY_TOOL = "accounting.set_quota"
_BUDGET_REMEDY_TOOL = "accounting.set_budget"
# The MCP layer owns the gate→remedy-tool mapping (ADR-0255): the service emits a transport-
# neutral ``gate`` discriminator in ``data["unmet"]`` and the transport names the tool.
_GATE_REMEDY_TOOL = {"quota": _QUOTA_REMEDY_TOOL, "budget": _BUDGET_REMEDY_TOOL}


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


def _guard_resource_kind(request: AllocationRequestPayload, resolver: ProviderResolver) -> None:
    """Reject a kind-selected resource whose kind is not composed (ADR-0269).

    A pool/id selector names no kind, so the guard is a no-op there — resolution fails
    closed downstream for an absent resource.

    Args:
        request: The incoming allocation request payload.
        resolver: The provider resolver carrying the live composed kind set.

    Raises:
        CategorizedError: With ``CONFIGURATION_ERROR`` when the kind is not composed.
    """
    if isinstance(request.resource, ResourceByKind):
        assert_kind_composed(request.resource.kind, resolver.registered_kinds())


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
        return _request_response(result, ctx)


def _request_response(result: RequestAdmissionResult, ctx: RequestContext) -> ToolResponse:
    if result.error is not None:
        return ToolResponse.failure_from_error(result.object_id, result.error)
    if result.resource is None:
        return _no_resource_response(result)
    if result.allocation is not None:
        return _grant_or_enqueue_response(result.resource, result.project, result.allocation, ctx)
    if result.denial is not None:
        caller_is_admin = result.project in projects_with_role(ctx, Role.ADMIN)
        return _denial_response(
            result.resource.id, result.project, result.denial, caller_is_admin=caller_is_admin
        )
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
    resource: Resource, project: str, allocation: Allocation, ctx: RequestContext
) -> ToolResponse:
    data = {"project": project}
    if allocation.state is not AllocationState.REQUESTED:
        data["resource_id"] = str(resource.id)
    return ToolResponse.success(
        str(allocation.id),
        allocation.state.value,
        suggested_next_actions=visible_next_actions(
            allocation_next_actions(allocation.state), ctx, project
        ),
        data=data,
    )


def _denial_response(
    resource_id: UUID, project: str, outcome: AdmissionOutcome, *, caller_is_admin: bool
) -> ToolResponse:
    category = outcome.category or ErrorCategory.ALLOCATION_DENIED
    data = denial_details(outcome)
    if isinstance(data.get("unmet"), list):
        data["unmet"] = _unmet_with_remedies(data["unmet"])
    _log.info("allocation denied for project %s on resource %s: %s", project, resource_id, category)
    return ToolResponse.failure(
        str(resource_id),
        category,
        detail=_denial_detail(outcome, caller_is_admin=caller_is_admin),
        suggested_next_actions=_denial_next_actions(outcome, caller_is_admin=caller_is_admin),
        data=data,
    )


def _unmet_with_remedies(unmet: list[object]) -> list[object]:
    """Name each unmet funding gate's remedy tool in the surfaced ``data["unmet"]`` (ADR-0255).

    The service emits a transport-neutral ``gate`` per entry; the MCP layer, which owns the tool
    names, augments each with its ``remedy``. Returns new entries (no mutation of the outcome).
    """
    augmented: list[object] = []
    for entry in unmet:
        if isinstance(entry, dict):
            tool = _GATE_REMEDY_TOOL.get(entry.get("gate"))
            augmented.append({**entry, "remedy": tool} if tool is not None else dict(entry))
        else:
            augmented.append(entry)
    return augmented


def _unmet_entries(outcome: AdmissionOutcome) -> list[dict[str, object]]:
    """The funding-gate entries from a denial, or ``[]`` when it is not a funding denial."""
    unmet = outcome.details.get("unmet")
    if not isinstance(unmet, list):
        return []
    return [entry for entry in unmet if isinstance(entry, dict)]


def _denial_next_actions(outcome: AdmissionOutcome, *, caller_is_admin: bool) -> list[str]:
    """Lead a funding denial with every admin tool that resolves it (ADR-0245/0255, #841).

    A funding denial otherwise points only at ``allocations.list``, which on a denied first
    request returns an empty list. Each unmet gate's remedy tool is led with (quota then budget,
    the ``unmet`` order) **only** when the caller holds ``Role.ADMIN`` on the project — the role
    ``accounting.set_quota`` / ``accounting.set_budget`` require; a non-admin caller
    (``allocations.request`` needs only ``Role.CONTRIBUTOR``) is not pointed at a tool it cannot
    invoke. Host-capacity, affinity, and generic denials carry no ``unmet`` and keep the plain
    breadcrumb.
    """
    if not caller_is_admin:
        return list(_DENIAL_NEXT_ACTIONS)
    remedies = [
        tool
        for entry in _unmet_entries(outcome)
        if (tool := _GATE_REMEDY_TOOL.get(entry.get("gate"))) is not None
    ]
    if remedies:
        return [*remedies, *_DENIAL_NEXT_ACTIONS]
    return list(_DENIAL_NEXT_ACTIONS)


def _denial_detail(outcome: AdmissionOutcome, *, caller_is_admin: bool) -> str:
    """Compose the human-readable denial detail (ADR-0255).

    A funding denial (carrying ``unmet``) enumerates every unmet gate + its remedy; the other
    denials keep their category-specific prose. The category branches also serve as the
    defensive fallback if a funding denial reached the transport without its ``unmet`` list.
    """
    unmet = _unmet_entries(outcome)
    if unmet:
        return _funding_denial_detail(unmet, caller_is_admin=caller_is_admin)
    if outcome.reason == AFFINITY_DENIAL_REASON:
        return "the project is not permitted to place on the selected resource"
    if outcome.reason == BUDGET_DENIAL_REASON:
        return (
            "project budget exhausted for the requested window; "
            f"{_budget_remedy_clause(caller_is_admin)}"
        )
    if outcome.category is ErrorCategory.QUOTA_EXCEEDED:
        return f"project concurrency quota exhausted; {_quota_remedy_clause(caller_is_admin)}"
    if outcome.reason == "at_capacity":
        cap = "?" if outcome.cap is None else str(outcome.cap)
        in_use = "?" if outcome.in_use is None else str(outcome.in_use)
        return f"host capacity exhausted (cap {cap}, in use {in_use})"
    return "allocation denied"


def _funding_denial_detail(unmet: list[dict[str, object]], *, caller_is_admin: bool) -> str:
    """Name every unmet funding gate and its remedy so the caller provisions both (#833).

    Each gate's clause carries its current/required figures and a role-aware remedy: an admin is
    pointed at the resolving tool; a non-admin, who cannot call it, is told to ask a project
    admin. Clauses are joined in ``unmet`` order (quota then budget).
    """
    clauses = [_gate_clause(entry, caller_is_admin=caller_is_admin) for entry in unmet]
    clauses = [clause for clause in clauses if clause]
    body = "; ".join(clauses) if clauses else "funding not provisioned"
    return f"project not provisioned for the requested allocation: {body}"


def _gate_clause(entry: dict[str, object], *, caller_is_admin: bool) -> str:
    """One funding gate's shortfall + remedy clause, or ``""`` for an unknown gate."""
    gate = entry.get("gate")
    if gate == "quota":
        limit = entry.get("limit")
        usage = (
            f"concurrency quota exhausted (in use {entry.get('current')} of {limit})"
            if limit is not None
            else "concurrency quota not provisioned"
        )
        return f"{usage}, {_quota_remedy_clause(caller_is_admin)}"
    if gate == "budget":
        required = entry.get("required_kcu")
        remaining = entry.get("remaining_kcu")
        if required is not None and remaining is not None:
            shortfall = f"budget exhausted (requested {required} kcu, {remaining} kcu remaining)"
        elif required is not None:
            shortfall = f"budget not provisioned (requested {required} kcu)"
        else:
            shortfall = "budget not provisioned"
        return f"{shortfall}, {_budget_remedy_clause(caller_is_admin)}"
    return ""


def _quota_remedy_clause(caller_is_admin: bool) -> str:
    """The role-aware tail of a quota denial: name the tool only for an admin caller (#841)."""
    if caller_is_admin:
        return f"raise it with {_QUOTA_REMEDY_TOOL}"
    return "ask your project admin to raise the quota"


def _budget_remedy_clause(caller_is_admin: bool) -> str:
    """The role-aware tail of a budget denial: name the tool only for an admin caller (#841)."""
    if caller_is_admin:
        return f"raise it with {_BUDGET_REMEDY_TOOL}"
    return "ask your project admin to raise the budget"
