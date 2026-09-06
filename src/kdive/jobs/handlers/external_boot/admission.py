"""Build an authority-marked payload whose identity comes from the activation row, not the caller.

This is charter criterion 3's home: the marker's ``provider_kind`` and ``authority_instance`` are
carried by neither ``ExternalBootActivation`` nor ``ExternalBootReservation``, so the enqueueing
caller must supply them — and a ``provider_kind`` disagreeing with the resolved
``ProviderRuntime.external_boot`` port has to be rejected here, at validation, rather than at
``allocate_external_boot_authority``. A pydantic validator cannot do it: it has no database and no
resolver.

For a ``preparing`` activation this boundary validates and persists the exact plan on the job but
does not call the provider. The claimed worker runs preparation only after authority allocation and
acknowledgement (ADR-0608). #2204 wires the MCP tools to this helper. A caller that composes a
marker by hand can build one disagreeing with the activation row; going through this helper makes
that disagreement unconstructible instead.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.payloads import (
    ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS,
    BootPayload,
    TeardownPayload,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.protocol import Purpose
from kdive.providers.ports.external_boot import ExternalBootPlan

__all__ = ["build_external_boot_payload"]

_ACTIVATIONS = ExternalBootActivationRepository()


def _refuse(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, terminal=True)


async def build_external_boot_payload(
    conn: AsyncConnection,
    *,
    activation_id: UUID,
    purpose: Purpose,
    operation: str,
    provider_kind: str,
    authority_instance: str,
    operation_identity: str,
    resolver: ProviderResolver,
    preparation_plan: ExternalBootPlan | None = None,
) -> tuple[JobKind, BootPayload | TeardownPayload]:
    """Return the ``JobKind`` and payload one authority-marked operation must be enqueued as.

    ``run_id``, ``system_id`` and ``plan_identity`` are taken from the activation row rather than
    accepted from the caller, so a marker whose facts disagree with the activation cannot be
    constructed at all. The returned ``JobKind`` is ``TEARDOWN`` exactly for the ``teardown``
    purpose and ``BOOT`` otherwise, matching the pin at
    ``0122_external_boot_authority.sql:465`` — so no caller picks the kind by hand either.
    """
    if operation not in ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS:
        raise _refuse(f"{operation!r} is not an enqueueable external-boot operation")
    activation = await _ACTIVATIONS.get(conn, activation_id)
    if activation is None:
        raise _refuse(f"activation {activation_id} does not exist")

    binding = await resolver.binding_for_system(conn, activation.system_id)
    if binding.kind.value != provider_kind:
        raise _refuse(
            f"provider_kind {provider_kind!r} does not match the {binding.kind.value!r} runtime "
            f"bound for system {activation.system_id}"
        )
    if binding.runtime.external_boot is None:
        raise _refuse(
            f"the {binding.kind.value!r} runtime bound for system {activation.system_id} "
            "has no external_boot port"
        )
    if activation.state.value == "preparing":
        if preparation_plan is None:
            raise _refuse("a preparing activation requires its durable preparation plan")
        if binding.runtime.external_boot_preparation is None:
            raise _refuse(
                f"the {binding.kind.value!r} runtime bound for system {activation.system_id} "
                "has no external_boot_preparation port"
            )
        if (
            preparation_plan.identity != activation.plan_identity
            or preparation_plan.ownership.system_id != str(activation.system_id)
            or preparation_plan.ownership.run_id != str(activation.run_id)
        ):
            raise _refuse("durable preparation plan does not match the activation")

    marker = {
        "activation_id": str(activation.id),
        "run_id": str(activation.run_id),
        "system_id": str(activation.system_id),
        "plan_identity": activation.plan_identity,
        "purpose": purpose,
        "provider_kind": provider_kind,
        "authority_instance": authority_instance,
        "operation": operation,
        "operation_identity": operation_identity,
    }
    if purpose == "teardown":
        return JobKind.TEARDOWN, TeardownPayload.model_validate(
            {"system_id": str(activation.system_id), "external_boot_authority_v1": marker}
        )
    return JobKind.BOOT, BootPayload.model_validate(
        {
            "run_id": str(activation.run_id),
            "external_boot_authority_v1": marker,
            "external_boot_plan_v1": preparation_plan,
        }
    )
