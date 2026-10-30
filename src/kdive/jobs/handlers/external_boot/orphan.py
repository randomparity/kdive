"""Worker dispatch for closed external-boot quarantine disposition (#2204)."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ResolveRecoveryOrphanPayload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.protocol import (
    AuthorityRecoveryOrphanDispositionRequestV1,
)


def _refuse(reason: str) -> CategorizedError:
    return CategorizedError(reason, category=ErrorCategory.CONFLICT, terminal=True)


async def resolve_recovery_orphan_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver | None,
) -> str:
    """Send only the claimed request identity to the authority the System's binding routes to.

    Whether that authority owns the selected rows is the serving host's check (ADR-0615).
    """
    payload = load_payload(job, ResolveRecoveryOrphanPayload)
    metadata = payload.recovery_request_v1
    if metadata is None:
        raise _refuse("recovery-object request is missing its immutable deadline")
    row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
    if row is None or row[0] >= metadata.readiness_deadline:
        raise CategorizedError(
            "recovery readiness deadline expired before authority dispatch",
            category=ErrorCategory.READINESS_FAILURE,
            terminal=True,
        )
    if resolver is None:
        raise CategorizedError(
            "recovery-object authority route is not configured",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        )
    binding = await resolver.binding_for_system(conn, UUID(payload.system_id))
    capability = binding.runtime.authority
    if capability is None or capability.sender is None:
        raise CategorizedError(
            "recovery-object authority route is not configured",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": "authority_route_missing"
                if capability is None
                else capability.missing_route_reason
            },
            terminal=True,
        )
    deadline = (
        asyncio.get_running_loop().time() + (metadata.readiness_deadline - row[0]).total_seconds()
    )
    response = await capability.sender.resolve_recovery_orphan(
        AuthorityRecoveryOrphanDispositionRequestV1(
            request_id=UUID(payload.request_id), job_id=job.id, job_attempt=job.attempt
        ),
        deadline=deadline,
    )
    return json.dumps(
        {
            "request_id": str(response.request_id),
            "disposition": response.disposition,
            "objects": response.objects,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def register_handlers(registry: HandlerRegistry, *, resolver: ProviderResolver | None) -> None:
    """Register the authority-only quarantine disposition handler."""
    from kdive.domain.operations.jobs import JobKind

    registry.register(
        JobKind.RESOLVE_RECOVERY_ORPHAN,
        lambda conn, job: resolve_recovery_orphan_handler(conn, job, resolver=resolver),
    )
