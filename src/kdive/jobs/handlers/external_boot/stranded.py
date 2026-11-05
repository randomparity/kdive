"""Produce both owners' evidence for a stranded activation (ADR-0816, #3574)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import SecretStr

from kdive.db.external_boot_activations import CasStatus, ExternalBootActivationRepository
from kdive.db.repositories import SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ObserveStrandedActivationPayload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.protocol import (
    AuthorityStrandedObservationRequestV1,
)

_AUTHORITY_DEADLINE_SECONDS = 60.0
_ACTIVATIONS = ExternalBootActivationRepository()


def _refuse(reason: str, message: str) -> CategorizedError:
    return CategorizedError(
        message, category=ErrorCategory.CONFLICT, details={"reason": reason}, terminal=True
    )


def _unconfigured(message: str, reason: str | None) -> CategorizedError:
    return CategorizedError(
        message,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
        terminal=True,
    )


def _unsupported() -> CategorizedError:
    return _unconfigured(
        "this provider has no stranded-activation observation",
        "stranded_observation_unsupported",
    )


async def observe_stranded_activation_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver | None,
    incarnation_credential: SecretStr,
) -> str:
    """Record this worker's own evidence, then ask the authority to record its own."""
    payload = load_payload(job, ObserveStrandedActivationPayload)
    system_id, activation_id = UUID(payload.system_id), UUID(payload.activation_id)
    if resolver is None:
        raise _unsupported()
    binding = await resolver.binding_for_system(conn, system_id)
    presence = binding.runtime.domain_presence
    if presence is None:
        raise _unsupported()
    capability = binding.runtime.authority
    if capability is None or capability.sender is None:
        raise _unconfigured(
            "stranded observation authority route is not configured",
            "authority_route_missing" if capability is None else capability.missing_route_reason,
        )
    system = await SYSTEMS.get(conn, system_id)
    domain_name = None if system is None else system.domain_name
    if domain_name is None or not await asyncio.to_thread(presence.is_defined, domain_name):
        raise _refuse(
            "worker_domain_absent",
            f"this worker's daemon holds no domain for System {system_id}; "
            "the activation is not the worker-owned stranded shape",
        )
    credential = incarnation_credential.get_secret_value().encode()
    async with conn.transaction():
        status = await _ACTIVATIONS.record_stranded_worker_ownership(
            conn,
            credential_hash=hashlib.sha256(credential).digest(),
            job_id=job.id,
            job_attempt=job.attempt,
            activation_id=activation_id,
            domain_name=domain_name,
        )
    if status is not CasStatus.APPLIED:
        raise _refuse(
            "worker_evidence_superseded",
            f"activation {activation_id} no longer accepts worker evidence: {status.value}",
        )
    response = await capability.sender.observe_stranded_activation(
        AuthorityStrandedObservationRequestV1(activation_id=activation_id),
        deadline=asyncio.get_running_loop().time() + _AUTHORITY_DEADLINE_SECONDS,
    )
    if response.disposition == "domain_present":
        raise _refuse(
            "authority_domain_present",
            f"the authority's daemon holds a domain for System {system_id}, so the activation "
            "is not the worker-owned stranded shape; end that domain before observing again",
        )
    if response.disposition == "objects_present":
        raise _refuse(
            "authority_objects_present",
            f"the authority's store is not proven absent for activation {activation_id}: "
            f"{', '.join(response.objects)}; inspect these references on the authority host, "
            "settlement stays refused until they are gone",
        )
    return json.dumps(
        {"activation_id": str(activation_id), "disposition": response.disposition},
        sort_keys=True,
        separators=(",", ":"),
    )


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver | None,
    incarnation_credential: SecretStr,
) -> None:
    """Register the worker half of the stranded observation."""
    registry.register(
        JobKind.OBSERVE_STRANDED_ACTIVATION,
        lambda conn, job: observe_stranded_activation_handler(
            conn, job, resolver=resolver, incarnation_credential=incarnation_credential
        ),
    )
