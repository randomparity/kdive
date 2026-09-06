"""Worker dispatch for closed external-boot quarantine disposition (#2204)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ResolveRecoveryOrphanPayload, load_payload
from kdive.providers.external_boot_authority.protocol import (
    AuthorityRecoveryOrphanDispositionRequestV1,
)
from kdive.providers.ports.authority import AuthorityRequestSender


def _refuse(reason: str) -> CategorizedError:
    return CategorizedError(reason, category=ErrorCategory.CONFLICT, terminal=True)


async def resolve_recovery_orphan_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    sender_factory: Callable[[], AuthorityRequestSender] | None,
) -> str:
    """Send only the claimed request identity to its fixed authority route."""
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
    if sender_factory is None:
        raise CategorizedError(
            "recovery-object authority route is not configured",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        )
    deadline = (
        asyncio.get_running_loop().time() + (metadata.readiness_deadline - row[0]).total_seconds()
    )
    response = await sender_factory().resolve_recovery_orphan(
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


def register_handlers(
    registry: HandlerRegistry, *, sender_factory: Callable[[], AuthorityRequestSender] | None
) -> None:
    """Register the authority-only quarantine disposition handler."""
    from kdive.domain.operations.jobs import JobKind

    registry.register(
        JobKind.RESOLVE_RECOVERY_ORPHAN,
        lambda conn, job: resolve_recovery_orphan_handler(conn, job, sender_factory=sender_factory),
    )
