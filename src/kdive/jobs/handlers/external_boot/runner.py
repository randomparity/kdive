"""The seven steps every external-boot operation handler shares (spec §6)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.authority import AllocatedAuthority, allocate_authority
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityFailureV1,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthorityResultV1,
)
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityTakeoverRequestV1,
)
from kdive.providers.ports.external_boot import (
    ExternalBootPorts,
    OpaqueProviderRef,
    RunningKernelObservation,
)

__all__ = ["OperationContext", "authority_ref", "run_operation"]

_ACTIVATIONS = ExternalBootActivationRepository()

# The phase a raise is attributed to. `_FailureContext.phase` admits a closed Literal and the
# commit re-checks it, so a name invented here is refused at SQL rather than stored.
#
# Only these two are reachable, and the reason is worth stating: a failure is committable only once
# an acknowledgement exists, because commit_external_boot_authority_result refuses unless
# `v_ack.authority_id IS NOT NULL` and `v_ack.journal_sequence`/`journal_digest` equal the values
# the result carries (0122_external_boot_authority.sql:904, :937-940) — and that precondition block
# runs for every operation, `fail` included. So a raise from steps 1-3 (no authority yet) and a
# raise from step 4 (no acknowledgement yet) cannot be committed as failures at all; they propagate
# as themselves and the job wedges per the specification's §8, which is #2203's to reap. Emitting a
# bound failure for them would mean inventing a journal sequence and digest, producing a result that
# looks committable, is refused as `superseded`, and writes no `jobs` row either way.
_PROVIDER_CALL = "provider-call"
_COMMIT = "commit"


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Everything an operation handler needs after admission succeeded."""

    job: Job
    marker: ExternalBootAuthorityMarkerV1
    activation: ExternalBootActivation
    binding: ProviderBinding
    port: ExternalBootPorts
    authority: AllocatedAuthority
    acknowledgement: AuthorityAcknowledgementV1
    prerequisites: Mapping[str, Any] = field(default_factory=dict)
    """Whatever ``require_preconditions`` read, so ``build_result`` need not read it again.

    ``build_result`` is synchronous and holds no connection, and the rows an operation's evidence
    must copy verbatim — the ready reservation for ``release``, the recorded release for
    ``cleanup``/``teardown`` — are exactly the rows its precondition already had to read. Carrying
    them forward is one query rather than two, and removes the window in which the second read
    could see a different row than the check approved.
    """


def _refuse(message: str) -> CategorizedError:
    """Every admission refusal is the same shape: terminal, configuration_error."""
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, terminal=True)


def authority_ref(context: OperationContext) -> OpaqueProviderRef:
    """The opaque handle a port call carries, naming the exact allocated generation."""
    return OpaqueProviderRef(
        ref=f"authority/{context.authority.authority_id}/{context.authority.generation}"
    )


async def _resolve_port(
    conn: AsyncConnection,
    marker: ExternalBootAuthorityMarkerV1,
    ports: ExternalBootHandlerPorts,
) -> tuple[ProviderBinding, ExternalBootPorts]:
    """Step 1. Refuse a marker whose provider_kind disagrees with the System's bound runtime."""
    binding = await ports.resolver.binding_for_system(conn, marker.system_id)
    if binding.kind.value != marker.provider_kind:
        raise _refuse(
            f"marker provider_kind {marker.provider_kind!r} does not match the "
            f"{binding.kind.value!r} runtime bound for system {marker.system_id}"
        )
    if binding.runtime.external_boot is None:
        raise _refuse(
            f"the {binding.kind.value!r} runtime bound for system {marker.system_id} "
            "has no external_boot port"
        )
    return binding, binding.runtime.external_boot


async def _read_activation(
    conn: AsyncConnection,
    marker: ExternalBootAuthorityMarkerV1,
    *,
    require_activation_state: frozenset[ExternalBootActivationState],
    require_activation_evidence: frozenset[str],
) -> ExternalBootActivation:
    """Steps 2, 2a and 2b. ``SELECT``-only, which is all the worker's grant permits."""
    activation = await _ACTIVATIONS.get(conn, marker.activation_id)
    if activation is None:
        raise _refuse(f"activation {marker.activation_id} does not exist")
    if (
        activation.run_id != marker.run_id
        or activation.system_id != marker.system_id
        or activation.plan_identity != marker.plan_identity
    ):
        raise _refuse(
            f"activation {marker.activation_id} does not match the marker's run, system, or plan"
        )
    if activation.state not in require_activation_state:
        raise _refuse(
            f"activation {marker.activation_id} is {activation.state.value!r}, which "
            f"{marker.operation!r} does not admit"
        )
    # A positive check on the column the operation will read, never an inference from the state.
    # external_boot_activation_state_evidence admits `abandoned` on terminal_evidence alone, and
    # admits the recovery states with a NULL recovery_point whenever pre_recovery_evidence is
    # present — so state does not imply the evidence is there. A missing recovery point and a
    # completed operation are different propositions and only the column distinguishes them.
    for column in sorted(require_activation_evidence):
        if getattr(activation, column) is None:
            raise _refuse(
                f"activation {marker.activation_id} has no {column}, which {marker.operation!r} "
                "reads"
            )
    return activation


async def _acknowledge(
    ports: ExternalBootHandlerPorts,
    marker: ExternalBootAuthorityMarkerV1,
    authority: AllocatedAuthority,
) -> AuthorityAcknowledgementV1:
    """Step 4. Fails closed when unwired, **before** the provider is touched."""
    if ports.acknowledger is None:
        raise _refuse(
            "no external-boot authority acknowledger is configured; the commit would be "
            "superseded and the provider must not be mutated first"
        )
    request = AuthorityTakeoverRequestV1(
        schema="external-boot-authority-v1",
        authority_id=authority.authority_id,
        generation=authority.generation,
        system_id=marker.system_id,
        activation_id=marker.activation_id,
        run_id=marker.run_id,
        plan_identity=marker.plan_identity,
        purpose=marker.purpose,
        operation=marker.operation,
        provider_kind=marker.provider_kind,
        authority_instance=marker.authority_instance,
        operation_identity=marker.operation_identity,
        operation_digest=authority.operation_digest,
    )
    return await ports.acknowledger.acknowledge(request)


def _bound_failure(
    context: OperationContext, exc: Exception, *, phase: str
) -> ExternalBootAuthorityFailure:
    """Wrap a raise as a failure bound to the same allocation and acknowledgement.

    Takes the whole context rather than the marker and allocation, because the journal facts are
    the **acknowledgement's** and the commit compares them to the stored row. Nothing here invents
    a journal sequence or digest; a failure with no acknowledgement to draw them from is not
    representable, which is the point.

    ``from None``, never ``from exc``. ``_FailureContext`` admits one field — a closed-``Literal``
    ``phase`` — so the original message never reaches the authority audit by value, and the commit
    re-checks that. Chaining would re-attach the provider's own exception, which for a real adapter
    can carry a host filesystem path in ``OSError.filename``/``.strerror``, to a traceback a worker
    log and from there a CI log can render. ``from None`` is the difference between a bound that
    holds and one that looks like it holds.
    """
    marker, authority = context.marker, context.authority
    category = (
        exc.category if isinstance(exc, CategorizedError) else ErrorCategory.INFRASTRUCTURE_FAILURE
    )
    terminal = exc.terminal if isinstance(exc, CategorizedError) else False
    return ExternalBootAuthorityFailure(
        ExternalBootAuthorityFailureV1.model_validate(
            {
                "schema": "external-boot-authority-result-v1",
                "authority_id": authority.authority_id,
                "generation": authority.generation,
                "activation_id": marker.activation_id,
                "run_id": marker.run_id,
                "system_id": marker.system_id,
                "plan_identity": marker.plan_identity,
                "purpose": marker.purpose,
                "provider_kind": marker.provider_kind,
                "authority_instance": marker.authority_instance,
                "admitted_operation": marker.operation,
                "operation_identity": marker.operation_identity,
                "operation_digest": authority.operation_digest,
                "journal_sequence": context.acknowledgement.journal_sequence,
                "journal_digest": context.acknowledgement.journal_digest,
                "result": {
                    "schema": "external-boot-authority-result-v1",
                    "operation": "fail",
                    "error_category": category,
                    "failure_context": {"phase": phase},
                    "terminal": terminal,
                },
            }
        )
    )


async def run_operation[R: ExternalBootAuthorityResultV1](
    conn: AsyncConnection,
    job: Job,
    marker: ExternalBootAuthorityMarkerV1,
    *,
    ports: ExternalBootHandlerPorts,
    require_activation_state: frozenset[ExternalBootActivationState],
    require_activation_evidence: frozenset[str],
    require_preconditions: Callable[
        [AsyncConnection, ExternalBootActivation, ExternalBootAuthorityMarkerV1],
        Awaitable[Mapping[str, Any]],
    ],
    call_port: Callable[[OperationContext], RunningKernelObservation | None],
    build_result: Callable[[OperationContext, RunningKernelObservation | None], R],
) -> R:
    """Run one authority-bound operation and return its result for the worker to commit.

    Steps 1, 2, 2a, 2b and 2c all run **before** allocation, so every refusal happens while there
    is still no authority. That matters beyond tidiness: a refusal *after* allocation, and a
    ``superseded`` allocation, cannot be committed as a failure at all — the commit's ``fail``
    branch needs a binding — so the job keeps its lease, is re-claimed when it lapses, burns an
    attempt, and eventually wedges ``running``. Refusing before allocation costs one database read
    and avoids that.

    The handler does **not** call the commit. It returns its result and the worker's
    ``_finalize_handler`` commits it, gated on ``_authority_binding_matches`` — the one check
    standing between a mismatched result and the authority tables.

    Generic in the result type so the runner returns exactly what ``build_result`` produced. That
    matters because ``_commit_external_result`` dispatches on ``isinstance``: collapsing an
    ``ExternalBootAuthoritySuccessV1`` to the base class here would make a handler that commits
    nothing type-check clean.
    """
    binding, port = await _resolve_port(conn, marker, ports)
    activation = await _read_activation(
        conn,
        marker,
        require_activation_state=require_activation_state,
        require_activation_evidence=require_activation_evidence,
    )
    prerequisites = await require_preconditions(conn, activation, marker)

    authority = await allocate_authority(
        conn, job, marker, incarnation_credential=ports.incarnation_credential
    )
    if authority is None:
        raise CategorizedError(
            f"external boot authority allocation for job {job.id} was superseded",
            category=ErrorCategory.STALE_HANDLE,
            terminal=False,
        )

    # Not wrapped: with no acknowledgement there are no journal facts to bind, and a failure result
    # carrying invented ones is refused as `superseded` anyway. See the phase constants above.
    acknowledgement = await _acknowledge(ports, marker, authority)

    context = OperationContext(
        job=job,
        marker=marker,
        activation=activation,
        binding=binding,
        port=port,
        authority=authority,
        acknowledgement=acknowledgement,
        prerequisites=prerequisites,
    )
    try:
        # ExternalBootPorts is sync, like every other provider surface jobs/handlers/ calls.
        observation = await asyncio.to_thread(call_port, context)
    except Exception as exc:
        raise _bound_failure(context, exc, phase=_PROVIDER_CALL) from None
    try:
        return build_result(context, observation)
    except Exception as exc:
        raise _bound_failure(context, exc, phase=_COMMIT) from None
