"""Restartable server-owned external-boot preparation (ADR-0595)."""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.external_boot_activations import (
    CasResult,
    CasStatus,
    ExternalBootActivationRepository,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootPlan,
    ExternalBootPreparationPorts,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
)


class PreparationStaleError(RuntimeError):
    """The activation no longer belongs to this preparation operation."""


def _lost_cas(status: CasStatus) -> NoReturn:
    match status:
        case CasStatus.NOT_FOUND:
            raise PreparationStaleError("external-boot activation was not found")
        case CasStatus.SUPERSEDED:
            raise PreparationStaleError("external-boot preparation was superseded")
        case CasStatus.CAPACITY_EXHAUSTED:
            raise PreparationStaleError("external-boot preparation exhausted capacity")
        case CasStatus.RETAINED_CAPACITY:
            raise PreparationStaleError("external-boot preparation retained capacity")
        case CasStatus.APPLIED:
            raise AssertionError("applied CAS has no activation")


def _applied(result: CasResult) -> ExternalBootActivation:
    if result.status is not CasStatus.APPLIED or result.activation is None:
        _lost_cas(result.status)
    return result.activation


async def prepare_external_boot(
    *,
    pool: AsyncConnectionPool,
    repository: ExternalBootActivationRepository,
    ports: ExternalBootPreparationPorts,
    plan: ExternalBootPlan,
    activation_id: UUID,
    system_id: UUID,
    operation_owner_id: UUID,
    authority_generation: int,
    authority: OpaqueProviderRef,
    materialize_operation_identity: str,
    prepare_operation_identity: str,
) -> ExternalBootActivation:
    """Observe durable receipts and execute only preparation phases still absent."""
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
    ):
        return await _prepare_external_boot_locked(
            conn=conn,
            repository=repository,
            ports=ports,
            plan=plan,
            activation_id=activation_id,
            system_id=system_id,
            operation_owner_id=operation_owner_id,
            authority_generation=authority_generation,
            authority=authority,
            materialize_operation_identity=materialize_operation_identity,
            prepare_operation_identity=prepare_operation_identity,
        )


async def prepare_external_boot_for_admission(
    conn: AsyncConnection,
    *,
    repository: ExternalBootActivationRepository,
    resolver: ProviderResolver,
    plan: ExternalBootPlan,
    activation_id: UUID,
    provider_kind: str,
    authority_instance: str,
) -> ExternalBootActivation:
    """Prepare one activation through its production provider binding before job admission."""
    activation = await repository.get(conn, activation_id)
    if activation is None:
        raise PreparationStaleError("external-boot activation was not found")
    provider = await resolver.binding_for_system(conn, activation.system_id)
    if provider.kind.value != provider_kind:
        raise PreparationStaleError("external-boot preparation provider binding is stale")
    ports = provider.runtime.external_boot_preparation
    if ports is None:
        raise PreparationStaleError("external-boot preparation port is not configured")
    operation_prefix = (
        f"external-boot/{activation.id}/{activation.operation_owner_id}/"
        f"{activation.authority_generation}"
    )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, activation.system_id):
        return await _prepare_external_boot_locked(
            conn=conn,
            repository=repository,
            ports=ports,
            plan=plan,
            activation_id=activation.id,
            system_id=activation.system_id,
            operation_owner_id=activation.operation_owner_id,
            authority_generation=activation.authority_generation,
            authority=OpaqueProviderRef(ref=authority_instance),
            materialize_operation_identity=f"{operation_prefix}/materialize",
            prepare_operation_identity=f"{operation_prefix}/prepare",
        )


async def _prepare_external_boot_locked(
    *,
    conn: AsyncConnection,
    repository: ExternalBootActivationRepository,
    ports: ExternalBootPreparationPorts,
    plan: ExternalBootPlan,
    activation_id: UUID,
    system_id: UUID,
    operation_owner_id: UUID,
    authority_generation: int,
    authority: OpaqueProviderRef,
    materialize_operation_identity: str,
    prepare_operation_identity: str,
) -> ExternalBootActivation:
    binding = ExternalBootActivationBinding(
        system_id=str(system_id), run_id=plan.ownership.run_id, activation_id=str(activation_id)
    )
    activation = await repository.get(conn, activation_id)
    if (
        activation is None
        or activation.system_id != system_id
        or activation.run_id != UUID(plan.ownership.run_id)
        or activation.plan_identity != plan.identity
        or activation.operation_owner_id != operation_owner_id
        or activation.authority_generation != authority_generation
    ):
        raise PreparationStaleError("external-boot preparation identity is stale")
    if activation.state is ExternalBootActivationState.PREPARED:
        return activation
    if activation.state is not ExternalBootActivationState.PREPARING:
        raise PreparationStaleError("external-boot activation is no longer preparing")

    materialize_request = ExternalBootPreparationRequest(
        phase="materialize",
        plan=plan,
        binding=binding,
        authority=authority,
        operation_identity=materialize_operation_identity,
    )
    materialized = ports.observe_preparation(materialize_request)
    if materialized.state == "absent":
        materialized = ports.execute_preparation(materialize_request)
    if materialized.materialization is None:
        raise PreparationStaleError("materialization receipt is incomplete")
    activation = _applied(
        await repository.record_materialization(
            conn,
            system_id=system_id,
            activation_id=activation_id,
            operation_owner_id=operation_owner_id,
            authority_generation=authority_generation,
            expected_state=ExternalBootActivationState.PREPARING,
            materialization=materialized.materialization,
        )
    )

    prepare_request = ExternalBootPreparationRequest(
        phase="prepare",
        plan=plan,
        binding=binding,
        authority=authority,
        operation_identity=prepare_operation_identity,
    )
    prepared = ports.observe_preparation(prepare_request)
    if prepared.state == "absent":
        prepared = ports.execute_preparation(prepare_request)
    if prepared.recovery_point is None:
        raise PreparationStaleError("preparation receipt is incomplete")
    return _applied(
        await repository.transition(
            conn,
            system_id=system_id,
            activation_id=activation_id,
            operation_owner_id=operation_owner_id,
            authority_generation=authority_generation,
            expected_state=ExternalBootActivationState.PREPARING,
            new_state=ExternalBootActivationState.PREPARED,
            recovery_point=prepared.recovery_point,
        )
    )
