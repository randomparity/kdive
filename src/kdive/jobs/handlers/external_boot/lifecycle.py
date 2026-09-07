"""The six enqueueable external-boot operation handlers (spec §7).

Each is the shared runner parameterized by its row of the specification's §7 table: the activation
states the **commit** admits, the evidence columns it reads, its footnoted prerequisites, its port
call, and its result variant. Nothing here calls ``commit_external_boot_authority_result``; the
handler returns its result and the worker commits it under ``_authority_binding_matches``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, LiteralString, cast
from uuid import NAMESPACE_URL, uuid5

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import ExternalBootActivationState as State
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.evidence import (
    authority_result,
    evidence_digest,
    terminal_evidence,
)
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperationHandler
from kdive.jobs.handlers.external_boot.ports import (
    ExternalBootAuthorityExecutor,
    ExternalBootHandlerPorts,
)
from kdive.jobs.handlers.external_boot.runner import (
    OperationContext,
    _CommandLineMismatch,
    authority_ref,
    run_operation,
)
from kdive.jobs.handlers.system_reclaim import reclaim_system_core_after_provider_teardown
from kdive.jobs.models import (
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
    ExternalBootDerivedReleaseCompletion,
    ExternalBootDerivedTeardownCompletion,
)
from kdive.jobs.payloads import RecoveryRequestV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityConflictResolutionRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityTeardownMutationRequestV1,
    RecoveryObjectBindingV1,
    canonical_teardown_proof_bytes,
)
from kdive.providers.ports.external_boot import RecoveryPoint, RunningKernelObservation

__all__ = [
    "activate_handler",
    "cleanup_handler",
    "recover_handler",
    "release_handler",
    "resolve_conflict_handler",
    "teardown_handler",
]


def _request_deadline(context: OperationContext, fallback: datetime) -> datetime:
    raw = context.job.payload.get("recovery_request_v1")
    if raw is None:
        return fallback
    return RecoveryRequestV1.model_validate(raw).readiness_deadline


_ACTIVATION_EVIDENCE: Final = frozenset({"materialization", "recovery_point"})
_RECOVERY_STATES: Final = frozenset(
    {State.ACTIVE, State.RECOVERED, State.ABANDONED, State.RECOVERY_CONFLICT, State.RECOVERY_FAILED}
)
_CLEANUP_STATES: Final = frozenset(
    {State.RECOVERED, State.ABANDONED, State.RECOVERY_CONFLICT, State.RECOVERY_FAILED}
)
_ORDINARY_CLEANUP_STATES: Final = frozenset({State.RECOVERED, State.ABANDONED})


async def execute_remote_module_lifecycle_on_authority_host(**values: Any) -> Any:
    """Load the remote provider only for a remote lifecycle operation."""
    from kdive.services.remote_module_authority_preparation import (
        execute_remote_module_lifecycle_on_authority_host as execute,
    )

    return await execute(**values)


async def _execute_remote_module_lifecycle(
    context: OperationContext,
    request: AuthorityMutationRequestV1,
    executor: ExternalBootAuthorityExecutor,
) -> None:
    if context.marker.provider_kind != "remote-libvirt" or request.operation not in {
        AuthorityOperation.RECOVER,
        AuthorityOperation.RESOLVE_CONFLICT,
        AuthorityOperation.CLEANUP,
        AuthorityOperation.TEARDOWN,
    }:
        return
    from kdive.db.remote_module_attempt_obligations import (
        ModuleAttemptWorkerWriteContext,
        RemoteModuleAttemptObligationRepository,
    )
    from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1

    raw_preparation = context.job.payload.get("remote_module_attempt_v1")
    if raw_preparation is None:
        raise _refuse("remote module lifecycle authority inputs are incomplete")
    preparation = ModuleAttemptPreparationRequestV1.model_validate(raw_preparation)
    action: Literal["restore", "reap"] = (
        "restore"
        if request.operation in {AuthorityOperation.RECOVER, AuthorityOperation.RESOLVE_CONFLICT}
        else "reap"
    )
    await execute_remote_module_lifecycle_on_authority_host(
        connection=context.connection,
        repository=RemoteModuleAttemptObligationRepository(),
        sender=cast(Any, executor),
        authority=request,
        preparation=preparation,
        worker_context=ModuleAttemptWorkerWriteContext(
            job_id=context.job.id,
            job_attempt=context.job.attempt,
            incarnation_credential=context.incarnation_credential,
            preparation=preparation,
        ),
        action=action,
        deadline=asyncio.get_running_loop().time() + 300.0,
    )


async def _derived_release_status(
    conn: AsyncConnection, sql: LiteralString, args: tuple[Any, ...]
) -> str:
    async with conn.cursor() as cur:
        await cur.execute(sql, args)
        row = await cur.fetchone()
    if row is None or row[0] not in {"applied", "superseded", "conflict", "not_applicable"}:
        raise RuntimeError("derived release function returned an invalid status")
    return str(row[0])


def _release_phase(context: OperationContext, operation: str) -> tuple[str, str]:
    root = {
        "authority_id": str(context.authority.authority_id),
        "generation": context.authority.generation,
        "system_id": str(context.marker.system_id),
        "activation_id": str(context.marker.activation_id),
        "run_id": str(context.marker.run_id),
        "plan_identity": context.marker.plan_identity,
        "provider_kind": context.marker.provider_kind,
        "authority_instance": context.marker.authority_instance,
        "worker_incarnation": context.job.worker_id,
        "root_operation_identity": context.marker.operation_identity,
        "root_operation_digest": context.authority.operation_digest,
    }
    canonical = json.dumps(root | {"operation": operation}, sort_keys=True, separators=(",", ":"))
    identity = hashlib.sha256(
        b"kdive-external-boot-release-phase-identity-v1\0" + canonical.encode()
    ).hexdigest()
    digest = hashlib.sha256(
        b"kdive-external-boot-release-phase-digest-v1\0" + canonical.encode()
    ).hexdigest()
    return f"sha256:{identity}", f"sha256:{digest}"


def _derived_request(context: OperationContext, operation: str) -> AuthorityMutationRequestV1:
    recovery = _recovery(context)
    identity, digest = _release_phase(context, operation)
    return AuthorityMutationRequestV1.model_validate(
        {
            "authority_id": context.authority.authority_id,
            "generation": context.authority.generation,
            "system_id": context.marker.system_id,
            "activation_id": context.marker.activation_id,
            "run_id": context.marker.run_id,
            "plan_identity": context.marker.plan_identity,
            "purpose": "release",
            "operation": operation,
            "provider_kind": context.marker.provider_kind,
            "authority_instance": context.marker.authority_instance,
            "operation_identity": identity,
            "operation_digest": digest,
            "attempt_id": uuid5(NAMESPACE_URL, identity),
            "expected_source_identity": recovery.source_state.definition,
            "intended_target_identity": recovery.target_state.definition,
            "recovery_objects": (
                ()
                if operation == "recover"
                else (
                    RecoveryObjectBindingV1(
                        system_id=context.marker.system_id,
                        activation_id=context.marker.activation_id,
                        reference=recovery.recovery_ref.ref,
                    ),
                )
            ),
        }
    )


async def _run_active_release(context: OperationContext) -> ExternalBootDerivedReleaseCompletion:
    """Commit the two derived mutations before the one root release completion."""
    executor = context.prerequisites.get("authority_executor")
    if executor is None:
        raise _refuse("no external-boot authority executor is configured")
    credential = context.prerequisites["incarnation_credential"]
    recover = _derived_request(context, "recover")
    if context.activation.state is not State.RECOVERED:
        status = await _derived_release_status(
            context.prerequisites["connection"],
            "SELECT public.begin_external_boot_derived_release_recovery(%s,%s,%s,%s,%s,%s,%s)",
            (
                credential,
                context.job.id,
                context.job.attempt,
                context.authority.authority_id,
                context.authority.generation,
                recover.attempt_id,
                context.prerequisites["deadline"],
            ),
        )
        if status != "applied":
            raise CategorizedError(
                "derived release recovery was superseded", category=ErrorCategory.STALE_HANDLE
            )
        await _execute_remote_module_lifecycle(
            context, recover, cast(ExternalBootAuthorityExecutor, executor)
        )
        observed = await cast(ExternalBootAuthorityExecutor, executor).execute(recover)
        _require_category(context, observed, "source")
        evidence = terminal_evidence(context, "recovered")
        status = await _derived_release_status(
            context.prerequisites["connection"],
            "SELECT public.commit_external_boot_derived_release_recovery("
            "%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (
                credential,
                context.job.id,
                context.job.attempt,
                context.authority.authority_id,
                context.authority.generation,
                recover.operation_identity,
                recover.operation_digest,
                json.dumps(evidence),
            ),
        )
        if status != "applied":
            raise CategorizedError(
                "derived release recovery commit was superseded",
                category=ErrorCategory.STALE_HANDLE,
            )
    cleanup = _derived_request(context, "cleanup")
    await _execute_remote_module_lifecycle(
        context, cleanup, cast(ExternalBootAuthorityExecutor, executor)
    )
    observed = await cast(ExternalBootAuthorityExecutor, executor).execute(cleanup)
    _require_category(context, observed, "absent")
    status = await _derived_release_status(
        context.prerequisites["connection"],
        "SELECT public.adopt_external_boot_release_cleanup_receipt_from_head(%s,%s,%s,%s,%s,%s)",
        (
            credential,
            context.job.id,
            context.job.attempt,
            context.authority.authority_id,
            context.authority.generation,
            observed.composite_state,
        ),
    )
    if status == "not_applicable":
        status = await _derived_release_status(
            context.prerequisites["connection"],
            "SELECT public.record_external_boot_release_cleanup_receipt_from_head("
            "%s,%s,%s,%s,%s,%s)",
            (
                credential,
                context.job.id,
                context.job.attempt,
                context.authority.authority_id,
                context.authority.generation,
                observed.composite_state,
            ),
        )
    if status != "applied":
        raise CategorizedError(
            "derived release cleanup receipt was superseded", category=ErrorCategory.STALE_HANDLE
        )
    reservation = context.prerequisites["reservation"]
    release = {
        "schema": "external-boot-release-evidence-v1",
        "activation_id": str(context.marker.activation_id),
        "system_id": str(context.marker.system_id),
        "store_identity": {"ref": reservation["store_identity"]},
        "owner_key": {"ref": reservation["owner_key"]},
        "reserved_bytes": reservation["reserved_bytes"],
        "enumeration_complete": True,
        "objects": [],
        "verified_at": _now(),
    }
    identity = evidence_digest(release)
    cleanup_evidence = {
        "schema": "external-boot-cleanup-evidence-v1",
        "activation_id": str(context.marker.activation_id),
        "system_id": str(context.marker.system_id),
        "release_identity": identity,
        "mode": "ordinary",
        "completed_at": _now(),
    }
    status = await _derived_release_status(
        context.prerequisites["connection"],
        "SELECT public.finalize_external_boot_derived_release("
        "%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
        (
            credential,
            context.job.id,
            context.job.attempt,
            context.authority.authority_id,
            context.authority.generation,
            identity,
            json.dumps(release),
            json.dumps(cleanup_evidence),
        ),
    )
    if status != "applied":
        raise CategorizedError(
            "derived release finalization was superseded", category=ErrorCategory.STALE_HANDLE
        )
    return ExternalBootDerivedReleaseCompletion.model_validate(
        authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "release",
                "result_ref": None,
                "release_identity": identity,
                "evidence": release,
            },
        ).model_dump(mode="json", by_alias=True)
    )


def _refuse(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, terminal=True)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _recovery(context: OperationContext) -> RecoveryPoint:
    """The persisted recovery point, guaranteed present by ``require_activation_evidence``."""
    recovery = context.activation.recovery_point
    if recovery is None:  # pragma: no cover - the evidence check refuses before the port is called
        raise _refuse(f"activation {context.marker.activation_id} has no recovery_point")
    return recovery


def _require_category(
    context: OperationContext, observation: AuthorityObservationV1, expected: str
) -> None:
    """The ``observe`` call is a post-mutation liveness precondition, and this is its whole point.

    Nothing in ``ExternalBootTerminalEvidenceV1`` consumes the observation — ``composite_state`` is
    the acknowledgement's digest, not one derived from it. The call exists so the operation can
    refuse to emit terminal evidence when the running kernel is not the one the activation's
    persisted ``materialization.kernel_observation`` records. The value is discarded after this
    comparison.
    """
    if observation.category != expected:
        raise _refuse(
            f"authority observed {observation.category!r} for {context.marker.operation!r}; "
            f"expected {expected!r}"
        )


def _require_observed_kernel_matches(
    context: OperationContext, observation: RunningKernelObservation | None
) -> None:
    materialization = context.activation.materialization
    if observation is None or materialization is None:
        raise _refuse(
            f"activation {context.marker.activation_id} produced no kernel observation to verify"
        )
    if observation.identity != materialization.kernel_observation:
        raise _refuse(
            f"the running kernel observed for activation {context.marker.activation_id} is not the "
            "one its persisted materialization records"
        )
    if observation.cmdline != observation.expected_cmdline:
        limit = min(len(observation.cmdline), len(observation.expected_cmdline))
        offset = next(
            (
                index
                for index in range(limit)
                if observation.cmdline[index] != observation.expected_cmdline[index]
            ),
            limit,
        )
        raise _CommandLineMismatch(
            observation.expected_cmdline[:2048], observation.cmdline[:2048], offset
        )


def _mutation_request(context: OperationContext) -> AuthorityMutationRequestV1:
    recovery = _recovery(context)
    attempt_id = context.activation.current_attempt_id or uuid5(
        NAMESPACE_URL, f"kdive/external-boot/{context.marker.operation_identity}"
    )
    objects = ()
    if context.marker.operation in {"cleanup", "teardown"}:
        objects = (
            RecoveryObjectBindingV1(
                system_id=context.marker.system_id,
                activation_id=context.marker.activation_id,
                reference=recovery.recovery_ref.ref,
            ),
        )
    values = {
        "authority_id": context.authority.authority_id,
        "generation": context.authority.generation,
        "system_id": context.marker.system_id,
        "activation_id": context.marker.activation_id,
        "run_id": context.marker.run_id,
        "plan_identity": context.marker.plan_identity,
        "purpose": context.marker.purpose,
        "operation": context.marker.operation,
        "provider_kind": context.marker.provider_kind,
        "authority_instance": context.marker.authority_instance,
        "operation_identity": context.marker.operation_identity,
        "operation_digest": context.authority.operation_digest,
        "attempt_id": attempt_id,
        "expected_source_identity": recovery.source_state.definition,
        "intended_target_identity": recovery.target_state.definition,
        "recovery_objects": objects,
    }
    if context.marker.expected_observed_composite is not None:
        values["expected_observed_composite"] = context.marker.expected_observed_composite
        return AuthorityConflictResolutionRequestV1.model_validate(values)
    return AuthorityMutationRequestV1.model_validate(values)


async def _execute(
    context: OperationContext,
) -> tuple[AuthorityObservationV1, RunningKernelObservation | None]:
    executor = context.authority_executor
    if executor is None:
        raise _refuse("no external-boot authority executor is configured")
    request = _mutation_request(context)
    await _execute_remote_module_lifecycle(context, request, executor)
    if isinstance(request, AuthorityConflictResolutionRequestV1):
        authority_observation = await cast(Any, executor).execute_conflict_resolution(request)
    else:
        authority_observation = await executor.execute(request)
    kernel_observation = None
    if authority_observation.category == "target":
        running_reader = getattr(executor, "observe_running", None)
        if running_reader is not None:
            kernel_observation = await running_reader(request)
        elif context.port is not None:
            kernel_observation = context.port.observe(_recovery(context), authority_ref(context))
        else:
            raise _refuse("no external-boot running observation reader is configured")
    return authority_observation, kernel_observation


async def _no_preconditions(
    _conn: AsyncConnection,
    _activation: ExternalBootActivation,
    _marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    return {}


async def _attempt_state(
    conn: AsyncConnection, activation: ExternalBootActivation
) -> tuple[str, datetime | None] | None:
    if activation.current_attempt_id is None:
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, recovery_readiness_deadline "
            "FROM external_boot_recovery_attempts WHERE attempt_id = %s",
            (activation.current_attempt_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return str(row["state"]), row["recovery_readiness_deadline"]


def _require_attempt_state(expected: str) -> Callable[..., Awaitable[Mapping[str, Any]]]:
    """Footnote †: the current recovery-attempt row must already be in ``expected``.

    **Nothing in this change creates or advances that row.** It is written by the
    ``recovery-attempt`` operation, which ADR-0593 decision 4 excludes as #2202's. So in production
    the ``recover`` and ``resolve-conflict`` handlers are registered and reachable but cannot reach
    an applied commit until #2202 supplies the attempt row; their tests seed it directly. This is
    stated rather than worked around.
    """

    async def check(
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        marker: ExternalBootAuthorityMarkerV1,
    ) -> Mapping[str, Any]:
        attempt = await _attempt_state(conn, activation)
        state = None if attempt is None else attempt[0]
        if state != expected:
            raise _refuse(
                f"{marker.operation!r} requires the current recovery attempt in {expected!r}, "
                f"not {state!r}"
            )
        return {"attempt_deadline": attempt[1] if attempt is not None else None}

    return check


async def _reservation(conn: AsyncConnection, activation_id: Any) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT store_identity, owner_key, reserved_bytes FROM external_boot_reservations "
            "WHERE activation_id = %s AND state = 'ready'",
            (activation_id,),
        )
        row = await cur.fetchone()
    return None if row is None else dict(row)


async def _release_row(conn: AsyncConnection, activation_id: Any) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT release_identity FROM external_boot_reservation_releases "
            "WHERE activation_id = %s",
            (activation_id,),
        )
        row = await cur.fetchone()
    return None if row is None else dict(row)


async def _require_releasable(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    """Footnote ‡ for ``release``: a ready reservation, and no release recorded yet."""
    if await _release_row(conn, activation.id) is not None:
        raise _refuse(f"activation {marker.activation_id} already has a recorded release")
    reservation = await _reservation(conn, activation.id)
    if reservation is None:
        raise _refuse(f"activation {marker.activation_id} has no ready recovery-store reservation")
    return {"reservation": reservation}


async def _require_cleanable(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    """Footnote ‡ for ``cleanup``/``teardown``: not already cleaned, and a release to name."""
    if activation.cleanup_complete:
        raise _refuse(f"activation {marker.activation_id} is already cleanup_complete")
    release = await _release_row(conn, activation.id)
    if release is None:
        raise _refuse(
            f"{marker.operation!r} requires a recorded release for activation "
            f"{marker.activation_id}"
        )
    return {"release_identity": release["release_identity"]}


async def _teardown_prerequisites(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    """Admit every restricting activation, including one with only a pending reservation."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT systems.state, latest.id AS latest_activation_id FROM systems "
            "LEFT JOIN LATERAL ("
            "SELECT id FROM external_boot_activations "
            "WHERE system_id = systems.id ORDER BY created_at DESC, id DESC LIMIT 1"
            ") AS latest ON true WHERE systems.id = %s",
            (activation.system_id,),
        )
        row = await cur.fetchone()
    admitted_states = {
        "provisioning",
        "ready",
        "reprovisioning",
        "restoring",
        "paused",
        "crashing",
        "crashed",
        "failed",
    }
    if (
        row is None
        or row["state"] not in admitted_states
        or row["latest_activation_id"] != activation.id
    ):
        raise _refuse(
            f"teardown requires the newest activation on nonterminal system {activation.system_id}"
        )
    return {"connection": conn}


def _handler(
    ports: ExternalBootHandlerPorts,
    *,
    require_activation_state: frozenset[State],
    require_activation_evidence: frozenset[str],
    require_preconditions: Callable[..., Awaitable[Mapping[str, Any]]],
    expected_observation: str,
    build_result: Callable[
        [OperationContext, AuthorityObservationV1], ExternalBootAuthoritySuccessV1
    ],
    before_port: Callable[
        [OperationContext],
        ExternalBootAuthoritySuccessV1 | None | Awaitable[ExternalBootAuthoritySuccessV1 | None],
    ]
    | None = None,
) -> ExternalBootOperationHandler:
    async def handler(
        conn: AsyncConnection, job: Job, marker: ExternalBootAuthorityMarkerV1
    ) -> ExternalBootAuthoritySuccessV1:
        def checked_build(
            context: OperationContext,
            observations: tuple[AuthorityObservationV1, RunningKernelObservation | None],
        ) -> ExternalBootAuthoritySuccessV1:
            authority_observation, kernel_observation = observations
            _require_category(context, authority_observation, expected_observation)
            if expected_observation == "target":
                _require_observed_kernel_matches(context, kernel_observation)
            return build_result(context, authority_observation)

        return await run_operation(
            conn,
            job,
            marker,
            ports=ports,
            require_activation_state=require_activation_state,
            require_activation_evidence=require_activation_evidence,
            require_preconditions=lambda conn, activation, marker: _with_executor(
                require_preconditions, ports, conn, activation, marker
            ),
            call_port=_execute,
            build_result=checked_build,
            before_port=before_port,
        )

    return handler


async def _with_executor(
    preconditions: Callable[..., Awaitable[Mapping[str, Any]]],
    ports: ExternalBootHandlerPorts,
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    values = dict(await preconditions(conn, activation, marker))
    values["authority_executor"] = ports.authority_executor
    return values


def activate_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Prepare and activate the external boot, then confirm the expected running kernel.

    ADR-0608 admits ``preparing`` so the same claimed job and authority generation own
    materialize, prepare, deadline, and activate.  ``prepared`` and ``activating`` remain the
    restart states after those durable intermediate commits.
    """

    def build(
        context: OperationContext, observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        deadline = context.activation.activation_readiness_deadline
        if deadline is None:
            raise _refuse("activating row has no activation readiness deadline")
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "activate",
                "result_ref": None,
                "evidence": terminal_evidence(context, "active"),
                "activation_readiness_deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    def before_port(context: OperationContext) -> ExternalBootAuthoritySuccessV1 | None:
        if context.activation.state is not State.PREPARED:
            deadline = context.activation.activation_readiness_deadline
            if deadline is not None and ports.clock() >= deadline:
                raise CategorizedError(
                    "activation readiness deadline expired",
                    category=ErrorCategory.BOOT_TIMEOUT,
                    terminal=True,
                )
            return None
        deadline = ports.clock() + ports.activation_readiness_timeout
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "deadline",
                "deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    return _handler(
        ports,
        require_activation_state=frozenset({State.PREPARING, State.PREPARED, State.ACTIVATING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_no_preconditions,
        expected_observation="target",
        build_result=build,
        before_port=before_port,
    )


def _recovering_handler(
    ports: ExternalBootHandlerPorts,
    *,
    operation: str,
    state: State,
    attempt_state: str,
) -> ExternalBootOperationHandler:
    """``recover`` and ``resolve-conflict`` differ in three parameters, so they share a body."""

    def build(
        context: OperationContext, observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": operation,
                "result_ref": None,
                "evidence": terminal_evidence(context, "recovered"),
            },
        )

    return _handler(
        ports,
        require_activation_state=frozenset({state}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_require_attempt_state(attempt_state),
        expected_observation="source",
        build_result=build,
    )


def recover_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Re-run the recovery point and confirm the kernel, under a ``recovering`` attempt row."""

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recover",
                "result_ref": None,
                "evidence": terminal_evidence(context, "recovered"),
            },
        )

    def before_port(context: OperationContext) -> ExternalBootAuthoritySuccessV1 | None:
        if context.activation.state is not State.ACTIVE:
            deadline = context.prerequisites.get("attempt_deadline")
            if deadline is not None and ports.clock() >= deadline:
                raise CategorizedError(
                    "recovery readiness deadline expired",
                    category=ErrorCategory.READINESS_FAILURE,
                    terminal=True,
                )
            return None
        deadline = _request_deadline(context, ports.clock() + ports.recovery_readiness_timeout)
        if ports.clock() >= deadline:
            raise CategorizedError(
                "recovery readiness deadline expired",
                category=ErrorCategory.READINESS_FAILURE,
                terminal=True,
            )
        attempt_id = uuid5(
            NAMESPACE_URL, f"kdive/external-boot/{context.marker.operation_identity}"
        )
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recovery-attempt",
                "attempt_id": str(attempt_id),
                "recovery_basis": "recovery_point",
                "deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    async def require_attempt(
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        marker: ExternalBootAuthorityMarkerV1,
    ) -> Mapping[str, Any]:
        if activation.state is State.ACTIVE:
            return {}
        return await _require_attempt_state("recovering")(conn, activation, marker)

    return _handler(
        ports,
        require_activation_state=frozenset({State.ACTIVE, State.RECOVERING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=require_attempt,
        expected_observation="source",
        build_result=build,
        before_port=before_port,
    )


def resolve_conflict_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Start a new recovery window before resolving a parked conflict."""

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "resolve-conflict",
                "result_ref": None,
                "evidence": terminal_evidence(context, "recovered"),
            },
        )

    async def before_port(context: OperationContext) -> ExternalBootAuthoritySuccessV1 | None:
        if context.activation.state is State.RECOVERING:
            deadline = context.prerequisites.get("attempt_deadline")
            if deadline is not None and ports.clock() >= deadline:
                raise CategorizedError(
                    "recovery readiness deadline expired",
                    category=ErrorCategory.READINESS_FAILURE,
                    terminal=True,
                )
            return None
        executor = context.authority_executor
        if executor is None:
            raise _refuse("no external-boot authority executor is configured")
        expected = context.marker.expected_observed_composite
        if expected is None:
            raise _refuse("resolve-conflict has no observed composite binding")
        observation = await cast(Any, executor).observe(_mutation_request(context))
        if observation.category == "unreadable" or observation.composite_state != expected:
            raise CategorizedError(
                "authority observation no longer matches the conflict binding",
                category=ErrorCategory.STALE_HANDLE,
                terminal=True,
            )
        deadline = _request_deadline(context, ports.clock() + ports.recovery_readiness_timeout)
        if ports.clock() >= deadline:
            raise CategorizedError(
                "recovery readiness deadline expired",
                category=ErrorCategory.READINESS_FAILURE,
                terminal=True,
            )
        attempt_id = uuid5(
            NAMESPACE_URL, f"kdive/external-boot/{context.marker.operation_identity}"
        )
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recovery-attempt",
                "attempt_id": str(attempt_id),
                "recovery_basis": "pre_recovery",
                "deadline": deadline.isoformat().replace("+00:00", "Z"),
                "observed_composite_state": expected,
            },
        )

    async def require_attempt(
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        marker: ExternalBootAuthorityMarkerV1,
    ) -> Mapping[str, Any]:
        if activation.state is State.RECOVERY_CONFLICT:
            await _require_attempt_state("conflict")(conn, activation, marker)
            return {}
        return await _require_attempt_state("recovering")(conn, activation, marker)

    return _handler(
        ports,
        require_activation_state=frozenset({State.RECOVERY_CONFLICT, State.RECOVERING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=require_attempt,
        expected_observation="source",
        build_result=build,
        before_port=before_port,
    )


def release_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Recover, clean, then credit only after the authority proves owned storage absence.

    The root release job drives its derived ``recover`` and ``cleanup`` phases in the authority
    journal.  The worker records an exact cleanup receipt from the terminal ``absent`` head before
    the SQL finalizer deletes the ready reservation and writes its one release credit.  It does not
    infer absence from a release request or credit while owned artifacts remain.
    """

    async def complete(context: OperationContext) -> ExternalBootDerivedReleaseCompletion:
        prerequisites = dict(context.prerequisites)
        prerequisites.update(
            connection=context.prerequisites["connection"],
            incarnation_credential=hashlib.sha256(
                ports.incarnation_credential.get_secret_value().encode("utf-8")
            ).digest(),
            deadline=_request_deadline(context, ports.clock() + ports.recovery_readiness_timeout),
        )
        return await _run_active_release(replace(context, prerequisites=prerequisites))

    async def handler(
        conn: AsyncConnection, job: Job, marker: ExternalBootAuthorityMarkerV1
    ) -> ExternalBootAuthoritySuccessV1:
        return cast(
            ExternalBootAuthoritySuccessV1,
            await run_operation(
                conn,
                job,
                marker,
                ports=ports,
                require_activation_state=frozenset(
                    {State.ACTIVE, State.RECOVERING, State.RECOVERED}
                ),
                require_activation_evidence=_ACTIVATION_EVIDENCE,
                require_preconditions=lambda conn, activation, marker: _with_executor(
                    _release_prerequisites, ports, conn, activation, marker
                ),
                call_port=_unreachable_release_port,
                build_result=_unreachable_release_result,
                before_port=complete,
            ),
        )

    return handler


def _unreachable_release_port(_context: OperationContext) -> None:
    raise AssertionError("derived release completion must run before a provider port")


def _unreachable_release_result(
    _context: OperationContext, _observation: object
) -> ExternalBootDerivedReleaseCompletion:
    raise AssertionError("derived release completion must run before result construction")


async def _release_prerequisites(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    values = dict(await _require_releasable(conn, activation, marker))
    values["connection"] = conn
    return values


def _cleanup_evidence(
    context: OperationContext, *, teardown_identity: str | None
) -> dict[str, Any]:
    ordinary = context.activation.state in _ORDINARY_CLEANUP_STATES
    evidence: dict[str, Any] = {
        "schema": "external-boot-cleanup-evidence-v1",
        "activation_id": str(context.marker.activation_id),
        "system_id": str(context.marker.system_id),
        "release_identity": context.prerequisites["release_identity"],
        "mode": "ordinary" if ordinary else "system_teardown",
        "completed_at": _now(),
    }
    if teardown_identity is not None:
        evidence["teardown_identity"] = teardown_identity
    return evidence


def cleanup_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Delete the recovery-store objects the release credited, naming the release it completes.

    ``mode`` is derived from the activation state rather than chosen: ``ordinary`` for
    ``recovered``/``abandoned`` and ``system_teardown`` for ``recovery_conflict``/
    ``recovery_failed``, which is exactly the pairing the commit re-checks
    (``0122…sql:1400-1413``) — and ``teardown_identity`` must be present for the second and absent
    for the first.
    """

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        ordinary = context.activation.state in _ORDINARY_CLEANUP_STATES
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "cleanup",
                "result_ref": None,
                "evidence": _cleanup_evidence(
                    context,
                    teardown_identity=(
                        None if ordinary else evidence_digest(_teardown_evidence(context))
                    ),
                ),
            },
        )

    return _handler(
        ports,
        require_activation_state=_CLEANUP_STATES,
        require_activation_evidence=frozenset({"recovery_point"}),
        require_preconditions=_require_cleanable,
        expected_observation="absent",
        build_result=build,
    )


def _teardown_evidence(context: OperationContext) -> dict[str, Any]:
    return {
        "schema": "external-boot-teardown-evidence-v1",
        "system_id": str(context.marker.system_id),
        "system_state": "torn_down",
        "observed_at": _now(),
    }


def teardown_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Route full System teardown through the authority's terminal proof receipt."""

    async def complete(context: OperationContext) -> ExternalBootDerivedTeardownCompletion:
        executor = ports.teardown_executor
        if executor is None:
            raise _refuse("no external-boot authority teardown executor is configured")
        artifact_store = ports.artifact_store
        if artifact_store is None:
            raise _refuse("no System artifact store is configured for authority teardown")
        request = AuthorityTeardownMutationRequestV1(
            authority_id=context.authority.authority_id,
            generation=context.authority.generation,
            system_id=context.marker.system_id,
            activation_id=context.marker.activation_id,
            run_id=context.marker.run_id,
            plan_identity=context.marker.plan_identity,
            purpose="teardown",
            operation="teardown",
            provider_kind=context.marker.provider_kind,
            authority_instance=context.marker.authority_instance,
            operation_identity=context.marker.operation_identity,
            operation_digest=context.authority.operation_digest,
            attempt_id=uuid5(NAMESPACE_URL, context.marker.operation_identity),
        )
        response = await executor.execute_teardown(request)
        if response.proof.disposition != "retained_quarantine":
            await reclaim_system_core_after_provider_teardown(
                context.prerequisites["connection"],
                artifact_store,
                context.marker.system_id,
                reclaim_snapshot_ledger=True,
                discharge_mutation_obligations=False,
            )
        async with context.prerequisites["connection"].cursor() as cursor:
            await cursor.execute(
                "SELECT public.finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    hashlib.sha256(
                        ports.incarnation_credential.get_secret_value().encode()
                    ).digest(),
                    context.job.id,
                    context.job.attempt,
                    context.authority.authority_id,
                    context.authority.generation,
                    response.journal_sequence,
                    response.journal_digest,
                    canonical_teardown_proof_bytes(response.proof),
                ),
            )
            row = await cursor.fetchone()
        if row is None or row[0] not in {"applied", "retained"}:
            raise CategorizedError(
                "teardown receipt was superseded", category=ErrorCategory.STALE_HANDLE
            )
        carrier = {
            "schema": "external-boot-authority-result-v1",
            "authority_id": context.authority.authority_id,
            "generation": context.authority.generation,
            "activation_id": context.marker.activation_id,
            "run_id": context.marker.run_id,
            "system_id": context.marker.system_id,
            "plan_identity": context.marker.plan_identity,
            "purpose": context.marker.purpose,
            "provider_kind": context.marker.provider_kind,
            "authority_instance": context.marker.authority_instance,
            "admitted_operation": context.marker.operation,
            "operation_identity": context.marker.operation_identity,
            "operation_digest": context.authority.operation_digest,
            "journal_sequence": response.journal_sequence,
            "journal_digest": response.journal_digest,
            "result": {
                "schema": "external-boot-authority-result-v1",
                "operation": "teardown",
                "result_ref": None,
                "response": response.model_dump(mode="json", by_alias=True),
            },
        }
        return ExternalBootDerivedTeardownCompletion.model_validate(carrier)

    return _handler(
        ports,
        require_activation_state=frozenset(set(State) - {State.TORN_DOWN}),
        require_activation_evidence=frozenset(),
        require_preconditions=lambda conn, activation, marker: _teardown_prerequisites(
            conn, activation, marker
        ),
        expected_observation="absent",
        build_result=_unreachable_teardown_result,
        before_port=complete,
    )


def _unreachable_teardown_result(
    _context: OperationContext, _observation: object
) -> ExternalBootDerivedTeardownCompletion:
    raise AssertionError("authority teardown completion must run before result construction")
