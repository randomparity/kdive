"""The three external-boot recovery contracts (ADR-0583, #2117).

Each service resolves its object, authorizes the caller, decides admission against the
System-wide matrix, and then reports that the external-boot recovery executor is not
installed. None of them writes, so every response is a failure envelope.

Why none of them writes: no production caller drives ``ExternalBootActivationRepository``'s
transition methods on this branch, ``allocate_external_boot_authority`` (migration 0122) is
gated on ``kdive_worker`` membership and revoked from the ``kdive_server`` role the MCP
server runs as, and ``ExternalBootAuthorityMarkerV1`` requires a ``provider_kind`` and
``authority_instance`` that neither an activation nor a reservation row carries. A tool that
began a recovery attempt here could not finish it, so these report the missing executor
instead and #2118 promotes them with it.

Ordering is resolve, authorize, admit, report. Authorization runs before the admission read
so an unauthorized caller learns nothing about whether the System carries an activation.
"""

from __future__ import annotations

import hashlib
from typing import LiteralString
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter, ValidationError

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import Digest
from kdive.domain.lifecycle.records import Run
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.admission import build_external_boot_payload
from kdive.jobs.payloads import ResolveRecoveryOrphanPayload
from kdive.log import bind_context
from kdive.mcp.platform_auth import audit_platform_denial
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import external_boot_denial as _external_boot_denial
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools.external_boot.recovery_idempotency import recovery_request, recovery_response
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    require_platform_role,
    require_role,
)

# The ceiling `safe_error_details` applies to the list keys it preserves. The refusal payloads
# below are built directly rather than through that filter, so they take the same bound from the
# same constant instead of growing a second one.
from kdive.serialization import _MAX_ERROR_ENTRIES, JsonValue
from kdive.services.debug.sessions import active_session_ids_for_system
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)

_UNAVAILABLE_REASON = "recovery_executor_unavailable"

RELEASE_TOOL = "runs.release_external_boot"
RESOLVE_CONFLICT_TOOL = "systems.resolve_external_boot_conflict"
ORPHAN_TOOL = "ops.resolve_recovery_orphan"

#: The single resolution ADR-0583 defines: put the recorded source state back.
SUPPORTED_RESOLUTION_OPERATION = "restore-recorded-source"
SUPPORTED_DISPOSITIONS = frozenset({"delete", "adopt"})
MAX_OBJECT_IDENTITIES = 64
#: Per-object-identity character cap, matching the reservation model's ``store_identity`` bound.
#: It bounds the orphan repair's references and nothing else — the conflict digest takes
#: :data:`MAX_OBSERVED_IDENTITY_LENGTH`, so neither bound moves when the other does.
MAX_OBJECT_IDENTITY_LENGTH = 1024
#: The exact length of a ``Digest``: ``'sha256:'`` plus 64 lowercase hex characters. The schema
#: bound on ``observed_identity``, so an oversized digest is rejected before any database read.
MAX_OBSERVED_IDENTITY_LENGTH = len("sha256:") + 64

_ACTIVE_JOB_STATES = [JobState.QUEUED.value, JobState.RUNNING.value]

# System-scoped kinds carry the System in their payload; run-scoped kinds carry only a
# `run_id`, so the Run lookup is what makes the refusal cover another Run's in-flight work.
#
# Two arms rather than one `OR`, because an `OR` across an indexed and an unindexed expression
# is planned as neither: measured on 200k jobs, the single-statement form walked `jobs_pkey`
# end to end (`Rows Removed by Filter: 200000`) and never touched the expression index, and a
# global `ORDER BY j.id` is what stopped the `LIMIT` from ending it early. Split, the
# `system_id` arm plans as `Index Scan using jobs_payload_system_id_idx` (migration 0082, an
# expression index on exactly `payload->>'system_id'`) and reads 3 buffers.
#
# The `run_id` arm uses `jobs_payload_run_id_idx` (migration 0137). It remains a separate arm:
# combining the two expressions under `OR` made PostgreSQL ignore the `system_id` index, while a
# global `ORDER BY` prevented either arm's `LIMIT` from stopping after the bounded result page.
#
# `UNION` rather than `UNION ALL`: nothing enforces that a payload carries only one of the two
# keys, and a row matching both arms would otherwise be counted twice against
# `_MAX_ERROR_ENTRIES` and raise `truncated` on a list that is not truncated. No global
# `ORDER BY`: the refusal needs existence, one page of ids, and whether the cap bit — never a
# particular page (`test_release_caps_the_blocking_job_ids_it_returns` asserts a subset).
_ACTIVE_JOBS_SQL: LiteralString = (
    "(SELECT j.id FROM jobs j "
    "WHERE j.state = ANY(%s) AND j.payload->>'system_id' = %s LIMIT %s) "
    "UNION "
    "(SELECT j.id FROM jobs j "
    "WHERE j.state = ANY(%s) AND j.payload->>'run_id' IN "
    "(SELECT id::text FROM runs WHERE system_id = %s) LIMIT %s)"
)

_REPOSITORY = ExternalBootActivationRepository()
_IDENTITY = TypeAdapter(Digest)

_CONFLICT_AUTHORITY_SQL: LiteralString = (
    "SELECT provider_kind, authority_instance "
    "FROM resolve_external_boot_conflict_dispatch_binding(%s, %s, %s, %s)"
)

_QUARANTINE_SQL: LiteralString = (
    "SELECT q.id, q.object_identity, q.resource_id, q.activation_id, q.provider_kind, "
    "q.authority_instance, q.object_kind, q.object_reference, q.ownership_digest, "
    "q.observed_digest, q.reserved_bytes, r.kind AS resource_kind, "
    "q.operation_identity, q.attempt_id, q.mutation_journal_sequence, "
    "q.mutation_journal_digest "
    "FROM external_boot_recovery_quarantine AS q "
    "JOIN systems AS s ON s.id = q.system_id "
    "JOIN allocations AS a ON a.id = s.allocation_id "
    "JOIN resources AS r ON r.id = a.resource_id AND r.id = q.resource_id "
    "WHERE q.system_id = %s AND q.status = 'quarantined' "
    "AND q.object_identity = ANY(%s) ORDER BY q.object_identity FOR UPDATE OF q"
)

_RELEASE_AUTHORITY_SQL: LiteralString = (
    "SELECT provider_kind, authority_instance "
    "FROM resolve_external_boot_release_dispatch_binding(%s, %s, %s, %s)"
)

_PROMOTION = (
    "Promoted when the external-boot recovery job handler and worker claim path land (#2118)."
)

#: The `maturity_detail` text the two admission contracts register with.
ADMISSION_STUB_DETAIL = (
    "Validates the caller's identity, role, and the System-wide external-boot admission "
    "matrix, then reports configuration_error with reason=recovery_executor_unavailable. No "
    "activation transition is committed and no recovery job is enqueued, because the "
    "external-boot recovery executor is not installed."
)

#: The `maturity_detail` text the quarantined-object repair registers with.
ORPHAN_STUB_DETAIL = (
    "Validates the caller's platform role and the bounded repair reference, then reports "
    "configuration_error with reason=recovery_executor_unavailable. No quarantined object is "
    "deleted or adopted and no recovery job is enqueued, because the external-boot recovery "
    "executor is not installed."
)


def degraded_stub_meta(detail: str) -> dict[str, object]:
    """Build the `partial` tool metadata a contract registers with.

    Built here rather than at each registrar so the reason a tool reports and the reason its
    schema advertises cannot drift apart, and so all three promote on one issue reference.
    """
    return _docmeta.maturity_meta("partial") | {
        "maturity_detail": {
            "reason": "degraded_stub",
            "detail": detail,
            "promotion": _PROMOTION,
        }
    }


def _executor_unavailable(object_id: str, tool: str) -> ToolResponse:
    """The one terminal response all three contracts share.

    One reason string for all three because one thing is missing: the external-boot recovery
    executor #2118 owns. Built here rather than at each call site so the reason and the
    disclosure cannot drift apart.
    """
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=(
            f"{tool} accepted this request but cannot serve it: the external-boot recovery "
            "executor is not installed, so nothing was changed"
        ),
        suggested_next_actions=["systems.get"],
        data={"reason": _UNAVAILABLE_REASON},
    )


def _config_error(object_id: str, *, reason: str, detail: str, next_action: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=detail,
        suggested_next_actions=[next_action],
        data={"reason": reason},
    )


def _conflict(
    object_id: str,
    *,
    reason: str,
    detail: str,
    next_actions: list[str],
    data: dict[str, JsonValue] | None = None,
) -> ToolResponse:
    payload: dict[str, JsonValue] = {"reason": reason}
    payload.update(data or {})
    return ToolResponse.failure(
        object_id,
        ErrorCategory.CONFLICT,
        detail=detail,
        suggested_next_actions=next_actions,
        data=payload,
    )


def _unresolved(object_id: str, *, reason: str, detail: str, next_action: str) -> ToolResponse:
    """A syntactically valid id that resolves to no visible row: ``not_found`` per errors.py.

    ``domain/errors.py`` states the rule normatively (a malformed id stays
    ``configuration_error``; one that resolves to nothing is ``not_found``), and ``runs.cancel``
    already follows it for the same object kind. The seam is mixed elsewhere in the repo, but
    this is new surface with no compatibility contract to preserve, and the category is what
    tells an agent whether retrying can help: ``configuration_error`` reads as fix-and-retry for
    a condition no retry changes. The disclosure property is unaffected — the missing and the
    foreign case stay byte-identical either way, which is the whole point of one envelope.
    """
    return ToolResponse.failure(
        object_id,
        ErrorCategory.NOT_FOUND,
        detail=detail,
        suggested_next_actions=[next_action],
        data={"reason": reason},
    )


def _unresolved_run(run_id: str) -> ToolResponse:
    """One envelope for a missing Run and for a Run in a project the caller does not hold."""
    return _unresolved(
        run_id,
        reason="unresolved_run",
        detail="run_id does not resolve to a Run available to this caller",
        next_action="runs.get",
    )


def _unresolved_system(system_id: str) -> ToolResponse:
    """One envelope for a missing System and for one the caller may not see."""
    return _unresolved(
        system_id,
        reason="unresolved_system",
        detail="system_id does not resolve to a System available to this caller",
        next_action="systems.get",
    )


def _bounded_ids(key: str, values: list[str]) -> dict[str, JsonValue]:
    """Carry a refusal's blocking ids under ``key``, bounded to one page of them.

    A System can hold more blockers than an envelope should carry, and these lists are built
    directly rather than through ``safe_error_details``, which bounds only its own reserved
    keys. ``truncated`` is present only when the cap bit, so a caller can tell a complete list
    from a prefix rather than having to compare its length against the cap.
    """
    bounded: dict[str, JsonValue] = {key: list(values[:_MAX_ERROR_ENTRIES])}
    if len(values) > _MAX_ERROR_ENTRIES:
        bounded["truncated"] = True
    return bounded


async def _active_job_ids_for_system(conn: AsyncConnection, system_id: UUID) -> list[str]:
    """Every queued or running job for the System, whichever Run owns it, bounded to one page.

    One row past ``_MAX_ERROR_ENTRIES`` is fetched *per arm* so :func:`_bounded_ids` still sees
    that the cap bit and marks the list ``truncated``; the refusal itself needs no exact count.
    Taking that many from each arm cannot under-report the cap: a page's worth of matches in
    either arm alone already exceeds it.
    """
    page = _MAX_ERROR_ENTRIES + 1
    params = (_ACTIVE_JOB_STATES, str(system_id), page, _ACTIVE_JOB_STATES, system_id, page)
    async with conn.cursor() as cur:
        await cur.execute(_ACTIVE_JOBS_SQL, params)
        rows = await cur.fetchall()
    return [str(row[0]) for row in rows]


async def request_release(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    resolver: ProviderResolver | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit and durably enqueue release of the Run's external-boot activation.

    Resolves and authorizes the Run, then decides ``external_boot_release`` against the
    activation restricting its System and refuses the two conditions ADR-0583 names as
    blocking a release. An admissible request resolves its exact server-owned durable
    authority and atomically enqueues the recovery job without provider I/O.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _invalid_uuid_error("run_id", run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _unresolved_run(run_id)
            require_role(ctx, run.project, Role.CONTRIBUTOR)
            # Below `require_role`, so the module docstring's "resolve, authorize, admit" is
            # literally true of every branch rather than only of the activation read. Nothing
            # leaked while it sat above — bindedness is already viewer-readable through
            # `runs.get` — but an ordering claim that holds for one branch is worse than none.
            if run.system_id is None:
                return _config_error(
                    run_id,
                    reason="run_not_bound",
                    detail="this Run is bound to no System, so it holds no external boot",
                    next_action="runs.get",
                )
            return await _release_locked(conn, ctx, run, run.system_id, resolver, idempotency_key)


async def _release_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    system_id: UUID,
    resolver: ProviderResolver | None,
    idempotency_key: str | None,
) -> ToolResponse:
    """Decide the release under the System lock, so every read sees one consistent activation.

    ``conn`` has already read the Run, so this transaction is a SAVEPOINT and the lock releases
    at end-of-request. Authority resolution and durable enqueue remain inside that lock.

    The restricting activation is read directly before the guard because the guard cannot
    express "nothing to release": it returns ``None`` both for an admitted operation and for a
    System no activation restricts — it must, since every ordinary call site needs an absent
    activation to admit its work — and its denial details are exactly ``activation_id``,
    ``activation_state``, and ``owning_run_id``, none of which exist when there is no row.
    """
    object_id = str(run.id)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        dedup_key = ""
        metadata = None
        if idempotency_key is not None:
            dedup_key, metadata, replay = await recovery_request(
                conn,
                tool=RELEASE_TOOL,
                object_key="run_id",
                object_id=object_id,
                arguments=(),
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return replay
        activation = await _REPOSITORY.get_restricting_for_system(conn, system_id)
        if activation is None:
            return _conflict(
                object_id,
                reason="no_active_activation",
                detail="no external-boot activation restricts this Run's System",
                next_actions=["runs.get"],
            )
        if idempotency_key is None:
            dedup_key, metadata, replay = await recovery_request(
                conn,
                tool=RELEASE_TOOL,
                object_key="run_id",
                object_id=object_id,
                arguments=(),
                idempotency_key=None,
                scope_identity=str(activation.id),
            )
            if replay is not None:
                return replay
        try:
            await check_external_boot_admission(
                conn,
                system_id,
                ExternalBootOperation.EXTERNAL_BOOT_RELEASE,
                project=run.project,
                run_id=run.id,
            )
        except ExternalBootDenied as exc:
            return _external_boot_denial(object_id, exc, ctx)
        operation_identity = (
            "sha256:"
            + hashlib.sha256(
                f"{activation.id}\0release\0{activation.plan_identity}\0{dedup_key}".encode()
            ).hexdigest()
        )
        job_ids = await _active_job_ids_for_system(conn, system_id)
        if job_ids:
            return _conflict(
                object_id,
                reason="system_job_active",
                detail="a queued or running job holds this System; release once it settles",
                # `jobs.cancel` is named because `jobs.wait` is a dead end for a job that will
                # never run (paused lane, dead worker) and `runs.cancel` is denied by the matrix.
                # `jobs.cancel` is the one unguarded escape, so it is the action that clears this.
                next_actions=["jobs.wait", "jobs.cancel", "runs.get"],
                data=_bounded_ids("job_ids", job_ids),
            )
        session_ids = await active_session_ids_for_system(conn, system_id)
        if session_ids:
            return _conflict(
                object_id,
                reason="debug_session_active",
                detail="a debug session is attaching to or live on this System",
                next_actions=["debug.detach", "runs.get"],
                data=_bounded_ids("session_ids", session_ids),
            )
        if resolver is None:
            return _executor_unavailable(object_id, RELEASE_TOOL)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _RELEASE_AUTHORITY_SQL,
                (activation.id, activation.system_id, activation.run_id, activation.plan_identity),
            )
            authorities = await cur.fetchall()
        if len(authorities) != 1:
            return _config_error(
                object_id,
                reason="release_authority_unresolved",
                detail="the exact durable external-boot release authority is unavailable",
                next_action="runs.get",
            )
        authority = authorities[0]
        try:
            kind, payload = await build_external_boot_payload(
                conn,
                activation_id=activation.id,
                purpose="release",
                operation="release",
                provider_kind=str(authority["provider_kind"]),
                authority_instance=str(authority["authority_instance"]),
                operation_identity=operation_identity,
                resolver=resolver,
            )
        except CategorizedError as exc:
            return _config_error(
                object_id,
                reason="release_authority_unresolved",
                detail=f"the durable release authority cannot dispatch recovery: {exc}",
                next_action="runs.get",
            )
        job = await queue.enqueue(
            conn,
            kind,
            payload.model_copy(update={"recovery_request_v1": metadata}),
            job_authorizing(ctx, run.project),
            dedup_key,
        )
    return recovery_response(job, "run_id", object_id)


def _resolution_input_error(
    system_id: str, operation: str, observed_identity: str
) -> ToolResponse | None:
    if operation != SUPPORTED_RESOLUTION_OPERATION:
        return _config_error(
            system_id,
            reason="unsupported_resolution_operation",
            detail=(
                "operation must be exactly "
                f"{SUPPORTED_RESOLUTION_OPERATION!r}, the one resolution ADR-0583 defines"
            ),
            next_action="systems.get",
        )
    if not _is_identity_shaped(observed_identity):
        return _config_error(
            system_id,
            reason="invalid_observed_identity",
            detail="observed_identity must be a 'sha256:<64 lowercase hex>' composite state",
            next_action="systems.get",
        )
    return None


def _is_identity_shaped(value: str) -> bool:
    """Check the caller's identity against the stored digest form.

    Shape only. The value is never compared with the activation's recorded composite state:
    that compare-and-set is one half of ``begin_recovery_attempt``, and running it would commit
    the transition this module cannot finish. Length needs no separate check — ``Digest`` is an
    anchored 71-character pattern, and the schema bounds the field before this runs.
    """
    try:
        _IDENTITY.validate_python(value)
    except ValidationError:
        return False
    return True


async def resolve_conflict(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resolver: ProviderResolver,
    system_id: str,
    operation: str,
    observed_identity: str,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Admit a recovery-conflict resolution, then report the missing executor.

    Resolves and authorizes the System, validates ``operation`` and the shape of
    ``observed_identity``, and decides ``external_boot_resolve_conflict`` — which the matrix
    admits only in ``recovery_conflict``. No job or session refusal applies: a System in
    ``recovery_conflict`` already fails the matrix for every operation that could start one.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _unresolved_system(system_id)
            require_role(ctx, system.project, Role.ADMIN)
            invalid = _resolution_input_error(system_id, operation, observed_identity)
            if invalid is not None:
                return invalid
            return await _resolve_conflict_locked(
                conn,
                ctx,
                resolver,
                uid,
                system.project,
                observed_identity,
                idempotency_key,
            )


async def _resolve_conflict_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    resolver: ProviderResolver,
    system_id: UUID,
    project: str,
    observed_identity: str,
    idempotency_key: str | None,
) -> ToolResponse:
    """Decide and enqueue the resolution atomically under the System lock.

    See :func:`_release_locked` for why the restricting activation is read directly rather
    than inferred from the guard.
    """
    object_id = str(system_id)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        dedup_key = ""
        metadata = None
        if idempotency_key is not None:
            dedup_key, metadata, replay = await recovery_request(
                conn,
                tool=RESOLVE_CONFLICT_TOOL,
                object_key="system_id",
                object_id=object_id,
                arguments=(SUPPORTED_RESOLUTION_OPERATION, observed_identity),
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return replay
        activation = await _REPOSITORY.get_restricting_for_system(conn, system_id)
        if activation is None:
            return _conflict(
                object_id,
                reason="no_recovery_conflict",
                detail="no external-boot activation restricts this System, so none is conflicted",
                next_actions=["runs.get"],
            )
        if idempotency_key is None:
            dedup_key, metadata, replay = await recovery_request(
                conn,
                tool=RESOLVE_CONFLICT_TOOL,
                object_key="system_id",
                object_id=object_id,
                arguments=(SUPPORTED_RESOLUTION_OPERATION, observed_identity),
                idempotency_key=None,
                scope_identity=str(activation.id),
            )
            if replay is not None:
                return replay
        try:
            await check_external_boot_admission(
                conn,
                system_id,
                ExternalBootOperation.EXTERNAL_BOOT_RESOLVE_CONFLICT,
                project=project,
            )
        except ExternalBootDenied as exc:
            return _external_boot_denial(object_id, exc, ctx)

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _CONFLICT_AUTHORITY_SQL,
                (
                    activation.id,
                    activation.system_id,
                    activation.run_id,
                    activation.plan_identity,
                ),
            )
            authorities = await cur.fetchall()
        if not authorities:
            return _config_error(
                object_id,
                reason="conflict_authority_unresolved",
                detail=(
                    "the restricting activation has no unambiguous durable authority binding "
                    "from which recovery can be dispatched"
                ),
                next_action="systems.get",
            )

        authority = authorities[0]
        operation_identity = (
            "sha256:"
            + hashlib.sha256(
                (
                    f"{activation.id}\0{SUPPORTED_RESOLUTION_OPERATION}\0{observed_identity}"
                    f"\0{dedup_key}"
                ).encode()
            ).hexdigest()
        )
        try:
            kind, payload = await build_external_boot_payload(
                conn,
                activation_id=activation.id,
                purpose="resolve-conflict",
                operation="resolve-conflict",
                provider_kind=str(authority["provider_kind"]),
                authority_instance=str(authority["authority_instance"]),
                operation_identity=operation_identity,
                expected_observed_composite=observed_identity,
                resolver=resolver,
            )
        except CategorizedError as exc:
            # Admission failures at this boundary are closed configuration refusals. They occur
            # before enqueue, and the surrounding transaction protects future additions here.
            return _config_error(
                object_id,
                reason="conflict_authority_unresolved",
                detail=f"the durable conflict authority cannot dispatch recovery: {exc}",
                next_action="systems.get",
            )
        job = await queue.enqueue(
            conn,
            kind,
            payload.model_copy(update={"recovery_request_v1": metadata}),
            job_authorizing(ctx, project),
            dedup_key,
        )
    return recovery_response(job, "system_id", object_id)


def _orphan_input_error(
    system_id: str, object_identities: list[str], disposition: str
) -> ToolResponse | None:
    if disposition not in SUPPORTED_DISPOSITIONS:
        return _config_error(
            system_id,
            reason="unsupported_disposition",
            detail=f"disposition must be one of {', '.join(sorted(SUPPORTED_DISPOSITIONS))}",
            next_action="systems.get",
        )
    within_bounds = 0 < len(object_identities) <= MAX_OBJECT_IDENTITIES and all(
        0 < len(identity) <= MAX_OBJECT_IDENTITY_LENGTH for identity in object_identities
    )
    if not within_bounds:
        return _config_error(
            system_id,
            reason="invalid_object_identities",
            detail=(
                f"object_identities must hold 1 to {MAX_OBJECT_IDENTITIES} references, each "
                f"1 to {MAX_OBJECT_IDENTITY_LENGTH} characters"
            ),
            next_action="systems.get",
        )
    return None


async def resolve_recovery_orphan(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    object_identities: list[str],
    disposition: str,
    resolver: ProviderResolver | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    """Atomically admit a bounded, durable quarantined recovery-object repair.

    The platform role is enforced before the System is *resolved*, matching the break-glass
    ``ops`` tools this one registers beside: a caller without ``platform_admin`` learns nothing
    about which System ids exist. Only the id's syntax is checked first, so the denial audit
    below records a bounded identifier rather than arbitrary caller input. It runs no admission
    check — ADR-0583 scopes the repair to quarantined recovery objects, which are not the
    activation the matrix keys on. Object references and provider authority come only from
    exact durable quarantine rows; caller identities are selectors, never provider paths.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=ORPHAN_TOOL, scope=f"denied:{uid}", args={"system_id": str(uid)}
        )
        return ToolResponse.denied(system_id, missing_roles=[PlatformRole.PLATFORM_ADMIN])
    invalid = _orphan_input_error(system_id, object_identities, disposition)
    if invalid is not None:
        return invalid
    if len(object_identities) != len(set(object_identities)):
        return _config_error(
            system_id,
            reason="duplicate_object_identities",
            detail="object_identities must not contain duplicates",
            next_action="systems.get",
        )
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, uid),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None:
                return _unresolved_system(system_id)
            dedup_key, metadata, replay = await recovery_request(
                conn,
                tool=ORPHAN_TOOL,
                object_key="system_id",
                object_id=str(uid),
                arguments=(disposition, *sorted(object_identities)),
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                return replay
            if resolver is None:
                return _config_error(
                    system_id,
                    reason="recovery_executor_unavailable",
                    detail=(
                        "ops.resolve_recovery_orphan cannot run because the external-boot "
                        "recovery executor is not installed"
                    ),
                    next_action="systems.get",
                )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_QUARANTINE_SQL, (uid, object_identities))
                rows = await cur.fetchall()
            if len(rows) != len(object_identities):
                return _conflict(
                    system_id,
                    reason="quarantine_binding_mismatch",
                    detail="the exact quarantined recovery-object set is unavailable",
                    next_actions=["systems.get"],
                )
            if any(row["provider_kind"] != row["resource_kind"] for row in rows):
                return _conflict(
                    system_id,
                    reason="quarantine_binding_mismatch",
                    detail="the quarantined recovery-object provider binding no longer matches",
                    next_actions=["systems.get"],
                )
            binding = await resolver.binding_for_system(conn, uid)
            if (
                any(row["provider_kind"] != binding.kind.value for row in rows)
                or binding.runtime.external_boot_recovery_objects is None
            ):
                return _executor_unavailable(system_id, ORPHAN_TOOL)
            canonical = "\0".join(
                f"{row['id']}:{row['ownership_digest']}:{row['observed_digest']}" for row in rows
            )
            binding_digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
            operation_identity = (
                "sha256:"
                + hashlib.sha256(f"{uid}\0{disposition}\0{binding_digest}".encode()).hexdigest()
            )
            request_id = uuid5(NAMESPACE_URL, f"kdive:{operation_identity}")
            payload = ResolveRecoveryOrphanPayload(
                schema="resolve-recovery-orphan-v1",
                system_id=str(uid),
                request_id=str(request_id),
                binding_digest=binding_digest,
                recovery_request_v1=metadata,
            )
            job = await queue.enqueue(
                conn,
                JobKind.RESOLVE_RECOVERY_ORPHAN,
                payload,
                job_authorizing(ctx, system.project),
                dedup_key,
            )
            object_ids = [row["id"] for row in rows]
            await conn.execute(
                "INSERT INTO external_boot_recovery_orphan_requests "
                "(id, system_id, disposition, binding_digest, object_ids, job_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (request_id, uid, disposition, binding_digest, object_ids, job.id),
            )
    return recovery_response(job, "system_id", str(uid))


__all__ = [
    "ADMISSION_STUB_DETAIL",
    "MAX_OBJECT_IDENTITIES",
    "MAX_OBJECT_IDENTITY_LENGTH",
    "MAX_OBSERVED_IDENTITY_LENGTH",
    "ORPHAN_STUB_DETAIL",
    "ORPHAN_TOOL",
    "RELEASE_TOOL",
    "RESOLVE_CONFLICT_TOOL",
    "SUPPORTED_DISPOSITIONS",
    "SUPPORTED_RESOLUTION_OPERATION",
    "degraded_stub_meta",
    "request_release",
    "resolve_conflict",
    "resolve_recovery_orphan",
]
