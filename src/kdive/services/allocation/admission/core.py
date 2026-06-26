"""Budget/quota + host-cap allocation admission (ADR-0007 §4-6, ADR-0040).

``admit`` is the fail-closed admission gate. It composes the per-**host** capacity cap
(ADR-0023) with the per-**project** invariant — a concurrency quota and a spend
budget — and reserves the lease estimate against budget in the same transaction it grants:

1. **Validate first** (no lock, no write): the selector size, the lease window, and the
   selector ≤ the chosen Resource's advertised caps. Any failure is a
   ``configuration_error`` so a negative/oversized request can never reach the ledger
   (ADR-0007 §2 — the budget-minting guard).
2. **Resolve idempotency** under the project lock: a replayed ``(principal,
   idempotency_key)`` returns the originally granted allocation with no second grant,
   reserve, or ``spent_kcu`` change (ADR-0040 §3).
3. **Check then debit** under ``PROJECT`` → ``RESOURCE`` (the global lock order, ADR-0040
   §1): ``max_concurrent_allocations`` (→ ``quota_exceeded``), then ``(limit_kcu −
   spent_kcu) ≥ estimate`` read O(1) from the budget row (→ ``allocation_denied``), then
   the host cap (→ ``allocation_denied``). On success, **in one transaction**: insert
   the ``granted`` Allocation (``lease_expiry``, ``requested_vcpus``/``requested_memory_gb``,
   ``active_started_at`` null), write the ``reserved`` ledger row and bump ``spent_kcu``
   (``accounting.reserve``), record the idempotency key, and write the audit row.

Any failing check returns a denial with **no** durable write (ADR-0023's all-or-nothing
rule). The ``cost_class`` is resolved admission-side from the chosen Resource (unlike
``accounting.estimate``, which prices a hypothetical class with no host).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from psycopg import AsyncConnection

import kdive.services.allocation.admission.pcie_claim as pcie_claim
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS
from kdive.domain.accounting.cost import (
    Selector,
    cost,
    quantize_kcu,
    rate,
    resolve_coeff,
    validate_against_resource,
    validate_size,
)
from kdive.domain.capacity.state import AllocationState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle import Allocation
from kdive.domain.lifecycle.lease import resolve_window_hours
from kdive.domain.pcie import MatchOutcome, PCIeClaim, PCIeDescriptor
from kdive.security import audit
from kdive.services.accounting import ledger as accounting
from kdive.services.allocation.admission.affinity import project_may_place
from kdive.services.allocation.error_details import categorized_details
from kdive.services.allocation.idempotency import (
    budget_snapshot,
    record_key,
    resolve_replay,
    within_budget,
)
from kdive.services.allocation.lease_bounds import configured_lease_bounds

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_SECONDS_PER_HOUR = 3600

# The idempotency-store ``kind`` discriminator for a request grant (ADR-0040 §3); the
# renewal path uses the same store under its own kind.
_REQUEST_KIND = "allocations.request"
BUDGET_DENIAL_REASON = "budget_exceeded"
AFFINITY_DENIAL_REASON = "affinity_denied"

# States that occupy a host-cap / grant-quota slot (ADR-0069, the load-bearing change). A
# DEDICATED occupancy predicate that EXCLUDES ``requested``: a queued row holds only a queue
# position, so it must occupy neither a host slot nor a grant-quota slot — otherwise it would
# block other grants and self-block its own promotion (the promotion's capacity replay would
# count the candidate against itself). This is not a redefinition of
# ``NON_TERMINAL_ALLOCATION_STATES``: ``requested`` stays non-terminal/live for lease expiry
# and reconciler logic, which reason about liveness, not occupancy. PCIe occupancy
# (``pcie_claim.active_claims``) keeps using the domain liveness rule; a queued row has an empty
# ``pcie_claim`` (resolve happens only at grant), so it contributes no device either way.
OCCUPYING = (
    AllocationState.GRANTED,
    AllocationState.ACTIVE,
    AllocationState.RELEASING,
)
OCCUPYING_VALUES = [s.value for s in OCCUPYING]

# A queued row rests in this state; the pending-cap count predicate is literally this one
# state, never the occupancy predicate (ADR-0069).
_REQUESTED_VALUE = AllocationState.REQUESTED.value


@dataclass(frozen=True)
class AllocationRequest:
    """Inputs for one allocation admission attempt.

    ``selector`` carries the priced size (vcpus / memory_gb) — for a shape-sized request
    the caller resolves the shape to this selector before admission (ADR-0067). ``disk_gb``
    is the resolved disk size persisted as the at-grant snapshot (``requested_disk_gb``);
    ``shape`` is the named preset the size resolved from (``None`` for full-custom), recorded
    as a label, not re-resolved later.

    ``pcie_specs`` is the resolved PCIe device-spec **union** (explicit ``pcie_devices``
    plus a shape's ``pcie_match``) — already composed by the caller. An empty union is a
    non-PCIe request. The specs are resolved to distinct free devices and claimed inside the
    per-Resource lock (ADR-0068), never pre-lock.

    ``on_capacity`` selects what a **capacity** denial does (ADR-0069): ``"deny"`` (default)
    returns the denial; ``"queue"`` instead enqueues a ``requested``
    allocation holding only a queue position. ``requested_kind`` / ``requested_resource_id``
    are the original target descriptor persisted on the queued row so the promotion sweep
    can re-resolve a host — exactly one is set, mirroring the by-kind / by-id selector.
    """

    ctx: RequestContext
    resource: Resource
    project: str
    selector: Selector
    window: object | None = None
    idempotency_key: str | None = None
    disk_gb: int | None = None
    shape: str | None = None
    pcie_specs: tuple[str, ...] = ()
    on_capacity: Literal["deny", "queue"] = "deny"
    requested_kind: ResourceKind | None = None
    requested_resource_id: UUID | None = None
    requested_pool: str | None = None


@dataclass(frozen=True)
class AdmissionOutcome:
    """The result of an admission attempt.

    On a grant, ``allocation`` is the inserted (or replayed) row and ``category`` is
    ``None``. On a denial, ``allocation`` is ``None`` and ``category`` is the most
    specific failure the handler maps to a typed response: ``configuration_error``
    (validation), ``quota_exceeded`` (over the concurrency cap / no quota row), or
    ``allocation_denied`` (over budget / no budget row / over the host cap). ``cap`` /
    ``in_use`` carry the host-cap counters for the denial diagnostic.

    ``queueable`` marks a denial that ``on_capacity=queue`` may enqueue (ADR-0069): the
    grant-quota (``quota_exceeded``) and host-cap (``allocation_denied`` /
    ``reason="at_capacity"``) denials. A **budget** denial shares the ``allocation_denied``
    category with the host-cap denial but is NOT queueable (waiting frees no budget), so the
    enqueue decision branches on this explicit flag, never on the category. Configuration and
    PCIe-busy denials are queueable; PCIe-config denials are not.
    """

    granted: bool
    allocation: Allocation | None
    category: ErrorCategory | None = None
    reason: str | None = None
    cap: int | None = None
    in_use: int | None = None
    queueable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


async def admit(
    conn: AsyncConnection,
    request: AllocationRequest,
) -> AdmissionOutcome:
    """Admit an allocation against the project budget/quota and the host cap.

    Validates inputs, resolves idempotency, runs the check-then-debit under
    ``PROJECT`` → ``RESOURCE``, and on success grants + reserves atomically. See the
    module docstring for the full ordering and the denial categories.

    Args:
        conn: An async connection (the transaction is opened here).
        request: The authenticated principal, target Resource/project, requested size,
            lease window, and optional retry key.

    Returns:
        An :class:`AdmissionOutcome`: a grant, or a typed denial with no durable write.
    """
    try:
        window_hours, estimate = await price_window_and_estimate(conn, request)
    except CategorizedError as exc:
        return AdmissionOutcome(
            granted=False,
            allocation=None,
            category=exc.category,
            details=categorized_details(exc),
        )
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, request.project):
            return await _admit_under_project_lock(
                conn,
                request,
                window_hours=window_hours,
                estimate=estimate,
            )
    except CategorizedError as exc:
        # Host-cap resolution fails closed on an invalid cap; the transaction rolled back,
        # so no durable write survived.
        return AdmissionOutcome(
            granted=False,
            allocation=None,
            category=exc.category,
            details=categorized_details(exc),
        )


async def price_window_and_estimate(
    conn: AsyncConnection, request: AllocationRequest
) -> tuple[Decimal, Decimal]:
    """Validate the request and price the lease estimate (no lock, no write).

    Resolves and clamps the lease window, validates the selector size against the resource
    caps, and prices the ``reserved`` estimate. Shared by synchronous admission and the
    promotion sweep so both price a request identically (ADR-0069). ``quantize_kcu`` is kept
    inside the same guard so an extreme window×size fails closed as a typed denial rather
    than an uncaught exception.

    Returns:
        ``(window_hours, estimate)`` — the clamped window and the quantized kcu reserve.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a bad window/size/over-caps request,
            or a value-too-large estimate.
    """
    window_hours = resolve_window_hours(request.window, bounds=configured_lease_bounds())
    validate_size(request.selector)
    validate_against_resource(request.selector, request.resource)
    coeff = await resolve_coeff(conn, request.resource.cost_class)
    estimate = quantize_kcu(
        cost(
            rate(coeff, vcpus=request.selector.vcpus, memory_gb=request.selector.memory_gb),
            window_hours,
        )
    )
    return window_hours, estimate


@dataclass(frozen=True)
class _GateResult:
    """The shared gate's outcome: the devices to grant, or the typed denial to route."""

    denial: AdmissionOutcome | None
    devices: list[PCIeClaim]


async def admission_gate(
    conn: AsyncConnection, request: AllocationRequest, *, estimate: Decimal
) -> _GateResult:
    """Replay the check-then-debit gate against the project + the chosen host.

    The single, shared admission gate (no fork): the grant-quota and budget checks, then the
    host cap and PCIe resolution. **The caller must already hold the ``PROJECT`` and per-
    ``RESOURCE`` locks** (the global order ``PROJECT → RESOURCE → ALLOCATION``); this function
    acquires no lock itself, so synchronous admit (``PROJECT → RESOURCE``) and the promotion
    sweep (``PROJECT → RESOURCE → ALLOCATION``) both keep the documented order and never
    invert against each other.

    Returns a structured result carrying either the claimed PCIe devices to grant, or the
    typed denial. The denial's explicit reason is what routes terminate-vs-wait at
    promotion: a **budget** denial is ``ALLOCATION_DENIED`` with
    ``reason="budget_exceeded"`` (terminate); a host-cap / quota / PCIe-busy denial is
    queueable (wait). A PCIe-config denial (``CONFIGURATION_ERROR``) and affinity denial are
    non-queueable but are not budget exhaustion.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the host cap is invalid or a PCIe spec
            is malformed grammar (the caller's transaction rolls back — no durable write).
    """
    if not project_may_place(request.resource, request.project):
        # Per-project affinity backstop for an explicit ``resource_id`` (the selection filter
        # already excludes scoped hosts from any-available selection — ADR-0112, Task 4.2). A
        # foreign project can never legally occupy a scoped host, so this is NOT queueable:
        # waiting cannot make the host placeable. A global host (``owner_project`` NULL) is a
        # strict no-op here, so no existing allocation regresses.
        return _GateResult(
            denial=AdmissionOutcome(
                granted=False,
                allocation=None,
                category=ErrorCategory.ALLOCATION_DENIED,
                reason=AFFINITY_DENIAL_REASON,
            ),
            devices=[],
        )
    if not await _within_alloc_quota(conn, request.project):
        return _GateResult(
            denial=AdmissionOutcome(
                granted=False,
                allocation=None,
                category=ErrorCategory.QUOTA_EXCEEDED,
                queueable=True,
            ),
            devices=[],
        )
    if not await within_budget(conn, request.project, estimate):
        # A budget denial shares ``allocation_denied`` with the host-cap denial but is NOT
        # queueable — waiting will not free budget (ADR-0069). It hard-denies / terminates.
        # The denial is bare: the synchronous path enriches funding denials with the
        # aggregated ``unmet`` report (#833); the promotion sweep replays this gate and routes
        # on the bare category/reason, so the gate must not carry presentation detail.
        return _GateResult(
            denial=AdmissionOutcome(
                granted=False,
                allocation=None,
                category=ErrorCategory.ALLOCATION_DENIED,
                reason=BUDGET_DENIAL_REASON,
            ),
            devices=[],
        )
    host = await _host_cap_check(conn, request.resource)
    if host is not None:
        return _GateResult(denial=host, devices=[])
    claim = await _resolve_pcie_claim(conn, request)
    if claim.denial is not None:
        return _GateResult(denial=claim.denial, devices=[])
    return _GateResult(denial=None, devices=claim.devices)


async def _admit_under_project_lock(
    conn: AsyncConnection,
    request: AllocationRequest,
    *,
    window_hours: Decimal,
    estimate: Decimal,
) -> AdmissionOutcome:
    """Run idempotency + the shared check-then-debit holding the PROJECT lock.

    Reuses :func:`admission_gate` (the same gate the promotion sweep replays). On a queueable
    capacity denial with ``on_capacity=queue`` it enqueues; a budget denial hard-denies; on
    success it grants. The gate acquires the nested ``RESOURCE`` lock.
    """
    if request.idempotency_key is not None:
        replay = await resolve_replay(
            conn,
            principal=request.ctx.principal,
            key=request.idempotency_key,
            kind=_REQUEST_KIND,
            operation_label="request",
        )
        if replay is not None:
            if replay.project != request.project:
                # The key already names a grant in another project. Returning that foreign
                # allocation would be a cross-project replay; the same key cannot mean two
                # requests. Fail closed (the client must use a fresh key per request).
                return AdmissionOutcome(
                    granted=False, allocation=None, category=ErrorCategory.CONFIGURATION_ERROR
                )
            return AdmissionOutcome(granted=True, allocation=replay)
    # Acquire RESOURCE before the gate (the global order PROJECT → RESOURCE); the gate itself
    # takes no lock so the promotion sweep can keep the same order with ALLOCATION nested
    # innermost. A queue-position enqueue holds no host, but running it under the already-held
    # RESOURCE lock is harmless and keeps the check-then-debit + insert in one locked scope.
    async with advisory_xact_lock(conn, LockScope.RESOURCE, request.resource.id):
        gate = await admission_gate(conn, request, estimate=estimate)
        if gate.denial is not None:
            return await _deny_or_enqueue(conn, request, gate.denial, estimate=estimate)
        return await _grant(
            conn,
            request,
            window_hours=window_hours,
            estimate=estimate,
            claimed_devices=gate.devices,
        )


async def _deny_or_enqueue(
    conn: AsyncConnection,
    request: AllocationRequest,
    denial: AdmissionOutcome,
    *,
    estimate: Decimal,
) -> AdmissionOutcome:
    """Return the denial, or enqueue a queued row when the caller opted into the queue.

    Enqueue only when ``on_capacity="queue"`` AND the denial is ``queueable`` (a capacity
    denial — the grant quota or the host cap). All durable writes run inside the PROJECT-
    locked transaction ``admit`` already opened, so the pending-cap check and the insert are
    atomic (ADR-0069). A denial actually returned to the synchronous caller is enriched with
    the aggregated funding report (#833); an enqueued request gets a queued allocation, not an
    onboarding diagnostic.
    """
    if request.on_capacity == "queue" and denial.queueable:
        return await _enqueue(conn, request)
    return await _enrich_funding_denial(conn, request.project, estimate, denial)


def _is_funding_denial(denial: AdmissionOutcome) -> bool:
    """A project-funding denial: over quota, or over budget (#833).

    The two onboarding gates a fresh project must provision. Excludes the host-cap denial
    (``ALLOCATION_DENIED`` / ``reason="at_capacity"``), the affinity denial, and PCIe denials —
    runtime denials that no funding change resolves.
    """
    return denial.category is ErrorCategory.QUOTA_EXCEEDED or (
        denial.category is ErrorCategory.ALLOCATION_DENIED and denial.reason == BUDGET_DENIAL_REASON
    )


async def _enrich_funding_denial(
    conn: AsyncConnection, project: str, estimate: Decimal, denial: AdmissionOutcome
) -> AdmissionOutcome:
    """Attach the aggregated ``details["unmet"]`` to a funding denial (#833).

    Re-reads both funding gates under the PROJECT lock ``admit`` already holds, so the report
    is consistent with the gate's short-circuit check above; a non-funding denial is returned
    unchanged. The gate's ``category`` / ``reason`` / ``queueable`` are untouched (the
    top-level category stays the gate's primary, onboarding order), so only the synchronous
    caller's view gains the aggregate — the promotion sweep, which never calls this, is
    unchanged.
    """
    if not _is_funding_denial(denial):
        return denial
    unmet = await funding_unmet(conn, project, estimate)
    return replace(denial, details={**denial.details, "unmet": unmet})


@dataclass(frozen=True)
class _PCIeClaimResult:
    """The in-lock PCIe resolution: a denial to return, or the devices to claim."""

    denial: AdmissionOutcome | None
    devices: list[PCIeClaim]


async def _resolve_pcie_claim(
    conn: AsyncConnection, request: AllocationRequest
) -> _PCIeClaimResult:
    """Resolve the requested device union to distinct free devices under the held lock.

    A locked read-modify-write (ADR-0068 Consequences): the host's occupancy set is read
    under the per-Resource lock this caller already holds, so two requests cannot both
    resolve the last free device. An empty union short-circuits to no devices. The matcher
    splits the two denial modes — ``CONFIG`` (no host descriptor matches; the card is not
    on this host) maps to ``configuration_error``; ``CAPACITY`` (matches exist but every
    one is claimed) maps to ``allocation_denied``, the queueable case. Malformed
    grammar raises a ``CategorizedError`` that ``admit`` catches and rolls back — no write.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any requested spec is malformed.
    """
    if not request.pcie_specs:
        return _PCIeClaimResult(denial=None, devices=[])
    descriptors = pcie_claim.descriptors_for(request.resource)
    claims = await pcie_claim.active_claims(conn, request.resource.id)
    resolution = pcie_claim.resolve_union(list(request.pcie_specs), descriptors, claims=claims)
    if resolution.outcome is MatchOutcome.MATCHED:
        return _PCIeClaimResult(denial=None, devices=_claims_from(resolution.devices))
    category = (
        ErrorCategory.CONFIGURATION_ERROR
        if resolution.outcome is MatchOutcome.CONFIG
        else ErrorCategory.ALLOCATION_DENIED
    )
    queueable = resolution.outcome is MatchOutcome.CAPACITY
    return _PCIeClaimResult(
        denial=AdmissionOutcome(
            granted=False,
            allocation=None,
            category=category,
            queueable=queueable,
        ),
        devices=[],
    )


def _claims_from(devices: list[PCIeDescriptor]) -> list[PCIeClaim]:
    """Project the matched descriptors to the persisted claim snapshot (no host-local label)."""
    return [
        PCIeClaim(bdf=d["bdf"], vendor_id=d["vendor_id"], device_id=d["device_id"]) for d in devices
    ]


async def _grant(
    conn: AsyncConnection,
    request: AllocationRequest,
    *,
    window_hours: Decimal,
    estimate: Decimal,
    claimed_devices: list[PCIeClaim],
) -> AdmissionOutcome:
    """Insert the granted Allocation, reserve, record the key, and audit (one txn)."""
    now = datetime.now(UTC)  # the DB sets created_at/updated_at; lease_expiry is explicit
    lease_expiry = now + timedelta(seconds=int(window_hours * _SECONDS_PER_HOUR))
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=request.ctx.principal,
            agent_session=request.ctx.agent_session,
            project=request.project,
            resource_id=request.resource.id,
            state=AllocationState.GRANTED,
            lease_expiry=lease_expiry,
            requested_vcpus=request.selector.vcpus,
            requested_memory_gb=request.selector.memory_gb,
            requested_disk_gb=request.disk_gb,
            shape=request.shape,
            pcie_claim=claimed_devices,
        ),
    )
    await accounting.reserve(conn, allocation, estimate)
    if request.idempotency_key is not None:
        await record_key(
            conn,
            principal=request.ctx.principal,
            key=request.idempotency_key,
            project=request.project,
            kind=_REQUEST_KIND,
            allocation_id=allocation.id,
        )
    await audit.record(
        conn,
        request.ctx,
        audit.AuditEvent(
            tool="allocations.request",
            object_kind="allocations",
            object_id=allocation.id,
            transition="->granted",
            args={"resource_id": str(request.resource.id), "project": request.project},
            project=request.project,
        ),
    )
    return AdmissionOutcome(granted=True, allocation=allocation)


async def _host_cap_check(conn: AsyncConnection, resource: Resource) -> AdmissionOutcome | None:
    """The per-host capacity check; return a denial outcome, or ``None`` if under cap.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the resource has no valid cap.
    """
    cap = _resolve_cap(resource)
    in_use = await _count_occupying(conn, resource.id)
    if in_use >= cap:
        # A host-cap denial is a CAPACITY denial — a freed slot admits it — so it is
        # queueable. It shares ``allocation_denied`` with the budget denial; the
        # ``queueable`` flag (not the category) is what routes the enqueue (ADR-0069).
        return AdmissionOutcome(
            granted=False,
            allocation=None,
            category=ErrorCategory.ALLOCATION_DENIED,
            reason="at_capacity",
            cap=cap,
            in_use=in_use,
            queueable=True,
        )
    return None


def _resolve_cap(resource: Resource) -> int:
    """Read and validate the per-host cap; fail closed on anything invalid."""
    return resource.capability_view.require_allocation_cap(resource_id=resource.id)


async def _count_occupying(conn: AsyncConnection, resource_id: object) -> int:
    """Count the host's allocations occupying a host-cap slot (GRANTED/ACTIVE/RELEASING).

    Uses the dedicated occupancy predicate (ADR-0069): a queued ``requested`` row holds only
    a queue position and is excluded, so it never consumes the host cap it is waiting for.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE resource_id = %s AND state = ANY(%s)",
            (resource_id, OCCUPYING_VALUES),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


async def _within_alloc_quota(conn: AsyncConnection, project: str) -> bool:
    """Report whether the project is under ``max_concurrent_allocations``.

    Fail-closed: a project with **no quota row** is over quota (ADR-0007 §4 — no silent
    default). Derived from :func:`quota_status` so the gate predicate and the aggregated
    funding report read identical figures and cannot drift.
    """
    limit, count = await quota_status(conn, project)
    return limit is not None and count < limit


async def quota_status(conn: AsyncConnection, project: str) -> tuple[int | None, int]:
    """Return ``(max_concurrent_allocations | None, occupying_count)`` for ``project`` (#833).

    ``None`` limit means **no quota row** (fail-closed, ADR-0007 §4). The occupying count is
    the project's GRANTED/ACTIVE/RELEASING allocations (the ADR-0069 occupancy predicate —
    a queued ``requested`` row does not count); it is reported even with no quota row so a
    funding denial can name the current occupancy. Read under the held PROJECT lock.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max_concurrent_allocations FROM quotas WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    limit = int(row[0]) if row is not None else None
    return limit, await _count_project_occupying(conn, project)


async def _count_project_occupying(conn: AsyncConnection, project: str) -> int:
    """Count the project's allocations occupying a grant-quota slot (GRANTED/ACTIVE/RELEASING)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE project = %s AND state = ANY(%s)",
            (project, OCCUPYING_VALUES),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


async def funding_unmet(
    conn: AsyncConnection, project: str, estimate: Decimal
) -> list[dict[str, Any]]:
    """The unmet project-funding gates (quota first, then budget) with their figures (#833).

    A transport-neutral aggregate for the synchronous denial enrichment: each entry carries a
    ``gate`` discriminator and the current/required figures, but **no** MCP tool name (the
    transport owns those, ADR-0245). Read under the caller's held PROJECT lock on the cold
    denial path, so it is consistent with the gate's own check. Empty when neither funding gate
    is unmet; a denial the gate classified as funding always yields at least its failing gate.
    """
    unmet: list[dict[str, Any]] = []
    limit, count = await quota_status(conn, project)
    if limit is None or count >= limit:
        entry: dict[str, Any] = {"gate": "quota", "current": count, "required": count + 1}
        if limit is not None:
            entry["limit"] = limit
        unmet.append(entry)
    budget = await _budget_unmet_entry(conn, project, estimate)
    if budget is not None:
        unmet.append(budget)
    return unmet


async def _budget_unmet_entry(
    conn: AsyncConnection, project: str, estimate: Decimal
) -> dict[str, Any] | None:
    """The budget funding entry, or ``None`` when the project is within budget (#833).

    Reports both the incremental cost (``required_kcu`` = the estimate) and the **absolute**
    ``required_limit_kcu`` — the smallest ``limit_kcu`` that admits, ``spent_kcu + estimate`` —
    so an agent on a project with prior spend sizes the budget right the first time. The
    limit/spent/remaining figures are omitted when there is no budget row (mirrors #838).
    """
    snapshot = await budget_snapshot(conn, project)
    if snapshot is None:
        return {
            "gate": "budget",
            "required_kcu": str(estimate),
            "required_limit_kcu": str(estimate),
        }
    limit_kcu, spent_kcu = snapshot
    remaining = limit_kcu - spent_kcu
    if remaining >= estimate:
        return None
    return {
        "gate": "budget",
        "required_kcu": str(estimate),
        "required_limit_kcu": str(spent_kcu + estimate),
        "limit_kcu": str(limit_kcu),
        "spent_kcu": str(spent_kcu),
        "remaining_kcu": str(remaining),
    }


async def _enqueue(conn: AsyncConnection, request: AllocationRequest) -> AdmissionOutcome:
    """Insert a queued ``requested`` allocation under the held PROJECT lock (ADR-0069).

    Holds only a queue position: ``resource_id`` NULL, no reserve, no lease, empty
    ``pcie_claim``; persists the original request inputs (size snapshot, shape, the requested
    PCIe union, and the target descriptor) so the promotion sweep can re-admit. The
    pending-cap check, the insert, the idempotency-key record, and the audit all run inside
    the one PROJECT-locked transaction ``admit`` opened, so two concurrent enqueues cannot
    both pass the cap. Over the cap → ``quota_exceeded`` with no write.

    Returns:
        A success outcome carrying the queued ``requested`` allocation, or a
        ``quota_exceeded`` denial when the pending cap is full.
    """
    if not await _within_pending_quota(conn, request.project):
        return AdmissionOutcome(
            granted=False, allocation=None, category=ErrorCategory.QUOTA_EXCEEDED
        )
    now = datetime.now(UTC)
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=request.ctx.principal,
            agent_session=request.ctx.agent_session,
            project=request.project,
            resource_id=None,
            state=AllocationState.REQUESTED,
            lease_expiry=None,
            requested_vcpus=request.selector.vcpus,
            requested_memory_gb=request.selector.memory_gb,
            requested_disk_gb=request.disk_gb,
            shape=request.shape,
            pcie_claim=[],
            requested_pcie_specs=list(request.pcie_specs),
            requested_kind=request.requested_kind,
            requested_resource_id=request.requested_resource_id,
            requested_pool=request.requested_pool,
        ),
    )
    if request.idempotency_key is not None:
        await record_key(
            conn,
            principal=request.ctx.principal,
            key=request.idempotency_key,
            project=request.project,
            kind=_REQUEST_KIND,
            allocation_id=allocation.id,
        )
    await audit.record(
        conn,
        request.ctx,
        audit.AuditEvent(
            tool="allocations.request",
            object_kind="allocations",
            object_id=allocation.id,
            transition="->requested",
            args={"project": request.project, "on_capacity": "queue"},
            project=request.project,
        ),
    )
    return AdmissionOutcome(granted=True, allocation=allocation)


async def _within_pending_quota(conn: AsyncConnection, project: str) -> bool:
    """Report whether the project is under ``max_pending_allocations``.

    Fail-closed: a project with **no quota row** has no pending depth (0 cap). Counts only
    rows literally in ``requested`` (the backlog), never the occupancy predicate, under the
    held PROJECT lock so the count-then-insert is atomic (ADR-0069).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max_pending_allocations FROM quotas WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    if row is None:
        return False
    cap = int(row[0])
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE project = %s AND state = %s",
            (project, _REQUESTED_VALUE),
        )
        count_row = await cur.fetchone()
    if count_row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(count_row[0]) < cap
